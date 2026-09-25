# microvms-agentd · RPC tools

The daemon's callable surface is HTTP, not MCP, gRPC, or JSON-RPC: there is no `.proto` file in the tree, and the only production router construction is the pair of `Router::new()` calls in `agentd/src/routes.rs:48-49`. The named unit a caller invokes is therefore a method-and-path pair, and this file has one H2 per pair, alphabetized on that string.

Twenty pairs exist, and the roster is closed by construction rather than by convention. One list, `surface_docs()`, is walked twice: once to build the router and once to serve `GET /v1/schema` (`agentd/src/routes.rs:443-752`, `agentd/src/routes.rs:51-59`, `agentd/src/routes.rs:433-435`). A path absent from that list is unroutable, and a path present in it with no arm in `handler_for` panics at startup rather than answering 404 to a documented route (`agentd/src/routes.rs:140`).

**Auth is per endpoint and takes three values.** `Bearer` endpoints sit behind `auth::require_token`, applied with `route_layer` so an unmatched path still falls through to the 404 fallback instead of being answered 401 (`agentd/src/routes.rs:66-69`). `Open` and `PlatformHook` endpoints go on an unguarded router (`agentd/src/routes.rs:53-58`). `PlatformHook` is unauthenticated because the platform holds no credential to present, and its request arrives over loopback indistinguishably from an in-VM process (`agentd/src/routes.rs:39-42`, `agentd/src/routes.rs:168-172`); the defense for `/run` is that it can succeed only once, not that the caller is identified. The auth middleware answers 503 when no token is installed at all, 503 when a token is presented while none is installed, and 401 when a presented token mismatches (`agentd/src/auth.rs:69-80`).

Every response on every endpoint carries the `microvms-agentd-version` header, stamped by a layer applied outside `route_layer` so it also covers the 401, 503, 413, and 404 that no handler produced (`protocol/src/lib.rs:68`, `agentd/src/routes.rs:85`). The protocol version is `1` and tracks the `/v1/` namespace rather than the crate version (`protocol/src/lib.rs:60`).

Failing `exec` endpoints return `ErrorBody { error, detail }`, where `error` is one of a closed set of slugs a client branches on and `detail` is prose for a log (`protocol/src/exec.rs:437-440`, `protocol/src/exec.rs:457-487`). Failing `fs` endpoints answer `text/plain` instead, because their bodies are opaque byte streams and there is no typed body module for them (`protocol/src/fs.rs:4-6`).

One place where `docs/PROTOCOL.md`, the hand-written contract, disagrees with the source and with the generated `docs/schema.json`:

- It describes `POST HOOKS/resume` as signalling in-memory state loss (`docs/PROTOCOL.md:19`). The source records the opposite as a dated measurement and records that the state-loss claim was inferred rather than measured (`agentd/src/routes.rs:309-323`). This file follows the measurement.

Its route table used to omit `GET /v1/schema` and `GET /v1/tcp`; both rows are present now, beside the `GET /v1/procs` row added with them.

## GET `/v1/exec/{id}`

```rs
pub async fn poll(State(state): State<AppState>, Path(id): Path<String>) -> Response {
```

Polls one exec's phase and, once the child has exited and before an ack, its captured output and exit status (`agentd/src/routes.rs:556-557`).

**Auth:** Bearer (`agentd/src/routes.rs:555`).

**Input:** the `{id}` path segment only, per the signature at `agentd/src/exec.rs:429`. No body, no query.

**Output:** `application/json` body `PollResponse { exec_id: String, phase: Phase, result: Option<Outcome> }`, where `result` is `#[serde(flatten)]` plus `skip_serializing_if = "Option::is_none"`, so a running exec serializes as `{"exec_id":"e1","phase":"running"}` and an exited one inlines `Outcome { exit_code: Option<i32>, signal: Option<i32>, timed_out: bool, stdout: String, stderr: String, truncated: bool, writers_may_be_alive: bool }` at the top level (`protocol/src/exec.rs:422-428`, `protocol/src/exec.rs:58-78`). `Phase` is `running` | `exited` | `acked` (`protocol/src/exec.rs:24-31`).

**Statuses:** 200; 401; 503; 404 `unknown_exec` (`agentd/src/schema.rs:439-449`).

Strictly read-only — nothing in the handler may write to the registry or an entry, and the `model/` crate asserts that against the transition function rather than against reachable states (`agentd/src/exec.rs:424-428`).

`agentd/src/exec.rs:429`

## GET `/v1/exec/{id}/stream`

```rs
pub async fn stream(
    State(state): State<AppState>,
    Path(id): Path<String>,
    query: Result<Query<StreamQuery>, QueryRejection>,
) -> Response {
```

