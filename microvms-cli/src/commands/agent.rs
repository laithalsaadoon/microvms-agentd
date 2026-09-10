// SPDX-License-Identifier: Apache-2.0
//! `agent-up` and `agent-prompt`: the L3 helpers as commands (`docs/AGENT-VMS.md`).
//!
//! # Two commands, one recipe, all of it in core
//!
//! Everything agent-shaped — the profile table, the Dockerfile, the token, the file set, the
//! headless command — is `microvms_core::agents`. This module parses, orders the steps,
//! owns the local state (ledger, name registry, history), and renders. A reader looking for
//! "what does the CLI know about Codex" should find nothing here, and that is the test of
//! whether the layer split held.
//!
//! # `agent-up` has two paths, decided by the registry
//!
//! A name this state directory has not registered means build, launch, provision, register.
//! A name it has registered means attach and re-provision: the same file uploads and the
//! same `chown`, with a fresh 12-hour token, and no build or launch (AGENT-8). The decision
//! is one local file read, so the refresh path costs zero control-plane calls before the
//! attach — which is also what keeps the behavioral guard honest: the fresh path enters the
//! sandbox door and the refresh path enters the attach door, and each is asserted.
//!
//! # A launch that cannot be provisioned is torn down
//!
//! Between `RunMicrovm` and the name registration there are three steps that can fail: the
//! token mint, the project upload, and the credential install. A VM left running after any of
//! them is a VM billing with no credentials in it and no name to address it by, so the
//! failure path terminates it and reports the identifiers, exactly as `run` does on an
//! interrupt (CLI-6). The name is registered last, only over a VM every step succeeded on.

use std::time::Duration;

use microvms_core::agents::{self, AgentSpec, AgentVm, BedrockAccess, PromptOptions};
use microvms_core::sandbox::TeardownOpts;
use microvms_core::{Error, ErrorKind};
use serde_json::{Map, Value, json};

use super::lifecycle::{Interrupt, epoch_secs, sync_error};
use crate::cli::{AgentArg, AgentPromptArgs, AgentUpArgs};
use crate::commands::{Ctx, Rendered, response_type};
use crate::exit::{CliError, Exit};
use crate::history::{Event, History};
use crate::ledger::{Ledger, NameRecord, Names};
use crate::seam::{Attach, state_dir};

/// The specs the flags describe, in the order given, one row per agent.
///
/// A repeated `--agent` collapses to one row rather than reaching core's refusal, because
/// `--agent codex --agent codex` is a shell-history accident and not a second model.
fn specs_from(args: &AgentUpArgs) -> Vec<AgentSpec> {
    let mut agents: Vec<AgentArg> = Vec::new();
    for agent in &args.agent {
        if !agents.contains(agent) {
            agents.push(*agent);
        }
    }
    if agents.is_empty() {
        agents.push(AgentArg::ClaudeCode);
    }
    agents
        .into_iter()
        .map(|arg| {
            let mut spec = AgentSpec::new(arg.agent());
            let (model, version) = match arg {
                AgentArg::ClaudeCode => (&args.claude_model, &args.claude_version),
                AgentArg::Codex => (&args.codex_model, &args.codex_version),
            };
            spec.model = model.clone();
            spec.cli_version = version.clone();
            spec
        })
        .collect()
}

/// The `agents` array every `agent-up` envelope carries: what is installed, which model,
/// and the exact command `agent-prompt` runs, so a caller who wants `exec --stream` over
/// it can spell it without knowing the template.
fn agents_report(specs: &[AgentSpec]) -> Value {
    Value::Array(
        specs
            .iter()
            .map(|spec| {
                json!({
                    "agent": spec.agent.as_str(),
                    "model": spec.model(),
                    "cliVersion": spec.cli_version,
                    "headlessCommand": agents::headless_command_template(spec.agent),
                })
            })
            .collect(),
    )
}

/// The one name-grammar refusal both paths share. Zero AWS calls.
fn require_valid_name(name: &str) -> Result<(), CliError> {
    crate::ledger::validate_name(name).map_err(|reason| {
        CliError::new(Exit::InvalidArg, reason)
            .suggest("names take ASCII letters, digits, `-` and `_`, up to 128 bytes")
    })
}

