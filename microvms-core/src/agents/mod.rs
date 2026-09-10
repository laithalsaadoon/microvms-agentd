// SPDX-License-Identifier: Apache-2.0
//! Agent VMs: the L3 helpers over the lifecycle (`docs/AGENT-VMS.md`).
//!
//! # What this module is
//!
//! One VM with a coding agent installed, model access wired, running as a non-root user,
//! and one call that hands the agent a task. Every step is a composition of
//! [`crate::sandbox`] and [`crate::session`] calls that
//! `examples/coding-agents-on-bedrock/run.sh` performed by hand; this module is where
//! that composition, and the four measured traps it closes, live once:
//!
//! 1. **Root silently breaks the agent.** Claude Code's `--dangerously-skip-permissions`
//!    refuses to run as uid 0 and then denies its own tool calls while still reporting
//!    (measured: zero shell calls as root, 147 as uid 1000). Every prompt runs as
//!    [`AGENT_UID`]/[`AGENT_GID`], and the image creates that user.
//! 2. **The daemon's execs start from an empty environment**, so the agent's subshells
//!    find no `PATH` and every command exits 127. The environment file sets it.
//! 3. **The daemon writes uploaded files as root, mode 0600**, which a demoted agent
//!    cannot read. One root `chown` follows the uploads.
//! 4. **Codex reaches Bedrock through `bedrock-runtime`'s `/openai/v1`** on the Responses
//!    wire API, with an inference-profile model id and hosted web search disabled. Its
//!    config file names that host for the launch region.
//!
//! # What it is not
//!
//! Not an orchestrator (`docs/STRATEGY.md`): no scheduling, no retries across VMs, no
//! turn loop. Not a harness provider class: a Harbor environment or an eve backend
//! imports its harness and lives there, calling this. And not a place agent detail
//! sprawls: every vendor-shaped fact is a row in [`profile`], dated.
//!
//! # Two shapes over one recipe
//!
//! [`AgentVm`] owns a [`Sandbox`] and is the shape for a caller who builds, launches,
//! provisions, prompts, and tears down in one process. The free functions
//! ([`install_access`], [`prompt`], [`installed_agents`]) take a [`Session`], because the
//! CLI's refresh and prompt paths hold an attached session for a VM some earlier process
//! launched and no sandbox at all; `AgentVm`'s methods delegate to them so the two
//! paths cannot drift.

pub mod bedrock;
pub mod profile;

use std::time::Duration;

use crate::control::artifact::{BaseImage, default_dockerfile};
use crate::control::{CreateImageRequest, Image};
use crate::error::{Error, ErrorKind};
use crate::region::Region;
use crate::sandbox::{RunRequest, Sandbox, TeardownOpts, TeardownReport};
use crate::session::{ExecHandle, Session, mint_exec_id};
use crate::sizing::SizeClass;

pub use bedrock::{BearerToken, Minted};
pub use profile::{Agent, CODEX_CONFIG_FILE, ENV_FILE, MARKER_FILE, Profile};

/// The uid every agent runs as, and the owner of `/workspace`. Created by the image.
pub const AGENT_UID: u32 = 1000;
/// The gid to match.
pub const AGENT_GID: u32 = 1000;
/// The image `WORKDIR`, the agent's `HOME`, and where a synced project lands. The same
/// path `microvms-cli/src/sync.rs` names as `REMOTE_WORKDIR`, on purpose: `run <DIR>`
/// and `agent-up --project` put the tree in the same place.
pub const WORKDIR: &str = "/workspace";
/// The size class agent sessions default to: a 4 GiB always-present ceiling at half the
/// floor cost of the 2048 default, because agent sessions are peaky (the example's
/// measured choice, `examples/coding-agents-on-bedrock/microvm.toml`).
pub const DEFAULT_SIZE: SizeClass = SizeClass::Mib1024;
/// The default wait for one prompt. Agent tasks run minutes, not seconds.
pub const DEFAULT_PROMPT_TIMEOUT: Duration = Duration::from_secs(900);
/// How long the root `chown` after the uploads may take.
const CHOWN_TIMEOUT: Duration = Duration::from_secs(60);

/// One agent to install, with its two overridable defaults.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AgentSpec {
    pub agent: Agent,
    /// The model id, or `None` for the profile's default.
    pub model: Option<String>,
    /// An npm version to pin the CLI to (`@<version>` on the install line), or `None`
    /// for the registry's latest at build time. A pin changes the Dockerfile text and
    /// therefore the reuse hash (AGENT-3).
    pub cli_version: Option<String>,
}

impl AgentSpec {
    /// The profile's defaults.
    pub fn new(agent: Agent) -> Self {
        Self {
            agent,
            model: None,
            cli_version: None,
        }
    }

    /// Override the model.
    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model = Some(model.into());
        self
    }

    /// Pin the CLI version.
    pub fn with_cli_version(mut self, version: impl Into<String>) -> Self {
        self.cli_version = Some(version.into());
        self
    }

    /// The model this spec resolves to.
    pub fn model(&self) -> &str {
        self.model
            .as_deref()
            .unwrap_or(self.agent.profile().default_model)
    }

    /// The `npm install -g` argument for this spec.
    fn npm_spec(&self) -> String {
        match &self.cli_version {
            Some(version) => format!("{}@{version}", self.agent.profile().npm_package),
            None => self.agent.profile().npm_package.to_string(),
        }
    }
}

