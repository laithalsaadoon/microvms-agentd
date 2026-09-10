// SPDX-License-Identifier: Apache-2.0
//! L3: a VM with coding agents in it, as JS sees it (`docs/AGENT-VMS.md`).
//!
//! # One object over the same lock
//!
//! [`AgentVm`] holds the *same* `Arc<tokio::sync::Mutex<Sandbox>>` that
//! [`crate::sandbox::Sandbox`] and every [`crate::session::Session`] it hands out hold, so
//! `vm.terminate()` and a session call cannot interleave — the runtime spelling of the
//! core's `&mut self`. The core's `AgentVm` owns its sandbox, which one `#[napi]` class
//! cannot share, so this file drives the layer through the core's free functions with the
//! specs kept beside the lock. Every refusal is the core's.
//!
//! # `#[napi(object)]` for the spec, a class for the token
//!
//! An [`AgentSpecInput`] is a plain object because a structurally valid one is a valid spec:
//! the agent name is parsed by the core and refused with the list. A [`BearerToken`] is a
//! class because it carries a secret: `JSON.stringify(token)` is `{}`, `String(token)` is
//! `[object BearerToken]`, and only `expose()` returns the text.
//!
//! # The upload is still the caller's
//!
//! S3 is not in the core's dependency set. `findImage` says whether the content-named image
//! exists; `buildArtifact` gives the bytes to put at `s3://<bucket>/<name>.zip`; `buildImage`
//! takes that URI.

use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use microvms_core::agents::bedrock::{self, BearerToken as CoreToken, MAX_LIFETIME};
use microvms_core::agents::{
    self, AGENT_GID, AGENT_UID, Agent, AgentSpec as CoreSpec, BedrockAccess,
    DEFAULT_PROMPT_TIMEOUT, DEFAULT_SIZE, PromptOptions as CorePromptOptions, WORKDIR, profile,
};
use microvms_core::control::{BaseImage, CreateImageRequest};
use microvms_core::sandbox::Sandbox as CoreSandbox;
use microvms_core::{Error, ErrorKind, Region as CoreRegion};
use napi_derive::napi;
use tokio::sync::Mutex;

use crate::cost::SizeClass;
use crate::errors::{AsyncError, js_async};
use crate::exec::{ExecHandle, ExecResult, seconds_async};
use crate::region::Region;
use crate::sandbox::{Image, Sandbox, TeardownOptions, TeardownReport};
use crate::session::Session;

/// One agent to install: its name, and the two defaults a caller may override.
///
/// `agent` is `"claude-code"` or `"codex"`; anything else is refused by the core with the
/// list. `model` defaults to the profile's row (an inference-profile id), `cliVersion` to the
/// registry's latest at build time; a pin changes the image name.
#[napi(object)]
#[derive(Clone)]
pub struct AgentSpecInput {
    pub agent: String,
    pub model: Option<String>,
    pub cli_version: Option<String>,
}

impl AgentSpecInput {
    fn into_core(self) -> Result<CoreSpec, Error> {
        let agent: Agent = self.agent.parse()?;
        let mut spec = CoreSpec::new(agent);
        spec.model = self.model;
        spec.cli_version = self.cli_version;
        Ok(spec)
    }
}

/// A resolved spec: the model it will use and the command `prompt` runs.
#[napi(object)]
pub struct AgentSpec {
    /// `"claude-code"` or `"codex"`.
    pub agent: String,
    /// The model this spec resolves to: the override, or the profile's default.
    pub model: String,
    /// The pinned CLI version, or `null` for the registry's latest at build time.
    pub cli_version: Option<String>,
    /// The exact command `prompt` runs, with `<TASK>` where the quoted task goes.
    pub headless_command: String,
}

impl AgentSpec {
    fn wrap(spec: &CoreSpec) -> Self {
        Self {
            agent: spec.agent.as_str().to_string(),
            model: spec.model().to_string(),
            cli_version: spec.cli_version.clone(),
            headless_command: agents::headless_command_template(spec.agent),
        }
    }
}

fn specs_from(inputs: Vec<AgentSpecInput>) -> Result<Vec<CoreSpec>, Error> {
    let specs = inputs
        .into_iter()
        .map(AgentSpecInput::into_core)
        .collect::<Result<Vec<_>, _>>()?;
    agents::require_specs(&specs)?;
    Ok(specs)
}