/// What `agent-up` learned, rendered once for both paths.
struct UpOutcome {
    vm_name: String,
    microvm_id: String,
    endpoint: String,
    agent_token: String,
    image_identifier: Option<String>,
    image_name: Option<String>,
    image_reused: Option<bool>,
    vm_reused: bool,
    specs: Vec<AgentSpec>,
    credential_expires_at: u64,
    project: Option<(usize, usize)>,
    agentd: Value,
}

impl UpOutcome {
    fn render(self) -> Rendered {
        let mut data = Map::new();
        data.insert("vmName".into(), json!(self.vm_name));
        data.insert("microvmId".into(), json!(self.microvm_id));
        data.insert("endpoint".into(), json!(self.endpoint));
        data.insert("agentToken".into(), json!(self.agent_token));
        data.insert("imageIdentifier".into(), json!(self.image_identifier));
        data.insert("imageName".into(), json!(self.image_name));
        data.insert("imageReused".into(), json!(self.image_reused));
        data.insert("vmReused".into(), json!(self.vm_reused));
        data.insert("agents".into(), agents_report(&self.specs));
        data.insert(
            "credentialExpiresAt".into(),
            json!(self.credential_expires_at),
        );
        data.insert("workdir".into(), json!(agents::WORKDIR));
        data.insert(
            "project".into(),
            match self.project {
                Some((bytes, members)) => json!({
                    "workdir": agents::WORKDIR,
                    "uploadedBytes": bytes,
                    "uploadedMembers": members,
                }),
                None => Value::Null,
            },
        );
        data.insert("agentd".into(), self.agentd);

        let hours_left = self
            .credential_expires_at
            .saturating_sub(epoch_secs())
            .div_ceil(3600);
        let agents_line = self
            .specs
            .iter()
            .map(|spec| format!("{} ({})", spec.agent, spec.model()))
            .collect::<Vec<_>>()
            .join(", ");
        let mut lines = vec![format!(
            "agent VM {}: {} at {}{}",
            self.vm_name,
            self.microvm_id,
            self.endpoint,
            if self.vm_reused {
                " (already running; credentials refreshed)"
            } else {
                ""
            }
        )];
        if let Some(image) = &self.image_identifier {
            lines.push(format!(
                "image: {image}{}",
                match self.image_reused {
                    Some(true) => " (reused)",
                    Some(false) => " (built)",
                    None => "",
                }
            ));
        }
        lines.push(format!("agents: {agents_line}"));
        lines.push(format!(
            "credentials: Bedrock bearer token, about {hours_left} h left; re-run this \
             command to refresh"
        ));
        if let Some((bytes, members)) = self.project {
            lines.push(format!(
                "project: {members} member(s), {bytes} byte(s) in {}",
                agents::WORKDIR
            ));
        }
        lines.push(format!(
            "prompt:   microvm agent-prompt --name {} \"<task>\"",
            self.vm_name
        ));
        lines.push(format!("teardown: microvm terminate {}", self.vm_name));
        let dense = format!(
            "{}\t{}\t{}\t{}",
            self.vm_name,
            self.microvm_id,
            self.endpoint,
            self.specs
                .iter()
                .map(|spec| spec.agent.as_str())
                .collect::<Vec<_>>()
                .join(",")
        );
        let (kind, _) = response_type("agent-up");
        Rendered::ok(kind, data, lines.join("\n"), dense)
    }
}

/// `agent-up`: see the module docs for the two paths.
pub async fn up<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &AgentUpArgs,
    interrupt: Interrupt<'_>,
) -> Result<Rendered, CliError> {
    require_valid_name(&args.vm_name)?;
    // Core refuses a lifetime outside (0, 12 h]; converting here keeps the message its own.
    let ttl = Duration::from_secs(u64::from(args.token_ttl_hours) * 3600);

    // The project pack, before anything else: an unreadable tree must cost zero AWS calls,
    // the same rule `run <DIR>` follows.
    let packed = match &args.project {
        Some(dir) => {
            let work = crate::sync::pack(dir).map_err(sync_error)?;
            ctx.out.progress(&format!(
                "packed {} ({} member(s), {} byte(s))",
                dir.display(),
                work.members,
                work.archive.len()
            ));
            Some(work)
        }
        None => None,
    };

    let root = state_dir(args.state_dir.clone(), ctx.env);
    let names = Names::new(&root);
    match names.lookup(&args.vm_name) {
        Some(record) => refresh(ctx, args, record, ttl, packed).await,
        None => fresh(ctx, args, &root, ttl, packed, interrupt).await,
    }
}

