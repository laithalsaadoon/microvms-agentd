# microvms-agentd · Public API

Use the SDKs to launch remote sandboxes for agents, execute tools, transfer files,
and collect results. Start with a complete example in the
[SDK tutorial](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/), or the package guide for
[Rust](../../microvms-core/README.md), [Python](../../microvms-py/README.md),
or [Node/TypeScript](../../microvms-js/README.md).

| Task | API |
| --- | --- |
| Launch a VM from an existing image | `Sandbox` and `RunRequest` |
| Execute a command or transfer files | `Session` |
| Stream, wait for, or cancel a command | `ExecHandle` |
| Run Claude Code or Codex with Bedrock access | `AgentVm` |
| Terminate and check cleanup | `TeardownOpts` and `TeardownReport` |
| Build images or call the control plane directly | `ControlPlane` and `CreateImageRequest` |
| Turn a task Dockerfile into one that runs `agentd` | `wrap_dockerfile` and `BaseImage::from_dockerfile` |
| Get the `agentd` binary for this client's version | `provision::agentd` |
| Build or reuse one image for many trials | `Sandbox::ensure_image` |
| Run one command to exactly one result | `Session::run_to_completion` |
| Hand a VM to another process | `Sandbox::detach` and `Sandbox::adopt` |
| Check a harness can launch before queueing work | `preflight` and `SizeClass::from_request` |

