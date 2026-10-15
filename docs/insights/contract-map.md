# microvms-agentd · Contract map

A contract here is a shared value shape or port that another source module interprets: domain enums, execution requests and results, and transport interfaces. Opaque service handles and simple re-exports are not included in the detailed inventory. The declaration boundaries are the domain, protocol, and application layers. `crates/microvms-domain/src/error.rs:118`, `crates/protocol/src/exec.rs:219`, `crates/microvms-app/src/control/transport.rs:248`.

Sections are ordered by distinct directly consuming production source paths found in the current tree, with related wire shapes grouped together. Test fixtures, fuzz harnesses, and re-export-only references do not contribute to ordering. Consumer citations identify imports or use sites; assumptions cite the code that interprets the shape. The source paths identify the owning layer even when adapters import the public re-exports. `crates/microvms-core/src/lib.rs:97-100`, `crates/microvms-app/src/session/mod.rs:50-61`.

## ErrorKind: coarse failure classes

**Producer:** `crates/microvms-domain/src/error.rs:117-152`

**Consumer(s):**

- `bindings/microvms-js/src/errors.rs:70`
- `bindings/microvms-js/src/provision.rs:66`
- `bindings/microvms-js/src/session.rs:38`
- `bindings/microvms-py/src/errors.rs:27`
- `bindings/microvms-py/src/sandbox.rs:48`
- `bindings/microvms-py/src/session.rs:57`
- `crates/microvms-app/src/agents/mod.rs:48`
- `crates/microvms-app/src/control/artifact.rs:30`
- `crates/microvms-app/src/control/ensure.rs:54`
- `crates/microvms-app/src/control/image.rs:30`
- `crates/microvms-app/src/control/microvm.rs:50`
- `crates/microvms-app/src/control/mod.rs:113`
- `crates/microvms-app/src/control/project.rs:10`
- `crates/microvms-app/src/control/transport.rs:36`
- `crates/microvms-app/src/entropy.rs:27`
- `crates/microvms-app/src/names.rs:18`
- `crates/microvms-app/src/sandbox.rs:72`
- `crates/microvms-app/src/session/exec.rs:39`
- `crates/microvms-app/src/session/http.rs:133`
- `crates/microvms-app/src/session/keepalive.rs:37`
- `crates/microvms-app/src/session/mod.rs:63`
- `crates/microvms-app/src/session/proxy.rs:72`
- `crates/microvms-app/src/session/sse.rs:26`
- `crates/microvms-app/src/workspace.rs:32`
- `crates/microvms-cli/src/cli.rs:800`
- `crates/microvms-cli/src/commands/agent.rs:34`
- `crates/microvms-cli/src/commands/attached.rs:41`
- `crates/microvms-cli/src/commands/lifecycle.rs:34`
- `crates/microvms-cli/src/commands/local.rs:642`
- `crates/microvms-cli/src/exit.rs:50`
- `crates/microvms-cli/src/seam.rs:49`
- `crates/microvms-domain/src/names.rs:22`
- `crates/microvms-domain/src/provision.rs:8`
- `crates/microvms-edges/src/agents/bedrock.rs:41`
- `crates/microvms-edges/src/control/project.rs:11`
- `crates/microvms-edges/src/control/services.rs:33`
- `crates/microvms-edges/src/control/transport.rs:13`
- `crates/microvms-edges/src/entropy.rs:8`
- `crates/microvms-edges/src/identity.rs:9`
- `crates/microvms-edges/src/names.rs:14`
- `crates/microvms-edges/src/provision.rs:88`
- `crates/microvms-edges/src/session/forward.rs:88`
- `crates/microvms-edges/src/session/http.rs:48`
- `crates/microvms-edges/src/session/serve.rs:38`
- `crates/microvms-edges/src/session/shell.rs:54`
- `crates/microvms-edges/src/session/tunnel.rs:64`

**Shape:**

