// SPDX-License-Identifier: Apache-2.0
//! L3: a VM with coding agents in it, as Python sees it (`docs/AGENT-VMS.md`).
//!
//! # One object over the same lock
//!
//! [`PyAgentVm`] holds the *same* `Arc<Mutex<Sandbox>>` that [`crate::sandbox::PySandbox`]
//! and every [`crate::session::PySession`] it hands out hold, so `vm.terminate()` and a
//! session call cannot interleave — the runtime spelling of the core's `&mut self`, exactly
//! as for the sandbox. The core's `AgentVm` owns its sandbox, which one `#[pyclass]` cannot
//! share, so this file drives the layer through the core's free functions
//! (`image_request_for`, `launch_request_for`, `install_access`, `prompt`) with the specs
//! kept beside the lock. Every refusal is the core's: an unknown agent name, an empty or
//! repeated spec set, a prompt for an agent the VM does not carry, a blank task, a token
//! lifetime past the ceiling.
//!
//! # The upload is still the caller's
//!
//! The agent image path does not upload, so the sequence a caller writes is the CLI's:
//! `image_name` or `find_image` to learn whether the image exists, `build_artifact` for the
//! bytes, their own `put_object` to `s3://<bucket>/<name>.zip`, then `build_image` with that
//! URI. `find_image` is what makes the second run cost seconds rather than minutes.
//!
//! # The token never crosses in a message
//!
//! [`PyBearerToken`] shows its length in `repr`, and only `expose()` returns the text — for a
//! caller who writes it somewhere themselves. Everything on this surface takes the object.

use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use microvms_core::agents::bedrock::{self, BearerToken, MAX_LIFETIME};
use microvms_core::agents::{
    self, AGENT_GID, AGENT_UID, Agent, AgentSpec, BedrockAccess, DEFAULT_PROMPT_TIMEOUT,
    DEFAULT_SIZE, PromptOptions, WORKDIR, profile,
};
use microvms_core::control::BaseImage;
use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::{Error, Region};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use crate::cost::PySizeClass;
use crate::errors::{CoreError, PyCoreResult};
use crate::exec::{PyExecHandle, PyExecResult, seconds};
use crate::region::PyRegion;
use crate::runtime;
use crate::sandbox::{PyEnsuredImage, PyImage, PySandbox, PyTeardownReport, SharedSandbox};
use crate::session::{PySession, session_op};

/// One agent to install: its name, and the two defaults a caller may override.
///
/// `agent` is `"claude-code"` or `"codex"`; anything else is refused by the core with the
/// list. `model` defaults to the profile's row (an inference-profile id), `cli_version` to
/// the registry's latest at build time; a pin changes the image name.
#[pyclass(frozen, from_py_object, name = "AgentSpec", module = "microvms")]
#[derive(Clone)]
pub struct PyAgentSpec {
    pub(crate) inner: AgentSpec,
}

impl PyAgentSpec {
    fn wrap(inner: AgentSpec) -> Self {
        Self { inner }
    }

    fn build(agent: Agent, model: Option<String>, cli_version: Option<String>) -> Self {
        let mut spec = AgentSpec::new(agent);
        spec.model = model;
        spec.cli_version = cli_version;
        Self { inner: spec }
    }
}

#[pymethods]
impl PyAgentSpec {
    #[new]
    #[pyo3(signature = (agent, *, model=None, cli_version=None))]
    fn new(agent: &str, model: Option<String>, cli_version: Option<String>) -> PyCoreResult<Self> {
        let agent: Agent = agent.parse().map_err(CoreError)?;
        Ok(Self::build(agent, model, cli_version))
    }

    /// Claude Code with the profile's defaults, or with overrides.
    #[staticmethod]
    #[pyo3(signature = (*, model=None, cli_version=None))]
    fn claude_code(model: Option<String>, cli_version: Option<String>) -> Self {
        Self::build(Agent::ClaudeCode, model, cli_version)
    }

    /// Codex with the profile's defaults, or with overrides.
    #[staticmethod]
    #[pyo3(signature = (*, model=None, cli_version=None))]
    fn codex(model: Option<String>, cli_version: Option<String>) -> Self {
        Self::build(Agent::Codex, model, cli_version)
    }

    /// `"claude-code"` or `"codex"`.
    #[getter]
    fn agent(&self) -> &str {
        self.inner.agent.as_str()
    }

    /// The model this spec resolves to: the override, or the profile's default.
    #[getter]
    fn model(&self) -> String {
        self.inner.model().to_string()
    }

    /// The pinned CLI version, or `None` for the registry's latest at build time.
    #[getter]
    fn cli_version(&self) -> Option<String> {
        self.inner.cli_version.clone()
    }

    /// The exact command `prompt` runs, with `<TASK>` where the quoted task goes.
    #[getter]
    fn headless_command(&self) -> String {
        agents::headless_command_template(self.inner.agent)
    }

    fn __eq__(&self, other: &Self) -> bool {
        self.inner == other.inner
    }

    fn __repr__(&self) -> String {
        format!(
            "AgentSpec(agent={:?}, model={:?}, cli_version={:?})",
            self.inner.agent.as_str(),
            self.inner.model(),
            self.inner.cli_version
        )
    }
}

/// A Bedrock bearer token, the region it was minted for, and when it stops working.
///
/// Opaque on purpose: `repr` shows the length, `expose()` is the one door to the text.
#[pyclass(frozen, from_py_object, name = "BearerToken", module = "microvms")]
#[derive(Clone)]
pub struct PyBearerToken {
    token: BearerToken,
    region: Region,
    expires_at: SystemTime,
}

impl PyBearerToken {
    fn access(&self) -> BedrockAccess {
        BedrockAccess {
            region: self.region.clone(),
            token: self.token.clone(),
        }
    }
}

#[pymethods]
impl PyBearerToken {
    /// The token text, for a caller writing it into an environment themselves.
    fn expose(&self) -> String {
        self.token.expose().to_string()
    }

    /// Effective expiry in Unix seconds, capped at known signing credential expiry.
    /// With missing credential expiry metadata this is only an upper bound.
    #[getter]
    fn expires_at(&self) -> f64 {
        self.expires_at
            .duration_since(UNIX_EPOCH)
            .map(|since| since.as_secs_f64())
            .unwrap_or(0.0)
    }