Follows an exec's output as Server-Sent Events, replaying from a byte offset and then tracking live output (`agentd/src/routes.rs:575`).

**Auth:** Bearer (`agentd/src/routes.rs:574`).

**Input:** the `{id}` path segment plus `StreamQuery { offset: Option<u64> }` as `application/x-www-form-urlencoded` query; absent `offset` means 0, that is everything still inside the replay window (`protocol/src/exec.rs:323-328`).

**Output:** `text/event-stream` carrying three typed events, all `data:` JSON — `output` = `OutputEvent { offset: u64, stream: StreamKind, output: String }` with `output` base64-encoded and `stream` one of `stdout` | `stderr`; `gap` = `GapEvent { from: u64, to: u64 }`; `exit` = `ExitEvent { exit_code: Option<i32>, signal: Option<i32>, timed_out: bool, truncated: bool, writers_may_be_alive: bool, offset: u64 }` (`protocol/src/exec.rs:326-360`, `protocol/src/exec.rs:84-87`, event names at `protocol/src/exec.rs:448-450`).

**Statuses:** 200; 400 `malformed_request` when `offset` is not a non-negative integer; 401; 503; 404 `unknown_exec` (`agentd/src/schema.rs:451-466`).

The next offset to resume from is a chunk's `offset` plus the length of its decoded bytes. `exit` is terminal and is emitted before the body ends, so a body that closes without it means the connection failed rather than the command finishing — that distinction is why this is SSE and not a chunked byte stream (`agentd/src/routes.rs:754-781`). A `gap` reports bytes that are genuinely gone, because a client that cannot tell missing output from no output reads a truncated log as a complete one.

`agentd/src/exec.rs:477`

## GET /v1/fs/file

```rs
pub async fn read_file(request: Request) -> Response {
```

Reads one file, or a 1-based inclusive line range of it, streamed rather than buffered (`agentd/src/routes.rs:669-674`).

**Auth:** Bearer (`agentd/src/routes.rs:668`).

**Input:** `FileReadQuery { path: String, start_line: Option<u64>, end_line: Option<u64> }` as query string; `path` is required, both bounds are 1-based and inclusive, absent `start_line` means 1 and absent `end_line` means through EOF (`protocol/src/fs.rs:39-57`). The handler takes a bare `axum::extract::Request` and parses the query itself via `file_read_query` (`agentd/src/fs.rs:1119`), so the typed shape lives on the `surface_docs()` row rather than in the signature (`agentd/src/routes.rs:663`).

**Output:** `application/octet-stream` — the file's bytes, or the requested window of them (`agentd/src/routes.rs:664`). No JSON envelope.

**Statuses:** 200; 400 for a missing `path`, a `path` naming a directory, a non-integer bound, `start_line=0`, or `end_line` before `start_line`; 401; 503; 404 only when the path is genuinely absent; 500 when the file cannot be opened or stat'ed (`agentd/src/schema.rs:561-592`).

An `end_line` past the last line reads through EOF with a 200 rather than a 416, because these are the AI SDK harness's `readTextFile` semantics and this route is what that method is built on (`protocol/src/fs.rs:34-37`). This is the one place in the fs surface where a client's `FileNotFoundError` is the right mapping of a 404; every protocol error above is a 400 for that reason (`agentd/src/schema.rs:581-586`, `protocol/src/fs.rs:13-16`). A range still streams — the read filters chunk by chunk and stops once the window closes, so nothing buffers a file to slice it (`agentd/src/fs.rs:1112-1117`).

`agentd/src/fs.rs:1118`

## GET /v1/fs/tar

```rs
pub async fn read_tar(State(state): State<AppState>, request: Request) -> Response {
```

Downloads the tree under `?path=` as one uncompressed tar, streamed from a spool file (`agentd/src/routes.rs:698-699`).

**Auth:** Bearer (`agentd/src/routes.rs:697`).

**Input:** `FsQuery { path: String, mode: Option<String> }` as query string; only `path` is meaningful here, since `mode` is a property of a write (`protocol/src/fs.rs:18-24`). Parsed by `fs_query` inside the handler (`agentd/src/fs.rs:1397`).

**Output:** `application/x-tar` — uncompressed, streamed from a spool file (`agentd/src/routes.rs:693`, `agentd/src/fs.rs:1446-1452`).

**Statuses:** 200; 400 for a missing `path` or one that is not a directory; 401; 503; 404 when the directory is genuinely absent; 413 when the tree exceeds `limits.max_tar_members` or `limits.max_tar_bytes`, measured by a walk before anything is allocated; 500 when the tree cannot be walked or packed (`agentd/src/schema.rs:614-645`).