```rust
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ErrorKind {
    /// No handler claimed this — a bug in this crate, not the platform.
    Unexpected,
    /// Refused locally, before any AWS call. Every trap closure lands here.
    InvalidArg,
    /// Transient. Run the identical request again.
    Retryable,
    /// An identity is wrong or absent; waiting will not fix it.
    Credentials,
    /// The daemon rejected the request on its merits.
    Protocol,
    /// The image build was never scheduled — the `clientToken` replay signature.
    BuildWedged,
    /// The MicroVM reached a terminal state before RUNNING; read `stateReason`.
    LaunchDied,
    /// The MicroVM ended before the resume (the suspended window, the maximum duration, or a
    /// terminate), and it can't be resumed; launch a new one. The service refused the resume,
    /// and `GetMicrovm` reports the VM TERMINATING or TERMINATED.
    WindowClosed,
    /// A control-plane failure with no more specific class.
    Platform,
    /// A client-side deadline elapsed. The VM and the exec are untouched.
    Timeout,
    /// Interrupted after launch; teardown ran and any leak is named in the payload.
    Interrupted,
    /// A prerequisite is missing.
    Precondition,
    /// The sandbox worked and the command in it exited non-zero.
    ///
    /// Its own class because it is the one failure that means nothing is wrong with
    /// the platform, the credentials, or this client — a CI caller needs to tell
    /// "your tests failed" from "we never got a VM", and one shared class cannot say
    /// both.
    ExecFailed,
}
```

**Assumptions consumers make:**

- The CLI and Python binding match every variant to their own exit or exception representation. `crates/microvms-cli/src/exit.rs:172-187`, `bindings/microvms-py/src/errors.rs:131-146`.
- The Node catalog enumerates `ALL` and derives code strings from each item; catalog membership depends on that array as well as the enum. `bindings/microvms-js/src/errors.rs:152-156`.

**Drift risk:** A variant change affects exhaustive adapters, while a catalog change affects enumeration independently. `crates/microvms-cli/src/exit.rs:172-187`, `bindings/microvms-js/src/errors.rs:152-156`.
Mitigation: review the enum, `ALL`, code strings, and adapter mappings together. `crates/microvms-domain/src/error.rs:154-204`.

## Region: endpoint and pricing selection

**Producer:** `crates/microvms-domain/src/region.rs:44-63`

**Consumer(s):**

- `bindings/microvms-js/src/agents.rs:37`
- `bindings/microvms-js/src/region.rs:27`
- `bindings/microvms-py/src/agents.rs:40`
- `bindings/microvms-py/src/control.rs:456`
- `bindings/microvms-py/src/region.rs:24`
- `bindings/microvms-py/src/sandbox.rs:48`
- `crates/microvms-app/src/adapters.rs:27`
- `crates/microvms-app/src/agents/mod.rs:49`
- `crates/microvms-app/src/agents/profile.rs:18`
- `crates/microvms-app/src/control/artifact.rs:435`
- `crates/microvms-app/src/control/connector.rs:40`
- `crates/microvms-app/src/control/mod.rs:115`
- `crates/microvms-app/src/control/transport.rs:37`
- `crates/microvms-app/src/names.rs:19`
- `crates/microvms-app/src/preflight.rs:40`
- `crates/microvms-app/src/sandbox.rs:73`
- `crates/microvms-cli/src/cli.rs:46`
- `crates/microvms-cli/src/commands/agent.rs:645`
- `crates/microvms-cli/src/commands/attached.rs:110`
- `crates/microvms-cli/src/commands/doctor.rs:17`
- `crates/microvms-cli/src/commands/mod.rs:604`
- `crates/microvms-cli/src/config.rs:463`
- `crates/microvms-cli/src/seam.rs:49`
- `crates/microvms-core/src/lib.rs:159`
- `crates/microvms-core/src/prelude.rs:36`
- `crates/microvms-domain/src/cost.rs:71`
- `crates/microvms-domain/src/names.rs:24`
- `crates/microvms-domain/src/preflight.rs:12`
- `crates/microvms-edges/src/adapters.rs:13`
- `crates/microvms-edges/src/agents/bedrock.rs:42`
- `crates/microvms-edges/src/control/services.rs:34`
- `crates/microvms-edges/src/control/transport.rs:14`

**Shape:**

```rust
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum Region {
    UsEast1,
    UsEast2,
    UsWest2,
    EuWest1,
    ApNortheast1,
    /// A region this client has not seen carry MicroVMs.
    ///
    /// **You are opting into the null-message trap.** If this region does not run
    /// MicroVMs, the first control-plane call answers `AccessDeniedException` with a
    /// null message and you will spend the next hour reading an IAM policy that is
    /// correct. Constructible only through [`Region::unlisted`], which says so at
    /// the call site.
    ///
    /// It exists because AWS adds regions faster than this list is re-read, and a
    /// client that refuses a region AWS has just launched in is its own kind of
    /// wrong. The override costs exactly the diagnostic above.
    Unlisted(String),
}
```