/// A Bedrock bearer token, the region it was minted for, and when it stops working.
///
/// A class rather than an object because it carries a secret: `JSON.stringify` gives `{}` and
/// only `expose()` returns the text.
#[napi]
pub struct BearerToken {
    token: CoreToken,
    region: CoreRegion,
    expires_at: std::time::SystemTime,
}

impl BearerToken {
    fn access(&self) -> BedrockAccess {
        BedrockAccess {
            region: self.region.clone(),
            token: self.token.clone(),
        }
    }

    async fn mint(region: &CoreRegion, ttl_seconds: Option<f64>) -> Result<Self, AsyncError> {
        let lifetime = match ttl_seconds {
            Some(ttl) => seconds_async(ttl)?,
            None => MAX_LIFETIME,
        };
        let minted = bedrock::mint(region, lifetime).await.map_err(js_async)?;
        Ok(Self {
            token: minted.token,
            region: region.clone(),
            expires_at: minted.expires_at,
        })
    }
}

#[napi]
impl BearerToken {
    /// The token text, for a caller writing it into an environment themselves.
    #[napi]
    pub fn expose(&self) -> String {
        self.token.expose().to_string()
    }

    /// The presign's expiry, seconds since the epoch. An upper bound: the service also caps
    /// validity at the signing credentials' own expiry.
    #[napi(getter)]
    pub fn expires_at(&self) -> f64 {
        self.expires_at
            .duration_since(UNIX_EPOCH)
            .map(|since| since.as_secs_f64())
            .unwrap_or(0.0)
    }

    /// The region the token was minted for.
    #[napi(getter)]
    pub fn region(&self) -> Region {
        Region {
            inner: self.region.clone(),
        }
    }

    /// The length of the token, never the token.
    #[napi(js_name = "toString")]
    pub fn describe(&self) -> String {
        format!(
            "BearerToken(<{} bytes>, region={}, expiresAt={:.0})",
            self.token.expose().len(),
            self.region.as_str(),
            self.expires_at()
        )
    }
}

/// Mints a Bedrock bearer token from the default credential chain.
///
/// A SigV4 presign of `POST https://bedrock.amazonaws.com/?Action=CallWithBearerToken`,
/// base64, prefixed `bedrock-api-key-` — the reference generator's recipe, in process.
/// `ttlSeconds` defaults to the ceiling, twelve hours; more is refused by the core.
#[napi]
pub async fn mint_bedrock_token(
    region: &Region,
    ttl_seconds: Option<f64>,
) -> Result<BearerToken, AsyncError> {
    BearerToken::mint(&region.inner, ttl_seconds).await
}

/// The knobs on one prompt.
#[derive(Default)]
#[napi(object)]
pub struct PromptOptions {
    /// A daemon-side wall-clock budget for the agent process, in seconds.
    pub timeout_sec: Option<f64>,
    /// A stable exec id, for a retry that must not spawn twice.
    pub exec_id: Option<String>,
}

impl PromptOptions {
    fn into_core(self) -> Result<CorePromptOptions, AsyncError> {
        Ok(CorePromptOptions {
            exec_id: self.exec_id,
            timeout: match self.timeout_sec {
                Some(timeout) => Some(seconds_async(timeout)?),
                None => None,
            },
        })
    }
}

/// The agents a running VM was provisioned with, read from its guest marker.
///
/// For a process holding only a session (`Session.direct` from the identifier triple): how a
/// credential refresh learns which agents and models to re-provision. A VM with no marker is
/// refused as a precondition, naming `agent-up`.
#[napi]
pub async fn installed_agents(session: &Session) -> Result<Vec<AgentSpec>, AsyncError> {
    let live = session.live().await;
    let specs = agents::installed_agents(live.session().map_err(js_async)?)
        .await
        .map_err(js_async)?;
    Ok(specs.iter().map(AgentSpec::wrap).collect())
}

/// Installs Bedrock access for `agents` into a running VM over `session`.
///
/// Three uploads (the environment file, Codex's config when Codex is among the agents, the
/// marker) and one root `chown` of `/workspace` to uid 1000. Re-runnable: a fresh token
/// overwrites the same files, which is how a twelve-hour token is refreshed.
#[napi]
pub async fn install_agent_access(
    session: &Session,
    agents_: Vec<AgentSpecInput>,
    token: &BearerToken,
) -> Result<(), AsyncError> {
    let specs = specs_from(agents_).map_err(js_async)?;
    let access = token.access();
    let live = session.live().await;
    agents::install_access(live.session().map_err(js_async)?, &specs, &access)
        .await
        .map_err(js_async)
}

