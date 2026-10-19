# microvms-agentd · Public API

The Rust client is `microvms-core`: its facade re-exports the shared wire contract,
application APIs, domain values, and production adapters.
`crates/microvms-core/src/lib.rs:85-95`

Import `microvms_core::prelude::*` for the production constructors below; their
extension traits connect the underlying port-based types to production adapters.
`crates/microvms-core/src/prelude.rs:2`

The entries use paths relative to `microvms_core` and retain the declarations from
their defining sources. See the [CLI reference](cli.md) for commands and the
[daemon API reference](rpc-tools.md) for HTTP routes and wire payloads.

## Selected Rust API

### agents::AgentVm::launch

```rs
    pub async fn launch(&mut self, request: RunRequest) -> Result<&Session, Error> {
```

Launches the requested VM and returns its session, with readiness waiting controlled by the request.

`crates/microvms-app/src/agents/mod.rs:901`

### agents::AgentVm::new

```rs
    pub fn new(sandbox: Sandbox, specs: Vec<AgentSpec>) -> Result<Self, Error> {
```

Wraps a sandbox with the agent specifications it carries, rejecting an empty or repeated set.

`crates/microvms-app/src/agents/mod.rs:768`

### agents::AgentVm::prompt_sync

```rs
    pub async fn prompt_sync(
        &self,
        agent: Agent,
        task: &str,
        options: &PromptOptions,
    ) -> Result<ExecResult, Error> {
```

Runs a task with a configured agent, waits for completion, and acknowledges the result.

`crates/microvms-app/src/agents/mod.rs:926`

### control::BaseImage::from_dockerfile

```rs
    pub fn from_dockerfile(dockerfile: &str) -> Result<Self, Error> {
```

Pairs the managed base with the first FROM reference in a task Dockerfile.

`crates/microvms-app/src/control/artifact.rs:403`

### control::EnsureImageRequest::new

```rs
    pub fn new(
        name_prefix: impl Into<String>,
        binary: Vec<u8>,
        dockerfile: impl Into<String>,
        s3_bucket: impl Into<String>,
        build_role_arn: impl Into<String>,
    ) -> Self {
```

Creates an image request with shared defaults for the build context, sizing, and image configuration.

`crates/microvms-app/src/control/ensure.rs:427`

### control::wrap_dockerfile

```rs
pub fn wrap_dockerfile(task: &str, opts: &WrapOptions) -> Result<String, Error> {
```

Appends the agentd startup instructions to a task Dockerfile using the supplied wrapping options.

`crates/microvms-app/src/control/artifact.rs:611`

### preflight::preflight

```rs
    pub async fn preflight(region: Option<Region>) -> PreflightReport {
```

Checks launch prerequisites for the supplied region or the region resolved from the environment.

`crates/microvms-core/src/lib.rs:165`

### prelude::ControlPlaneExt::new

```rs
    fn new(region: Region) -> impl Future<Output = Result<Self, Error>> + Send;
```

Resolves credentials and constructs a control-plane client for the selected region.

`crates/microvms-core/src/prelude.rs:123`

### prelude::SandboxExt::new

```rs
    fn new(region: Region) -> impl Future<Output = Result<Self, Error>> + Send;
```

Resolves credentials and constructs a sandbox with no VM launched.

`crates/microvms-core/src/prelude.rs:176`

### prelude::SessionExt::attach

```rs
    fn attach(
        region: Region,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        port: Option<u16>,
        request_timeout: Option<Duration>,
    ) -> impl Future<Output = Result<Self, Error>> + Send;
```

Constructs a session for an existing VM using its retained agent token without probing or bootstrapping the guest.

`crates/microvms-core/src/prelude.rs:314`

### prelude::SessionExt::direct

```rs
    fn direct(endpoint: impl Into<String>, agent_token: impl Into<String>) -> Result<Self, Error>;
```

Constructs a session for a daemon reached directly, with no proxy authentication.

`crates/microvms-core/src/prelude.rs:328`

### protocol::exec::StartRequest::new

```rs
    pub fn new(exec_id: impl Into<String>, command: Vec<String>) -> Self {
```

Creates an argv command request under the supplied idempotency key with the wire defaults for optional fields.

`crates/protocol/src/exec.rs:534`

### provision::agentd

```rs
pub fn agentd(version: Option<&str>, state_dir: Option<&Path>) -> Result<Provisioned, Error> {
```

Returns the requested daemon binary from an override, the cache, or a verified release download in a blocking call.

`crates/microvms-edges/src/provision.rs:646`

### sandbox::Sandbox::adopt

```rs
    pub async fn adopt(
        control: ControlPlane,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
    ) -> Result<Self, Error> {
```

Reconstructs a sandbox for an existing VM from retained connection details and the lifecycle reported by the service.