**Assumptions consumers make:**

- The CLI maintains an explicit mapping between its argument variants and the domain variants. `crates/microvms-cli/src/cli.rs:570-577`.
- The Python parsing entry point delegates to `FromStr`, while named constructors produce domain variants directly. `bindings/microvms-py/src/region.rs:38-81`.
- Pricing retains the selected region in its rate table and exposes it by reference. `crates/microvms-domain/src/cost.rs:846-865`.

**Drift risk:** A new domain variant requires the CLI argument mapping to change with it. `crates/microvms-cli/src/cli.rs:570-577`.
Mitigation: update the mapping and binding constructors with the domain region catalog. `crates/microvms-domain/src/region.rs:73-79`, `bindings/microvms-py/src/region.rs:38-81`.

## WireKind: specific daemon and transport outcomes

**Producer:** `crates/microvms-domain/src/error.rs:220-277`

**Consumer(s):**

- `bindings/microvms-js/src/errors.rs:162`
- `bindings/microvms-py/src/errors.rs:165`
- `crates/microvms-app/src/agents/mod.rs:539`
- `crates/microvms-app/src/control/microvm.rs:390`
- `crates/microvms-app/src/session/complete.rs:18`
- `crates/microvms-app/src/session/exec.rs:39`
- `crates/microvms-app/src/session/files.rs:18`
- `crates/microvms-app/src/session/http.rs:33`
- `crates/microvms-app/src/session/mod.rs:63`
- `crates/microvms-app/src/session/proxy.rs:72`
- `crates/microvms-app/src/session/split.rs:57`
- `crates/microvms-app/src/session/sse.rs:26`
- `crates/microvms-app/src/workspace.rs:32`
- `crates/microvms-cli/src/exit.rs:50`
- `crates/microvms-edges/src/session/http.rs:13`

**Shape:**

```rust
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum WireKind {
    /// 401: the presented bearer token is not the installed one. Fatal.
    Unauthorized,
    /// 400: malformed body, missing query key, bad mode, refused tar member.
    ///
    /// Never 404. A 404 here reads as a missing file, which is exactly how one
    /// defect hid for a review round.
    ProtocolError,
    /// 404: a genuinely absent exec id, file, or directory.
    NotFound,
    /// 409: well-formed, but the target is in the wrong state.
    Conflict,
    /// 410: stdin already saw EOF, or the child stopped reading.
    ///
    /// Distinct from [`WireKind::Conflict`], which is "you did not ask for stdin"
    /// and is fixed at start time. This one is a lifecycle fact.
    StdinClosed,
    /// 413: over a configured cap (body, tar members or bytes, stdin write).
    TooLarge,
    /// 408: the child did not drain its stdin pipe within the write timeout.
    ///
    /// Retryable, and the daemon deliberately keeps its stdin handle open so a
    /// retry can succeed. Some bytes may already have landed; reconciling that is
    /// the caller's problem, which is why it is a distinct variant.
    RequestTimeout,
    /// 503: the run hook has not landed, so the control API is closed.
    ///
    /// Not 404 and not a dropped connection. Retry: the platform is about to
    /// deliver the token.
    NotBootstrapped,
    /// 5xx other than 503 and 507: spawn failure, io failure, a panicking task.
    ServerError,
    /// 507: a write would take the target filesystem under the daemon's disk reserve.
    ///
    /// Not retryable, and that's why the daemon answers 507 rather than 500
    /// (`crates/agentd/src/fs.rs`): an identical retry against a full disk fails the
    /// same way, and a retry loop keyed on a 5xx would repeat it. The body names the
    /// free and reserved bytes. The remedy is freeing space, then retrying.
    InsufficientStorage,
    /// The request never produced a status: connection refused, reset, timeout.
    ///
    /// Retryable because it says nothing about the daemon's state. A VM that has
    /// just reached RUNNING commonly refuses a connection or two before the proxy
    /// path is wired up.
    Transport,
    /// `CreateMicrovmAuthToken` failed.
    ///
    /// Retryable, and load-bearing rather than optimistic: proxy tokens expire at
    /// 60 minutes, so a long run *will* mint mid-flight, and a throttle at that
    /// moment must not kill a trial that is otherwise healthy.
    AuthTokenMint,
    /// A client-side wait or stream deadline elapsed. The exec is untouched.
    ExecTimeout,
    /// Output bytes are gone for good — the replay ring evicted them, or this
    /// subscriber lagged the live channel.
    OutputGap,
}
```

