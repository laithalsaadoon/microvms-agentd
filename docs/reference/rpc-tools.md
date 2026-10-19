# microvms-agentd · RPC tools

The daemon exposes these HTTP methods through its schema-driven router. The route table supplies the census. The dispatcher connects each method and path to the handler quoted below, and serves health through a wrapper that adds response headers. Shared wire types live in the protocol crate. `crates/agentd/src/routes.rs:31-57`, `crates/agentd/src/routes.rs:151-184`, `crates/protocol/src/lib.rs:1-17`.

Control routes require the bearer token; health and schema are open, while the lifecycle hooks use the platform-hook classification. `crates/agentd/src/routes.rs:52-70`, `crates/agentd/src/routes.rs:535-828`.

## GET `/v1/exec/{id}`

```rs
pub async fn poll(State(state): State<AppState>, Path(id): Path<String>) -> Response {
```

Reads an exec’s phase and buffered result without acknowledging it. `crates/agentd/src/routes.rs:616-623`.

**Input:** The `id` path segment; no request body. `crates/agentd/src/exec.rs:439`.

**Output:** JSON `PollResponse`: `exec_id` and `phase`, with available `Outcome` fields flattened into the same object: `exit_code`, `signal`, `timed_out`, `stdout`, `stderr`, `truncated`, and `writers_may_be_alive`; absent output is omitted. The wire phases are `running`, `exited`, and `acked`. `crates/protocol/src/exec.rs:432-438`, `crates/protocol/src/exec.rs:58-78`, `crates/protocol/src/exec.rs:24-31`.

`crates/agentd/src/exec.rs:439`

## GET `/v1/exec/{id}/stream`

```rs
pub async fn stream(
    State(state): State<AppState>,
    Path(id): Path<String>,
    query: Result<Query<StreamQuery>, QueryRejection>,
) -> Response {
```

Replays available output from a byte offset and follows live output as Server-Sent Events. `crates/agentd/src/routes.rs:628-644`.

**Input:** The `id` path segment and optional query `offset: u64`, defaulting to zero; no request body. `crates/protocol/src/exec.rs:333-338`, `crates/agentd/src/exec.rs:487-512`.

**Output:** `text/event-stream`, with JSON event payloads: `output` has `offset`, `stream`, and base64 `output`; `gap` has `from` and `to`; `exit` has `exit_code`, `signal`, `timed_out`, `truncated`, `writers_may_be_alive`, and total `offset`. `crates/protocol/src/exec.rs:342-370`, `crates/agentd/src/exec.rs:515`.

`crates/agentd/src/exec.rs:487`

## GET /v1/fs/file

```rs
pub async fn read_file(request: Request) -> Response {
```

Streams a file or an inclusive line range. `crates/agentd/src/routes.rs:734-743`.

**Input:** Query `FileReadQuery`: required string `path` and optional unsigned `start_line` and `end_line`; line numbering starts at one, and an omitted end reads through EOF. `crates/protocol/src/fs.rs:39-57`.

**Output:** `application/octet-stream` containing the selected bytes; the full-file path preserves the file’s bytes. `crates/agentd/src/fs.rs:1175-1188`.

`crates/agentd/src/fs.rs:1132`

## GET /v1/fs/tar

```rs
pub async fn read_tar(State(state): State<AppState>, request: Request) -> Response {
```

Downloads a directory tree as an uncompressed tar archive. `crates/agentd/src/routes.rs:759-768`.

**Input:** Query `FsQuery` with required string `path`; the shared type also carries optional `mode`, which the download handler does not use. `crates/protocol/src/fs.rs:18-24`, `crates/agentd/src/fs.rs:1410-1440`.

**Output:** `application/x-tar`, streamed from the completed archive spool. `crates/agentd/src/fs.rs:1439-1464`.

`crates/agentd/src/fs.rs:1410`

## GET /v1/health

```rs
async fn health(State(state): State<AppState>) -> Json<Health> {
```