/// Refuses an empty spec list and a repeated agent.
///
/// Both are caller mistakes with no sensible reading: an image with no agent is
/// `microvm build`, and two rows for one agent would have two models for one CLI.
///
/// Public because the bindings hold a spec list beside a shared sandbox rather than an
/// [`AgentVm`], and the refusal has to be this one rather than a copy of it.
pub fn require_specs(specs: &[AgentSpec]) -> Result<(), Error> {
    if specs.is_empty() {
        return Err(Error::invalid_arg(
            "no agent named. An agent VM installs at least one of: claude-code, codex.",
        ));
    }
    for (index, spec) in specs.iter().enumerate() {
        if specs[..index]
            .iter()
            .any(|earlier| earlier.agent == spec.agent)
        {
            return Err(Error::invalid_arg(format!(
                "the agent {} is named twice; one row per agent, because a second row \
                 would be a second model for the same CLI.",
                spec.agent
            )));
        }
    }
    Ok(())
}

/// The specs in the fixed order the image layers, the marker, and the name use.
fn ordered(specs: &[AgentSpec]) -> Vec<&AgentSpec> {
    let mut sorted: Vec<&AgentSpec> = specs.iter().collect();
    sorted.sort_by_key(|spec| spec.agent);
    sorted
}

/// The spec in `specs` for `agent`, or the refusal [`AgentVm::prompt`] gives.
pub fn spec_for(specs: &[AgentSpec], agent: Agent) -> Result<&AgentSpec, Error> {
    specs
        .iter()
        .find(|spec| spec.agent == agent)
        .ok_or_else(|| {
            Error::invalid_arg(format!(
                "this VM carries {}, not {agent}.",
                image_stem(specs).trim_start_matches("agent-vm-")
            ))
        })
}

/// [`AgentVm::image_name`] for a caller holding the sandbox and the specs separately:
/// the bindings, whose sandbox sits behind a lock shared with every session they hand out.
pub fn image_name_for(
    sandbox: &Sandbox,
    specs: &[AgentSpec],
    request: &CreateImageRequest,
) -> String {
    let hash = sandbox.artifact_content_hash_for(request);
    format!("{}-{}", image_stem(specs), &hash[..12])
}

/// [`AgentVm::image_request`] for a caller holding the sandbox and the specs separately.
pub fn image_request_for(
    sandbox: &Sandbox,
    specs: &[AgentSpec],
    binary: Vec<u8>,
    build_role_arn: impl Into<String>,
    size: SizeClass,
) -> Result<CreateImageRequest, Error> {
    require_specs(specs)?;
    let base = BaseImage::al2023();
    let dockerfile = dockerfile(specs, &base, sandbox.port())?;
    let mut request =
        CreateImageRequest::new(image_stem(specs), binary, String::new(), build_role_arn);
    request.base_image = base;
    request.dockerfile = Some(dockerfile);
    request.size = size;
    let name = image_name_for(sandbox, specs, &request);
    request.name = name.clone();
    request.token_scope = Some(name);
    Ok(request)
}

/// [`AgentVm::launch_request`] for a caller holding the specs separately.
pub fn launch_request_for(
    specs: &[AgentSpec],
    image_identifier: impl Into<String>,
    execution_role_arn: Option<String>,
) -> RunRequest {
    let identifier = image_identifier.into();
    let mut request = RunRequest::new().with_image(&identifier).with_egress();
    request.execution_role_arn = execution_role_arn;
    request.token_scope = Some(image_stem(specs));
    request
}

/// The stem of the image name: `agent-vm-<agents>` in profile order (AGENT-3). The
/// caller appends the artifact content hash, exactly as `build --reuse` does.
pub fn image_stem(specs: &[AgentSpec]) -> String {
    let agents: Vec<&str> = ordered(specs)
        .into_iter()
        .map(|spec| spec.agent.as_str())
        .collect();
    format!("agent-vm-{}", agents.join("-"))
}

/// The Dockerfile for an image carrying `specs` (AGENT-2).
///
/// Built by splicing into [`default_dockerfile`]'s output rather than by re-listing its
/// lines, so the stanza this produces *is* the default build's stanza plus layers: a
/// change to the daemon lines there reaches here with no second copy to update. The
/// splice points are the two lines the stanza is guaranteed to carry (`RUN chmod 0755
/// /agentd` and `RUN mkdir -p /workspace`), and the test asserts both were found.
pub fn dockerfile(specs: &[AgentSpec], base: &BaseImage, port: u16) -> Result<String, Error> {
    require_specs(specs)?;
    let stanza = default_dockerfile(port, Some(WORKDIR), base, None);
    let mut lines: Vec<String> = stanza.lines().map(str::to_string).collect();

    let chmod = lines
        .iter()
        .position(|line| line == "RUN chmod 0755 /agentd")
        .ok_or_else(|| stanza_drift("RUN chmod 0755 /agentd"))?;
    let mut layers = vec![
        String::new(),
        "# The coding agents. Node 22 runs both CLIs; the rest is what a coding agent".to_string(),
        "# expects of a working shell. Unpinned unless a cli_version was given, so the".to_string(),
        "# image carries the registry's latest at build time and a pin changes the reuse hash."
            .to_string(),
        format!(
            "RUN dnf install -y --setopt=install_weak_deps=0 {} && dnf clean all",
            profile::SYSTEM_PACKAGES
        ),
    ];
    let packages: Vec<String> = ordered(specs)
        .into_iter()
        .map(AgentSpec::npm_spec)
        .collect();
    layers.push(format!(
        "RUN npm install -g {} && npm cache clean --force",
        packages.join(" ")
    ));
    layers.extend([
        String::new(),
        "# The non-root user the agents run as. Appended directly because `useradd` is not"
            .to_string(),
        "# in the minimal base. Not decoration: Claude Code refuses --dangerously-skip-permissions"
            .to_string(),
        "# as root and then denies its own tool calls while still reporting (measured: zero"
            .to_string(),
        "# shell calls as uid 0, 147 as uid 1000 on the same task).".to_string(),
        format!(
            "RUN echo \"agent:x:{AGENT_UID}:{AGENT_GID}::{WORKDIR}:/bin/bash\" >> /etc/passwd \\"
        ),
        format!("    && echo \"agent:x:{AGENT_GID}:\" >> /etc/group"),
    ]);
    lines.splice(chmod + 1..chmod + 1, layers);

    let mkdir = lines
        .iter()
        .position(|line| line == &format!("RUN mkdir -p {WORKDIR}"))
        .ok_or_else(|| stanza_drift("RUN mkdir -p /workspace"))?;
    // Owned by the agent uid, because WORKDIR is where every exec lands and a root-owned
    // one fails a demoted workload at its first write (docs/EMBEDDING.md).
    lines[mkdir] = format!("RUN mkdir -p {WORKDIR} && chown {AGENT_UID}:{AGENT_GID} {WORKDIR}");

    let mut text = lines.join("\n");
    text.push('\n');
    Ok(text)
}