/// The refresh path (AGENT-8): attach, re-provision, report.
async fn refresh<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &AgentUpArgs,
    record: NameRecord,
    ttl: Duration,
    packed: Option<crate::sync::Packed>,
) -> Result<Rendered, CliError> {
    if record.microvm_id.is_empty() {
        return Err(CliError::new(
            Exit::NameTaken,
            format!(
                "the name {:?} is registered to a torn record (a process died mid-register); \
                 inspect the file before reusing the name.",
                args.vm_name
            ),
        )
        .suggest(format!(
            "the record is {}",
            Names::new(&state_dir(args.state_dir.clone(), ctx.env))
                .path_of(&args.vm_name)
                .display()
        )));
    }
    ctx.out.progress(&format!(
        "{} is registered to {}; refreshing its credentials rather than launching",
        args.vm_name, record.microvm_id
    ));
    // The flag wins over the record, as every attached command reads it.
    let region = if args.region.region.is_some() || args.region.unlisted_region.is_some() {
        args.region.resolve(ctx.env)?
    } else {
        microvms_core::Region::unlisted(&record.region)
    };
    let session = ctx
        .seam
        .attach_session(
            region.clone(),
            Attach {
                endpoint: record.endpoint.clone(),
                agent_token: record.agent_token.clone(),
                microvm_id: record.microvm_id.clone(),
                port: args.port,
            },
        )
        .await?;

    // The marker's specs unless the caller named agents: the refresh keeps the models the
    // VM was provisioned with, and a typed `--agent` is the caller changing them.
    let specs = if args.agent.is_empty() {
        agents::installed_agents(&session).await?
    } else {
        specs_from(args)
    };

    if let Some(work) = &packed {
        ctx.out.progress(&format!(
            "uploading {} member(s) to {}",
            work.members,
            agents::WORKDIR
        ));
        // Before the install, whose chown is what hands the uploaded tree to the agent.
        session.upload_tar(agents::WORKDIR, &work.archive).await?;
    }
    ctx.out.progress("minting a Bedrock bearer token");
    let minted = agents::bedrock::mint(&region, ttl).await?;
    let access = BedrockAccess {
        region: region.clone(),
        token: minted.token,
    };
    agents::install_access(&session, &specs, &access).await?;
    ctx.out.progress("credentials installed");

    Ok(UpOutcome {
        vm_name: args.vm_name.clone(),
        microvm_id: record.microvm_id,
        endpoint: record.endpoint,
        agent_token: record.agent_token,
        image_identifier: None,
        image_name: None,
        image_reused: None,
        vm_reused: true,
        specs,
        credential_expires_at: epoch_of(minted.expires_at),
        project: packed.map(|work| (work.archive.len(), work.members)),
        agentd: Value::Null,
    }
    .render())
}

fn epoch_of(at: std::time::SystemTime) -> u64 {
    at.duration_since(std::time::UNIX_EPOCH)
        .map(|since| since.as_secs())
        .unwrap_or_default()
}