/// Starts one task for `agent` over `session` and resolves with its handle. Does not wait.
#[napi]
pub async fn prompt_agent(
    session: &Session,
    agent: AgentSpecInput,
    task: String,
    options: Option<PromptOptions>,
) -> Result<ExecHandle, AsyncError> {
    let spec = agent.into_core().map_err(js_async)?;
    let options = options.unwrap_or_default().into_core()?;
    let live = session.live().await;
    let handle = agents::prompt(live.session().map_err(js_async)?, &spec, &task, &options)
        .await
        .map_err(js_async)?;
    Ok(ExecHandle::wrap(handle))
}

/// The layer's fixed values as JSON, for a caller that wants to reason about the guest.
#[napi]
pub fn agent_constants() -> String {
    let profiles: serde_json::Map<String, serde_json::Value> = Agent::ALL
        .iter()
        .map(|agent| {
            let row = agent.profile();
            (
                agent.as_str().to_string(),
                serde_json::json!({
                    "npmPackage": row.npm_package,
                    "defaultModel": row.default_model,
                    "verified": row.verified,
                }),
            )
        })
        .collect();
    serde_json::json!({
        "uid": AGENT_UID,
        "gid": AGENT_GID,
        "workdir": WORKDIR,
        "envFile": profile::ENV_FILE,
        "codexConfigFile": profile::CODEX_CONFIG_FILE,
        "markerFile": profile::MARKER_FILE,
        "defaultMemoryMib": DEFAULT_SIZE.baseline_mib(),
        "defaultPromptTimeoutSec": DEFAULT_PROMPT_TIMEOUT.as_secs_f64(),
        "maxTokenLifetimeSec": MAX_LIFETIME.as_secs_f64(),
        "profiles": profiles,
    })
    .to_string()
}

/// What an agent image is derived from: the daemon binary and the build role.
#[napi(object)]
pub struct AgentImageOptions {
    /// The daemon binary's bytes, zipped into the artifact.
    pub binary: napi::bindgen_prelude::Uint8Array,
    /// The build role, which must grant logs on `/aws/lambda-microvms/*`.
    pub build_role_arn: String,
    /// Where `buildArtifact`'s bytes were uploaded. Required by `buildImage`, ignored by the
    /// name and artifact calls.
    pub code_artifact_uri: Option<String>,
}

/// Everything a launch takes beyond what the layer fixes (egress on, the image).
#[derive(Default)]
#[napi(object)]
pub struct AgentLaunchOptions {
    /// The image ARN from `findImage` or `buildImage`.
    pub image_identifier: String,
    /// The execution role. Optional in the model; every real launch needs one.
    pub execution_role_arn: Option<String>,
    pub max_idle_sec: Option<u32>,
    pub suspended_sec: Option<u32>,
    pub auto_resume: Option<bool>,
    pub max_duration_sec: Option<u32>,
}

/// One VM with coding agents in it: the sandbox plus the specs it is built for.
///
/// The sequence is the CLI's `agent-up` and `agent-prompt`, one method per step: `findImage`
/// or `buildArtifact` + your upload + `buildImage`; `launch`; `installAccess`; `prompt` or
/// `promptSync`; `terminate`. `sandbox()` and `session()` reach the same VM for suspend,
/// resume, file transfer, and any other exec.
#[napi]
pub struct AgentVm {
    sandbox: Arc<Mutex<CoreSandbox>>,
    specs: Vec<CoreSpec>,
    region: CoreRegion,
}

impl AgentVm {
    fn image_request(
        &self,
        sandbox: &CoreSandbox,
        options: AgentImageOptions,
        size: Option<&SizeClass>,
    ) -> Result<CreateImageRequest, AsyncError> {
        let size = size.map(|size| size.inner).unwrap_or(DEFAULT_SIZE);
        let mut request = agents::image_request_for(
            sandbox,
            &self.specs,
            options.binary.to_vec(),
            options.build_role_arn,
            size,
        )
        .map_err(js_async)?;
        if let Some(uri) = options.code_artifact_uri {
            request.code_artifact_uri = uri;
        }
        Ok(request)
    }

    fn require_session(
        sandbox: &CoreSandbox,
    ) -> Result<&microvms_core::session::Session, AsyncError> {
        sandbox
            .session()
            .ok_or_else(|| {
                Error::new(
                    ErrorKind::Precondition,
                    "this agent VM has not been launched; call `launch` first.",
                )
            })
            .map_err(js_async)
    }