    /// The region the token was minted for.
    #[getter]
    fn region(&self) -> PyRegion {
        PyRegion {
            inner: self.region.clone(),
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "BearerToken(<{} bytes>, region={:?}, expires_at={:.0})",
            self.token.expose().len(),
            self.region.as_str(),
            self.expires_at()
        )
    }
}

fn lifetime_of(ttl_seconds: Option<f64>) -> Result<Duration, Error> {
    match ttl_seconds {
        Some(ttl) => seconds(ttl),
        None => Ok(MAX_LIFETIME),
    }
}

/// The mint both spellings of `mint_bedrock_token`, and `install_access` without a token,
/// drive. The lifetime is checked before the future exists, so a bad one costs no call.
fn mint_op(
    region: Region,
    ttl_seconds: Option<f64>,
) -> Result<impl Future<Output = Result<PyBearerToken, Error>> + Send + 'static, Error> {
    let lifetime = lifetime_of(ttl_seconds)?;
    Ok(async move {
        let minted = bedrock::mint(&region, lifetime).await?;
        Ok(PyBearerToken {
            token: minted.token,
            region,
            expires_at: minted.expires_at,
        })
    })
}

/// Mints a Bedrock bearer token from the default credential chain.
///
/// A SigV4 presign of `POST https://bedrock.amazonaws.com/?Action=CallWithBearerToken`,
/// base64, prefixed `bedrock-api-key-` — the reference generator's recipe, in process.
/// `ttl_seconds` defaults to the ceiling, twelve hours; more is refused by the core.
#[pyfunction]
#[pyo3(signature = (region, *, ttl_seconds=None))]
pub fn mint_bedrock_token(
    py: Python<'_>,
    region: PyRegion,
    ttl_seconds: Option<f64>,
) -> PyCoreResult<PyBearerToken> {
    Ok(runtime::block_on(py, mint_op(region.inner, ttl_seconds)?)?)
}

/// The awaitable twin of `mint_bedrock_token`.
#[pyfunction]
#[pyo3(signature = (region, *, ttl_seconds=None))]
pub async fn mint_bedrock_token_async(
    region: PyRegion,
    ttl_seconds: Option<f64>,
) -> PyCoreResult<PyBearerToken> {
    Ok(runtime::spawn(mint_op(region.inner, ttl_seconds)?).await?)
}

/// Mint with explicit STS credentials and their expiry without changing process env.
/// `credentials_expires_at` is Unix seconds from STS Expiration. Secrets never enter repr.
#[pyfunction]
#[pyo3(signature = (region, *, access_key_id, secret_access_key, session_token=None, credentials_expires_at, ttl_seconds=None))]
pub fn mint_bedrock_token_with_credentials(
    region: PyRegion,
    access_key_id: &str,
    secret_access_key: &str,
    session_token: Option<&str>,
    credentials_expires_at: f64,
    ttl_seconds: Option<f64>,
) -> PyCoreResult<PyBearerToken> {
    let expiry = UNIX_EPOCH
        .checked_add(seconds(credentials_expires_at)?)
        .ok_or_else(|| Error::invalid_arg("credential expiry is out of range"))?;
    let minted = bedrock::mint_with_credentials(
        &region.inner,
        access_key_id,
        secret_access_key,
        session_token,
        expiry,
        lifetime_of(ttl_seconds)?,
    )?;
    Ok(PyBearerToken {
        token: minted.token,
        region: region.inner,
        expires_at: minted.expires_at,
    })
}

/// The agents a running VM was provisioned with, read from its guest marker.
///
/// For a process holding only a session (`Session.direct` from the identifier triple):
/// this is how a credential refresh learns which agents and models to re-provision. A VM
/// with no marker is refused as a precondition, naming `agent-up`.
#[pyfunction]
pub fn installed_agents(py: Python<'_>, session: &PySession) -> PyCoreResult<Vec<PyAgentSpec>> {
    Ok(runtime::block_on(py, installed_agents_op(session))?)
}

/// The awaitable twin of `installed_agents`.
#[pyfunction]
pub async fn installed_agents_async(session: Py<PySession>) -> PyCoreResult<Vec<PyAgentSpec>> {
    Ok(runtime::spawn(installed_agents_op(session.get())).await?)
}

fn installed_agents_op(
    session: &PySession,
) -> impl Future<Output = Result<Vec<PyAgentSpec>, Error>> + Send + 'static {
    session_op!(session.held(), |session| Ok(agents::installed_agents(
        session
    )
    .await?
    .into_iter()
    .map(PyAgentSpec::wrap)
    .collect()))
}

/// Installs Bedrock access for `agents` into a running VM over `session`.
///
/// Three uploads (the environment file, Codex's config when Codex is among the agents,
/// the marker) and one root `chown` of `/workspace` to uid 1000. Re-runnable: a fresh
/// token overwrites the same files, which is how a twelve-hour token is refreshed.
#[pyfunction]
pub fn install_agent_access(
    py: Python<'_>,
    session: &PySession,
    agents: Vec<PyAgentSpec>,
    token: PyBearerToken,
) -> PyCoreResult<()> {
    Ok(runtime::block_on(
        py,
        install_access_op(session, agents, &token),
    )?)
}

/// The awaitable twin of `install_agent_access`.
#[pyfunction]
pub async fn install_agent_access_async(
    session: Py<PySession>,
    agents: Vec<PyAgentSpec>,
    token: PyBearerToken,
) -> PyCoreResult<()> {
    Ok(runtime::spawn(install_access_op(session.get(), agents, &token)).await?)
}

fn install_access_op(
    session: &PySession,
    agents: Vec<PyAgentSpec>,
    token: &PyBearerToken,
) -> impl Future<Output = Result<(), Error>> + Send + 'static {
    let specs: Vec<AgentSpec> = agents.into_iter().map(|spec| spec.inner).collect();
    let access = token.access();
    session_op!(session.held(), |session| agents::install_access(
        session, &specs, &access
    )
    .await)
}