**Assumptions consumers make:**

- An unrecognized HTTP status becomes a coarse protocol failure; the client does not guess the nearest named wire condition. `crates/microvms-app/src/session/http.rs:113-133`.
- File existence maps only `NotFound` to `false`; other failures propagate. `crates/microvms-app/src/session/files.rs:119-124`.
- Completion retries retryable failures but handles `ExecTimeout` through its deadline path. `crates/microvms-app/src/session/complete.rs:156-161`, `crates/microvms-app/src/session/complete.rs:239-256`.

**Drift risk:** Changing a status mapping can alter absence handling and retry decisions without changing request signatures. `crates/microvms-app/src/session/files.rs:119-124`, `crates/microvms-app/src/session/complete.rs:239-256`.
Mitigation: review consumers that branch on the affected variant alongside its mapping. `crates/microvms-domain/src/error.rs:324-389`.

## SizeClass: discrete resource selection

**Producer:** `crates/microvms-domain/src/sizing.rs:118-125`

**Consumer(s):**

- `bindings/microvms-js/src/cost.rs:41`
- `bindings/microvms-py/src/cost.rs:68`
- `bindings/microvms-py/src/sandbox.rs:44`
- `crates/microvms-app/src/agents/mod.rs:52`
- `crates/microvms-app/src/control/ensure.rs:55`
- `crates/microvms-app/src/control/image.rs:31`
- `crates/microvms-app/src/control/mod.rs:116`
- `crates/microvms-app/src/defaults.rs:28`
- `crates/microvms-cli/src/cli.rs:46`
- `crates/microvms-cli/src/commands/agent.rs:635`
- `crates/microvms-domain/src/cost.rs:72`

**Shape:**

```rust
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum SizeClass {
    Mib512,
    Mib1024,
    Mib2048,
    Mib4096,
    Mib8192,
}
```

**Assumptions consumers make:**

- The CLI maps its memory arguments to exact enum variants. `crates/microvms-cli/src/cli.rs:457-473`.
- The cost calculation uses baseline CPU and memory accessors. `crates/microvms-domain/src/cost.rs:1586-1605`.
- The Node binding converts JavaScript numbers before delegating class selection to the domain. `bindings/microvms-js/src/cost.rs:374-400`.

**Drift risk:** Changing the baseline meaning would change pricing and image identity for existing variants. `crates/microvms-domain/src/cost.rs:1586-1605`, `crates/microvms-app/src/control/ensure.rs:166-184`.
Mitigation: keep the resource table, conversion methods, and adapter mappings consistent. `crates/microvms-domain/src/sizing.rs:139-155`, `crates/microvms-cli/src/cli.rs:457-473`.

## StartRequest: starting an exec

**Producer:** `crates/protocol/src/exec.rs:217-299`

**Consumer(s):**

- `bindings/microvms-js/src/session.rs:292`
- `bindings/microvms-py/src/session.rs:588`
- `crates/agentd/src/exec.rs:89`
- `crates/agentd/src/exec_start.rs:63`
- `crates/agentd/src/routes.rs:594`
- `crates/microvms-app/src/agents/mod.rs:510`
- `crates/microvms-app/src/defaults.rs:37`
- `crates/microvms-app/src/session/complete.rs:98`
- `crates/microvms-app/src/session/mod.rs:461`
- `crates/microvms-app/src/workspace.rs:374`
- `crates/microvms-core/src/lib.rs:192`

**Shape:** Source documentation comments omitted.

```rust
#[derive(Debug, Deserialize, JsonSchema, Serialize)]
#[non_exhaustive]
pub struct StartRequest {
    pub exec_id: String,
    pub command: Vec<String>,
    #[serde(default)]
    pub shell: Shell,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub user: Option<NameOrId>,
    #[serde(default)]
    pub group: Option<NameOrId>,
    #[serde(default)]
    pub timeout_sec: Option<f64>,
    #[serde(default)]
    pub stdin: bool,
    #[serde(default)]
    pub reap_group_on_exit: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub poweroff_on_exit: bool,
    #[serde(default)]
    pub inherit_image_env: bool,
}
```