fn stanza_drift(line: &str) -> Error {
    Error::new(
        ErrorKind::Unexpected,
        format!(
            "default_dockerfile no longer emits `{line}`, so the agent layers have no splice \
             point. Update agents::dockerfile beside the stanza change."
        ),
    )
}

/// Model access for the agents: a Bedrock bearer token minted for a region.
///
/// A struct rather than an enum until a second access path exists to name
/// (`docs/AGENT-VMS.md`, phase 2).
#[derive(Clone, Debug)]
pub struct BedrockAccess {
    pub region: Region,
    pub token: BearerToken,
}

/// One file to place in the guest.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GuestFile {
    pub path: String,
    pub contents: Vec<u8>,
    /// Octal, as the file route takes it.
    pub mode: &'static str,
}

/// Wraps `value` in double quotes for `sh`, escaping the four characters that are
/// special inside them. Used for the environment file, whose values are model ids, a
/// region, and a token (base64 plus the prefix), none of which contain them today; the
/// escaping is what keeps that "today" from mattering.
pub fn sh_double_quote(value: &str) -> String {
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('"');
    for ch in value.chars() {
        if matches!(ch, '"' | '\\' | '$' | '`') {
            quoted.push('\\');
        }
        quoted.push(ch);
    }
    quoted.push('"');
    quoted
}

/// Wraps `value` in single quotes for `sh`. A single quote inside becomes `'\''`, the
/// one sequence that is literal under every POSIX shell. Used for the task text, which
/// is the caller's prose and may contain anything.
pub fn sh_single_quote(value: &str) -> String {
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('\'');
    for ch in value.chars() {
        if ch == '\'' {
            quoted.push_str("'\\''");
        } else {
            quoted.push(ch);
        }
    }
    quoted.push('\'');
    quoted
}

/// The files `install_access` places (AGENT-5, AGENT-6): the environment file, Codex's
/// config when Codex is present, and the marker. Pure, so a test reads the exact bytes.
pub fn provisioning_files(
    specs: &[AgentSpec],
    access: &BedrockAccess,
) -> Result<Vec<GuestFile>, Error> {
    require_specs(specs)?;
    let specs = ordered(specs);

    // PATH first and explicit: the daemon spawns execs from an empty environment, and
    // Claude Code's Bash tool snapshots the shell it starts from, so without this line
    // the agent's subshells find no `ls`, `wc`, or `python3`.
    let mut env = vec![
        ("HOME", WORKDIR.to_string()),
        ("PATH", "/usr/local/bin:/usr/bin:/bin".to_string()),
        ("AWS_REGION", access.region.as_str().to_string()),
    ];
    for spec in &specs {
        env.extend(profile::env_pairs(
            spec.agent,
            spec.model(),
            access.token.expose(),
        ));
    }
    let mut env_file = String::new();
    for (key, value) in env {
        env_file.push_str(&format!("export {key}={}\n", sh_double_quote(&value)));
    }

    let mut files = vec![GuestFile {
        path: ENV_FILE.to_string(),
        contents: env_file.into_bytes(),
        mode: "0600",
    }];
    if let Some(codex) = specs.iter().find(|spec| spec.agent == Agent::Codex) {
        files.push(GuestFile {
            path: CODEX_CONFIG_FILE.to_string(),
            contents: profile::codex_config(codex.model(), &access.region).into_bytes(),
            mode: "0600",
        });
    }
    let marker = serde_json::json!({
        "version": 1,
        "region": access.region.as_str(),
        "agents": specs.iter().map(|spec| serde_json::json!({
            "agent": spec.agent.as_str(),
            "model": spec.model(),
            "cliVersion": spec.cli_version,
        })).collect::<Vec<_>>(),
    });
    files.push(GuestFile {
        path: MARKER_FILE.to_string(),
        contents: serde_json::to_vec_pretty(&marker).map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("the marker will not serialize: {error}"),
            )
        })?,
        // World-readable is fine and useful: it names models, never a credential, and the
        // agent itself may want to read it.
        mode: "0644",
    });
    Ok(files)
}