/// Starts one task for `agent` over `session` and returns its handle. Does not wait.
///
/// Runs the agent's headless command as uid 1000 in `/workspace`, sourcing the installed
/// environment file. `timeout_sec` is the daemon-side budget for the agent process;
/// `exec_id` is the idempotency key for a retry that must not spawn twice.
/// `permission_mode` is agent-default or unrestricted. `reap_group_on_exit` stops
/// residual children after the main agent exits. Neither option grants guest root.
#[pyfunction]
#[pyo3(signature = (session, agent, task, *, timeout_sec=None, exec_id=None, permission_mode="agent-default", reap_group_on_exit=false))]
#[allow(
    clippy::too_many_arguments,
    reason = "keyword-only prompt options mirror core"
)]
pub fn prompt_agent(
    py: Python<'_>,
    session: &PySession,
    agent: PyAgentSpec,
    task: &str,
    timeout_sec: Option<f64>,
    exec_id: Option<String>,
    permission_mode: &str,
    reap_group_on_exit: bool,
) -> PyCoreResult<PyExecHandle> {
    let options = prompt_options(exec_id, timeout_sec, permission_mode, reap_group_on_exit)?;
    Ok(runtime::block_on(
        py,
        prompt_agent_op(session, agent.inner, task.to_string(), options),
    )?)
}

/// The awaitable twin of `prompt_agent`.
#[pyfunction]
#[pyo3(signature = (session, agent, task, *, timeout_sec=None, exec_id=None, permission_mode="agent-default", reap_group_on_exit=false))]
#[allow(
    clippy::too_many_arguments,
    reason = "keyword-only prompt options mirror core"
)]
pub async fn prompt_agent_async(
    session: Py<PySession>,
    agent: PyAgentSpec,
    task: String,
    timeout_sec: Option<f64>,
    exec_id: Option<String>,
    permission_mode: &str,
    reap_group_on_exit: bool,
) -> PyCoreResult<PyExecHandle> {
    let options = prompt_options(exec_id, timeout_sec, permission_mode, reap_group_on_exit)?;
    let op = prompt_agent_op(session.get(), agent.inner, task, options);
    Ok(runtime::spawn(op).await?)
}

fn prompt_agent_op(
    session: &PySession,
    agent: AgentSpec,
    task: String,
    options: PromptOptions,
) -> impl Future<Output = Result<PyExecHandle, Error>> + Send + 'static {
    session_op!(session.held(), |session| agents::prompt(
        session, &agent, &task, &options
    )
    .await
    .map(PyExecHandle::wrap))
}

/// The prompt options every prompting method's keywords build.
fn prompt_options(
    exec_id: Option<String>,
    timeout_sec: Option<f64>,
    permission_mode: &str,
    reap_group_on_exit: bool,
) -> Result<PromptOptions, Error> {
    Ok(PromptOptions {
        exec_id,
        timeout: timeout_sec.map(seconds).transpose()?,
        permission_mode: permission_mode.parse()?,
        reap_group_on_exit,
    })
}

/// The layer's fixed values, for a caller that wants to reason about the guest.
#[pyfunction]
pub fn agent_constants(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("uid", AGENT_UID)?;
    dict.set_item("gid", AGENT_GID)?;
    dict.set_item("workdir", WORKDIR)?;
    dict.set_item("env_file", profile::ENV_FILE)?;
    dict.set_item("codex_config_file", profile::CODEX_CONFIG_FILE)?;
    dict.set_item("marker_file", profile::MARKER_FILE)?;
    dict.set_item("default_memory_mib", DEFAULT_SIZE.baseline_mib())?;
    dict.set_item(
        "default_prompt_timeout_sec",
        DEFAULT_PROMPT_TIMEOUT.as_secs_f64(),
    )?;
    dict.set_item("max_token_lifetime_sec", MAX_LIFETIME.as_secs_f64())?;
    // The agents a VM carries when its caller names none, the core's list.
    dict.set_item(
        "default_agents",
        agents::default_specs()
            .iter()
            .map(|spec| spec.agent.as_str())
            .collect::<Vec<_>>(),
    )?;
    let profiles = PyDict::new(py);
    for agent in Agent::ALL {
        let row = agent.profile();
        let entry = PyDict::new(py);
        entry.set_item("npm_package", row.npm_package)?;
        entry.set_item("default_model", row.default_model)?;
        entry.set_item("verified", row.verified)?;
        profiles.set_item(agent.as_str(), entry)?;
    }
    dict.set_item("profiles", profiles)?;
    Ok(dict)
}

/// One VM with coding agents in it: the sandbox plus the specs it is built for.
///
/// The sequence is the CLI's `agent-up` and `agent-prompt`, one method per step:
/// `find_image` or `build_artifact` + your upload + `build_image`; `launch`;
/// `install_access`; `prompt` or `prompt_sync`; `terminate`. `sandbox` and `session` reach
/// the same VM for suspend, resume, `cp`-shaped transfers, and any other exec. Each step that
/// calls AWS or the VM has an awaitable `_async` twin that drives the same future.
#[pyclass(frozen, name = "AgentVm", module = "microvms")]
pub struct PyAgentVm {
    sandbox: SharedSandbox,
    specs: Vec<AgentSpec>,
    region: Region,
}

/// The VM's session, or the core's refusal, the one its own `AgentVm` makes before a launch.
fn launched(sandbox: &Sandbox) -> Result<&microvms_core::session::Session, Error> {
    agents::launched_session(sandbox)
}

/// `agents`, or the core's default list when the caller names none.
fn specs_or_default(agents: Option<Vec<PyAgentSpec>>) -> Vec<AgentSpec> {
    agents
        .map(|agents| agents.into_iter().map(|spec| spec.inner).collect())
        .unwrap_or_else(agents::default_specs)
}

impl PyAgentVm {
    fn own(sandbox: Sandbox, specs: Vec<AgentSpec>, region: Region) -> Self {
        Self {
            sandbox: Arc::new(tokio::sync::Mutex::new(sandbox)),
            specs,
            region,
        }
    }

    fn read<T>(&self, py: Python<'_>, body: impl FnOnce(&Sandbox) -> T) -> T {
        body(&runtime::lock_now(py, &self.sandbox))
    }

    fn image_request(
        &self,
        py: Python<'_>,
        binary: Vec<u8>,
        build_role_arn: &str,
        size: Option<PySizeClass>,
    ) -> Result<microvms_core::control::CreateImageRequest, Error> {
        let size = size.map(|size| size.inner).unwrap_or(DEFAULT_SIZE);
        self.read(py, |sandbox| {
            agents::image_request_for(sandbox, &self.specs, binary, build_role_arn, size)
        })
    }