**Assumptions consumers make:**

- The daemon interprets command elements as an argument vector when `shell` is false and rejects an empty vector in that branch. `crates/agentd/src/exec_start.rs:434-443`.
- The Python adapter starts with `StartRequest::new` and sets fields through builders, preserving defaults for fields it does not select. `bindings/microvms-py/src/session.rs:789-814`.
- The application publishes client defaults by constructing a request with the same constructor. `crates/microvms-app/src/defaults.rs:35-53`.

**Drift risk:** A new field needs consistent constructor defaults, adapter builders, and daemon interpretation. `crates/protocol/src/exec.rs:505-551`, `bindings/microvms-py/src/session.rs:789-814`.
Mitigation: review the request declaration and all construction paths together, then regenerate the wire schema. `crates/agentd/src/routes.rs:594`.

## ExecResult: client completion state

**Producer:** `crates/microvms-app/src/session/exec.rs:72-83`

**Consumer(s):**

- `bindings/microvms-js/src/exec.rs:49`
- `bindings/microvms-py/src/exec.rs:52`
- `crates/microvms-app/src/agents/mod.rs:51`
- `crates/microvms-app/src/session/complete.rs:16`
- `crates/microvms-app/src/session/mod.rs:50`
- `crates/microvms-cli/src/commands/agent.rs:803`
- `crates/microvms-cli/src/commands/attached.rs:355`

**Shape:**

```rust
#[derive(Debug)]
pub struct ExecResult {
    pub exec_id: String,
    pub phase: protocol::exec::Phase,
    /// `None` while running. Present once the child has exited.
    pub outcome: Option<protocol::exec::Outcome>,
    /// What the client did when its own deadline expired, for a result
    /// [`super::complete::CompletionPlan::drive`] returned after one (BIND-9, BIND-10).
    /// `None` for every other result: a poll, a wait, an ack, and a run that finished in
    /// time.
    pub client_deadline: Option<super::complete::ClientDeadline>,
}
```

**Assumptions consumers make:**

- Completion accepts an acknowledgment as complete only when the phase is done and an outcome is present; otherwise it falls back to waiting. `crates/microvms-app/src/session/complete.rs:145-161`.
- The CLI renders both the raw outcome and the derived POSIX status, notes, and synthesized flag. `crates/microvms-cli/src/commands/attached.rs:704-735`.
- The Node wrapper exposes result fields through the application accessors. `bindings/microvms-js/src/exec.rs:102-126`.

**Drift risk:** A new completion condition can be lost if an adapter renders only raw exit status. `crates/microvms-cli/src/commands/attached.rs:704-735`.
Mitigation: preserve both the outcome and application-derived status accessors in each adapter. `bindings/microvms-js/src/exec.rs:102-126`.

## ExecEvent: output, gaps, and termination

**Producer:** `crates/microvms-app/src/session/sse.rs:240-257`

**Consumer(s):**

- `bindings/microvms-js/src/exec.rs:49`
- `bindings/microvms-py/src/exec.rs:52`
- `bindings/microvms-py/src/session.rs:865`
- `crates/microvms-app/src/session/complete.rs:17`
- `crates/microvms-app/src/session/exec.rs:37`
- `crates/microvms-app/src/session/split.rs:56`
- `crates/microvms-cli/src/commands/attached.rs:39`

**Shape:**

```rust
#[derive(Debug)]
pub enum ExecEvent {
    /// Output bytes, with the offset they start at.
    Output {
        stream: protocol::exec::StreamKind,
        offset: u64,
        data: Vec<u8>,
    },
    /// A byte range that is gone for good.
    ///
    /// Surfaced as a typed event rather than logged, because the alternative is reading
    /// a truncated log as a complete one. `from` is inclusive and `to` exclusive, so
    /// `to` is where a cursor resumes.
    Gap { from: u64, to: u64 },
    /// The terminal event. Its absence is what distinguishes a cut connection from a
    /// finished command — the byte sequences are otherwise identical.
    Exit(protocol::exec::ExitEvent),
}
```

**Assumptions consumers make:**

- The reconnect cursor advances to the end of delivered output bytes and past the end of a reported gap. `crates/microvms-app/src/session/exec.rs:655-687`.
- The split stream routes output by pipe, applies the chosen gap policy, and closes channels when its drive ends. `crates/microvms-app/src/session/split.rs:196-240`.
- The Node wrapper retains raw bytes and also provides text decoded with replacement for invalid UTF-8. `bindings/microvms-js/src/exec.rs:192-224`.