Reports liveness, bootstrap state, exec activity, disk status, identity repair, and observed lifecycle hooks. `crates/agentd/src/routes.rs:438-473`.

**Input:** No path parameters, query, or request body. `crates/agentd/src/routes.rs:438`.

**Output:** JSON `Health` with `version`, `bootstrapped`, `disk`, `identity_degraded`, `identity_repaired`, `busy`, `execs`, `hooks`, `hooks_dropped`, `identity_steps`, and `image_env_keys`. Nested values describe disk capacity, hook timing and optional handler outcomes, and identity-repair outcomes. `crates/protocol/src/health.rs:20-147`, `crates/protocol/src/health.rs:151-222`.

The route's handler is `health_response`, which returns this body with two response headers. `microvms-default-user-configured: 1` says the image sets a default exec user. `microvm-poweroff-on-exit` is `1` when the daemon's power-off check passes and `0` otherwise, and the Rust client's `Session::run` refuses to send the `poweroff_on_exit` opt-in unless it reads `1`. `crates/agentd/src/routes.rs:179`, `crates/agentd/src/routes.rs:421-436`, `crates/protocol/src/health.rs:9-16`, `crates/microvms-app/src/session/mod.rs:521-536`.

`crates/agentd/src/routes.rs:438`

## GET /v1/procs

```rs
pub async fn procs(State(state): State<AppState>) -> Response {
```

Lists registered execs with the live process IDs in each captured process group. `crates/agentd/src/routes.rs:683-694`.

**Input:** No path parameters, query, or request body. `crates/agentd/src/exec.rs:1066`.

**Output:** JSON `ProcsResponse` with `procs: Vec<ProcGroup>`; each entry has `exec_id`, optional `pgid`, `started_at`, `child_exited`, `reap`, and `pids`. `crates/protocol/src/exec.rs:402-429`.

`crates/agentd/src/exec.rs:1066`

## GET /v1/schema

```rs
async fn schema_route(State(state): State<AppState>) -> Json<serde_json::Value> {
```

Publishes the daemon’s route, shape, status, and configured-limit contract. `crates/agentd/src/routes.rs:495-497`, `crates/agentd/src/routes.rs:822-826`.

**Input:** No path parameters, query, or request body. `crates/agentd/src/routes.rs:495`.

**Output:** JSON produced by `schema::document`, including route metadata and shared type definitions. `crates/agentd/src/routes.rs:495-497`, `crates/agentd/src/schema.rs:232-324`.

`crates/agentd/src/routes.rs:495`

## GET /v1/tcp

```rs
pub async fn open(
    State(state): State<AppState>,
    Query(query): Query<TunnelQuery>,
    upgrade: WebSocketUpgrade,
) -> Response {
```

Upgrades to a WebSocket and relays bytes to the requested loopback TCP port. `crates/agentd/src/tunnel.rs:106-111`.

**Input:** Query `TunnelQuery`: required `port: u16`, optional `identity: bool` defaulting to false, and a WebSocket upgrade request. `crates/protocol/src/tunnel.rs:17-38`, `crates/agentd/src/tunnel.rs:126-130`.

**Output:** A WebSocket upgrade response, followed by binary frames; with identity requested, the relay performs the Noise handshake before dialing. Outcomes use the shared WebSocket close codes. `crates/agentd/src/tunnel.rs:138-151`, `crates/protocol/src/tunnel.rs:55-89`.

`crates/agentd/src/tunnel.rs:126`

## POST /aws/lambda-microvms/runtime/v1/ready

```rs
async fn ready_hook(State(state): State<AppState>) -> StatusCode {
```

Records and acknowledges the image-build readiness probe. `crates/agentd/src/routes.rs:324-331`.

**Input:** No request body; the handler receives the daemon’s `AppState`. `crates/agentd/src/routes.rs:324`.

**Output:** HTTP 200 with no body. `crates/agentd/src/routes.rs:324-331`.

`crates/agentd/src/routes.rs:324`