    // The futures each step's two spellings drive. Each reads what it needs off the sandbox
    // under the same lock it then calls through, so the request and the call see one state.

    fn open_op(
        region: Region,
        specs: Vec<AgentSpec>,
    ) -> Result<impl Future<Output = Result<Self, Error>> + Send + 'static, Error> {
        agents::require_specs(&specs)?;
        Ok(async move {
            let sandbox = Sandbox::new(region.clone()).await?;
            Ok(Self::own(sandbox, specs, region))
        })
    }

    async fn from_name_op(
        store: microvms_core::names::FileNameStore,
        region: Region,
        name: String,
        specs: Vec<AgentSpec>,
        port: Option<u16>,
    ) -> Result<Self, Error> {
        let vm =
            agents::AgentVm::from_name(&store, specs, &name, Some(region.clone()), port).await?;
        let (sandbox, specs) = vm.into_parts();
        Ok(Self::own(sandbox, specs, region))
    }

    async fn adopt_op(
        region: Region,
        specs: Vec<AgentSpec>,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        port: Option<u16>,
    ) -> Result<Self, Error> {
        let vm = agents::AgentVm::adopt_in(
            region.clone(),
            specs,
            microvm_id,
            endpoint,
            agent_token,
            port,
        )
        .await?;
        let (sandbox, specs) = vm.into_parts();
        Ok(Self::own(sandbox, specs, region))
    }

    fn find_image_op(
        &self,
        binary: Vec<u8>,
        build_role_arn: String,
        size: Option<PySizeClass>,
    ) -> impl Future<Output = Result<Option<String>, Error>> + Send + 'static {
        let inner = Arc::clone(&self.sandbox);
        let specs = self.specs.clone();
        let size = size.map(|size| size.inner).unwrap_or(DEFAULT_SIZE);
        async move {
            let sandbox = inner.lock().await;
            let name =
                agents::image_request_for(&sandbox, &specs, binary, &build_role_arn, size)?.name;
            let found = sandbox.find_image_by_name(&name).await?;
            Ok(found.map(|image| image.image_arn))
        }
    }

    fn ensure_image_op(
        &self,
        binary: Vec<u8>,
        build_role_arn: String,
        s3_bucket: String,
        size: Option<PySizeClass>,
        s3_key_prefix: Option<String>,
    ) -> impl Future<Output = Result<PyEnsuredImage, Error>> + Send + 'static {
        let inner = Arc::clone(&self.sandbox);
        let specs = self.specs.clone();
        let size = size.map(|size| size.inner).unwrap_or(DEFAULT_SIZE);
        async move {
            let mut sandbox = inner.lock().await;
            let request = agents::ensure_request_for(
                &sandbox,
                &specs,
                binary,
                &build_role_arn,
                size,
                &s3_bucket,
                s3_key_prefix,
            )?;
            sandbox
                .ensure_image(request)
                .await
                .map(PyEnsuredImage::from)
        }
    }

    fn build_image_op(
        &self,
        binary: Vec<u8>,
        code_artifact_uri: String,
        build_role_arn: String,
        size: Option<PySizeClass>,
    ) -> impl Future<Output = Result<PyImage, Error>> + Send + 'static {
        let inner = Arc::clone(&self.sandbox);
        let specs = self.specs.clone();
        let size = size.map(|size| size.inner).unwrap_or(DEFAULT_SIZE);
        async move {
            let mut sandbox = inner.lock().await;
            let mut request =
                agents::image_request_for(&sandbox, &specs, binary, &build_role_arn, size)?;
            request.code_artifact_uri = code_artifact_uri;
            sandbox.build_image(request).await.map(PyImage::wrap)
        }
    }

    fn launch_op(
        &self,
        request: microvms_core::sandbox::RunRequest,
    ) -> impl Future<Output = Result<PySession, Error>> + Send + 'static {
        let inner = Arc::clone(&self.sandbox);
        async move {
            // The core's launch waits for RUNNING and then for the daemon to answer (#254).
            inner.lock().await.run(request).await?;
            Ok(PySession::in_sandbox(inner))
        }
    }

    /// The install both spellings of `install_access` drive, minting first when the caller
    /// gave no token.
    fn install_access_op(
        &self,
        token: Option<PyBearerToken>,
        ttl_seconds: Option<f64>,
    ) -> Result<impl Future<Output = Result<PyBearerToken, Error>> + Send + 'static, Error> {
        let mint = match token {
            Some(_) => None,
            None => Some(mint_op(self.region.clone(), ttl_seconds)?),
        };
        let inner = Arc::clone(&self.sandbox);
        let specs = self.specs.clone();
        Ok(async move {
            let token = match (token, mint) {
                (Some(token), _) => token,
                (None, Some(mint)) => mint.await?,
                (None, None) => unreachable!("a mint is planned whenever no token was given"),
            };
            let access = token.access();
            let sandbox = inner.lock().await;
            let session = launched(&sandbox)?;
            agents::install_access(session, &specs, &access).await?;
            Ok(token)
        })
    }

    fn prompt_op(
        &self,
        agent: &str,
        task: String,
        options: PromptOptions,
    ) -> Result<impl Future<Output = Result<PyExecHandle, Error>> + Send + 'static, Error> {
        let agent: Agent = agent.parse()?;
        let spec = agents::spec_for(&self.specs, agent)?.clone();
        let inner = Arc::clone(&self.sandbox);
        Ok(async move {
            let sandbox = inner.lock().await;
            let session = launched(&sandbox)?;
            agents::prompt(session, &spec, &task, &options)
                .await
                .map(PyExecHandle::wrap)
        })
    }

    fn prompt_sync_op(
        &self,
        agent: &str,
        task: String,
        options: PromptOptions,
    ) -> Result<impl Future<Output = Result<PyExecResult, Error>> + Send + 'static, Error> {
        let agent: Agent = agent.parse()?;
        let spec = agents::spec_for(&self.specs, agent)?.clone();
        let inner = Arc::clone(&self.sandbox);
        Ok(async move {
            let sandbox = inner.lock().await;
            let session = launched(&sandbox)?;
            agents::prompt_sync(session, &spec, &task, &options)
                .await
                .map(PyExecResult::wrap)
        })
    }
}