/// The fresh path: build (or reuse), launch, provision, register.
async fn fresh<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &AgentUpArgs,
    root: &std::path::Path,
    ttl: Duration,
    packed: Option<crate::sync::Packed>,
    interrupt: Interrupt<'_>,
) -> Result<Rendered, CliError> {
    ctx.infra
        .require(&["execution_role_arn", "build_role_arn"])?;
    let Some(bucket) = ctx.infra.bucket.clone() else {
        return Err(CliError::new(
            Exit::Precondition,
            "no --bucket and no $MICROVM_BUCKET. The agent image's artifact is uploaded to a \
             derived key in that bucket (`aws s3 cp`); microvms-core does not upload, and an \
             S3 client in this CLI would be a second path to AWS.",
        ));
    };
    let region = args.region.resolve(ctx.env)?;
    let size = args.memory.size_class();
    let specs = specs_from(args);

    // The daemon binary: the caller's, or this CLI's own release asset, the same chain
    // `run` and `build` use.
    let mut agentd_report = Value::Null;
    let binary_path = match &args.binary {
        Some(binary) => {
            if !binary.exists() {
                return Err(CliError::new(
                    Exit::Precondition,
                    format!("daemon binary not found: {}", binary.display()),
                )
                .suggest("cargo build --release -p agentd --target aarch64-unknown-linux-musl")
                .suggest("or pass no binary at all: the CLI provisions its own version's asset"));
            }
            binary.clone()
        }
        None => {
            let resolved = {
                let out = &mut *ctx.out;
                crate::provision::resolve(
                    root,
                    env!("CARGO_PKG_VERSION"),
                    ctx.env,
                    ctx.fetch,
                    &mut |line| out.progress(line),
                )?
            };
            agentd_report = json!({
                "path": resolved.path.display().to_string(),
                "source": resolved.source.as_str(),
                "verified": match resolved.source {
                    crate::provision::Source::Fetched(verification) => json!(verification.as_str()),
                    _ => Value::Null,
                },
            });
            resolved.path
        }
    };
    let binary = std::fs::read(&binary_path).map_err(|error| {
        CliError::new(
            Exit::Precondition,
            format!("could not read {}: {error}", binary_path.display()),
        )
    })?;

    ctx.out.progress(&format!(
        "preparing agent VM {} in {region} ({size}): {}",
        args.vm_name,
        specs
            .iter()
            .map(|spec| spec.agent.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    ));
    let sandbox = ctx.seam.open_sandbox(region.clone(), args.port).await?;
    let mut vm = AgentVm::new(sandbox, specs.clone())?;
    let mut request = vm.image_request(
        binary,
        ctx.infra.build_role_arn.clone().unwrap_or_default(),
        size,
    )?;
    request.code_artifact_uri = format!("s3://{bucket}/{}.zip", request.name);
    let image_name = request.name.clone();

    let mut ledger = Ledger::new(region.as_str(), root);
    let region_name = region.as_str().to_string();

    // Everything that creates or bills, raced against the interrupt. The vm and the ledger
    // are borrowed inside and read after, so a cancelled body leaves every identifier core
    // recorded readable — the CLI-6 arrangement `run` uses.
    let launched: Result<(String, bool, u64), Error> = {
        let body = Box::pin(launch_and_provision(
            ctx,
            args,
            &region,
            &mut vm,
            &mut ledger,
            request,
            ttl,
            packed.as_ref(),
        ));
        tokio::select! {
            result = body => result,
            () = interrupt => Err(Error::new(
                ErrorKind::Interrupted,
                "interrupted after launch. Tearing the VM down: an agent VM with no name and \
                 no credentials is a VM nobody can use, and anything this teardown fails to \
                 remove is named in the failure envelope's `data.leaked`.",
            )),
        }
    };

    let microvm_id = vm.sandbox().microvm().map(|microvm| microvm.id.clone());
    let image_identifier = vm.sandbox().image().map(|image| image.identifier.clone());

    let (image_arn, image_reused, expires_at) = match launched {
        Ok(parts) => parts,
        Err(error) => {
            // A VM was launched and then something between it and the registration failed:
            // tear it down, because nothing can address it. The image stays — it is the
            // durable, reusable artifact and its snapshot bills for a week either way.
            let mut leaked = Vec::new();
            let mut terminate_accepted = false;
            if microvm_id.is_some() {
                ledger.mark_outstanding();
                ctx.out.progress("tearing down the unprovisioned VM");
                let report = vm.terminate(TeardownOpts::default()).await;
                terminate_accepted = report.terminate_accepted;
                let mut still = report.undeleted.clone();
                if report.terminate_accepted
                    && let Some(id) = &microvm_id
                {
                    still.retain(|entry| entry != id);
                }
                for identifier in &still {
                    ctx.out.warn(&format!(
                        "could not delete {identifier} — it is still billing; record this id."
                    ));
                }
                ledger.mark_deleted(&still);
                ledger.clear();
                leaked = still;
            }
            let mut failure = crate::exit::classify(&error);
            failure = failure.with_data("leaked", json!(leaked));
            failure = failure.with_data("terminateAccepted", json!(terminate_accepted));
            if let Some(id) = &microvm_id {
                failure = failure.with_data("microvmId", json!(id));
            }
            if let Some(image) = &image_identifier {
                failure = failure.with_data("imageIdentifier", json!(image));
            }
            return Err(failure);
        }
    };

    // Kept by definition, so the ledger says so and `microvm ls` shows the VM.
    ledger.mark_outstanding();
    let session = vm.session().expect("launch_and_provision built one");
    let microvm_id = microvm_id.expect("a launched VM has an id");
    let endpoint = session.endpoint().to_string();
    let agent_token = session.agent_token().to_string();

    // Registered last, over a VM every step succeeded on. A registry write failure is a
    // hard error for `run --vm-name`'s reason: a caller told "registered" who later finds
    // `--name` answering "no VM named" has a phantom worse than a loud failure now.
    let record = NameRecord {
        name: args.vm_name.clone(),
        microvm_id: microvm_id.clone(),
        endpoint: endpoint.clone(),
        agent_token: agent_token.clone(),
        region: region_name.clone(),
        at: epoch_secs(),
        identity_host_seed: None,
        identity_vm_public_key: None,
    };
    Names::new(root).register(&record).map_err(|error| {
        CliError::new(
            Exit::Precondition,
            format!(
                "the agent VM is RUNNING and provisioned, but its name could not be \
                 registered: {error}. Address it by the identifiers in this envelope's data.",
            ),
        )
        .with_data("microvmId", json!(microvm_id))
        .with_data("endpoint", json!(endpoint))
        .with_data("agentToken", json!(agent_token))
        .with_data("vmName", json!(args.vm_name))
    })?;
    ctx.out.progress(&format!(
        "registered name {} for {microvm_id}",
        args.vm_name
    ));

    let history = History::for_vm(root, &microvm_id);
    if !image_reused {
        history.append(Event::ImageBuilt {
            image_identifier: image_arn.clone(),
            image_name: image_name.clone(),
        });
    }
    history.append(Event::Launched {
        image_identifier: image_arn.clone(),
        endpoint: endpoint.clone(),
        region: region_name,
    });

    Ok(UpOutcome {
        vm_name: args.vm_name.clone(),
        microvm_id,
        endpoint,
        agent_token,
        image_identifier: Some(image_arn),
        image_name: Some(image_name),
        image_reused: Some(image_reused),
        vm_reused: false,
        specs,
        credential_expires_at: expires_at,
        project: packed.map(|work| (work.archive.len(), work.members)),
        agentd: agentd_report,
    }
    .render())
}

/// The billable half of the fresh path: image (built or reused), launch, upload, token,
/// install. Returns the image ARN, whether it was reused, and the token's expiry.
#[allow(clippy::too_many_arguments)]
async fn launch_and_provision<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &AgentUpArgs,
    region: &microvms_core::Region,
    vm: &mut AgentVm,
    ledger: &mut Ledger,
    request: microvms_core::control::CreateImageRequest,
    ttl: Duration,
    packed: Option<&crate::sync::Packed>,
) -> Result<(String, bool, u64), Error> {
    let name = request.name.clone();
    ctx.out.progress(&format!(
        "checking for an existing image named {name} (content-hash keyed, like build --reuse)"
    ));
    let (image_arn, reused) = match vm.sandbox().find_image_by_name(&name).await? {
        Some(existing) => {
            ctx.out.progress(&format!(
                "reusing {} — the daemon, the agents, and their versions are unchanged",
                existing.image_arn
            ));
            (existing.image_arn, true)
        }
        None => {
            ctx.out.progress(&format!(
                "building image {name} — several minutes, server-side, once per agent set"
            ));
            // Before the upload, so a request core itself refuses costs zero transport calls.
            vm.sandbox().preflight(&request)?;
            let bytes = vm.sandbox().build_artifact_for(&request)?;
            ctx.out.progress(&format!(
                "uploading {} bytes of artifact to {}",
                bytes.len(),
                request.code_artifact_uri
            ));
            ctx.seam
                .put_artifact(&request.code_artifact_uri, bytes)
                .await?;
            let image = vm.build(request).await?;
            (image.identifier.clone(), false)
        }
    };
    ledger.record_image(&image_arn, &name);

    let mut launch = vm.launch_request(&image_arn, ctx.infra.execution_role_arn.clone());
    launch.max_idle_sec = args.max_idle_sec;
    launch.suspended_sec = args.suspended_sec;
    launch.auto_resume = args.auto_resume;
    launch.max_duration_sec = args.max_duration_sec;
    ctx.out.progress("launching with egress");
    let session = vm.launch(launch).await?;
    let endpoint = session.endpoint().to_string();
    if let Some(microvm) = vm.sandbox().microvm() {
        ledger.record_microvm(&microvm.id);
    }
    ctx.out.progress(&format!("microvm RUNNING at {endpoint}"));

    if let Some(work) = packed {
        ctx.out.progress(&format!(
            "uploading {} member(s) to {}",
            work.members,
            agents::WORKDIR
        ));
        vm.session()
            .expect("launch built one")
            .upload_tar(agents::WORKDIR, &work.archive)
            .await?;
    }

    ctx.out.progress("minting a Bedrock bearer token");
    let minted = agents::bedrock::mint(region, ttl).await?;
    let access = BedrockAccess {
        region: region.clone(),
        token: minted.token,
    };
    vm.install_access(&access).await?;
    ctx.out
        .progress("credentials installed; the workspace belongs to uid 1000");
    Ok((image_arn, reused, epoch_of(minted.expires_at)))
}