/// Places the provisioning files and hands `/workspace` to the agent uid (AGENT-5).
///
/// The daemon writes every upload as root, so the `chown` is not optional and it runs
/// as the daemon's own user, after the uploads, in one exec. Re-runnable: a second call
/// with a fresh token overwrites the same files, which is how a 12-hour token is
/// refreshed on a long-lived VM (AGENT-8).
pub async fn install_access(
    session: &Session,
    specs: &[AgentSpec],
    access: &BedrockAccess,
) -> Result<(), Error> {
    for file in provisioning_files(specs, access)? {
        session
            .upload_file(&file.path, &file.contents, Some(file.mode))
            .await?;
    }
    let request = crate::protocol::exec::StartRequest {
        exec_id: mint_exec_id(),
        command: vec![format!("chown -R {AGENT_UID}:{AGENT_GID} {WORKDIR}")],
        shell: true,
        cwd: None,
        env: std::collections::HashMap::new(),
        user: None,
        group: None,
        timeout_sec: Some(CHOWN_TIMEOUT.as_secs_f64()),
        stdin: false,
    };
    let result = session.run_sync(request, CHOWN_TIMEOUT).await?;
    if !result.succeeded() {
        return Err(Error::new(
            ErrorKind::ExecFailed,
            format!(
                "handing {WORKDIR} to uid {AGENT_UID} failed (exit {:?}): {}",
                result.exit_code(),
                result.stderr().trim()
            ),
        ));
    }
    Ok(())
}

/// The agents a running VM was provisioned with, read from the guest marker (AGENT-10).
///
/// Answers from the VM rather than from a local record so a process that holds only
/// the identifier triple can prompt correctly. A VM with no marker is one `agent-up`
/// never provisioned, and the error says which command fixes that.
pub async fn installed_agents(session: &Session) -> Result<Vec<AgentSpec>, Error> {
    let bytes = match session.download_file(MARKER_FILE).await {
        Ok(bytes) => bytes,
        Err(error) if error.wire_kind() == Some(crate::error::WireKind::NotFound) => {
            return Err(Error::new(
                ErrorKind::Precondition,
                format!(
                    "{MARKER_FILE} is not in this VM, so it was not provisioned as an agent \
                     VM. `microvm agent-up --vm-name <NAME>` installs an agent and writes \
                     the marker; `--agent` names one explicitly when the marker is absent."
                ),
            ));
        }
        Err(error) => return Err(error),
    };
    let marker: serde_json::Value = serde_json::from_slice(&bytes).map_err(|error| {
        Error::new(
            ErrorKind::Protocol,
            format!("{MARKER_FILE} is not the JSON agent-up wrote: {error}"),
        )
    })?;
    let rows = marker["agents"].as_array().ok_or_else(|| {
        Error::new(
            ErrorKind::Protocol,
            format!("{MARKER_FILE} carries no `agents` array"),
        )
    })?;
    let mut specs = Vec::with_capacity(rows.len());
    for row in rows {
        let name = row["agent"].as_str().unwrap_or_default();
        let agent = Agent::parse(name).ok_or_else(|| {
            Error::new(
                ErrorKind::Protocol,
                format!("{MARKER_FILE} names an agent this client has no profile for: {name:?}"),
            )
        })?;
        let mut spec = AgentSpec::new(agent);
        if let Some(model) = row["model"].as_str() {
            spec.model = Some(model.to_string());
        }
        if let Some(version) = row["cliVersion"].as_str() {
            spec.cli_version = Some(version.to_string());
        }
        specs.push(spec);
    }
    require_specs(&specs)?;
    Ok(specs)
}

/// The knobs on one prompt.
#[derive(Clone, Debug, Default)]
pub struct PromptOptions {
    /// A stable exec id, for a retry that must not spawn twice. `None` mints one.
    pub exec_id: Option<String>,
    /// A daemon-side wall-clock budget for the agent process. `None` leaves the exec
    /// unbounded on the daemon's side; the caller's wait still has its own deadline.
    pub timeout: Option<Duration>,
}

/// The start request for one task (AGENT-7). Pure; the test reads every field.
pub fn prompt_request(
    spec: &AgentSpec,
    task: &str,
    options: &PromptOptions,
) -> Result<crate::protocol::exec::StartRequest, Error> {
    if task.trim().is_empty() {
        return Err(Error::invalid_arg(
            "the task is empty. An agent prompted with nothing answers with nothing, at the \
             price of a model call.",
        ));
    }
    let command = format!(
        ". {ENV_FILE} && {}",
        profile::headless_command(spec.agent, &sh_single_quote(task))
    );
    Ok(crate::protocol::exec::StartRequest {
        exec_id: options.exec_id.clone().unwrap_or_else(mint_exec_id),
        command: vec![command],
        shell: true,
        cwd: Some(WORKDIR.to_string()),
        // Empty on the wire: the environment file is the environment, sourced by the
        // command, so the token never rides in a request body.
        env: std::collections::HashMap::new(),
        user: Some(AGENT_UID),
        group: Some(AGENT_GID),
        timeout_sec: options.timeout.map(|timeout| timeout.as_secs_f64()),
        stdin: false,
    })
}

/// The headless command with `<TASK>` where the quoted task goes, for a caller who wants
/// to run it through `exec --stream` or read what `prompt` would do.
pub fn headless_command_template(agent: Agent) -> String {
    format!(
        ". {ENV_FILE} && {}",
        profile::headless_command(agent, "'<TASK>'")
    )
}

/// Starts one task and returns its handle. Wait, stream, poll, or ack through the
/// handle, exactly as for any other exec.
pub async fn prompt(
    session: &Session,
    spec: &AgentSpec,
    task: &str,
    options: &PromptOptions,
) -> Result<ExecHandle, Error> {
    session.run(prompt_request(spec, task, options)?).await
}