    fn spec(&self, agent: &str) -> Result<CoreSpec, AsyncError> {
        let agent: Agent = agent.parse().map_err(js_async)?;
        agents::spec_for(&self.specs, agent)
            .cloned()
            .map_err(js_async)
    }
}

#[napi]
impl AgentVm {
    /// Resolves credentials for `region` and returns a VM with nothing built or launched.
    ///
    /// `agents` defaults to Claude Code alone. An empty list or a repeated agent is refused by
    /// the core before any AWS call.
    #[napi(factory)]
    pub async fn create(
        region: &Region,
        agents_: Option<Vec<AgentSpecInput>>,
    ) -> Result<AgentVm, AsyncError> {
        let specs = match agents_ {
            Some(inputs) => specs_from(inputs).map_err(js_async)?,
            None => vec![CoreSpec::new(Agent::ClaudeCode)],
        };
        let sandbox = CoreSandbox::new(region.inner.clone())
            .await
            .map_err(js_async)?;
        Ok(AgentVm {
            sandbox: Arc::new(Mutex::new(sandbox)),
            specs,
            region: region.inner.clone(),
        })
    }

    /// The specs this VM carries, resolved, in profile order.
    #[napi]
    pub fn agents(&self) -> Vec<AgentSpec> {
        let mut specs = self.specs.clone();
        specs.sort_by_key(|spec| spec.agent);
        specs.iter().map(AgentSpec::wrap).collect()
    }

    #[napi]
    pub fn region(&self) -> Region {
        Region {
            inner: self.region.clone(),
        }
    }

    /// The sandbox this VM drives, for suspend, resume, and the lifecycle getters. The same
    /// lock: a call here and a call there cannot interleave.
    #[napi]
    pub fn sandbox(&self) -> Sandbox {
        Sandbox::from_arc(Arc::clone(&self.sandbox))
    }

    /// The session, once `launch` has resolved.
    #[napi]
    pub async fn session(&self) -> Option<Session> {
        let guard = self.sandbox.lock().await;
        guard
            .session()
            .is_some()
            .then(|| Session::in_sandbox(Arc::clone(&self.sandbox)))
    }

    /// The Dockerfile `buildImage` will send: the client's agentd stanza plus the agent
    /// layers. Nothing in it is a secret.
    #[napi]
    pub async fn dockerfile(&self) -> Result<String, AsyncError> {
        let port = self.sandbox.lock().await.port();
        agents::dockerfile(&self.specs, &BaseImage::al2023(), port).map_err(js_async)
    }

    /// The image name for these specs and this daemon binary: `agent-vm-<agents>-<hash12>`.
    /// Content-addressed, so an unchanged binary and spec set name the image a previous run
    /// built; `findImage` looks it up.
    #[napi]
    pub async fn image_name(
        &self,
        options: AgentImageOptions,
        size: Option<&SizeClass>,
    ) -> Result<String, AsyncError> {
        let guard = self.sandbox.lock().await;
        Ok(self.image_request(&guard, options, size)?.name)
    }

    /// The ARN of an existing image named per `imageName`, or `null` when there is none.
    #[napi]
    pub async fn find_image(
        &self,
        options: AgentImageOptions,
        size: Option<&SizeClass>,
    ) -> Result<Option<String>, AsyncError> {
        let guard = self.sandbox.lock().await;
        let name = self.image_request(&guard, options, size)?.name;
        let found = guard.find_image_by_name(&name).await.map_err(js_async)?;
        Ok(found.map(|image| image.image_arn))
    }

    /// The artifact bytes to upload to `s3://<bucket>/<imageName>.zip` before `buildImage`.
    #[napi]
    pub async fn build_artifact(
        &self,
        options: AgentImageOptions,
        size: Option<&SizeClass>,
    ) -> Result<napi::bindgen_prelude::Buffer, AsyncError> {
        let guard = self.sandbox.lock().await;
        let request = self.image_request(&guard, options, size)?;
        Ok(guard.build_artifact_for(&request).map_err(js_async)?.into())
    }

    /// Builds the image and waits for it to become usable. `codeArtifactUri` is where you
    /// uploaded `buildArtifact`'s bytes. Several minutes, server-side.
    #[napi]
    pub async fn build_image(
        &self,
        options: AgentImageOptions,
        size: Option<&SizeClass>,
    ) -> Result<Image, AsyncError> {
        let mut guard = self.sandbox.lock().await;
        let request = self.image_request(&guard, options, size)?;
        let image = guard.build_image(request).await.map_err(js_async)?;
        Ok(Image::wrap(image))
    }