/// `launch`'s keywords, both spellings', as one value for [`launch_request`].
struct LaunchArgs {
    image_identifier: String,
    execution_role_arn: Option<String>,
    agent_token: Option<String>,
    client_token: Option<String>,
    max_idle_sec: Option<u32>,
    suspended_sec: Option<u32>,
    auto_resume: bool,
    max_duration_sec: Option<u32>,
    image_version: Option<String>,
    egress_network_connectors: Option<Vec<String>>,
    log_group: Option<String>,
    log_stream: Option<String>,
    disable_logging: bool,
    launch_env: Option<std::collections::HashMap<String, String>>,
    shell: bool,
    ready_timeout: Option<f64>,
}

/// The core launch request for `launch`'s keywords: the agent recipe's request with each knob
/// the layer leaves open set, every unset one left at the core's figure.
fn launch_request(
    specs: &[AgentSpec],
    args: LaunchArgs,
) -> Result<microvms_core::sandbox::RunRequest, Error> {
    let mut request =
        agents::launch_request_for(specs, &args.image_identifier, args.execution_role_arn)
            .with_vpc_egress(args.egress_network_connectors.unwrap_or_default());
    if let Some(env) = args.launch_env {
        request.launch_env = env;
    }
    request.shell = args.shell;
    if let Some(timeout) = args.ready_timeout {
        request.ready_timeout = seconds(timeout)?;
    }
    request.image_version = args.image_version;
    request.logging =
        crate::sandbox::logging_for(args.log_group, args.log_stream, args.disable_logging)?;
    request.agent_token = args.agent_token;
    request.client_token = args.client_token;
    if let Some(idle) = args.max_idle_sec {
        request.max_idle_sec = idle;
    }
    if let Some(suspended) = args.suspended_sec {
        request.suspended_sec = suspended;
    }
    request.auto_resume = args.auto_resume;
    if let Some(ceiling) = args.max_duration_sec {
        request.max_duration_sec = ceiling;
    }
    Ok(request)
}

#[pymethods]
impl PyAgentVm {
    /// Resolves credentials for `region` and returns a VM with nothing built or launched.
    ///
    /// `agents` defaults to Claude Code alone. An empty list or a repeated agent is refused
    /// by the core before any AWS call.
    #[new]
    #[pyo3(signature = (region, agents=None))]
    fn new(
        py: Python<'_>,
        region: PyRegion,
        agents: Option<Vec<PyAgentSpec>>,
    ) -> PyCoreResult<PyAgentVm> {
        let open = Self::open_op(region.inner, specs_or_default(agents))?;
        Ok(runtime::block_on(py, open)?)
    }

    /// The awaitable twin of `AgentVm(region, agents)`.
    #[staticmethod]
    #[pyo3(signature = (region, agents=None))]
    async fn create_async(
        region: PyRegion,
        agents: Option<Vec<PyAgentSpec>>,
    ) -> PyCoreResult<PyAgentVm> {
        let open = Self::open_op(region.inner, specs_or_default(agents))?;
        Ok(runtime::spawn(open).await?)
    }