`microvms-core` is the Rust client. The Python `microvms` package and Node
`microvms` package expose bindings to it. Core re-exports `protocol`, the shared
wire types. This page is a selected API reference; see
[Rust API docs](https://docs.rs/microvms-core) for the complete Rust surface and
[CLI reference](cli.md) for `microvm` commands.

## Launch networking

Rust `RunMicrovmRequest` and `RunRequest` accept `egress_network_connectors`.
Python `run` accepts `egress_network_connectors=[arn]`; Node `run` accepts
`egressNetworkConnectors: [arn]`. These attach existing VPC connector ARNs;
creation and VPC configuration use the separate AWS Lambda core API.

Custom connectors conflict with the managed `egress` option. Supplying an ARN
does not certify isolation: no internet egress requires a VPC without an IGW
or NAT gateway and no alternative internet route. See [Networking](../NETWORKING.md).

## Harness helpers

These calls compose the lifecycle for a harness that brings its own task image and
runs many trials. Each is one core function, and each binding passes straight through
to it. [Embedding](../EMBEDDING.md) is the walkthrough; this section names them and
their source.

| Core (Rust) | Python `microvms` | Node `microvms` |
| --- | --- | --- |
| `control::wrap_dockerfile` | `wrap_dockerfile` | `wrapDockerfile` |
| `control::BaseImage::from_dockerfile` | `BaseImage.from_dockerfile` | `baseImageFromDockerfile` |
| `provision::agentd` | `provision_agentd`, `provision_agentd_report` | `provisionAgentd`, `provisionAgentdReport` |
| `Sandbox::ensure_image` | `Sandbox.ensure_image` | `Sandbox.ensureImage` |
| `Session::run_to_completion` | `Session.run_to_completion` | `Session.runToCompletion` |
| `Sandbox::detach` | `Sandbox.detach` | `Sandbox.detach` |
| `control::egress_posture_for`, `Session::egress_posture` | `egress_posture_for`, `Session.egress_posture` | `egressPostureFor`, `Session.egressPosture` |
| `SizeClass::from_request` | `SizeClass.from_request` | `SizeClass.fromRequest` |
| `preflight::preflight` | `preflight` | `preflight` |

### wrap_dockerfile and BaseImage::from_dockerfile

`wrap_dockerfile(task, &WrapOptions)` returns the task Dockerfile verbatim with the
`agentd` stanza appended: `USER root` only when the task's last `USER` is someone
else, then the same stanza `default_dockerfile` writes, so `ENTRYPOINT []` and
`CMD ["/agentd"]` are the last instructions whatever the task set. It refuses a
Dockerfile with no `FROM`, an unfinished last instruction, a keepalive at or over
the client's stream idle timeout, a port of 0, a workdir that isn't one absolute
path, and `inherit_workdir` with no `WORKDIR` anywhere.
`BaseImage::from_dockerfile` pairs the managed base's `name` with the Dockerfile's
own first `FROM`, so the create call's `FROM` guard passes by construction. Both are
local and make no AWS call. `microvms-app/src/control/artifact.rs:562-611`,
`microvms-app/src/control/artifact.rs:375-408`, `microvms-app/src/control/artifact.rs:536-550`.

### provision::agentd

`provision::agentd(version, state_dir)` returns verified aarch64 `agentd` bytes as
`Provisioned { bytes, path, source, version, sha256 }`. It answers from a caller's
path (`$MICROVM_AGENTD`), then the cache under the state directory, then a fetch of
this repository's release asset, proven with `gh attestation verify` when `gh`
can run, else checked against the release's `SHA256SUMS`; a fetch it can't verify
is an error. Every binary it returns is checked to be an aarch64 ELF, and the
version defaults to the core's own, never "latest". It blocks, because a fetch runs
subprocesses. `microvms-edges/src/provision.rs:1-51`, `microvms-edges/src/provision.rs:179-190`,
`microvms-edges/src/provision.rs:880-890`.

### Sandbox::ensure_image

`ensure_image(EnsureImageRequest)` builds or reuses the content-addressed image for
its inputs. The name is `<name_prefix>-<hash12>`, hashed over the daemon, the
Dockerfile, the build context, the base, and the size class, so equal inputs name
one image. It reuses a ready image, waits out one a sibling is building, deletes a
failed one (any one under `force`), or builds, uploading the artifact only when a
build is needed. Everything local runs before the first call, so a request the
client refuses costs nothing. It returns `EnsuredImage { image, reused,
artifact_uri, uploaded, warnings }`. `microvms-app/src/sandbox.rs:905-940`,
`microvms-app/src/control/ensure.rs:1-40`, `microvms-app/src/control/ensure.rs:240-268`,
`microvms-app/src/control/ensure.rs:297-311`.

### Session::run_to_completion

`run_to_completion(request, CompletionOptions, on_output)` starts the exec and drives
it to exactly one `ExecResult`: output goes to the callback when there is one, a
stream that ends without its `exit` event falls back to wait-and-ack, and on the
client deadline (the request's `timeout_sec` plus `client_grace`, 60 seconds by
default) it kills the process group, acks within the grace, and synthesizes exit
code 124 when even that fails. A callback that answers `Break` stops delivery, and
the exec is still waited for and acked. `microvms-app/src/session/complete.rs:1-58`,
`microvms-app/src/session/complete.rs:261-272`.

### Sandbox::detach

`detach()` hands the VM to another process and returns `Detached { microvm_id,
endpoint, region, port }` plus the agent token through `agent_token()`, which
`Debug` redacts. The VM keeps running and nothing is sent to AWS; the sandbox that
detached refuses every later lifecycle call, and it drops without the leak warning.
It's refused (`Precondition`) when there's no live VM to hand off. The adopting
process passes those fields to `Sandbox::adopt`. `microvms-app/src/sandbox.rs:682-699`,
`microvms-app/src/sandbox.rs:1348-1404`.

### Egress posture

`egress_posture_for(egress, egress_network_connectors, deny_egress, region)` answers,
with no AWS call, the `EgressPosture` a launch with those options would report, or
the refusal it would raise. The posture is `Open` (`INTERNET_EGRESS` requested),
`Unsealed` (the default), or `BestEffort` (the advisory in-guest deny); it never
answers `Sealed`, because no launch option proves VPC routing without an internet or
NAT gateway. `Session::egress_posture` carries the launched session's answer, which
is also the CLI envelope's `egressPosture`. `microvms-app/src/control/connector.rs:119-143`,
`microvms-app/src/control/connector.rs:180-196`, `microvms-app/src/session/mod.rs:267-269`.

### preflight and SizeClass::from_request

`preflight(region)` runs these checks, in order, and reports each as a `Check`:
the region resolves (from the argument or `$AWS_REGION` / `$AWS_DEFAULT_REGION`),
the default credential chain resolves credentials, and one free
`ListManagedMicrovmImages` page answers in that region. `PreflightReport::ok()` is
true exactly when no fatal check failed or was skipped. It doesn't check roles,
buckets, quotas, or connectors. `SizeClass::from_request(cpus, memory_mib)` returns
the smallest class whose baseline covers the request, `SizeClass::DEFAULT` when
neither axis asks for anything, and an invalid-argument refusal naming the largest
class when no class covers it. `microvms-app/src/preflight.rs:1-32`,
`microvms-app/src/preflight.rs:93-111`, `microvms-app/src/preflight.rs:191-201`,
`microvms-domain/src/sizing.rs:170-212`.

## microvms-core

### Error

```rs
#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct Error {
```

A failure classified once at the point it is raised, deliberately a struct with a private body rather than an enum, because an enum over every raise site would make each new failure a breaking change for a binding that matched exhaustively.

`microvms-domain/src/error.rs:41-43`

### Region

```rs
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum Region {
```

An AWS region, closed over the five that run MicroVMs plus a named escape hatch, so a typo'd region is a compile error rather than an `AccessDeniedException` carrying a null message.

`microvms-domain/src/region.rs:44-45`

### ErrorKind

```rs
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ErrorKind {
```

The coarse failure classes, one per non-zero row of the CLI's exit table, with the integer exit code left to the CLI because a library owning process exit codes would be a library with an opinion about being a process.

`microvms-domain/src/error.rs:126-127`

### SizeClass

```rs
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum SizeClass {
    Mib512,
    Mib1024,
    Mib2048,
    Mib4096,
    Mib8192,
}
```

The five documented size classes, named for the baseline a caller writes into `minimumMemoryInMiB` and deliberately not for the peak, since naming both would suggest the two are picked independently.

`microvms-domain/src/sizing.rs:118-125`

### Session

```rs
pub struct Session {
```

The control API of one running MicroVM.

`microvms-app/src/session/mod.rs:209`

### ControlPlane

```rs
pub struct ControlPlane {
```

The control-plane client, holding its transport and clock behind `Arc` so a caller keeping one across tasks does not need a second credential chain.

`microvms-app/src/control/mod.rs:130`

### Sandbox

```rs
pub struct Sandbox {
```

One MicroVM's whole life: the state machine, the suspended window, and explicit teardown.

`microvms-app/src/sandbox.rs:602`

### RunRequest

```rs
let mut request = RunRequest::new().with_image(&image_arn);
request.execution_role_arn = Some(execution_role_arn);
let session = sandbox.run(request).await?;
```

Use `microvms_core::sandbox::RunRequest` to launch an image containing `agentd`.
Pass the actual image ARN returned by a build; the SDK does not perform the CLI's
friendly image-name lookup. Defaults are a ten-minute idle window, a ten-minute
suspended window, a one-hour maximum lifetime, and no auto-resume. Network
connector omission does not establish internet isolation; see launch networking
above. Wait for `session.wait_until_ready` before executing commands.

### TeardownOpts and TeardownReport

```rs
let report = sandbox.terminate(TeardownOpts::default()).await;
```

These types live in `microvms_core::sandbox`. Termination returns a report
instead of an error: inspect `failures` and `undeleted`. Defaults request VM
termination and retain the image and logs. Add `.waiting_for_terminated()` to the
options when the caller must observe the final state. `Sandbox` does not clean
up on drop, so call `terminate` on both success and failure paths; the
[Rust quickstart](../../microvms-core/README.md) shows that pattern.

### AgentVm

```rs
pub struct AgentVm {
```

One VM with coding agents in it: the `Sandbox` plus the `AgentSpec`s it was built for, with `image_request`, `build`, `launch_request`, `launch`, `install_access`, `prompt`, and `terminate` as the L3 steps over the lifecycle. The free functions beside it (`image_request_for`, `launch_request_for`, `install_access`, `prompt`, `installed_agents`, `spec_for`) are the same steps for a caller holding the sandbox and the specs separately, which is how the bindings drive it; `agents::bedrock::mint` is the in-process Bedrock bearer token.

`microvms-app/src/agents/mod.rs`

### WireKind

```rs
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum WireKind {
```

The daemon-side failure classes the conformance suite asserts on, several of which collapse onto one `ErrorKind` at the exit code rather than at the raise site.

`microvms-domain/src/error.rs:218-219`

### RunHookTimeout

```rs
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct RunHookTimeout(u32);
```

A timeout for the `run`, `resume`, `suspend`, or `terminate` hook, accepting 1..=60 seconds and offering no conversion from `BuildHookTimeout`.

`microvms-domain/src/hooks.rs:47-48`

### Transport

```rs
pub struct Transport {
```

A backend, the agent token, and the proxy auth every request needs, kept separate from `Session` because `ExecHandle` needs it and holding a whole session would make the two mutually recursive.

`microvms-app/src/session/mod.rs:74`

### BuildHookTimeout

```rs
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct BuildHookTimeout(u32);
```

A timeout for the `ready` or `validate` image-build hook, accepting 1..=3600 seconds, and a distinct type so a build-sized value cannot reach a field that caps at 60.

`microvms-domain/src/hooks.rs:53-54`

### EstimatedUsd

```rs
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct EstimatedUsd(Decimal);
```

Dollars derived from published rates and not the bill, with no `From<EstimatedUsd> for f64`, no `Into`, no `Deref`, and no `as_f64`, so laundering an estimate into a float does not compile.

`microvms-domain/src/cost.rs:520-521`

### ExecHandle

```rs
pub struct ExecHandle {
```

One exec addressed by its caller-minted id, which is also the idempotency key, so rebuilding a handle with the same id after a process restart still addresses the same server-side exec.

`microvms-app/src/session/exec.rs:332`

### RateTable

```rs
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RateTable {
```

The us-east-1 rates, held privately so that pricing compute from the ARM rate is a property of the type rather than of a code path a caller can bypass.

`microvms-domain/src/cost.rs:845-846`

### CostReport

```rs
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CostReport {
```

Per-phase cost attribution for one sandbox, measured or projected, holding the rate table it was computed against so it stays reproducible after `pinned_rates` is updated.

`microvms-domain/src/cost.rs:1474-1475`

### ExecResult

```rs
#[derive(Debug)]
pub struct ExecResult {
    pub exec_id: String,
    pub phase: protocol::exec::Phase,
    /// `None` while running. Present once the child has exited.
    pub outcome: Option<protocol::exec::Outcome>,
    pub client_deadline: Option<super::complete::ClientDeadline>,
}
```

An exec's phase and, once it has one, its outcome — a thin wrapper over the daemon's `PollResponse` rather than a re-modelling of it, so the two cannot disagree. `client_deadline` is the one field the wire doesn't carry: what the client did when its own deadline expired, set only on a result `Session::run_to_completion` returned after one. `posix_exit_code()` is the code a POSIX shell would report (124 for any timeout, the daemon's or the client's; 128 plus the signal for another signal death; otherwise the exit code), and `notes()` lists one line per condition worth telling a reader (truncated output, the daemon's deadline, writers still alive, the client deadline). The bindings carry both, plus `synthesized`. `microvms-app/src/session/exec.rs:124-156`, `microvms-app/src/session/exec.rs:158-209`.