// ── agent-prompt ────────────────────────────────────────────────────────────

/// Which spec a prompt runs: the flag's agent, or the marker's sole agent (AGENT-10).
///
/// The marker is read either way when it can be, so a typed `--agent` still picks up the
/// model the VM was provisioned with. A VM with no marker and a typed agent runs the
/// profile's defaults; a VM with no marker and no flag is a precondition failure naming
/// `agent-up`, which core's error already does.
async fn choose_spec(
    session: &microvms_core::session::Session,
    requested: Option<AgentArg>,
) -> Result<AgentSpec, CliError> {
    let installed = agents::installed_agents(session).await;
    match (requested, installed) {
        (Some(arg), Ok(specs)) => {
            let agent = arg.agent();
            Ok(specs
                .into_iter()
                .find(|spec| spec.agent == agent)
                .unwrap_or_else(|| AgentSpec::new(agent)))
        }
        (Some(arg), Err(error)) if error.kind() == ErrorKind::Precondition => {
            Ok(AgentSpec::new(arg.agent()))
        }
        (Some(_), Err(error)) => Err(error.into()),
        (None, Ok(mut specs)) => {
            if specs.len() == 1 {
                return Ok(specs.remove(0));
            }
            let names: Vec<&str> = specs.iter().map(|spec| spec.agent.as_str()).collect();
            Err(CliError::new(
                Exit::Precondition,
                format!(
                    "this VM carries {} agents ({}); say which one with --agent.",
                    specs.len(),
                    names.join(", ")
                ),
            )
            .suggest(format!("--agent {}", names[0])))
        }
        (None, Err(error)) => Err(error.into()),
    }
}