/// One VM with coding agents in it: the [`Sandbox`] plus the specs it was built for.
///
/// Runtime-checked, like the sandbox it wraps, and for the same reason: one object is
/// what the bindings can hold.
pub struct AgentVm {
    sandbox: Sandbox,
    specs: Vec<AgentSpec>,
}

impl std::fmt::Debug for AgentVm {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentVm")
            .field("agents", &self.specs)
            .field("sandbox", &self.sandbox)
            .finish()
    }
}

impl AgentVm {
    /// Wraps a sandbox for `specs`. Refuses an empty or repeated set.
    pub fn new(sandbox: Sandbox, specs: Vec<AgentSpec>) -> Result<Self, Error> {
        require_specs(&specs)?;
        Ok(Self { sandbox, specs })
    }

    /// A VM for Claude Code with the profile's defaults.
    pub fn claude_code(sandbox: Sandbox) -> Self {
        Self {
            sandbox,
            specs: vec![AgentSpec::new(Agent::ClaudeCode)],
        }
    }

    /// A VM for Codex with the profile's defaults.
    pub fn codex(sandbox: Sandbox) -> Self {
        Self {
            sandbox,
            specs: vec![AgentSpec::new(Agent::Codex)],
        }
    }

    /// The specs this VM carries, in profile order.
    pub fn specs(&self) -> Vec<AgentSpec> {
        ordered(&self.specs).into_iter().cloned().collect()
    }

    pub fn sandbox(&self) -> &Sandbox {
        &self.sandbox
    }

    pub fn sandbox_mut(&mut self) -> &mut Sandbox {
        &mut self.sandbox
    }

    /// The session, once [`AgentVm::launch`] has run.
    pub fn session(&self) -> Option<&Session> {
        self.sandbox.session()
    }

    /// The image name for these specs and this binary: the stem plus the first twelve
    /// hex characters of the artifact content hash, so an unchanged binary and an
    /// unchanged spec set name the image `build --reuse` would find (AGENT-3).
    pub fn image_name(&self, request: &CreateImageRequest) -> String {
        image_name_for(&self.sandbox, &self.specs, request)
    }

    /// The create request for this VM's image, named per [`AgentVm::image_name`].
    ///
    /// `code_artifact_uri` is left **empty** for the caller to fill: the upload is theirs
    /// (S3 is not in this crate's dependency set), and the key they choose is usually
    /// derived from the name this method just computed. `Sandbox::preflight` refuses a
    /// request whose URI is still blank.
    pub fn image_request(
        &self,
        binary: Vec<u8>,
        build_role_arn: impl Into<String>,
        size: SizeClass,
    ) -> Result<CreateImageRequest, Error> {
        image_request_for(&self.sandbox, &self.specs, binary, build_role_arn, size)
    }

    /// Builds the image. The caller has uploaded the artifact to the request's URI.
    pub async fn build(&mut self, request: CreateImageRequest) -> Result<&Image, Error> {
        self.sandbox.build_image(request).await
    }

    /// The launch request: egress on (AGENT-9), because neither agent reaches Bedrock
    /// without it, and the token label set to the image name.
    pub fn launch_request(
        &self,
        image_identifier: impl Into<String>,
        execution_role_arn: Option<String>,
    ) -> RunRequest {
        launch_request_for(&self.specs, image_identifier, execution_role_arn)
    }

    /// Launches and waits for the daemon to answer.
    pub async fn launch(&mut self, request: RunRequest) -> Result<&Session, Error> {
        let session = self.sandbox.run(request).await?;
        session
            .wait_until_ready(crate::session::DEFAULT_READY_TIMEOUT)
            .await?;
        Ok(session)
    }

    fn require_session(&self) -> Result<&Session, Error> {
        self.sandbox.session().ok_or_else(|| {
            Error::new(
                ErrorKind::Precondition,
                "this agent VM has not been launched; call `launch` first.",
            )
        })
    }

    /// [`install_access`] on this VM's session.
    pub async fn install_access(&self, access: &BedrockAccess) -> Result<(), Error> {
        install_access(self.require_session()?, &self.specs, access).await
    }

    /// [`prompt`] on this VM's session, for one of the agents it carries.
    pub async fn prompt(
        &self,
        agent: Agent,
        task: &str,
        options: &PromptOptions,
    ) -> Result<ExecHandle, Error> {
        let spec = spec_for(&self.specs, agent)?;
        prompt(self.require_session()?, spec, task, options).await
    }