## POST /aws/lambda-microvms/runtime/v1/resume

```rs
async fn resume_hook(State(state): State<AppState>) -> StatusCode {
```

Records the resume notice and runs the configured workload resume handler before acknowledging it. `crates/agentd/src/routes.rs:370-377`.

**Input:** No request body; the handler receives the daemon’s `AppState`. `crates/agentd/src/routes.rs:370`.

**Output:** HTTP 200 with no body. `crates/agentd/src/routes.rs:370-377`.

`crates/agentd/src/routes.rs:370`

## POST /aws/lambda-microvms/runtime/v1/run

```rs
async fn run_hook(
    State(state): State<AppState>,
    body: Result<Json<RunHookEnvelope>, JsonRejection>,
) -> Response {
```

Installs the launch token and environment on the first successful bootstrap, then runs the configured workload run handler. `crates/agentd/src/routes.rs:278-309`.

**Input:** JSON `RunHookEnvelope` with `runHookPayload`, a string containing the inner JSON payload. The decoded `RunHook` carries `agent_token`, `env`, `identity_seed`, and `identity_host_public_key`. `crates/protocol/src/hook.rs:27-30`, `crates/protocol/src/hook.rs:58-84`.

**Output:** An empty HTTP 200 on installation or an identical-token replay; malformed input returns HTTP 400, and a conflicting installed token returns HTTP 409. `crates/agentd/src/routes.rs:222-258`, `crates/agentd/src/routes.rs:278-314`.

`crates/agentd/src/routes.rs:222`

## POST /aws/lambda-microvms/runtime/v1/suspend

```rs
async fn suspend_hook(State(state): State<AppState>) -> StatusCode {
```

Records the suspend notice and runs the configured workload suspend handler before acknowledging it. `crates/agentd/src/routes.rs:343-350`.

**Input:** No request body; the handler receives the daemon’s `AppState`. `crates/agentd/src/routes.rs:343`.

**Output:** HTTP 200 with no body. `crates/agentd/src/routes.rs:343-350`.

`crates/agentd/src/routes.rs:343`

## POST /aws/lambda-microvms/runtime/v1/terminate

```rs
async fn terminate_hook(State(state): State<AppState>) -> StatusCode {
```

Records the termination notice and runs the configured workload termination handler before acknowledging it. `crates/agentd/src/routes.rs:388-395`.

**Input:** No request body; the handler receives the daemon’s `AppState`. `crates/agentd/src/routes.rs:388`.

**Output:** HTTP 200 with no body. `crates/agentd/src/routes.rs:388-395`.

`crates/agentd/src/routes.rs:388`

## POST /aws/lambda-microvms/runtime/v1/validate

```rs
async fn validate_hook(State(state): State<AppState>) -> StatusCode {
```

Records and acknowledges the image-build validation probe. `crates/agentd/src/routes.rs:335-342`.

**Input:** No request body; the handler receives the daemon’s `AppState`. `crates/agentd/src/routes.rs:335`.

**Output:** HTTP 200 with no body. `crates/agentd/src/routes.rs:335-342`.

`crates/agentd/src/routes.rs:335`

## POST /v1/exec/start

```rs
pub async fn start(
    State(state): State<AppState>,
    body: Result<Json<StartRequest>, JsonRejection>,
) -> Response {
```

Starts a command under a caller-supplied exec ID, with retries on the same ID reusing the existing exec. `crates/agentd/src/routes.rs:601-612`.

**Input:** JSON `StartRequest`: required `exec_id: String` and `command: Vec<String>`; optional/defaulted `shell`, `cwd`, `env`, `user`, `group`, `timeout_sec`, `stdin`, `reap_group_on_exit`, `poweroff_on_exit`, and `inherit_image_env`. `shell` accepts a boolean or string; `user` and `group` accept unsigned IDs or names. `crates/protocol/src/exec.rs:219-299`, `crates/protocol/src/exec.rs:119-124`, `crates/protocol/src/exec.rs:176-182`.