/// `agent-prompt`: one task, headless, demoted, in the workspace.
pub async fn prompt<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &AgentPromptArgs,
) -> Result<Rendered, CliError> {
    // Refused before the attach: a blank task must cost zero calls. Core refuses it too;
    // this is the same message a round trip earlier.
    if args.task.trim().is_empty() {
        return Err(CliError::new(
            Exit::InvalidArg,
            "the task is empty. An agent prompted with nothing answers with nothing, at the \
             price of a model call.",
        ));
    }
    let (session, microvm_id) = super::attached::attach(ctx, &args.region, &args.attach).await?;
    let spec = choose_spec(&session, args.agent).await?;
    let options = PromptOptions {
        exec_id: args.exec_id.clone(),
        timeout: None,
    };
    ctx.out.progress(&format!(
        "prompting {} ({}) as uid {} in {}",
        spec.agent,
        spec.model(),
        agents::AGENT_UID,
        agents::WORKDIR
    ));
    let handle = agents::prompt(&session, &spec, &args.task, &options).await?;
    let exec_id = handle.exec_id().to_string();
    let history = History::for_vm(
        &state_dir(args.attach.state_dir.clone(), ctx.env),
        &microvm_id,
    );

    let result = if args.detach {
        history.append(Event::Exec {
            exec_id: exec_id.clone(),
            exit_code: None,
            truncated: false,
            writers_may_be_alive: None,
        });
        microvms_core::session::ExecResult {
            exec_id: exec_id.clone(),
            phase: microvms_core::protocol::exec::Phase::Running,
            outcome: None,
        }
    } else {
        let timeout = Duration::from_secs_f64(args.timeout.max(0.0));
        let result = handle.wait_and_ack(timeout).await?;
        history.append(Event::Exec {
            exec_id: exec_id.clone(),
            exit_code: result.exit_code(),
            truncated: result
                .outcome
                .as_ref()
                .is_some_and(|outcome| outcome.truncated),
            writers_may_be_alive: result
                .outcome
                .as_ref()
                .map(|outcome| outcome.writers_may_be_alive),
        });
        result
    };

    let mut rendered = super::attached::render_exec_as("agent-prompt", &exec_id, &result);
    rendered
        .data
        .insert("agent".into(), json!(spec.agent.as_str()));
    rendered.data.insert("model".into(), json!(spec.model()));
    Ok(rendered)
}