    /// Tears the VM down; see [`Sandbox::terminate`].
    pub async fn terminate(&mut self, opts: TeardownOpts) -> TeardownReport {
        self.sandbox.terminate(opts).await
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::control::artifact::{
        require_daemon_cmd, require_matching_agentd_port, require_matching_from, require_workdir,
    };
    use crate::session::testing::{Recorder, Reply, session_with};

    fn both() -> Vec<AgentSpec> {
        vec![
            AgentSpec::new(Agent::Codex),
            AgentSpec::new(Agent::ClaudeCode),
        ]
    }

    fn access() -> BedrockAccess {
        BedrockAccess {
            region: Region::UsEast1,
            token: bedrock::mint_with(
                &aws_credential_types::Credentials::new("AKID", "secret", None, None, "t"),
                &Region::UsEast1,
                Duration::from_secs(60),
                std::time::SystemTime::UNIX_EPOCH,
            )
            .expect("mints")
            .token,
        }
    }

    // ── the Dockerfile (AGENT-2) ─────────────────────────────────────────────

    /// **The derived Dockerfile passes every guard preflight runs**, and carries the four
    /// things the recipe needs: the layers, the user, the owned WORKDIR, the daemon lines.
    ///
    /// **Falsification** — drop the `chown` from the mkdir line and the ownership assert
    /// goes red; emit the `dnf` line per profile and the count assert goes red; move
    /// `WORKDIR` above the layers and the order assert goes red.
    #[test]
    fn the_dockerfile_passes_the_guards_and_carries_the_recipe() {
        let base = BaseImage::al2023();
        let text = dockerfile(&both(), &base, 9000).expect("derives");

        require_matching_from(&base, &text).expect("FROM pairs with the base");
        require_workdir(&base, Some(&text)).expect("a WORKDIR is declared");
        require_daemon_cmd(&text).expect("ENTRYPOINT [] + CMD agentd survive");
        require_matching_agentd_port(9000, &text).expect("the port agrees");

        assert_eq!(
            text.matches("RUN dnf install").count(),
            1,
            "the system layer is emitted once for two profiles:\n{text}"
        );
        assert!(
            text.contains("RUN dnf install -y --setopt=install_weak_deps=0 nodejs22 nodejs22-npm "),
            "with weak deps off, bare `npm` is Node 18's and the npm layer exits 127:\n{text}"
        );
        assert!(
            text.contains("RUN npm install -g @anthropic-ai/claude-code @openai/codex && npm"),
            "both CLIs in profile order, in one layer:\n{text}"
        );
        assert!(
            text.contains("agent:x:1000:1000::/workspace:/bin/bash"),
            "{text}"
        );
        assert!(
            text.contains("RUN mkdir -p /workspace && chown 1000:1000 /workspace"),
            "{text}"
        );
        let layers_at = text.find("RUN npm install").expect("layers");
        let workdir_at = text.find("WORKDIR /workspace").expect("workdir");
        let cmd_at = text.find("CMD [\"/agentd\"]").expect("cmd");
        assert!(layers_at < workdir_at && workdir_at < cmd_at, "{text}");
    }

    /// A version pin appears on the install line and nowhere else, so it changes the
    /// Dockerfile text (and therefore the reuse hash) and nothing about the recipe.
    #[test]
    fn a_cli_version_pin_reaches_the_install_line() {
        let base = BaseImage::al2023();
        let pinned = vec![AgentSpec::new(Agent::ClaudeCode).with_cli_version("2.1.0")];
        let text = dockerfile(&pinned, &base, 9000).expect("derives");
        assert!(text.contains("@anthropic-ai/claude-code@2.1.0"), "{text}");
        let plain = dockerfile(&[AgentSpec::new(Agent::ClaudeCode)], &base, 9000).expect("d");
        assert_ne!(text, plain);
    }

    #[test]
    fn an_empty_or_repeated_spec_set_is_refused_before_any_text_is_built() {
        let base = BaseImage::al2023();
        assert_eq!(
            dockerfile(&[], &base, 9000).expect_err("empty").kind(),
            ErrorKind::InvalidArg
        );
        let twice = vec![AgentSpec::new(Agent::Codex), AgentSpec::new(Agent::Codex)];
        let error = dockerfile(&twice, &base, 9000).expect_err("repeated");
        assert!(
            error.to_string().contains("codex is named twice"),
            "{error}"
        );
    }

    /// The name stem is order-independent and spells the agents in profile order.
    #[test]
    fn the_image_stem_is_stable_under_spec_order() {
        assert_eq!(image_stem(&both()), "agent-vm-claude-code-codex");
        assert_eq!(
            image_stem(&[AgentSpec::new(Agent::Codex)]),
            "agent-vm-codex"
        );
    }

    // ── the provisioning files (AGENT-5, AGENT-6) ────────────────────────────

    /// **Claude alone gets two files; Codex adds its config.** The environment file sets
    /// PATH and HOME, carries the token under each agent's variable, and every value is
    /// double-quoted. The marker names the agents and their resolved models.
    #[test]
    fn the_file_set_follows_the_profiles_and_the_env_file_is_quoted() {
        let access = access();
        let claude_only =
            provisioning_files(&[AgentSpec::new(Agent::ClaudeCode)], &access).expect("files");
        assert_eq!(
            claude_only
                .iter()
                .map(|f| f.path.as_str())
                .collect::<Vec<_>>(),
            vec![ENV_FILE, MARKER_FILE]
        );

        let files = provisioning_files(&both(), &access).expect("files");
        assert_eq!(
            files.iter().map(|f| f.path.as_str()).collect::<Vec<_>>(),
            vec![ENV_FILE, CODEX_CONFIG_FILE, MARKER_FILE]
        );
        let env = String::from_utf8(files[0].contents.clone()).expect("utf-8");
        assert_eq!(files[0].mode, "0600");
        assert!(
            env.contains("export PATH=\"/usr/local/bin:/usr/bin:/bin\"\n"),
            "{env}"
        );
        assert!(env.contains("export HOME=\"/workspace\"\n"), "{env}");
        assert!(env.contains("export AWS_REGION=\"us-east-1\"\n"), "{env}");
        assert!(
            env.contains("export CLAUDE_CODE_USE_BEDROCK=\"1\"\n"),
            "{env}"
        );
        assert!(
            env.contains("export ANTHROPIC_MODEL=\"global.anthropic.claude-opus-5\"\n"),
            "{env}"
        );
        let token = access.token.expose();
        assert!(env.contains(&format!("export AWS_BEARER_TOKEN_BEDROCK=\"{token}\"\n")));
        assert!(env.contains(&format!("export OPENAI_API_KEY=\"{token}\"\n")));

        let config = String::from_utf8(files[1].contents.clone()).expect("utf-8");
        assert!(
            config.contains("bedrock-runtime.us-east-1.amazonaws.com/openai/v1"),
            "{config}"
        );
        assert!(
            config.contains("model = \"global.openai.gpt-5.6-sol\""),
            "{config}"
        );

        let marker: serde_json::Value = serde_json::from_slice(&files[2].contents).expect("json");
        assert_eq!(files[2].mode, "0644");
        assert_eq!(marker["agents"][0]["agent"], "claude-code");
        assert_eq!(marker["agents"][1]["agent"], "codex");
        assert_eq!(marker["agents"][1]["model"], "global.openai.gpt-5.6-sol");
        assert!(
            !files[2]
                .contents
                .windows(token.len())
                .any(|w| w == token.as_bytes()),
            "the marker is world-readable and must never carry the token"
        );
    }

    #[test]
    fn shell_quoting_escapes_what_each_quote_style_cannot_hold() {
        assert_eq!(sh_double_quote("a$b\"c`d\\e"), "\"a\\$b\\\"c\\`d\\\\e\"");
        assert_eq!(sh_single_quote("it's"), "'it'\\''s'");
        assert_eq!(sh_single_quote("plain"), "'plain'");
    }

    // ── install and read-back through the recorder ───────────────────────────

    /// **The uploads go out in file order, then exactly one root chown.** The exec that
    /// hands the workspace over runs as the daemon's own user (no `user` on the wire),
    /// after every PUT.
    #[tokio::test(start_paused = true)]
    async fn install_access_uploads_then_chowns_as_root() {
        let recorder = Recorder::with([
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
            Reply::ok(serde_json::json!({"exec_id":"c1","phase":"running"})),
            Reply::ok(serde_json::json!({"exec_id":"c1","phase":"exited"})),
            Reply::ok(serde_json::json!({
                "exec_id":"c1","phase":"exited","exit_code":0,"signal":null,
                "stdout":"","stderr":"","truncated":false,"writers_may_be_alive":false
            })),
        ]);
        let (session, _, _) = session_with(Arc::clone(&recorder));

        install_access(&session, &both(), &access())
            .await
            .expect("installs");

        let seen = recorder.requests();
        assert_eq!(seen.len(), 6, "{seen:#?}");
        for (request, (encoded, mode)) in seen.iter().zip([
            ("%2Fworkspace%2F.agent-env", "0600"),
            ("%2Fworkspace%2F.codex%2Fconfig.toml", "0600"),
            ("%2Fworkspace%2F.agent-vm.json", "0644"),
        ]) {
            assert_eq!(request.method, "PUT");
            assert_eq!(
                request.path,
                format!("/v1/fs/file?path={encoded}&mode={mode}"),
                "each file goes to the file route with its own mode"
            );
        }
        assert_eq!(seen[3].path, "/v1/exec/start");
        let start: serde_json::Value = serde_json::from_slice(&seen[3].body).expect("json");
        assert_eq!(start["command"][0], "chown -R 1000:1000 /workspace");
        assert_eq!(start["shell"], true);
        assert!(
            start["user"].is_null(),
            "the chown runs as the daemon's own user"
        );
        assert_eq!(seen[5].path, "/v1/exec/c1/ack");
    }

    /// A failed chown is a failure, not a warning: a workspace the agent cannot write is
    /// a VM every prompt fails in.
    #[tokio::test(start_paused = true)]
    async fn a_failed_chown_fails_the_install() {
        let recorder = Recorder::with([
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
            Reply::ok(serde_json::json!({"exec_id":"c1","phase":"running"})),
            Reply::ok(serde_json::json!({"exec_id":"c1","phase":"exited"})),
            Reply::ok(serde_json::json!({
                "exec_id":"c1","phase":"exited","exit_code":1,"signal":null,
                "stdout":"","stderr":"chown: cannot access","truncated":false,
                "writers_may_be_alive":false
            })),
        ]);
        let (session, _, _) = session_with(recorder);
        let error = install_access(&session, &[AgentSpec::new(Agent::ClaudeCode)], &access())
            .await
            .expect_err("fails");
        assert_eq!(error.kind(), ErrorKind::ExecFailed);
        assert!(
            error.to_string().contains("chown: cannot access"),
            "{error}"
        );
    }

    /// **The marker round-trips**: what `provisioning_files` wrote is what
    /// `installed_agents` reads back, models included.
    #[tokio::test]
    async fn installed_agents_reads_the_marker_back() {
        let specs = vec![
            AgentSpec::new(Agent::ClaudeCode).with_model("global.anthropic.claude-sonnet-5"),
            AgentSpec::new(Agent::Codex).with_cli_version("0.50.0"),
        ];
        let files = provisioning_files(&specs, &access()).expect("files");
        let marker = files.last().expect("marker").contents.clone();
        let recorder = Recorder::with([Reply::Body(200, marker)]);
        let (session, _, _) = session_with(recorder);

        let read = installed_agents(&session).await.expect("reads");
        assert_eq!(read.len(), 2);
        assert_eq!(read[0].agent, Agent::ClaudeCode);
        assert_eq!(read[0].model(), "global.anthropic.claude-sonnet-5");
        assert_eq!(read[1].agent, Agent::Codex);
        assert_eq!(read[1].cli_version.as_deref(), Some("0.50.0"));
        assert_eq!(read[1].model(), "global.openai.gpt-5.6-sol");
    }

    /// A VM with no marker is a precondition failure naming the command that writes one,
    /// and any other refusal propagates as itself.
    #[tokio::test]
    async fn a_missing_marker_names_agent_up_and_other_failures_propagate() {
        let recorder = Recorder::with([
            Reply::Body(404, b"not found".to_vec()),
            Reply::Body(503, b"not bootstrapped".to_vec()),
        ]);
        let (session, _, _) = session_with(recorder);
        let error = installed_agents(&session).await.expect_err("absent");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        assert!(error.to_string().contains("agent-up"), "{error}");
        let other = installed_agents(&session).await.expect_err("503");
        assert_ne!(other.kind(), ErrorKind::Precondition);
    }

    // ── the prompt (AGENT-7) ─────────────────────────────────────────────────

    /// **Every field of the start request is the recipe's.** uid and gid 1000, the
    /// workspace as cwd, an empty wire env, the env file sourced first, the task
    /// single-quoted, the daemon-side timeout only when asked for.
    #[test]
    fn the_prompt_request_runs_the_agent_demoted_in_the_workspace() {
        let spec = AgentSpec::new(Agent::ClaudeCode);
        let request = prompt_request(
            &spec,
            "count the files; it's quick",
            &PromptOptions {
                exec_id: Some("p1".into()),
                timeout: Some(Duration::from_secs(120)),
            },
        )
        .expect("builds");
        assert_eq!(request.exec_id, "p1");
        assert_eq!(request.user, Some(1000));
        assert_eq!(request.group, Some(1000));
        assert_eq!(request.cwd.as_deref(), Some("/workspace"));
        assert!(request.shell);
        assert!(
            request.env.is_empty(),
            "the token never rides in the request"
        );
        assert_eq!(request.timeout_sec, Some(120.0));
        assert!(!request.stdin);
        assert_eq!(
            request.command,
            vec![
                ". /workspace/.agent-env && claude -p 'count the files; it'\\''s quick' \
                 --allowedTools Bash,Read,Edit,Write,Grep,Glob"
                    .to_string()
            ]
        );

        let codex = prompt_request(
            &AgentSpec::new(Agent::Codex),
            "hi",
            &PromptOptions::default(),
        )
        .expect("builds");
        assert!(
            codex.command[0].contains("codex exec --skip-git-repo-check -s workspace-write 'hi'")
        );
        assert!(codex.timeout_sec.is_none());
        assert!(!codex.exec_id.is_empty(), "an id was minted");
    }

    #[test]
    fn an_empty_task_is_refused_locally() {
        let error = prompt_request(
            &AgentSpec::new(Agent::Codex),
            "  \n",
            &PromptOptions::default(),
        )
        .expect_err("empty");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
    }

    // ── AgentVm ──────────────────────────────────────────────────────────────

    fn sandbox() -> Sandbox {
        Sandbox::with_control_plane(crate::control::ControlPlane::with_transport(
            Arc::new(crate::control::fake::FakeControlPlane::new()),
            Region::UsEast1,
            Arc::new(crate::control::fake::TestClock::new()),
        ))
    }

    /// **The image request is named by content and the URI is left to the caller.**
    /// Same binary and specs give the same name; a different binary gives a different one.
    #[test]
    fn the_image_request_is_content_named_and_egress_is_on_at_launch() {
        let vm = AgentVm::new(sandbox(), both()).expect("specs");
        let a = vm
            .image_request(
                b"daemon".to_vec(),
                "arn:aws:iam::123456789012:role/b",
                DEFAULT_SIZE,
            )
            .expect("request");
        let b = vm
            .image_request(
                b"daemon".to_vec(),
                "arn:aws:iam::123456789012:role/b",
                DEFAULT_SIZE,
            )
            .expect("request");
        let c = vm
            .image_request(
                b"other".to_vec(),
                "arn:aws:iam::123456789012:role/b",
                DEFAULT_SIZE,
            )
            .expect("request");
        assert_eq!(a.name, b.name);
        assert_ne!(a.name, c.name);
        assert!(
            a.name.starts_with("agent-vm-claude-code-codex-"),
            "{}",
            a.name
        );
        assert_eq!(a.name.len(), "agent-vm-claude-code-codex-".len() + 12);
        assert_eq!(a.token_scope.as_deref(), Some(a.name.as_str()));
        assert!(a.code_artifact_uri.is_empty(), "the upload is the caller's");
        assert!(
            a.dockerfile
                .as_deref()
                .is_some_and(|d| d.contains("@openai/codex"))
        );
        assert_eq!(a.size, SizeClass::Mib1024);

        let launch = vm.launch_request("arn:image", Some("arn:role".into()));
        assert!(
            launch.egress,
            "neither agent reaches Bedrock without egress"
        );
        assert_eq!(launch.image_identifier.as_deref(), Some("arn:image"));
        assert_eq!(
            launch.token_scope.as_deref(),
            Some("agent-vm-claude-code-codex")
        );
    }

    /// The named constructors carry the request's own names, and a prompt for an agent
    /// the VM does not carry is refused before any wire call.
    #[tokio::test]
    async fn the_named_constructors_carry_one_agent_each() {
        let claude = AgentVm::claude_code(sandbox());
        assert_eq!(claude.specs(), vec![AgentSpec::new(Agent::ClaudeCode)]);
        let codex = AgentVm::codex(sandbox());
        assert_eq!(codex.specs(), vec![AgentSpec::new(Agent::Codex)]);

        let error = codex
            .prompt(Agent::ClaudeCode, "hi", &PromptOptions::default())
            .await
            .expect_err("wrong agent");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("carries codex"), "{error}");
        let unlaunched = codex
            .prompt(Agent::Codex, "hi", &PromptOptions::default())
            .await
            .expect_err("no session");
        assert_eq!(unlaunched.kind(), ErrorKind::Precondition);
    }
}