**Drift risk:** A new variant requires every exhaustive stream consumer to decide how it affects delivery and completion. `crates/microvms-app/src/session/split.rs:196-240`, `bindings/microvms-js/src/exec.rs:192-224`.
Mitigation: review cursor advancement, gap handling, and adapter conversion together. `crates/microvms-app/src/session/exec.rs:655-687`.

## PollResponse and Outcome: retained execution output

**Producer:** `crates/protocol/src/exec.rs:431-438`, `crates/protocol/src/exec.rs:57-78`

**Consumer(s):**

- `bindings/microvms-js/src/exec.rs:106`
- `bindings/microvms-py/src/exec.rs:91`
- `crates/agentd/src/exec.rs:88`
- `crates/agentd/src/routes.rs:608`
- `crates/microvms-app/src/session/exec.rs:85`
- `crates/microvms-cli/src/commands/attached.rs:715`

**Shape:**

```rust
#[derive(Debug, Deserialize, JsonSchema, Serialize)]
pub struct PollResponse {
    pub exec_id: String,
    pub phase: Phase,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(flatten)]
    pub result: Option<Outcome>,
}
```

```rust
#[derive(Clone, Debug, Default, Deserialize, JsonSchema, Serialize)]
pub struct Outcome {
    /// Exit code, or `None` when the child died to a signal.
    pub exit_code: Option<i32>,
    /// Signal number that killed the child, when one did.
    pub signal: Option<i32>,
    /// The daemon execution deadline fired. Absent on older daemons, which cannot
    /// distinguish deadline expiry from other signal deaths.
    #[serde(default)]
    pub timed_out: bool,
    pub stdout: String,
    pub stderr: String,
    /// Set when either stream hit `max_output_bytes` and was cut. An explicit
    /// flag rather than a sentinel string in the output: a marker inside the
    /// bytes is indistinguishable from output that happens to contain it.
    pub truncated: bool,
    /// Set when the post-exit linger deadline expired with the pipes still open,
    /// meaning some grandchild is alive and may write more that nobody will see.
    /// Reported rather than hidden, because a harness that sees empty output from
    /// a command it knows produced some needs to be able to tell why.
    pub writers_may_be_alive: bool,
}
```

**Assumptions consumers make:**

- The application renames `result` to `outcome` and sets `client_deadline` to `None` for responses read from the daemon. `crates/microvms-app/src/session/exec.rs:85-93`.
- The daemon returns no result on a later poll of an acknowledged entry; the acknowledgment response itself can carry the released outcome. `crates/agentd/src/exec.rs:452-463`, `crates/agentd/src/exec.rs:922-929`.

**Drift risk:** Adding an outcome field changes the flattened poll payload as well as the terminal outcome shape. `crates/protocol/src/exec.rs:431-438`.
Mitigation: preserve defaults for additive wire fields and review both schema and result conversion. `crates/protocol/src/exec.rs:63-66`, `crates/microvms-app/src/session/exec.rs:85-93`.

## control::Transport: control-plane calls

**Producer:** `crates/microvms-app/src/control/transport.rs:248-271`

**Consumer(s):**

- `crates/microvms-app/src/control/mod.rs:132`
- `crates/microvms-core/src/prelude.rs:29`
- `crates/microvms-edges/src/control/transport.rs:11`

**Shape:**

```rust
pub trait Transport: Send + Sync {
    /// Performs `call`, returning whatever the far side answered.
    ///
    /// A transport-level failure (no status at all) is an `Err`; a 4xx or 5xx is an
    /// `Ok(Reply)` carrying the status, because classifying a status is
    /// [`classify_failure`]'s job and a transport that classified would be a second
    /// place the taxonomy lives.
    fn send(
        &self,
        call: Call,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Reply, Error>> + Send + '_>>;

    /// Resolves the credentials the next [`Transport::send`] would sign with, sending nothing
    /// to the service (BIND-15's credentials check).
    ///
    /// Resolving may reach an SSO, credential-process, or instance-metadata source, none of
    /// which bills. The default answers `Ok`: a transport that does not sign (every scripted
    /// one) has nothing to resolve.
    fn resolve_credentials(
        &self,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), Error>> + Send + '_>> {
        Box::pin(async { Ok(()) })
    }
}
```