/// Keeps the compiler honest about the flag/profile mapping: every core agent has a flag.
#[cfg(test)]
mod tests {
    use super::*;
    use microvms_core::agents::Agent;

    #[test]
    fn every_core_agent_has_a_flag_spelling() {
        for agent in Agent::ALL {
            let arg = match agent {
                Agent::ClaudeCode => AgentArg::ClaudeCode,
                Agent::Codex => AgentArg::Codex,
            };
            assert_eq!(arg.agent(), agent);
        }
    }

    fn args(agents: &[AgentArg]) -> AgentUpArgs {
        AgentUpArgs {
            binary: None,
            vm_name: "dev".into(),
            agent: agents.to_vec(),
            claude_model: Some("global.anthropic.claude-sonnet-5".into()),
            codex_model: None,
            claude_version: None,
            codex_version: Some("0.50.0".into()),
            project: None,
            memory: crate::cli::MemoryMib::Mib1024,
            token_ttl_hours: 12,
            max_idle_sec: 600,
            suspended_sec: 600,
            auto_resume: false,
            max_duration_sec: 3600,
            port: None,
            state_dir: None,
            region: crate::cli::RegionFlags {
                region: None,
                unlisted_region: None,
            },
            infra: crate::cli::InfraFlags::default(),
        }
    }

    /// No `--agent` means Claude Code; a repeat collapses; each agent gets its own model
    /// and version flag and never the other's.
    #[test]
    fn the_flags_become_one_spec_per_agent_with_their_own_knobs() {
        let default = specs_from(&args(&[]));
        assert_eq!(default.len(), 1);
        assert_eq!(default[0].agent, Agent::ClaudeCode);
        assert_eq!(default[0].model(), "global.anthropic.claude-sonnet-5");
        assert_eq!(default[0].cli_version, None);

        let both = specs_from(&args(&[
            AgentArg::Codex,
            AgentArg::Codex,
            AgentArg::ClaudeCode,
        ]));
        assert_eq!(both.len(), 2);
        assert_eq!(both[0].agent, Agent::Codex);
        assert_eq!(both[0].cli_version.as_deref(), Some("0.50.0"));
        assert_eq!(both[0].model(), "openai.gpt-5.6-sol");
        assert_eq!(both[1].agent, Agent::ClaudeCode);
        assert_eq!(both[1].cli_version, None);
    }

    /// The envelope's `agents` array carries the command a caller would stream.
    #[test]
    fn the_agents_report_names_the_headless_command() {
        let report = agents_report(&[AgentSpec::new(Agent::Codex)]);
        assert_eq!(report[0]["agent"], "codex");
        assert!(
            report[0]["headlessCommand"]
                .as_str()
                .is_some_and(|command| command.contains("codex exec") && command.contains("<TASK>")),
            "{report}"
        );
    }
}