`microvms-app/src/session/exec.rs:66-77`

### Image

```rs
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Image {
```

A built image, and the log group the service created alongside it.

`microvms-app/src/control/image.rs:58-59`

## protocol

### protocol::exec::Phase

```rs
#[derive(Clone, Copy, Debug, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
```

An exec's phase on the wire, with schemars reading the same `#[serde(...)]` attributes serde does so the published schema describes what the daemon actually emits.

`protocol/src/exec.rs:22-24`

### protocol::health::Health

```rs
#[derive(Debug, Deserialize, JsonSchema, Serialize)]
pub struct Health {
```

The `GET /v1/health` response: daemon version, bootstrap state, disk pressure, whether startup identity repair degraded, and the exec-activity pair `busy` / `execs`.

`protocol/src/health.rs:10-11`

### protocol::exec::StartRequest

```rs
#[derive(Debug, Deserialize, JsonSchema, Serialize)]
pub struct StartRequest {
```

The `POST /v1/exec/start` body, whose `command` field is either an argv array or, with `shell` set (`true`, or a shell's name such as `"bash"`), a single script string. `user` and `group` take a name or a numeric id, and `inherit_image_env` starts the child from the image's `ENV`.

`protocol/src/exec.rs:215-289`

### protocol::exec::Outcome

```rs
#[derive(Clone, Debug, Default, Deserialize, JsonSchema, Serialize)]
pub struct Outcome {
```

Captured output and exit status of a finished exec.

`protocol/src/exec.rs:57-58`

### protocol::exec::PollResponse

```rs
#[derive(Debug, Deserialize, JsonSchema, Serialize)]
pub struct PollResponse {
    pub exec_id: String,
    pub phase: Phase,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(flatten)]
    pub result: Option<Outcome>,
}
```

The `GET /v1/exec/{id}` body, which flattens the outcome into the response and omits it entirely while the exec is still running.

`protocol/src/exec.rs:421-428`

## microvms-py

The [Python SDK reference](https://laithalsaadoon.github.io/microvms-agentd/reference/python/) is generated from the committed stub and gives every class, function, and exception its full signature.

The Python module is declared rather than assembled: a `#[pymodule] mod microvms` lists its members in `#[pymodule_export]` use statements (the agent layer's `AgentSpec`, `AgentVm`, and `BearerToken`, with `mint_bedrock_token`, `installed_agents`, `install_agent_access`, `prompt_agent`, and `agent_constants`, arrived with `docs/AGENT-VMS.md`), so the macro can see the whole membership and `maturin generate-stubs` emits the real surface instead of a `__getattr__` escape hatch (`microvms-py/src/lib.rs:115-157`). The exception hierarchy stays imperative in `#[pymodule_init]`, because `create_exception!` builds its types at runtime and leaves no introspection record for `#[pymodule_export]` to carry (`microvms-py/src/lib.rs:110-165`). Every method is sync, blocking on one shared multi-thread tokio runtime with `py.detach` first (`microvms-py/src/lib.rs:42-46`). The generated stub and its PEP 561 marker are committed as `microvms-py/microvms.pyi` and `microvms-py/py.typed`, and `mise run stubs:check` fails when the committed stub no longer matches the pyo3 surface (`mise.toml:216-218`).

### microvms-py Region

```rs
#[pyclass(frozen, from_py_object, name = "Region", module = "microvms")]
#[derive(Clone)]
pub struct PyRegion {
```

An AWS region, closed over the five that run MicroVMs plus a named escape hatch, exported to Python as `Region`.

`microvms-py/src/region.rs:30-32`

### microvms-py Sandbox

```rs
#[pyclass(frozen, name = "Sandbox", module = "microvms")]
pub struct PySandbox {
```

One MicroVM's whole life, with `build_image`, `run`, `suspend`, `resume`, and `terminate` as the transitions and every state guard left in the core.

`microvms-py/src/sandbox.rs:472-473`

### microvms-py Session

```rs
#[pyclass(frozen, name = "Session", module = "microvms")]
pub struct PySession {
```

One running MicroVM's control API, with the proxy auth handled for you.

`microvms-py/src/session.rs:449-450`

### microvms-py EstimatedUsd

```rs
#[pyclass(
    frozen,
    skip_from_py_object,
    name = "EstimatedUsd",
    module = "microvms"
)]
#[derive(Clone, Copy)]
pub struct PyEstimatedUsd {
```

A dollar figure with no `__float__`, `__int__`, `__index__`, or `__add__`, whose `amount` answers a string, so `float(usd)` raises `TypeError` — the Python equivalent of the core's missing impl.

`microvms-py/src/cost.rs:169-176`

## microvms-js

The [TypeScript SDK reference](https://laithalsaadoon.github.io/microvms-agentd/reference/typescript/) is generated from the committed `index.d.ts` and gives every class, interface, and function its full signature.

The Node surface has no barrel: every `#[napi]` item in the crate is exported, and `index.d.ts` plus the `index.js` loader and the compiled `.node` addon are generated by `napi build` and excluded from the repository as one platform's build output (`.gitignore:27-29`). Two shapes appear side by side and mean different things: `#[napi]` on a struct is a JS class with methods, while `#[napi(object)]` is a copied plain object with no methods, which is how the same wire results that pyo3 renders as frozen classes arrive in Node (`microvms-js/src/exec.rs:65-66`, `microvms-js/src/session.rs:53-54`). Construction diverges from Python for a reason that is structural rather than stylistic: `PySandbox` has a `#[new]` constructor that blocks on the shared runtime (`microvms-py/src/sandbox.rs:522-528`), and a `#[napi(constructor)]` cannot be async, so the Node class is built through a static factory instead (`microvms-js/src/sandbox.rs:631-635`).

### microvms-js Region

```rs
#[napi]
#[derive(Clone)]
pub struct Region {
```

An AWS region, closed over the five that run MicroVMs plus a named escape hatch, taken as an instance rather than a string everywhere on this surface.

`microvms-js/src/region.rs:33-35`

### microvms-js Session

```rs
#[napi]
pub struct Session {
```

One running MicroVM's control API, with the proxy auth handled for you.

`microvms-js/src/session.rs:443-444`

### microvms-js Sandbox

```rs
#[napi]
pub struct Sandbox {
```

One MicroVM's whole life, with `buildImage`, `run`, `suspend`, `resume`, and `terminate` as the transitions and every state guard left in the core.

`microvms-js/src/sandbox.rs:614-615`

### microvms-js ExecProcess

```rs
#[napi]
pub struct ExecProcess {
```

A long-running exec in the AI SDK's `SandboxProcess` shape, built by `Session.spawn` and never by a constructor, and the one entry on this surface with no peer in `microvms-py`.

`microvms-js/src/process.rs:194-195`

## HTTP

The daemon's routes all come from one list, `surface_docs`, which `app` walks to build the router and `GET /v1/schema` walks to publish the document (`agentd/src/routes.rs:443-752`). A route cannot be served unless it appears in that list, and a listed route with no handler panics at startup rather than serving an undocumented surface (`agentd/src/routes.rs:110-142`). Each row also declares its auth, which is what splits the router in two: `Auth::Bearer` rows go behind the token guard, `Auth::Open` and `Auth::PlatformHook` rows do not (`agentd/src/routes.rs:51-59`).

The six lifecycle hooks sit under a prefix fixed by the service, `/aws/lambda-microvms/runtime/v1` (`protocol/src/hook.rs:15`). They are unauthenticated because the platform has no token to present, and a consumer must never call them.

### POST /aws/lambda-microvms/runtime/v1/ready

The image-build readiness probe, answering 200 even before bootstrap, because the question it answers is whether the daemon started.

`agentd/src/routes.rs:473-481`

### POST /aws/lambda-microvms/runtime/v1/resume

Acknowledged; the token, filesystem, exec records, and even backgrounded processes survive a suspend/resume cycle, but the guest's view of time jumps, so any timeout or lease held by a running command expires at once.

`agentd/src/routes.rs:521-530`

### POST /aws/lambda-microvms/runtime/v1/run

The one-shot token bootstrap and the optional launch environment beside it, both one JSON parse deeper than the request body inside `runHookPayload`, sharing the platform's 4096-byte payload budget.

`agentd/src/routes.rs:491-513`

### POST /aws/lambda-microvms/runtime/v1/suspend

Acknowledged and logged.

`agentd/src/routes.rs:514-520`

### POST /aws/lambda-microvms/runtime/v1/terminate

Acknowledged; begins graceful shutdown with in-flight requests draining.

`agentd/src/routes.rs:531-537`

### POST /aws/lambda-microvms/runtime/v1/validate

The image-build validation probe, on the same reasoning as `ready`.

`agentd/src/routes.rs:482-490`

### POST /v1/exec/start

Starts a command under a caller-minted `exec_id`, idempotent on that id, so a retry returns success without spawning a second child.

`agentd/src/routes.rs:538-549`

### GET `/v1/exec/{id}`

Polls status and output, read-only, so polling never mutates the entry and output survives until an explicit ack.

`agentd/src/routes.rs:550-560`

### POST `/v1/exec/{id}/ack`

Releases output and enters TTL collection; only acked entries are ever collected, so output nobody read is never destroyed.

`agentd/src/routes.rs:594-604`

### POST `/v1/exec/{id}/kill`

Sends SIGTERM then SIGKILL to the whole process group rather than the direct child alone, because a shell that backgrounded a server leaves the interesting process outside the child pid.

`agentd/src/routes.rs:605-616`

### POST `/v1/exec/{id}/stdin`

Writes to a child's stdin or signals EOF, a separate request from the output stream so a dropped attach does not cost the ability to feed the process.

`agentd/src/routes.rs:579-593`

### GET /v1/procs

Process accounting: every registered exec with its process group's live pids, read from `/proc` inside the guest so the image needs no `ps`. `child_exited: true` beside a non-empty `pids` is a command that finished while something it backgrounded did not; `reap` echoes the start request's `reap_group_on_exit`.

`agentd/src/routes.rs`

### GET /v1/tcp

A WebSocket relayed to `127.0.0.1:<port>` in the guest, loopback-only, one connection per socket, with the outcome carried in close codes because the route leaves HTTP behind after its 101.

`agentd/src/routes.rs`

### GET `/v1/exec/{id}/stream`

Follows output as Server-Sent Events from a byte offset, resumable with `?offset=N`; a body that ends without an `exit` event means the connection failed, not the command.

`agentd/src/routes.rs:561-578`

### GET /v1/fs/file

Reads one file, or a 1-based inclusive line range of it, always streamed — an `end_line` past the last line reads through EOF without error, and omitting both bounds returns the whole file byte-identically.

`agentd/src/routes.rs:662-677`

### PUT /v1/fs/file

Writes one file, deliberately not confined to a root, because the same token authorizes exec and a root prefix would add no security while breaking harnesses that write to home directories and `/etc`.

`agentd/src/routes.rs:678-690`

### GET /v1/fs/tar

Downloads a tree as tar, packing symlinks as symlinks, which is the producing half of what extraction accepts.

`agentd/src/routes.rs:691-702`

### PUT /v1/fs/tar

Uploads and extracts a tar under `?path=`, the one confined write path because member paths come from the archive rather than the caller, mirroring the CPython tarfile `data` filter.

`agentd/src/routes.rs:703-718`

### GET /v1/health

Reports liveness, daemon version, bootstrap completion, and whether any exec is still running; `busy` exists so an orchestrator outside the VM can hold it alive, since the platform measures idleness by inbound traffic through a proxy that terminates outside the guest.

`agentd/src/routes.rs:719-740`

### GET /v1/schema

Returns this document: every route, shape, status code, and operative limit.

`agentd/src/routes.rs:741-750`

## See also

- [contract map](../insights/contract-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [business logic](../insights/business-logic.md)
- [system overview](../architecture/system-overview.md)
- [debugging guide](../insights/debugging-guide.md)