**Output:** JSON `StartResponse` containing `exec_id` and `phase`. `crates/protocol/src/exec.rs:373-376`.

`crates/agentd/src/exec.rs:341`

## POST `/v1/exec/{id}/ack`

```rs
pub async fn ack(State(state): State<AppState>, Path(id): Path<String>) -> Response {
```

Releases an exited exec’s buffered output and starts its collection TTL. `crates/agentd/src/routes.rs:658-667`.

**Input:** The `id` path segment; no request body. `crates/agentd/src/exec.rs:865`.

**Output:** HTTP 200 with JSON `PollResponse`: `exec_id`, `phase: "acked"`, and the released `Outcome` flattened into the same object. HTTP 409 reports `still_running` or `already_acked` when no result can be released. `crates/agentd/src/exec.rs:865-894`, `crates/agentd/src/exec.rs:897-929`, `crates/protocol/src/exec.rs:432-438`.

`crates/agentd/src/exec.rs:865`

## POST `/v1/exec/{id}/kill`

```rs
pub async fn kill(State(state): State<AppState>, Path(id): Path<String>) -> Response {
```

Sends SIGTERM to an exec’s process group and escalates to SIGKILL if the group remains alive after the grace period. `crates/agentd/src/exec.rs:1659-1677`.

**Input:** The `id` path segment; no request body. `crates/agentd/src/exec.rs:939`.

**Output:** JSON `KillResponse` with `exec_id: String` and `killed: bool`; false indicates that no signal was delivered because the group had already exited. `crates/protocol/src/exec.rs:386-391`.

`crates/agentd/src/exec.rs:939`

## POST `/v1/exec/{id}/stdin`

```rs
pub async fn write_stdin(
    State(state): State<AppState>,
    Path(id): Path<String>,
    body: Result<Json<StdinRequest>, JsonRejection>,
) -> Response {
```

Writes bytes to an exec’s stdin and optionally closes the pipe with EOF. `crates/agentd/src/routes.rs:646-656`.

**Input:** The `id` path segment and JSON `StdinRequest` with optional `data_b64: String` and `signal: String`; `"eof"` closes the pipe after any supplied bytes. `crates/protocol/src/exec.rs:308-317`.

**Output:** JSON `StdinResponse` with `exec_id: String`, `written: usize`, and `eof: bool`. `crates/protocol/src/exec.rs:325-329`.

`crates/agentd/src/exec.rs:716`

## PUT /v1/fs/file

```rs
pub async fn write_file(State(state): State<AppState>, request: Request) -> Response {
```

Streams a request body into a file and applies an optional file mode. `crates/agentd/src/fs.rs:1222-1246`, `crates/agentd/src/fs.rs:1265-1331`.

**Input:** Query `FsQuery` with required string `path` and optional octal string `mode`, plus an `application/octet-stream` body. `crates/protocol/src/fs.rs:18-24`, `crates/agentd/src/routes.rs:747-757`.

**Output:** HTTP 204 with no body after a successful write. `crates/agentd/src/fs.rs:1331`.

`crates/agentd/src/fs.rs:1222`

## PUT /v1/fs/tar

```rs
pub async fn write_tar(State(state): State<AppState>, request: Request) -> Response {
```

Uploads and extracts an uncompressed tar archive into the requested directory. `crates/agentd/src/routes.rs:767-782`.

**Input:** Query `FsQuery` with an absolute string `path` and an `application/x-tar` body; the shared query type also includes `mode`, which extraction does not use. `crates/protocol/src/fs.rs:18-24`, `crates/agentd/src/fs.rs:1473-1499`.

**Output:** HTTP 204 with no body after extraction completes. `crates/agentd/src/fs.rs:1519-1534`.

`crates/agentd/src/fs.rs:1473`

## See also

These pages cite many of the same source files as this one.

- [module map](../architecture/module-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [processes](../behavior/processes.md)
- [business logic](../insights/business-logic.md)
- [debugging guide](../insights/debugging-guide.md)