**Assumptions consumers make:**

- The retry policy expects a reply even for unsuccessful HTTP statuses, then classifies the status itself before deciding whether to retry. `crates/microvms-app/src/control/transport.rs:293-314`.
- The composition root supplies a shared transport to `ControlPlane::from_ports`. `crates/microvms-core/src/prelude.rs:146-157`.

**Drift risk:** A transport that classifies HTTP failures itself can bypass the application retry policy. `crates/microvms-app/src/control/transport.rs:248-258`, `crates/microvms-app/src/control/transport.rs:293-314`.
Mitigation: return raw replies for HTTP responses and reserve transport errors for failures without a response. `crates/microvms-app/src/control/transport.rs:249-254`.

## HttpBackend: guest HTTP and streaming

**Producer:** `crates/microvms-app/src/session/http.rs:156-177`

**Consumer(s):**

- `crates/microvms-app/src/session/mod.rs:163`
- `crates/microvms-edges/src/session/http.rs:20`

**Shape:**

```rust
pub trait HttpBackend: Send + Sync {
    /// Sends one request and collects the whole body, whatever the status.
    ///
    /// Deliberately does not fail on a status: a conformance suite asserts on 401 and
    /// 409 as expected outcomes, and a client that could only reach them through an
    /// error would be a client that cannot test the protocol.
    fn send(&self, request: HttpRequest) -> BoxFuture<'_, Result<HttpResponse, Error>>;

    /// Opens a streaming response, reading the status before any body byte.
    ///
    /// Status first so a 404 on an unknown exec id surfaces as `NotFound` rather than
    /// as an empty stream. `idle_timeout` bounds *silence* rather than duration: an SSE
    /// body is idle by design between chunks, so the useful bound is how long a gap may
    /// last. Without one, a half-open connection — the failure a NAT or proxy timeout
    /// produces, where no FIN ever arrives — hangs forever and the reconnect logic
    /// never runs.
    fn open_stream(
        &self,
        request: HttpRequest,
        idle_timeout: Duration,
    ) -> BoxFuture<'_, Result<OpenStream, Error>>;
}
```

**Assumptions consumers make:**

- The production backend captures the status and body without converting unsuccessful HTTP statuses into transport errors. `crates/microvms-edges/src/session/http.rs:116-136`.
- The session interprets the response status after its backend returns. `crates/microvms-app/src/session/mod.rs:163-171`.
- The streaming backend disables the overall request timeout and uses the supplied idle timeout for arriving chunks. `crates/microvms-edges/src/session/http.rs:140-179`, `crates/microvms-edges/src/session/http.rs:249-277`.

**Drift risk:** Replacing idle timeouts with total-duration timeouts can cut off an active stream. `crates/microvms-edges/src/session/http.rs:140-179`, `crates/microvms-edges/src/session/http.rs:249-277`.
Mitigation: retain separate collected-response and streaming implementations with the status available before stream consumption. `crates/microvms-app/src/session/http.rs:153-176`.

## Other contracts

- `Error` carries a coarse kind, an optional wire kind, a message, and a source chain; adapters read it through methods. `crates/microvms-domain/src/error.rs:32-109`, `bindings/microvms-py/src/errors.rs:153-168`.
- `Reply` preserves control-plane status, raw body, and the service error type for application classification. `crates/microvms-app/src/control/transport.rs:203-234`, `crates/microvms-edges/src/control/transport.rs:206-234`.
- The CLI JSON envelope has stable success and failure fields read by the conformance client. `crates/microvms-cli/src/envelope.rs:355-383`, `conformance/harness/envelope.py:46-67`.
- `TeardownReport` records cleanup outcomes and retained resource identifiers. `crates/microvms-app/src/sandbox.rs:517-550`, `crates/microvms-cli/src/commands/lifecycle.rs:1313-1333`.
- `StdinRequest` carries optional base64 bytes and a signal; the application serializes it for the daemon. `crates/protocol/src/exec.rs:307-317`, `crates/microvms-app/src/session/exec.rs:760-773`.

## See also

These pages cite many of the same source files as this one.

- [module map](../architecture/module-map.md)
- [impact analysis](impact-analysis.md)
- [processes](../behavior/processes.md)
- [business logic](business-logic.md)
- [dependency graph](../diagrams/structural/dependency-graph.md)