Symlinks are packed as symlinks rather than followed, which is the producing half of the contract `PUT /v1/fs/tar` implements on the consuming half; following them would silently change what a round trip means (`agentd/src/fs.rs:1381-1384`).

`agentd/src/fs.rs:1396`

## GET /v1/health

```rs
async fn health(State(state): State<AppState>) -> Json<Health> {
```

Reports liveness, daemon version, bootstrap state, disk headroom, identity-repair status, and whether any exec is still producing output (`agentd/src/routes.rs:725-737`).

**Auth:** Open — unauthenticated, and deliberately so, since `bootstrapped` is how a client learns whether the control API is open yet (`agentd/src/routes.rs:724`, `agentd/src/schema.rs:673-682`).

**Input:** none. No body, no query, no path parameters, per the signature at `agentd/src/routes.rs:373`.

**Output:** `application/json` body `Health { version: Cow<'static, str>, bootstrapped: bool, disk: Option<DiskHealth>, identity_degraded: bool, identity_repaired: bool, busy: bool, execs: usize, hooks: Vec<HookObservation>, hooks_dropped: u64, identity_steps: Vec<IdentityStep>, image_env_keys: Option<usize> }`, with `DiskHealth { available_bytes: u64, reserve_bytes: u64, under_pressure: bool }`, `HookObservation { hook: String, fired_at: u64, handler: Option<HandlerOutcome> }`, `HandlerOutcome { exit_code: Option<i32>, signal: Option<i32>, timed_out: bool, duration_ms: u64, error: Option<String> }`, and `IdentityStep { name: String, outcome: String, error: Option<String> }` (`protocol/src/health.rs:11-142`, `protocol/src/health.rs:208-217`, `protocol/src/health.rs:190-204`, `protocol/src/health.rs:162-176`, `protocol/src/health.rs:146-154`).

**Statuses:** 200, always, bootstrapped or not (`agentd/src/schema.rs:673-682`).

`image_env_keys` is how many variables the daemon's image-environment snapshot holds (the environment it inherited as the container `CMD`, minus every `AGENTD_*` variable, taken once at startup), and a count rather than the values because this route answers without the agent token; `null` is also what a daemon that predates `inherit_image_env` reads as, which is how a caller tells whether that flag will be honored (`protocol/src/health.rs:128-141`). `disk: null` is distinct from zero: unmeasurable free space is not a full disk, and a monitor conflating them would page on a missing `statvfs` (`protocol/src/health.rs:31-33`). `busy`, `execs`, `hooks`, `hooks_dropped`, `identity_steps`, and `image_env_keys` are the only fields carrying `#[serde(default)]`, because the daemon is baked into an image while a client is installed separately, so a required field would make a health call fail outright against an older daemon — turning a missing signal into an unreachable VM (`protocol/src/health.rs:68-74`). `busy` means producing, not unfinished: an exec waiting to be acked is not busy, so `busy: false` with a non-zero `execs` is a VM holding unacked output somebody still has to collect (`protocol/src/health.rs:63-67`). The field lives here rather than on a guest-callable keepalive route because the platform measures idleness at an endpoint proxy that terminates outside the guest, so in-guest traffic cannot reset the idle timer (`agentd/src/routes.rs:356-372`).

`agentd/src/routes.rs:373`

## GET /v1/procs

```rs
pub async fn procs(State(state): State<AppState>) -> Response {
```

Process accounting: every registered exec with its process group's live pids, read from `/proc` inside the guest so the image needs no `ps` (`agentd/src/exec.rs`, `procs`; `agentd/src/routes.rs`, the `/v1/procs` row).

**Auth:** Bearer. The answer names every pid the daemon spawned, and a caller without the token has no business with it.

**Input:** none. No body, no query, no path parameters.

**Output:** `application/json` body `ProcsResponse { procs: Vec<ProcGroup> }` with `ProcGroup { exec_id: String, pgid: Option<u32>, started_at: u64, child_exited: bool, reap: bool, pids: Vec<u32> }` (`protocol/src/exec.rs`). One entry per registered exec in any phase, registry order. `pgid` is `null` when the child was reaped before `Child::id()` answered, and such an entry lists no pids. `started_at` is epoch seconds on the daemon's clock, the same convention as a hook observation's `fired_at`. `pids` are the live pids whose pgrp — field 2 after the closing `)` of `/proc/<pid>/stat` — equals `pgid`; zombies (`Z`) are not live and are not listed.

**Statuses:** 200; 401; 503 (`agentd/src/schema.rs`, `PROCS`).