    /// Adopts the agent VM registered as `name` in `registry`; see `Sandbox.from_name`.
    #[staticmethod]
    #[pyo3(signature = (region, name, registry, agents=None, *, port=None))]
    fn from_name(
        py: Python<'_>,
        region: PyRegion,
        name: String,
        registry: &crate::names::PyNameRegistry,
        agents: Option<Vec<PyAgentSpec>>,
        port: Option<u16>,
    ) -> PyCoreResult<PyAgentVm> {
        let op = Self::from_name_op(
            registry.store.clone(),
            region.inner,
            name,
            specs_or_default(agents),
            port,
        );
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `AgentVm.from_name`.
    #[staticmethod]
    #[pyo3(signature = (region, name, registry, agents=None, *, port=None))]
    async fn from_name_async(
        region: PyRegion,
        name: String,
        registry: Py<crate::names::PyNameRegistry>,
        agents: Option<Vec<PyAgentSpec>>,
        port: Option<u16>,
    ) -> PyCoreResult<PyAgentVm> {
        let op = Self::from_name_op(
            registry.get().store.clone(),
            region.inner,
            name,
            specs_or_default(agents),
            port,
        );
        Ok(runtime::spawn(op).await?)
    }

    /// An agent VM for a VM another process launched; see `Sandbox.adopt`.
    ///
    /// `agents` states what the VM carries and defaults to Claude Code alone; read
    /// `installed_agents(session)` first when the adopting process does not know.
    #[staticmethod]
    #[pyo3(signature = (region, microvm_id, endpoint, agent_token, agents=None, *, port=None))]
    fn adopt(
        py: Python<'_>,
        region: PyRegion,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        agents: Option<Vec<PyAgentSpec>>,
        port: Option<u16>,
    ) -> PyCoreResult<PyAgentVm> {
        let op = Self::adopt_op(
            region.inner,
            specs_or_default(agents),
            microvm_id,
            endpoint,
            agent_token,
            port,
        );
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `AgentVm.adopt`.
    #[staticmethod]
    #[pyo3(signature = (region, microvm_id, endpoint, agent_token, agents=None, *, port=None))]
    async fn adopt_async(
        region: PyRegion,
        microvm_id: String,
        endpoint: String,
        agent_token: String,
        agents: Option<Vec<PyAgentSpec>>,
        port: Option<u16>,
    ) -> PyCoreResult<PyAgentVm> {
        let op = Self::adopt_op(
            region.inner,
            specs_or_default(agents),
            microvm_id,
            endpoint,
            agent_token,
            port,
        );
        Ok(runtime::spawn(op).await?)
    }

    /// The specs this VM carries, in profile order.
    #[getter]
    fn agents(&self) -> Vec<PyAgentSpec> {
        let mut specs = self.specs.clone();
        specs.sort_by_key(|spec| spec.agent);
        specs.into_iter().map(PyAgentSpec::wrap).collect()
    }

    #[getter]
    fn region(&self) -> PyRegion {
        PyRegion {
            inner: self.region.clone(),
        }
    }

    /// The sandbox this VM drives, for suspend, resume, and the lifecycle getters. The
    /// same lock: a call here and a call there cannot interleave.
    #[getter]
    fn sandbox(&self) -> PySandbox {
        PySandbox::from_arc(Arc::clone(&self.sandbox))
    }

    /// The session, once `launch` has run.
    #[getter]
    fn session(&self, py: Python<'_>) -> Option<PySession> {
        let has_session = self.read(py, |sandbox| sandbox.session().is_some());
        has_session.then(|| PySession::in_sandbox(Arc::clone(&self.sandbox)))
    }

    /// The Dockerfile `build_image` will send: the client's agentd stanza plus the agent
    /// layers. Read it to see what the image will contain; nothing in it is a secret.
    fn dockerfile(&self, py: Python<'_>) -> PyCoreResult<String> {
        let port = self.read(py, Sandbox::port);
        Ok(agents::dockerfile(&self.specs, &BaseImage::al2023(), port)?)
    }

    /// The image name for these specs and this daemon binary: `agent-vm-<agents>-<hash12>`.
    ///
    /// Content-addressed, so an unchanged binary, spec set and size name the image a previous
    /// run built; `find_image` looks it up, and `ensure_image` builds or reuses it.
    #[pyo3(signature = (*, binary, build_role_arn, size=None))]
    fn image_name(
        &self,
        py: Python<'_>,
        binary: Vec<u8>,
        build_role_arn: &str,
        size: Option<PySizeClass>,
    ) -> PyCoreResult<String> {
        Ok(self.image_request(py, binary, build_role_arn, size)?.name)
    }

    /// The ARN of an existing image named per `image_name`, or `None` when there is none.
    #[pyo3(signature = (*, binary, build_role_arn, size=None))]
    fn find_image(
        &self,
        py: Python<'_>,
        binary: Vec<u8>,
        build_role_arn: &str,
        size: Option<PySizeClass>,
    ) -> PyCoreResult<Option<String>> {
        let op = self.find_image_op(binary, build_role_arn.to_string(), size);
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `find_image`.
    #[pyo3(signature = (*, binary, build_role_arn, size=None))]
    async fn find_image_async(
        &self,
        binary: Vec<u8>,
        build_role_arn: String,
        size: Option<PySizeClass>,
    ) -> PyCoreResult<Option<String>> {
        Ok(runtime::spawn(self.find_image_op(binary, build_role_arn, size)).await?)
    }

    /// Builds or reuses this VM's image, named per `image_name`: returned at once when ready,
    /// waited on while building, deleted and rebuilt when failed, and uploaded to
    /// `s3://<s3_bucket>/<s3_key_prefix>/<name>/artifact.zip` only when a build is needed.
    #[pyo3(signature = (*, binary, build_role_arn, s3_bucket, size=None, s3_key_prefix=None))]
    fn ensure_image(
        &self,
        py: Python<'_>,
        binary: Vec<u8>,
        build_role_arn: &str,
        s3_bucket: &str,
        size: Option<PySizeClass>,
        s3_key_prefix: Option<String>,
    ) -> PyCoreResult<PyEnsuredImage> {
        let op = self.ensure_image_op(
            binary,
            build_role_arn.to_string(),
            s3_bucket.to_string(),
            size,
            s3_key_prefix,
        );
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `ensure_image`. A lifecycle transition: cancelling the awaitable
    /// leaves the build or reuse running to completion.
    #[pyo3(signature = (*, binary, build_role_arn, s3_bucket, size=None, s3_key_prefix=None))]
    async fn ensure_image_async(
        &self,
        binary: Vec<u8>,
        build_role_arn: String,
        s3_bucket: String,
        size: Option<PySizeClass>,
        s3_key_prefix: Option<String>,
    ) -> PyCoreResult<PyEnsuredImage> {
        let op = self.ensure_image_op(binary, build_role_arn, s3_bucket, size, s3_key_prefix);
        Ok(runtime::spawn_shielded(op).await?)
    }

    /// The artifact bytes to upload to `s3://<bucket>/<image_name>.zip` before `build_image`.
    #[pyo3(signature = (*, binary, build_role_arn, size=None))]
    fn build_artifact<'py>(
        &self,
        py: Python<'py>,
        binary: Vec<u8>,
        build_role_arn: &str,
        size: Option<PySizeClass>,
    ) -> PyCoreResult<Bound<'py, PyBytes>> {
        let request = self.image_request(py, binary, build_role_arn, size)?;
        let bytes = self.read(py, |sandbox| sandbox.build_artifact_for(&request))?;
        Ok(PyBytes::new(py, &bytes))
    }

    /// Builds the image and waits for it to become usable. `code_artifact_uri` is where
    /// you uploaded `build_artifact`'s bytes. Several minutes, server-side.
    #[pyo3(signature = (*, binary, code_artifact_uri, build_role_arn, size=None))]
    fn build_image(
        &self,
        py: Python<'_>,
        binary: Vec<u8>,
        code_artifact_uri: &str,
        build_role_arn: &str,
        size: Option<PySizeClass>,
    ) -> PyCoreResult<PyImage> {
        let op = self.build_image_op(
            binary,
            code_artifact_uri.to_string(),
            build_role_arn.to_string(),
            size,
        );
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `build_image`. A lifecycle transition: cancelling the awaitable
    /// leaves the build running to completion.
    #[pyo3(signature = (*, binary, code_artifact_uri, build_role_arn, size=None))]
    async fn build_image_async(
        &self,
        binary: Vec<u8>,
        code_artifact_uri: String,
        build_role_arn: String,
        size: Option<PySizeClass>,
    ) -> PyCoreResult<PyImage> {
        let op = self.build_image_op(binary, code_artifact_uri, build_role_arn, size);
        Ok(runtime::spawn_shielded(op).await?)
    }

    /// Launches with egress and waits for the daemon to answer.
    ///
    /// Egress is not optional: neither agent reaches Bedrock without it. The managed
    /// internet connector is the default; `egress_network_connectors` replaces it with
    /// customer-managed VPC connectors, whose VPC must route to Bedrock. The idle knobs
    /// default to the core's figures (ten-minute idle and suspended windows, a one-hour
    /// ceiling); a multi-hour session raises `max_duration_sec` and polls `health` from
    /// outside to stay awake.
    ///
    /// `image_identifier` is an image ARN or a bare image name; the core resolves a name with
    /// one `ListMicrovmImages` read and raises `PreconditionError` for a name no image carries.
    ///
    /// `launch_env`, `shell` and `ready_timeout` mean what they mean on `Sandbox.run`:
    /// `ready_timeout` bounds the wait for RUNNING, and the wait for the daemon after it is
    /// `session_constants()["defaultReadyTimeoutSeconds"]`.
    #[pyo3(signature = (
        *,
        image_identifier,
        execution_role_arn=None,
        agent_token=None,
        client_token=None,
        max_idle_sec=None,
        suspended_sec=None,
        auto_resume=false,
        max_duration_sec=None,
        image_version=None,
        egress_network_connectors=None,
        log_group=None,
        log_stream=None,
        disable_logging=false,
        launch_env=None,
        shell=false,
        ready_timeout=None,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "one keyword-only parameter per launch knob the layer leaves open"
    )]
    fn launch(
        &self,
        py: Python<'_>,
        image_identifier: &str,
        execution_role_arn: Option<String>,
        agent_token: Option<String>,
        client_token: Option<String>,
        max_idle_sec: Option<u32>,
        suspended_sec: Option<u32>,
        auto_resume: bool,
        max_duration_sec: Option<u32>,
        // Pins the launch to one image version rather than the latest active one.
        image_version: Option<String>,
        // Customer-managed VPC egress connector ARNs. When given they replace the managed
        // internet connector, and the VPC must route to Bedrock. Not a doc comment: a doc
        // comment on a function parameter is a compile error.
        egress_network_connectors: Option<Vec<String>>,
        log_group: Option<String>,
        log_stream: Option<String>,
        disable_logging: bool,
        launch_env: Option<std::collections::HashMap<String, String>>,
        shell: bool,
        ready_timeout: Option<f64>,
    ) -> PyCoreResult<PySession> {
        let request = launch_request(
            &self.specs,
            LaunchArgs {
                image_identifier: image_identifier.to_string(),
                execution_role_arn,
                agent_token,
                client_token,
                max_idle_sec,
                suspended_sec,
                auto_resume,
                max_duration_sec,
                image_version,
                egress_network_connectors,
                log_group,
                log_stream,
                disable_logging,
                launch_env,
                shell,
                ready_timeout,
            },
        )?;
        Ok(runtime::block_on(py, self.launch_op(request))?)
    }

    /// The awaitable twin of `launch`, with its keywords. A lifecycle transition: cancelling
    /// the awaitable leaves the launch running to completion, so the VM it starts is still
    /// this object's to terminate.
    #[pyo3(signature = (
        *,
        image_identifier,
        execution_role_arn=None,
        agent_token=None,
        client_token=None,
        max_idle_sec=None,
        suspended_sec=None,
        auto_resume=false,
        max_duration_sec=None,
        image_version=None,
        egress_network_connectors=None,
        log_group=None,
        log_stream=None,
        disable_logging=false,
        launch_env=None,
        shell=false,
        ready_timeout=None,
    ))]
    #[allow(
        clippy::too_many_arguments,
        reason = "`launch`'s keywords, one per launch knob the layer leaves open"
    )]
    async fn launch_async(
        &self,
        image_identifier: String,
        execution_role_arn: Option<String>,
        agent_token: Option<String>,
        client_token: Option<String>,
        max_idle_sec: Option<u32>,
        suspended_sec: Option<u32>,
        auto_resume: bool,
        max_duration_sec: Option<u32>,
        image_version: Option<String>,
        egress_network_connectors: Option<Vec<String>>,
        log_group: Option<String>,
        log_stream: Option<String>,
        disable_logging: bool,
        launch_env: Option<std::collections::HashMap<String, String>>,
        shell: bool,
        ready_timeout: Option<f64>,
    ) -> PyCoreResult<PySession> {
        let request = launch_request(
            &self.specs,
            LaunchArgs {
                image_identifier,
                execution_role_arn,
                agent_token,
                client_token,
                max_idle_sec,
                suspended_sec,
                auto_resume,
                max_duration_sec,
                image_version,
                egress_network_connectors,
                log_group,
                log_stream,
                disable_logging,
                launch_env,
                shell,
                ready_timeout,
            },
        )?;
        Ok(runtime::spawn_shielded(self.launch_op(request)).await?)
    }

    /// Hands the VM off to another process; see `Sandbox.detach`. The adopter passes the
    /// same agents to `AgentVm.adopt`.
    fn detach(&self, py: Python<'_>) -> PyCoreResult<crate::sandbox::PyDetached> {
        let inner = runtime::lock_now(py, &self.sandbox).detach()?;
        Ok(crate::sandbox::PyDetached { inner })
    }

    /// Mints a token (or takes yours) and installs Bedrock access for this VM's agents.
    ///
    /// Returns the token used, so `expires_at` says when to call this again. Re-runnable
    /// on a running VM: that call is the credential refresh.
    #[pyo3(signature = (*, token=None, ttl_seconds=None))]
    fn install_access(
        &self,
        py: Python<'_>,
        token: Option<PyBearerToken>,
        ttl_seconds: Option<f64>,
    ) -> PyCoreResult<PyBearerToken> {
        let op = self.install_access_op(token, ttl_seconds)?;
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `install_access`.
    #[pyo3(signature = (*, token=None, ttl_seconds=None))]
    async fn install_access_async(
        &self,
        token: Option<PyBearerToken>,
        ttl_seconds: Option<f64>,
    ) -> PyCoreResult<PyBearerToken> {
        let op = self.install_access_op(token, ttl_seconds)?;
        Ok(runtime::spawn(op).await?)
    }

    /// Starts one task for `agent` and returns its handle. Does not wait.

    #[pyo3(signature = (agent, task, *, timeout_sec=None, exec_id=None, permission_mode="agent-default", reap_group_on_exit=false))]
    #[allow(
        clippy::too_many_arguments,
        reason = "keyword-only prompt options mirror core"
    )]
    fn prompt(
        &self,
        py: Python<'_>,
        agent: &str,
        task: &str,
        timeout_sec: Option<f64>,
        exec_id: Option<String>,
        permission_mode: &str,
        reap_group_on_exit: bool,
    ) -> PyCoreResult<PyExecHandle> {
        let options = prompt_options(exec_id, timeout_sec, permission_mode, reap_group_on_exit)?;
        let op = self.prompt_op(agent, task.to_string(), options)?;
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `prompt`. Cancelling it before the daemon answers may leave the
    /// task started; pass an `exec_id` to reach it with `session.exec(exec_id)`.
    #[pyo3(signature = (agent, task, *, timeout_sec=None, exec_id=None, permission_mode="agent-default", reap_group_on_exit=false))]
    #[allow(
        clippy::too_many_arguments,
        reason = "keyword-only prompt options mirror core"
    )]
    async fn prompt_async(
        &self,
        agent: String,
        task: String,
        timeout_sec: Option<f64>,
        exec_id: Option<String>,
        permission_mode: &str,
        reap_group_on_exit: bool,
    ) -> PyCoreResult<PyExecHandle> {
        let options = prompt_options(exec_id, timeout_sec, permission_mode, reap_group_on_exit)?;
        let op = self.prompt_op(&agent, task, options)?;
        Ok(runtime::spawn(op).await?)
    }

    /// Start, wait, ack: one task's whole result. `timeout` defaults to 900 seconds,
    /// because agent tasks run minutes, and is also the daemon-side budget.

    #[pyo3(signature = (agent, task, *, timeout=DEFAULT_PROMPT_TIMEOUT.as_secs_f64(), exec_id=None, permission_mode="agent-default", reap_group_on_exit=false))]
    #[allow(
        clippy::too_many_arguments,
        reason = "keyword-only prompt options mirror core"
    )]
    fn prompt_sync(
        &self,
        py: Python<'_>,
        agent: &str,
        task: &str,
        timeout: f64,
        exec_id: Option<String>,
        permission_mode: &str,
        reap_group_on_exit: bool,
    ) -> PyCoreResult<PyExecResult> {
        let options = prompt_options(exec_id, Some(timeout), permission_mode, reap_group_on_exit)?;
        let op = self.prompt_sync_op(agent, task.to_string(), options)?;
        Ok(runtime::block_on(py, op)?)
    }

    /// The awaitable twin of `prompt_sync`. Cancelling it stops the wait; the task keeps
    /// running in the VM until its own timeout.
    #[pyo3(signature = (agent, task, *, timeout=DEFAULT_PROMPT_TIMEOUT.as_secs_f64(), exec_id=None, permission_mode="agent-default", reap_group_on_exit=false))]
    #[allow(
        clippy::too_many_arguments,
        reason = "keyword-only prompt options mirror core"
    )]
    async fn prompt_sync_async(
        &self,
        agent: String,
        task: String,
        timeout: f64,
        exec_id: Option<String>,
        permission_mode: &str,
        reap_group_on_exit: bool,
    ) -> PyCoreResult<PyExecResult> {
        let options = prompt_options(exec_id, Some(timeout), permission_mode, reap_group_on_exit)?;
        let op = self.prompt_sync_op(&agent, task, options)?;
        Ok(runtime::spawn(op).await?)
    }

    /// Tears down, best-effort, never raising; see `Sandbox.terminate`.

    #[pyo3(signature = (
        *,
        delete_image=false,
        delete_log_group=false,
        delete_attempts=None,
        delete_backoff=None,
        wait_for_terminated=crate::sandbox::WaitForTerminated::Flag(false),
    ))]
    fn terminate(
        &self,
        py: Python<'_>,
        delete_image: bool,
        delete_log_group: bool,
        delete_attempts: Option<u32>,
        delete_backoff: Option<f64>,
        wait_for_terminated: crate::sandbox::WaitForTerminated,
    ) -> PyCoreResult<PyTeardownReport> {
        self.sandbox().terminate(
            py,
            delete_image,
            delete_log_group,
            delete_attempts,
            delete_backoff,
            wait_for_terminated,
        )
    }

    /// The awaitable twin of `terminate`; see `Sandbox.terminate_async`.
    #[pyo3(signature = (
        *,
        delete_image=false,
        delete_log_group=false,
        delete_attempts=None,
        delete_backoff=None,
        wait_for_terminated=crate::sandbox::WaitForTerminated::Flag(false),
    ))]
    async fn terminate_async(
        &self,
        delete_image: bool,
        delete_log_group: bool,
        delete_attempts: Option<u32>,
        delete_backoff: Option<f64>,
        wait_for_terminated: crate::sandbox::WaitForTerminated,
    ) -> PyCoreResult<PyTeardownReport> {
        let opts = crate::sandbox::teardown_opts(
            delete_image,
            delete_log_group,
            delete_attempts,
            delete_backoff,
            wait_for_terminated,
        )?;
        Ok(runtime::spawn_shielded(self.sandbox().terminate_op(opts)).await)
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Tears down on the way out, whatever happened inside the block. Returns `False`, so
    /// an exception raised inside the block propagates.
    #[pyo3(signature = (exc_type=None, exc_value=None, traceback=None))]
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<Py<PyAny>>,
        exc_value: Option<Py<PyAny>>,
        traceback: Option<Py<PyAny>>,
    ) -> bool {
        let _ = (exc_type, exc_value, traceback);
        let _ = self.terminate(
            py,
            false,
            false,
            None,
            None,
            crate::sandbox::WaitForTerminated::Flag(false),
        );
        false
    }

    /// `async with AgentVm(...)`: returns the VM itself.
    async fn __aenter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// `__exit__`'s teardown, awaited; it runs to completion even when the task awaiting it
    /// is cancelled.
    #[pyo3(signature = (exc_type=None, exc_value=None, traceback=None))]
    async fn __aexit__(
        &self,
        exc_type: Option<Py<PyAny>>,
        exc_value: Option<Py<PyAny>>,
        traceback: Option<Py<PyAny>>,
    ) -> bool {
        let _ = (exc_type, exc_value, traceback);
        let opts = microvms_core::sandbox::TeardownOpts::default();
        let _ = runtime::spawn_shielded(self.sandbox().terminate_op(opts)).await;
        false
    }

    fn __repr__(&self, py: Python<'_>) -> String {
        self.read(py, |sandbox| {
            format!(
                "AgentVm(agents={:?}, lifecycle={:?}, microvm_id={:?})",
                self.specs
                    .iter()
                    .map(|spec| spec.agent.as_str())
                    .collect::<Vec<_>>(),
                sandbox.lifecycle().as_str(),
                sandbox.microvm().map(|vm| vm.id.as_str()),
            )
        })
    }
}