    /// Launches with egress and waits for the daemon to answer.
    ///
    /// Egress is not optional: neither agent reaches Bedrock without it. The idle knobs default
    /// to the core's figures (ten-minute idle and suspended windows, a one-hour ceiling).
    #[napi]
    pub async fn launch(&self, options: AgentLaunchOptions) -> Result<Session, AsyncError> {
        let mut request = agents::launch_request_for(
            &self.specs,
            options.image_identifier,
            options.execution_role_arn,
        );
        if let Some(idle) = options.max_idle_sec {
            request.max_idle_sec = idle;
        }
        if let Some(suspended) = options.suspended_sec {
            request.suspended_sec = suspended;
        }
        request.auto_resume = options.auto_resume.unwrap_or(request.auto_resume);
        if let Some(ceiling) = options.max_duration_sec {
            request.max_duration_sec = ceiling;
        }
        {
            let mut guard = self.sandbox.lock().await;
            let session = guard.run(request).await.map_err(js_async)?;
            session
                .wait_until_ready(microvms_core::session::DEFAULT_READY_TIMEOUT)
                .await
                .map_err(js_async)?;
        }
        Ok(Session::in_sandbox(Arc::clone(&self.sandbox)))
    }

    /// Mints a token (or takes yours) and installs Bedrock access for this VM's agents.
    ///
    /// Resolves with the token used, so `expiresAt` says when to call this again. Re-runnable
    /// on a running VM: that call is the credential refresh.
    #[napi]
    pub async fn install_access(
        &self,
        token: Option<&BearerToken>,
        ttl_seconds: Option<f64>,
    ) -> Result<BearerToken, AsyncError> {
        let token = match token {
            Some(token) => BearerToken {
                token: token.token.clone(),
                region: token.region.clone(),
                expires_at: token.expires_at,
            },
            None => BearerToken::mint(&self.region, ttl_seconds).await?,
        };
        let access = token.access();
        {
            let guard = self.sandbox.lock().await;
            agents::install_access(Self::require_session(&guard)?, &self.specs, &access)
                .await
                .map_err(js_async)?;
        }
        Ok(token)
    }

    /// Starts one task for `agent` and resolves with its handle. Does not wait.
    #[napi]
    pub async fn prompt(
        &self,
        agent: String,
        task: String,
        options: Option<PromptOptions>,
    ) -> Result<ExecHandle, AsyncError> {
        let spec = self.spec(&agent)?;
        let options = options.unwrap_or_default().into_core()?;
        let guard = self.sandbox.lock().await;
        let handle = agents::prompt(Self::require_session(&guard)?, &spec, &task, &options)
            .await
            .map_err(js_async)?;
        Ok(ExecHandle::wrap(handle))
    }

    /// Start, wait, ack: one task's whole result. `timeoutSec` defaults to 900 seconds,
    /// because agent tasks run minutes, and is also the daemon-side budget.
    #[napi]
    pub async fn prompt_sync(
        &self,
        agent: String,
        task: String,
        options: Option<PromptOptions>,
    ) -> Result<ExecResult, AsyncError> {
        let spec = self.spec(&agent)?;
        let options = options.unwrap_or_default();
        let timeout: Duration = match options.timeout_sec {
            Some(timeout) => seconds_async(timeout)?,
            None => DEFAULT_PROMPT_TIMEOUT,
        };
        let core_options = CorePromptOptions {
            exec_id: options.exec_id,
            timeout: Some(timeout),
        };
        let request = agents::prompt_request(&spec, &task, &core_options).map_err(js_async)?;
        let guard = self.sandbox.lock().await;
        let result = Self::require_session(&guard)?
            .run_sync(request, timeout)
            .await
            .map_err(js_async)?;
        Ok(ExecResult::wrap(result))
    }

    /// Tears down, best-effort, never rejecting; see `Sandbox.terminate`.
    #[napi]
    pub async fn terminate(
        &self,
        options: Option<TeardownOptions>,
    ) -> Result<TeardownReport, AsyncError> {
        let opts = options.unwrap_or_default().into_opts()?;
        let mut guard = self.sandbox.lock().await;
        Ok(TeardownReport::wrap(guard.terminate(opts).await))
    }
}