`child_exited` reads the write-once terminal marker rather than the polled result, for the reason `activity` gives: an ack takes the result, so a handler reading it would report every acked exec as running. The `/proc` walk runs on the blocking pool — one `read_dir` plus one small read per live pid — because it is synchronous filesystem I/O on a current-thread runtime. The row this route exists for is `child_exited: true` beside a non-empty `pids`: a command that finished while something it backgrounded did not (issue #157), whose `exec_id` is what `POST /v1/exec/{id}/kill` takes. `reap` echoes the start request's `reap_group_on_exit`, the opt-in under which the daemon escalates the group itself when the child exits.

`agentd/src/exec.rs`

## GET /v1/schema

```rs
async fn schema_route(State(state): State<AppState>) -> Json<serde_json::Value> {
```

Serves the machine-readable wire contract: every route, shape, status code, and operative limit (`agentd/src/routes.rs:747`).

**Auth:** Open (`agentd/src/routes.rs:746`) — a client needs the contract before it holds a token, since the token arrives at the platform's `/run` hook, and gating the document would make version negotiation impossible during exactly the window it matters (`agentd/src/routes.rs:418-432`).

**Input:** none, per the signature at `agentd/src/routes.rs:433`.

**Output:** the schema document, built by `schema::document(state.config(), &surface_docs())` (`agentd/src/routes.rs:434`). Its top-level keys are `$defs`, `$schema`, `auth`, `daemon_version`, `definition_collisions`, `generated_from`, `hook_prefix`, `limits`, `protocol_version`, `routes`, `title`, `unmatched_path`, `version_header`, and the committed copy is `docs/schema.json`, regenerated and compared by the `schema:check` task (`mise.toml:181-186`) and by a test tier that also drives every documented route through the real router to confirm the daemon still answers it (`agentd/tests/schema_artifact.rs:1-12`).

**Statuses:** 200 (`agentd/src/schema.rs:712-717`).

Nothing here is secret. Every path, shape, and status code is in the published repository, and the limits are the operator's own configuration; the one sensitive fact — whether a token is installed — lives on `GET /v1/health` instead (`agentd/src/routes.rs:428-432`). Note a divergence internal to the source: the `surface_docs()` row declares the response as `octet_stream("this document")`, so `docs/schema.json` publishes `application/octet-stream`, while the handler returns `Json<serde_json::Value>`, which axum serves as `application/json` (`agentd/src/routes.rs:742` against `agentd/src/routes.rs:433-435`).

`agentd/src/routes.rs:433`

## GET /v1/tcp

```rs
pub async fn open(
    State(state): State<AppState>,
    Query(query): Query<TunnelQuery>,
    upgrade: WebSocketUpgrade,
) -> Response {
```

Upgrades to a WebSocket relayed to `127.0.0.1:<port>` inside the guest, so a caller outside the VM can speak an arbitrary TCP protocol to a server inside it (`agentd/src/tunnel.rs:126`, `agentd/src/routes.rs`, the `/v1/tcp` row).

**Auth:** Bearer.

**Input:** `TunnelQuery { port: u16, identity: bool }` as query string (`agentd/src/tunnel.rs`). `port` is required; `identity=1` asks for the Noise KK handshake against the per-VM key delivered in the launch payload before the dial.

**Output:** a `101 Switching Protocols` and then binary WebSocket frames in both directions, each frame a byte range rather than a message — TCP has no message boundaries and neither does this. No JSON body on any path.

**Statuses:** 101; 400 `malformed_request` when `?port` is absent or not a `u16`; 401; 503 (`agentd/src/schema.rs`, `TUNNEL_OPEN`). The real outcomes are **close codes**, because a WebSocket route leaves HTTP behind after its 101: 4502 nothing listening on the port, 4400 port 0, 4500 a mid-relay failure, 4401 this VM was launched without an identity seed, 4403 the handshake was refused.

The dial is loopback-only and never resolves a name — a relay that could reach another host would be an open proxy inside the VM reachable with the agent token. One connection per WebSocket, deliberately: multiplexing would reimplement per-stream ids, flow control, and close handshakes the platform already provides per connection. The dial happens after the upgrade so a refused dial arrives as a close code with a reason; on the endpoint path every WebSocket failure a caller can observe would otherwise be a 1006 with no reason. Rests on a measured platform property: binary frames survive a port-scoped token byte-exact (`docs/PLATFORM.md`, 2026-08-29).

`agentd/src/tunnel.rs:126`

## POST /aws/lambda-microvms/runtime/v1/ready

```rs
async fn ready_hook(State(state): State<AppState>) -> StatusCode {
```

Answers the platform's image-build readiness probe (`agentd/src/routes.rs:477-479`).

**Auth:** PlatformHook (`agentd/src/routes.rs:476`) — unauthenticated; the prefix is fixed by the service and cannot be renamed or moved under `/v1` (`protocol/src/hook.rs:15`, `agentd/src/routes.rs:39-42`).

**Input:** none on the wire. The handler's only extractor is `State<AppState>`, used to record the firing that `GET /v1/health` reports under `hooks`, per the signature at `agentd/src/routes.rs:282`.

**Output:** a bare `StatusCode`, no body, per the signature at `agentd/src/routes.rs:282`.

**Statuses:** 200 only. The platform terminates the VM on any other status, so there is no failure a hook can usefully report (`agentd/src/schema.rs:719-724`).

Called during the image build, before any instance exists and therefore before any token has been delivered, so 200 is correct even with the control API closed: the question is whether the daemon started, not whether it is bootstrapped. Gating this on bootstrap state would fail every build (`agentd/src/routes.rs:275-281`).

`agentd/src/routes.rs:282`

## POST /aws/lambda-microvms/runtime/v1/resume

```rs
async fn resume_hook(State(state): State<AppState>) -> StatusCode {
```

Acknowledges a resume from suspension, logging loudly if the bootstrap state did not survive (`agentd/src/routes.rs:525-528`).

**Auth:** PlatformHook (`agentd/src/routes.rs:524`).

**Input:** none beyond the injected `AppState`, per the signature at `agentd/src/routes.rs:328`.

**Output:** a bare `StatusCode`, no body, per the signature at `agentd/src/routes.rs:328`.

**Statuses:** 200 only (`agentd/src/schema.rs:719-724`).

Suspend is a freeze and restore, not a stop and start. Measured 2026-08-05 in us-east-1: the in-memory agent token, the filesystem, exec records, and even backgrounded processes all survive a suspend/resume cycle, and the endpoint URL is unchanged (`agentd/src/routes.rs:308-323`). What does not survive is the guest's view of time, which the guest observes as a single jump, so any timeout, lease, or session held by a running command expires at once on resume (`agentd/src/routes.rs:321-323`). A resume that arrives without an installed token is logged as a warning naming the contradiction, not treated as routine (`agentd/src/routes.rs:333-341`). `docs/PROTOCOL.md:19` still describes this hook as signalling in-memory state loss; the source records that claim as inferred rather than measured, and wrong (`agentd/src/routes.rs:316-319`).

`agentd/src/routes.rs:328`

## POST /aws/lambda-microvms/runtime/v1/run

```rs
async fn run_hook(
    State(state): State<AppState>,
    body: Result<Json<RunHookEnvelope>, JsonRejection>,
) -> Response {
```

Installs the per-VM agent token once, plus the optional launch environment that becomes the base environment of every later exec (`agentd/src/routes.rs:497-510`).

**Auth:** PlatformHook (`agentd/src/routes.rs:496`) — unauthenticated because the platform has no credential to present; the defense is that this route can succeed only once (`agentd/src/routes.rs:168-173`).

**Input:** `application/json` body `RunHookEnvelope { run_hook_payload: Option<String> }`, serialized under the platform's own camelCase key `runHookPayload` (`protocol/src/hook.rs:27-30`). The caller's own JSON is one `serde_json` parse deeper inside that string, and parses to `RunHook { agent_token: String, env: HashMap<String, String>, identity_seed: Option<String>, identity_host_public_key: Option<String> }` (`protocol/src/hook.rs:46-72`): `{"runHookPayload": "{\"agent_token\": \"...\", \"env\": {\"KEY\": \"VALUE\"}}"}`. `agent_token` is required and non-empty; `env` is optional, values must be strings; the two `identity_*` halves are optional and must be strings when present; unknown keys are ignored (`protocol/src/hook.rs:145-189`).

**Output:** a bare status, no body on success; on refusal, `text/plain` naming the problem (`agentd/src/routes.rs:216`).

**Statuses:** 200 on install or on replay of an identical token; 400 on a body that is not JSON, a missing `runHookPayload`, a payload that is not a JSON object, an absent or non-string or empty `agent_token`, a non-object `env`, or a non-string `env` value; 409 when a different token is already installed (`agentd/src/schema.rs:726-755`).

Bootstrap is one-shot, and an identical replay is 200 because the platform may retry its own hook and a 409 there would fail a launch that is fine (`agentd/src/routes.rs:262-267`). The refusal body names which key or shape was wrong and never quotes a value, which is why `RunHookError` is a typed enum rather than serde's own message — serde quotes the value it rejected, and the value beside a bad `env` is a credential (`protocol/src/hook.rs:74-99`, `protocol/src/hook.rs:142-144`). An unknown key is ignored rather than refused, because any 400 here makes the platform terminate the VM before forwarding traffic, so a newer client's unrecognised field must not kill the launch (`protocol/src/hook.rs:134-140`). The token never becomes part of the launch environment: `AppState::bootstrap` takes the two as separate arguments precisely so no code path can move one into the other (`agentd/src/routes.rs:175-179`, `agentd/src/routes.rs:236`). The token and the `env` share one 4096-byte platform budget measured in UTF-8 bytes, enforced client-side in `microvms-core` before the call (`agentd/src/routes.rs:506-510`).

`agentd/src/routes.rs:180`

## POST /aws/lambda-microvms/runtime/v1/suspend

```rs
async fn suspend_hook(State(state): State<AppState>) -> StatusCode {
```

Acknowledges and logs an incoming suspend (`agentd/src/routes.rs:518`).

**Auth:** PlatformHook (`agentd/src/routes.rs:517`).

**Input:** none, per the signature at `agentd/src/routes.rs:301`.

**Output:** a bare `StatusCode`, no body, per the signature at `agentd/src/routes.rs:301`.

**Statuses:** 200 only (`agentd/src/schema.rs:719-724`).

`agentd/src/routes.rs:301`

## POST /aws/lambda-microvms/runtime/v1/terminate

```rs
async fn terminate_hook(State(state): State<AppState>) -> StatusCode {
```

Acknowledges termination, which begins graceful shutdown with in-flight requests draining (`agentd/src/routes.rs:535`).

**Auth:** PlatformHook (`agentd/src/routes.rs:534`).

**Input:** none, per the signature at `agentd/src/routes.rs:346`.

**Output:** a bare `StatusCode`, no body, per the signature at `agentd/src/routes.rs:346`.

**Statuses:** 200 only (`agentd/src/schema.rs:719-724`).

The draining behavior is the `surface_docs()` row's documented contract for this hook (`agentd/src/routes.rs:531-537`).

`agentd/src/routes.rs:346`

## POST /aws/lambda-microvms/runtime/v1/validate

```rs
async fn validate_hook(State(state): State<AppState>) -> StatusCode {
```

Answers the platform's image-build validation probe (`agentd/src/routes.rs:486-488`).

**Auth:** PlatformHook (`agentd/src/routes.rs:485`).

**Input:** none on the wire; the handler takes `State<AppState>` to record the firing, per the signature at `agentd/src/routes.rs:293`.

**Output:** a bare `StatusCode`, no body, per the signature at `agentd/src/routes.rs:293`.

**Statuses:** 200 only (`agentd/src/schema.rs:719-724`).

Like `ready`, this is an image-build hook rather than an instance hook: the build calls it to decide whether the snapshot it just produced is usable, so a daemon that omits it fails the build rather than the run — a confusing place to discover the omission (`agentd/src/routes.rs:44-47`, `agentd/src/routes.rs:292`).

`agentd/src/routes.rs:293`

## POST /v1/exec/start

```rs
pub async fn start(
    State(state): State<AppState>,
    body: Result<Json<StartRequest>, JsonRejection>,
) -> Response {
```

Starts a command under a caller-minted `exec_id`, idempotently on that id (`agentd/src/routes.rs:545-546`).

**Auth:** Bearer (`agentd/src/routes.rs:544`).

**Input:** `application/json` body `StartRequest { exec_id: String, command: Vec<String>, shell: Shell, cwd: Option<String>, env: HashMap<String, String>, user: Option<NameOrId>, group: Option<NameOrId>, timeout_sec: Option<f64>, stdin: bool, reap_group_on_exit: bool, inherit_image_env: bool }`, with the untagged unions `NameOrId = Id(u32) | Name(String)` (a JSON integer or string) and `Shell = Flag(bool) | Named(String)` (a JSON boolean or string). Every field after `command` carries `#[serde(default)]`, so a request may be as small as `{"exec_id":"e1","command":["true"]}` (`StartRequest` at `protocol/src/exec.rs:217-289`, `NameOrId` at `protocol/src/exec.rs:117-124`, `Shell` at `protocol/src/exec.rs:174-182`). Resolution of names and the shell, and the environment layering `inherit_image_env` opts into, are in `agentd/src/exec_start.rs` and `docs/PROTOCOL.md`, "Naming the user, group and shell".

**Output:** `application/json` body `StartResponse { exec_id: String, phase: Phase }` (`protocol/src/exec.rs:362-365`).

**Statuses:** 200 on start or on a retry of an already-started `exec_id`; 400 `malformed_request` when the body is invalid, `exec_id` is empty, `timeout_sec` is not a positive finite number, `command` is empty with `shell` false, or `shell` is an empty string; 400 `unknown_user`, `unknown_group` or `unknown_shell` when a name or shell does not resolve in the guest, with nothing spawned and `detail` naming the value; 401; 503; 413 when the body exceeds `limits.max_body_bytes`; 500 `spawn_failed` when the child cannot be spawned, deliberately not 404 (`agentd/src/schema.rs:395-437`).

`command` is argv when `shell` is false and a single script string when it is true or names a shell (`protocol/src/exec.rs:215-223`). Omitting `cwd` means the child inherits the daemon's own working directory, which is the image `WORKDIR` (`protocol/src/exec.rs:228-230`). `stdin` is opt-in rather than always-on: a child holding an open stdin pipe nobody will write to blocks forever the first time it reads, and `/bin/sh`, `git`, and any tool probing for input all behave differently against a pipe than against `/dev/null` (`protocol/src/exec.rs:252-262`). `timeout_sec` is validated before the child spawns, because the predecessor raised inside the waiter thread by which point the child was already running and became an orphan (`protocol/src/exec.rs:247-249`).

`agentd/src/exec.rs:341`

## POST `/v1/exec/{id}/ack`

```rs
pub async fn ack(State(state): State<AppState>, Path(id): Path<String>) -> Response {
```

Releases an exited exec's buffered output to the caller and starts its TTL collection clock (`agentd/src/routes.rs:600-601`).

**Auth:** Bearer (`agentd/src/routes.rs:599`).

**Input:** the `{id}` path segment only, per the signature at `agentd/src/exec.rs:855`. No body.

**Output:** `application/json` body `PollResponse { exec_id: String, phase: Phase, result: Option<Outcome> }` — the same shape `GET /v1/exec/{id}` returns (`protocol/src/exec.rs:422-428`, `agentd/src/routes.rs:595`).

**Statuses:** 200; 401; 503; 404 `unknown_exec`; 409 `still_running`; 409 `already_acked` (`agentd/src/schema.rs:514-536`).

This is the only way output leaves the daemon's custody, and only acked entries are ever collected, so output nobody read is never destroyed (`agentd/src/routes.rs:600-601`, `agentd/src/schema.rs:516-519`). Acking a still-running exec is 409 rather than a silent success, because succeeding would drop output still being written — which is precisely what the Python predecessor's unlink-on-exit did (`agentd/src/exec.rs:850-854`). A duplicate ack is 409 `already_acked` rather than a 200 with an empty body, since an empty 200 reads as "the command produced no output" (`agentd/src/schema.rs:530-535`).

`agentd/src/exec.rs:855`

## POST `/v1/exec/{id}/kill`

```rs
pub async fn kill(State(state): State<AppState>, Path(id): Path<String>) -> Response {
```

Escalates SIGTERM then SIGKILL to the exec's whole process group (`agentd/src/routes.rs:611-613`).

**Auth:** Bearer (`agentd/src/routes.rs:610`).

**Input:** the `{id}` path segment only, per the signature at `agentd/src/exec.rs:929`. No body.

**Output:** `application/json` body `KillResponse { exec_id: String, killed: bool }`, where `killed: false` with a 200 means the process group had already exited — which is the outcome a kill was asking for (`protocol/src/exec.rs:376-381`).

**Statuses:** 200; 401; 503; 404 `unknown_exec` (`agentd/src/schema.rs:538-548`).

The signal goes to the process group and not just the direct child, because a shell that backgrounded a server leaves the interesting process outside the child pid, and `kill(child)` returned success while the workload kept running (`agentd/src/exec.rs:924-928`). `KillResponse` is a named type rather than a `serde_json::json!` literal precisely so the published schema can describe the one field a client most needs to branch on (`protocol/src/exec.rs:368-374`).

`agentd/src/exec.rs:929`

## POST `/v1/exec/{id}/stdin`

```rs
pub async fn write_stdin(
    State(state): State<AppState>,
    Path(id): Path<String>,
    body: Result<Json<StdinRequest>, JsonRejection>,
) -> Response {
```

Writes bytes to a running child's stdin, or closes the pipe with an explicit EOF (`agentd/src/routes.rs:586-590`).

**Auth:** Bearer (`agentd/src/routes.rs:585`).

**Input:** the `{id}` path segment plus `application/json` body `StdinRequest { data_b64: Option<String>, signal: Option<String> }`; both fields are `#[serde(default)]` and meaningful together, since a final chunk plus EOF in one request is the common case for feeding a prompt. `data_b64` is base64 because stdin is arbitrary bytes and a JSON string cannot carry non-UTF-8; `signal` accepts `"eof"` (`protocol/src/exec.rs:291-307`).

**Output:** `application/json` body `StdinResponse { exec_id: String, written: usize, eof: bool }`, with `eof` echoed back so a client confirms the pipe closed rather than inferring it (`protocol/src/exec.rs:315-319`, `agentd/src/schema.rs:469-474`).

**Statuses:** 200; 400 `malformed_request`; 401; 503; 404 `unknown_exec`; 408 `stdin_write_timeout`, retryable and some bytes may already have been written; 409 `stdin_not_requested` when the exec was started without `stdin: true`, fixable at start time hence 409 and not 400; 410 `stdin_closed`, since retrying will never succeed; 413 `stdin_write_too_large` against `limits.max_stdin_write_bytes`; 500 `stdin_write_failed` (`agentd/src/schema.rs:468-512`).

A separate endpoint from the output stream on purpose: multiplexing the write half onto the read connection would make a dropped attach also drop the ability to feed the process, so reconnecting becomes load-bearing for correctness rather than only for observation (`agentd/src/exec.rs:700-705`). EOF is explicit rather than inferred, because a child reading stdin cannot exit until the daemon drops its own handle (`agentd/src/routes.rs:586-590`).

`agentd/src/exec.rs:706`

## PUT /v1/fs/file

```rs
pub async fn write_file(State(state): State<AppState>, request: Request) -> Response {
```

Writes one file at `?path=`, streaming the body to disk rather than buffering it (`agentd/src/routes.rs:685-687`).

**Auth:** Bearer (`agentd/src/routes.rs:684`).

**Input:** `FsQuery { path: String, mode: Option<String> }` as query string, plus an `application/octet-stream` body of the file's bytes. `mode` is an octal file mode carried as a string so `0644` and `644` both parse and neither reads as decimal 644 (`protocol/src/fs.rs:18-24`, `agentd/src/routes.rs:679-680`).

**Output:** no body; 204 on success (`agentd/src/schema.rs:595`).

**Statuses:** 204; 400 for a missing `path` or an invalid octal `mode`; 401; 503; 413 against `limits.max_body_bytes`, enforced on the wire; 507 when the target filesystem is under the configured disk reserve; 500 when the parent cannot be created or the write or chmod fails (`agentd/src/schema.rs:594-612`).

Deliberately not confined to a root: the same token authorizes exec, so a root prefix would add no security while breaking harnesses that write to home directories and `/etc` (`agentd/src/routes.rs:685-687`). The mode is parsed before a single byte lands, so a rejected mode leaves nothing behind (`agentd/src/fs.rs:1216-1217`). 507 rather than 500 for disk pressure, because a 500 is indistinguishable from a daemon defect and a client retries it — right for a defect, actively harmful for a full disk — and rather than 413, because the write is not too large for the protocol, only for the space left (`agentd/src/schema.rs:379-387`).

`agentd/src/fs.rs:1208`

## PUT /v1/fs/tar

```rs
pub async fn write_tar(State(state): State<AppState>, request: Request) -> Response {
```

Uploads and extracts an uncompressed tar under `?path=`, confined to that root (`agentd/src/routes.rs:710-715`).

**Auth:** Bearer (`agentd/src/routes.rs:709`).

**Input:** `FsQuery { path: String, mode: Option<String> }` as query string, plus an `application/x-tar` body spooled to an unlinked temp file rather than buffered (`protocol/src/fs.rs:18-24`, `agentd/src/routes.rs:705`). `path` must be absolute: a relative root would resolve against the daemon's own working directory, which the caller cannot see (`agentd/src/fs.rs:1465-1471`).

**Output:** no body; 204 on success (`agentd/src/schema.rs:648`).

**Statuses:** 204; 400 for a missing or non-absolute `path`, a truncated body, or a member violating the extraction contract — an escaping path, an absolute or out-of-tree link target, or a device or fifo member, with the refused member's name in the body; 401; 503; 413 against `limits.max_body_bytes` on the wire or `limits.max_tar_members` / `limits.max_tar_bytes` once decoded; 507 under disk pressure; 500 on a filesystem failure (`agentd/src/schema.rs:647-671`).

This is the one confined write path in the fs surface, and the reason is that member paths come from the archive rather than from the caller (`agentd/src/fs.rs:1455-1458`). Extraction mirrors the CPython `tarfile` `data` filter: in-tree symlinks preserved, absolute link targets refused, relative targets resolved lexically so a symlink written earlier in the same archive cannot redirect a later member (`agentd/src/routes.rs:710-716`). The confinement is held by four generated properties rather than by an enumeration of known-bad strings, the first being that nothing lands outside the root — asserted by walking the disk afterwards rather than by restating which members were refused (`agentd/tests/proptest_tar.rs:1-20`).

`agentd/src/fs.rs:1459`

## See also

- [impact analysis](../insights/impact-analysis.md) — 11 shared source citations
- [contract map](../insights/contract-map.md) — 10 shared source citations
- [debugging guide](../insights/debugging-guide.md) — 7 shared source citations
- [business logic](../insights/business-logic.md) — 6 shared source citations
- [state machines](../behavior/state-machines.md) — 5 shared source citations