`crates/microvms-app/src/sandbox.rs:1411`

### sandbox::Sandbox::detach

```rs
    pub fn detach(&mut self) -> Result<Detached, Error> {
```

Returns the details another process needs to adopt the VM and makes this sandbox inactive without stopping the VM.

`crates/microvms-app/src/sandbox.rs:1550`

### sandbox::Sandbox::ensure_image

```rs
    pub async fn ensure_image(
        &mut self,
        request: crate::control::EnsureImageRequest,
    ) -> Result<crate::control::EnsuredImage, Error> {
```

Builds or reuses the content-addressed image selected by the request.

`crates/microvms-app/src/sandbox.rs:948`

### sandbox::Sandbox::resume

```rs
    pub async fn resume(&mut self) -> Result<&mut Session, Error> {
```

Resumes a suspended VM within its allowed window and returns a usable session.

`crates/microvms-app/src/sandbox.rs:1719`

### sandbox::Sandbox::run

```rs
    pub async fn run(&mut self, request: RunRequest) -> Result<&mut Session, Error> {
```

Launches a VM and returns its session, waiting for the running state and daemon readiness when requested.

`crates/microvms-app/src/sandbox.rs:1113`

### sandbox::Sandbox::suspend

```rs
    pub async fn suspend(&mut self) -> Result<(), Error> {
```

Requests suspension and waits for SUSPENDED or TERMINATED, recording the resulting lifecycle on the sandbox.

`crates/microvms-app/src/sandbox.rs:1635`

### sandbox::Sandbox::terminate

```rs
    pub async fn terminate(&mut self, opts: TeardownOpts) -> TeardownReport {
```

Attempts the configured teardown steps and returns a report containing any failures and remaining resources.

`crates/microvms-app/src/sandbox.rs:1827`

### session::ExecHandle::kill

```rs
    pub async fn kill(&self) -> Result<bool, Error> {
```

Signals the command's process group and returns whether anything was signaled.

`crates/microvms-app/src/session/exec.rs:806`

### session::ExecHandle::stream_with

```rs
    pub fn stream_with(
        &self,
        options: StreamOptions,
    ) -> impl Stream<Item = Result<ExecEvent, Error>> + '_ {
```

Streams execution events with the requested cursor and reconnect options.

`crates/microvms-app/src/session/exec.rs:431`

### session::ExecHandle::wait

```rs
    pub async fn wait(&self, timeout: Duration) -> Result<ExecResult, Error> {
```

Polls until execution completes or the client deadline expires, leaving the execution record available for later polling.

`crates/microvms-app/src/session/exec.rs:384`

### session::ExecHandle::write_stdin

```rs
    pub async fn write_stdin(&self, data: &[u8], eof: bool) -> Result<StdinAck, Error> {
```

Writes bytes to an execution started with stdin enabled and optionally sends EOF in the same request.

`crates/microvms-app/src/session/exec.rs:760`

### session::Session::download_file

```rs
    pub async fn download_file(&self, path: &str) -> Result<Vec<u8>, Error> {
```

Reads a remote file into a byte vector.

`crates/microvms-app/src/session/mod.rs:603`

### session::Session::download_tar

```rs
    pub async fn download_tar(&self, remote: &str) -> Result<Vec<u8>, Error> {
```

Downloads a remote directory tree as tar bytes.

`crates/microvms-app/src/session/mod.rs:631`

### session::Session::run

```rs
    pub async fn run(&self, req: protocol::exec::StartRequest) -> Result<ExecHandle, Error> {
```

Starts a command and returns its execution handle without waiting for completion.

`crates/microvms-app/src/session/mod.rs:520`

### session::Session::run_to_completion

```rs
    pub async fn run_to_completion(
        &self,
        request: protocol::exec::StartRequest,
        options: CompletionOptions,
        on_output: Option<OutputSink>,
    ) -> Result<ExecResult, Error> {
```

Starts a command and drives streaming, deadline handling, and acknowledgment to one execution result.

`crates/microvms-app/src/session/complete.rs:263`

### session::Session::upload_file

```rs
    pub async fn upload_file(
        &self,
        path: &str,
        data: &[u8],
        mode: Option<&str>,
    ) -> Result<(), Error> {
```

Writes a remote file, creating parent directories and applying an optional mode expressed as an octal string.

`crates/microvms-app/src/session/mod.rs:593`

### session::Session::upload_tar

```rs
    pub async fn upload_tar(&self, remote: &str, archive: &[u8]) -> Result<(), Error> {
```

Uploads tar bytes for extraction under the requested remote directory.

`crates/microvms-app/src/session/mod.rs:626`

## See also

These pages cite many of the same source files as this one.

- [contract map](../insights/contract-map.md)
- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [impact analysis](../insights/impact-analysis.md)
- [system overview](../architecture/system-overview.md)
