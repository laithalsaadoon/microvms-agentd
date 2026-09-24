# microvms-agentd · Processes

Three initiator families drive every process here. The daemon's HTTP surface, assembled by
walking one list so a route cannot be served unless it is documented — `agentd/src/routes.rs:36`,
dispatched through the exhaustive match at `agentd/src/routes.rs:110`. The client library's public
lifecycle methods on `Sandbox` — `microvms-core/src/sandbox.rs:795`, `:814`, `:958`, `:1040`,
`:1138`. And the CLI's command handlers, dispatched from `microvms-cli/src/main.rs:67`.

The eight processes below carry the load-bearing behavior. Everything else, including the
proxy-token mint that runs inside every request and the `microvm run` command that composes four
of these processes, is listed under `## Minor flows` with its entry point.

## Image build and the stalled-build probe

Entry point: `microvms-core/src/sandbox.rs:795`

1. `Sandbox::build_image` records the requested size class and hands the request to the control plane; the local guards live one level down because the create happens *after* the caller's artifact upload — `microvms-core/src/sandbox.rs:795`.
2. `ControlPlane::create_image` runs `preflight` before its own wire call, and delegates rather than keeping a second copy of the list, so the two call sites cannot drift — `microvms-core/src/control/image.rs:179`.
3. `preflight` is the whole guard list and is callable before the upload: image name, `require_workdir` under `inherit_workdir`, and for a supplied Dockerfile the matching `FROM`, the agreeing agentd port, a keepalive under the stream idle timeout, and a `CMD` that runs the daemon — `microvms-core/src/control/image.rs:258`.
4. The wire body injects the one architecture value and derives the one accepted OS capability from a boolean, mints the `clientToken` from a label rather than accepting one, then goes out through `send_with_retry` — `microvms-core/src/control/image.rs:228`.
5. `wait_for_image` refuses an empty identifier before the loop — an empty one collapses the URI onto the collection and polls the *listing* until the deadline — then polls `GetMicrovmImage`, returning on `Image::is_ready` and raising through `build_failure` on `Image::is_failed` — `microvms-core/src/control/image.rs:368`.
6. Once elapsed time passes `WaitOpts::stall_grace` — `DEFAULT_STALL_GRACE` is 240s at `microvms-core/src/control/image.rs:37` — the wait probes exactly once, tracked by a `probed` flag rather than re-armed — `microvms-core/src/control/image.rs:397`.
7. `probe_stalled_build` returns `Ok` for an unreadable build list and `Ok` for an empty one, because neither is evidence; only an all-`PENDING` list raises `ErrorKind::BuildWedged` naming the `clientToken` replay signature — `microvms-core/src/control/image.rs:431`.
8. Back in `build_image`, `image_exists` is set before anything is launched, so a teardown can name the image whether or not a VM ever ran from it — `microvms-core/src/sandbox.rs:811`.

### Related

- `microvms-core/src/control/image.rs:484`
- `microvms-core/src/control/image.rs:567`
- `microvms-core/src/control/artifact.rs:673`
- `microvms-core/src/control/token.rs:98`
- `microvms-core/src/control/mod.rs:1072`
- `microvms-core/src/control/transport.rs:275`

## Launch and the RUNNING wait

Entry point: `microvms-core/src/sandbox.rs:899`

1. `Sandbox::run` refuses a second launch on the same sandbox before any call. This is STATE-3's local half, and the refusal is here because a retry loop around a timed-out launch is the plausible mistake — `microvms-core/src/sandbox.rs:903`.
2. The image identifier comes from the request or from a previously built image; neither is a `Precondition` error naming both remedies — `microvms-core/src/sandbox.rs:929`.
3. An agent token is minted unless the caller supplied one, then `RunHookPayload::for_launch` re-checks the 4096-byte budget even though this code built the JSON, because neither the token nor the launch env is this crate's — `microvms-core/src/sandbox.rs:961`.
4. `ControlPlane::run_microvm` closes the local guards: identifier, duration range, idle duration, pinned version, and the execution role ARN — `microvms-core/src/control/microvm.rs:433`.
5. Connector intents are split into separate ingress and egress members rather than concatenated, the combined count is checked against the `NetworkConnectorList` ceiling, and `RunMicrovm` goes out with a `clientToken` minted from a scope label — `microvms-core/src/control/microvm.rs:509`.
6. On acceptance the lifecycle moves to `Pending`, `image_exists` is set, and `suspended_window` is recorded from *this* request — the only place the value is knowable, since `GetMicrovm` does not return it — `microvms-core/src/sandbox.rs:993`.
7. `wait_for_running` delegates to `wait_for_state` with the terminal set as `fail_on`, so a VM that dies during startup raises through `reached_terminal_state` with both the state and `stateReason` — `microvms-core/src/control/microvm.rs:596`.
8. Reaching RUNNING sets `token_installed` and increments `bootstrap_count` (STATE-2, STATE-3), then a `ControlPlaneMinter` goes behind an `Arc` into `Session::builder` so minting happens inside every later request — `microvms-core/src/sandbox.rs:1063`.

### Related

- `microvms-core/src/control/microvm.rs:652`
- `microvms-core/src/control/microvm.rs:688`
- `microvms-core/src/control/microvm.rs:201`
- `microvms-core/src/sandbox.rs:1610`
- `microvms-core/src/session/mod.rs:293`
- `microvms-core/src/sandbox.rs:545`

## Suspend and resume with the launch-time window

Entry point: `microvms-core/src/sandbox.rs:1274`

1. `Sandbox::suspend` resolves the VM id through `require_microvm`, then refuses any lifecycle but RUNNING (STATE-5). Zero control-plane calls on the refusal is the observable a test asserts on — `microvms-core/src/sandbox.rs:1278`.
2. `SuspendMicrovm` goes out **first**, and only then does the lifecycle move to `Suspending` — moving before the call would leave a throttled or dropped request stuck in a state neither suspend nor resume accepts, bricking the handle over one bad request — `microvms-core/src/sandbox.rs:1292`.
3. `suspended_at` is stamped from the control plane's own clock after the call and before the wait, because the `idlePolicy` window starts when the platform begins suspending — `microvms-core/src/sandbox.rs:1297`.
4. `wait_for_state` is called with `SUSPEND_WANTED`, so TERMINATED is a wanted outcome that sets `was_terminated` rather than an error raised out of the middle of a teardown; anything else is `ErrorKind::Platform` — `microvms-core/src/sandbox.rs:1313`.
5. `Sandbox::resume` refuses a terminated VM first (STATE-11), then any lifecycle but SUSPENDED (STATE-7) — `microvms-core/src/sandbox.rs:1362`.
6. `require_open_suspended_window` compares elapsed time against the recorded window and refuses locally once it has passed; with no window recorded — the attach path — it returns `Ok`, because no default would be reliable — `microvms-core/src/sandbox.rs:1422`.
7. `ResumeMicrovm` goes out, then `wait_for_state` waits for RUNNING with `DEAD_STATES` as `fail_on`. The terminal set is wrong here because SUSPENDED, the state the call was made from, is in it — `microvms-core/src/sandbox.rs:1385`.
8. `Session::rebind` takes the endpoint the service just reported, invalidating the cached proxy token (STATE-8), and `suspended_at` is cleared so the next cycle measures its own window — `microvms-core/src/sandbox.rs:1401`.

### Related

- `microvms-core/src/control/microvm.rs:764`
- `microvms-core/src/control/microvm.rs:772`
- `microvms-core/src/session/mod.rs:395`
- `microvms-core/src/session/proxy.rs:417`
- `microvms-core/src/sandbox.rs:1568`
- `agentd/src/routes.rs:328`

## Teardown

Entry point: `microvms-core/src/sandbox.rs:1460`

1. `Sandbox::terminate` returns a `TeardownReport` rather than a `Result` and marks the sandbox torn down. It runs where a caller's `finally` would, and an error raised from there would replace the real failure — `microvms-core/src/sandbox.rs:1461`.
2. The session is dropped first, because its only remaining asset is a cached proxy token for a VM that is going away — `microvms-core/src/sandbox.rs:1466`.
3. The lifecycle moves to `Terminating` and `was_terminated` is set **before** the call, so a terminate whose call fails still blocks a later resume instead of leaving the sandbox looking resumable — `microvms-core/src/sandbox.rs:1473`.
4. `TerminateMicrovm` goes out; a failure is pushed onto both `failures` and `undeleted` rather than raised — `microvms-core/src/sandbox.rs:1476`.
5. When `wait_for_terminated` was requested, `wait_for_state(["TERMINATED"])` runs. A timeout there is recorded as a failure but **not** a leak, because the platform accepted the terminate — `microvms-core/src/sandbox.rs:1492`.
6. The image goes second, through `delete_image`'s retry loop; the identifier is checked before the loop so an invalid one costs one comparison rather than nineteen backoff sleeps, and the refusal is `false` rather than an error because this path must not raise — `microvms-core/src/control/image.rs:1165`.
7. `try_delete_image` collects **every** page of versions before the first delete, drops all but the first version, deletes the image, and parses the readback for a failure spelling — a one-page read would leave an image nothing can delete — `microvms-core/src/control/image.rs:1195`.
8. The build log group is handled **last**, and named rather than deleted: CloudWatch Logs is not in the crate's dependency set, so the group lands in `undeleted` with a failure line saying why — `microvms-core/src/sandbox.rs:1536`.

### Related

- `microvms-core/src/control/image.rs:99`
- `microvms-core/src/control/microvm.rs:786`
- `microvms-core/src/sandbox.rs:455`
- `microvms-core/src/sandbox.rs:524`
- `microvms-cli/src/commands/lifecycle.rs:1230`
- `microvms-cli/src/ledger.rs:172`

## Runtime hooks and one-shot bootstrap

Entry point: `agentd/src/routes.rs:180`

1. `ready` and `validate` answer 200 during the image build, before any instance and therefore before any token exists. Gating them on bootstrap state would fail every build — `agentd/src/routes.rs:282`.
2. Hook routes go into the unauthenticated `open` router while Bearer endpoints go into `control` behind the token guard, split at assembly by each endpoint's declared auth — the platform has no credential to present and the hook prefix is fixed by the service — `agentd/src/routes.rs:53`.
3. `run_hook` answers 400 for a body that is not JSON and 400 for an envelope carrying no `runHookPayload`. Never 404: a client that maps 404 onto "missing file" would report a phantom absent artifact for a protocol error — `agentd/src/routes.rs:189`.
4. The `runHookPayload` member is parsed a *second* time, because the platform wraps the caller's string — so `agent_token` sits one JSON parse deeper than the request body. A rejection returns the typed error's own text, since this is the one route whose failure is invisible from outside the VM — `agentd/src/routes.rs:206`.
5. `AppState::bootstrap` takes the token lock through `recover`, installs into an empty slot, or compares constant-time against what is there. The launch env is installed under the same lock so a racer that loses the token cannot win the environment — `agentd/src/state.rs:284`.
6. The three outcomes map to statuses: `Installed` and `AlreadyIdentical` are both 200 — the platform may retry its own hook, and 409 would fail a launch that is fine — while `Conflict` is 409. The log line carries the launch-env *count*, never the values — `agentd/src/routes.rs:236`.
7. `suspend`, `resume`, and `terminate` acknowledge. `resume` warns loudly when the token is absent, because measured behavior is that a suspend/resume preserves in-memory bootstrap state and exec records — `agentd/src/routes.rs:328`.
8. Every later control request runs `require_token`, which separates not-yet-bootstrapped (503) from a wrong credential (401) using `token_matches`'s three-valued answer, and drains a bounded prefix of a rejected body so hyper does not answer with a TCP RST — `agentd/src/auth.rs:69`.

### Related

- `agentd/src/routes.rs:110`
- `agentd/src/routes.rs:441`
- `agentd/src/state.rs:78`
- `agentd/src/state.rs:359`
- `agentd/src/auth.rs:92`
- `agentd/src/routes.rs:150`

## Exec start and its detached waiter

Entry point: `agentd/src/exec.rs:341`

1. `start` rejects a malformed body and an empty `exec_id` with 400, then validates `timeout_sec` before anything is spawned — validating it in the waiter left a running child with nobody to reap it — `agentd/src/exec.rs:353`.
2. `build_command` assembles the child: one joined script to `sh -c` when `shell` is set, `env_clear()` so the daemon's environment and the agent token cannot reach it, launch env then request env so the request's copy of a key wins, an omitted `cwd` inheriting rather than defaulting to `/`, `process_group(0)`, and `/dev/null` on stdin unless stdin was requested — `agentd/src/exec.rs:1185`.
3. Idempotency is decided under the registry lock before the spawn, so two concurrent retries cannot both find the slot empty; a known id returns 200 with the existing entry's phase and output untouched — `agentd/src/exec.rs:376`.
4. `spawn` starts the child and captures the pgid immediately, while `Child::id()` still answers — a lazy read would find nothing for exactly the fast-then-forking commands that most need killing — `agentd/src/exec.rs:1265`.
5. The three pipes are taken out of the `Child`, a bounded broadcast channel is created, and `Shared` plus the registry entry are built. Taking stdin out is also why the daemon must drop its own copy on EOF, since `wait()` closes only what the `Child` still owns — `agentd/src/exec.rs:1267`.
6. A detached task runs `super_wait`, whose first phase selects `child.wait()` against both pipe pumps together. Concurrent draining is required: a child filling a 64 KiB pipe buffer blocks in `write` forever if nobody reads, and the timeout branch signals the whole process group before completing the wait — `agentd/src/exec.rs:1386`.
7. Phase two lingers after the child is gone so grandchildren still holding the write end are read, setting `writers_may_be_alive` when the deadline cuts the drain short. This is the case temp files got wrong — they were unlinked here, so anything a backgrounded process wrote afterward went to a file with no name — `agentd/src/exec.rs:1425`; recorded as `.erpaval/solutions/best-practices/pipes-not-tempfiles-for-subprocess-output.md`.
8. The waiter writes the `terminal` marker **before** `result`, so a stream that sees `Finished` immediately can always find an exit event, then drops the daemon's stdin copy and sends `Frame::Finished` — `agentd/src/exec.rs:1332`.

### Related

- `agentd/src/exec.rs:1163`
- `agentd/src/exec.rs:1519`
- `agentd/src/exec.rs:1567`
- `agentd/src/exec.rs:1628`
- `agentd/src/exec.rs:969`
- `agentd/src/state.rs:412`

## Exec stream attach and byte-offset reconnect

Entry point: `agentd/src/exec.rs:471`

1. `stream` refuses a non-integer offset with 400 and an unknown id with 404, so a client can tell a protocol mistake from an absent exec — `agentd/src/exec.rs:476`.
2. `Shared::attach` subscribes to the live channel and snapshots the replay ring under one lock. `publish` holds that same lock across its broadcast send, which is what makes the pair atomic — written as two statements it is a silent one-chunk hole that only appears under load — `agentd/src/exec.rs:303`.
3. `Log::since` returns the gap the requested offset fell into rather than papering it over, plus the cursor clamped forward to the ring's start. Handing back a later window with no marker is the failure a cursorless attach has by construction — `agentd/src/exec.rs:188`.
4. The terminal marker is read **after** the snapshot, so an exec that finishes between the two is observed as finished rather than waiting forever on a `Finished` that was sent before the subscribe — `agentd/src/exec.rs:495`.
5. `Attach::take_chunk` drops the prefix already delivered, and emits a `gap` event when a live chunk lands past the cursor rather than a chunk at a discontinuous offset the client cannot reconcile — `agentd/src/exec.rs:531`.
6. The response carries `x-accel-buffering: no`, because a buffering proxy otherwise holds events until its own buffer fills, turning a live stream into a batch delivered at exit — `agentd/src/exec.rs:505`.
7. On the client, `ExecHandle::advance` advances its cursor only past bytes it handed to the consumer, and past a gap's `to` — otherwise a reconnect asks for the evicted range again and is told about the same gap forever — `microvms-core/src/session/exec.rs:529`.
8. A body that ends with no exit event becomes `Reconnect` with `attempts + 1`, backed off from a fixed table and re-attached at the cursor; because the streaming path builds its own headers, that reconnect also re-mints an expired proxy token — `microvms-core/src/session/exec.rs:572`. The cursor is the whole mechanism: `.erpaval/solutions/architecture-patterns/byte-offset-cursor-is-what-makes-reconnect-work.md`.

### Related

- `agentd/src/exec.rs:265`
- `agentd/src/exec.rs:578`
- `agentd/src/exec.rs:551`
- `microvms-core/src/session/exec.rs:594`
- `microvms-core/src/session/exec.rs:719`
- `microvms-core/src/session/exec.rs:350`

## Tar upload and confined extraction

Entry point: `agentd/src/fs.rs:1459`

1. `write_tar` reads `?path=` and refuses a relative root, which would otherwise resolve against the daemon's own working directory — the image `WORKDIR`, and not something the caller can see — `agentd/src/fs.rs:1465`.
2. The disk guard preflights the extraction root before the body is spooled, so an upload aimed at a full filesystem is refused without first spending the disk and the wire time to receive it — `agentd/src/fs.rs:1485`.
3. `spool_body` writes the archive to an unlinked spool under the same guard. Pressure becomes 507 naming the actual free space; a body that dies on the wire, including the 413 the body-limit layer injects, becomes 400 because nothing on the daemon's side failed — `agentd/src/fs.rs:1489`.
4. Extraction is handed to `spawn_blocking`, since `tar`'s reader is blocking rather than async — `agentd/src/fs.rs:1505`.
5. `Confined::open` holds the kernel's half of the confinement for the whole extraction, and ownership and xattr preservation are switched off the way CPython's `data` filter drops them — `agentd/src/fs.rs:657`.
6. Per member the PAX-aware accessors are used rather than their fixed-field header counterparts, then `resolve_member` classifies the destination as under the root, escaping, or naming the root itself — where only a directory is tolerated, since a file or link there would redirect every later member — `agentd/src/fs.rs:705`.
7. Character devices, block devices, and FIFOs are refused outright. Link targets are refused when absolute, and otherwise checked against the base depth their kind implies — a symlink resolves from its own parent, a hard link from the root — `agentd/src/fs.rs:730`.
8. Data members are counted against the byte cap, streamed through the confined descriptor, and paced against the disk after each one; directory and file modes are deferred and replayed deepest-first masked to `0o755`, because applying a `0o500` directory mode at creation blocks every write beneath it — `agentd/src/fs.rs:836`.

### Related

- `agentd/src/fs.rs:274`
- `agentd/src/fs.rs:228`
- `agentd/src/fs.rs:198`
- `agentd/src/fs.rs:851`
- `agentd/src/fs.rs:898`
- `agentd/src/fs.rs:106`

## Minor flows

- Daemon startup — entry at `agentd/src/main.rs:14`. Builds a current-thread runtime capped at four blocking threads, runs identity repair before the listener binds, spawns the 30-second exec collector, then serves.
- Startup identity repair — entry at `agentd/src/identity.rs:249`. Mints one fresh id and reuses it so the machine id and the hostname agree; without a fresh id every id-derived step is reported failed rather than silently skipped.
- Graceful shutdown — entry at `agentd/src/serve.rs:19`. Selects SIGTERM against ctrl-c and drains in-flight requests, so a harness waiting on `/v1/exec/{id}` gets its status rather than a transport error.
- Exec poll — entry at `agentd/src/exec.rs:423`. Strictly read-only with respect to the registry; an acked entry reports no output, because repeating it would contradict the phase.
- Exec stdin write — entry at `agentd/src/exec.rs:700`. A separate endpoint from the output stream on purpose: multiplexing the write half onto the read connection makes reconnecting load-bearing for correctness rather than only for observation.
- Exec ack — entry at `agentd/src/exec.rs:849`. Releases the buffered output and starts the TTL clock; `acked_at` is what separates "still running" from "an earlier ack already took it", both of which find an empty slot.
- Exec kill — entry at `agentd/src/exec.rs:923`. Signals the whole process group rather than the direct child, and answers `killed: false` rather than 500 when no pgid was ever captured.
- Expired-exec collection — entry at `agentd/src/exec.rs:969`. A plain function the daemon's own loop calls, not a task spawned per request; only acked entries are eligible, since an unacked one holds output nobody read.
- Exec activity — entry at `agentd/src/exec.rs:1015`. Reports whether anything is still producing and how many entries are registered, for an orchestrator outside the VM.
- Health — entry at `agentd/src/routes.rs:373`. Version, bootstrap state, a disk reading that is null when unmeasurable, the identity-repair verdict, and the exec-activity pair.
- Schema publication — entry at `agentd/src/routes.rs:431`. Serves the same `surface_docs()` list the router was assembled from, unauthenticated so a client can negotiate versions before it holds a token.
- Version stamping — entry at `agentd/src/routes.rs:150`. Applied outside `route_layer` so it covers handler bodies, the auth middleware's 401/503, the body-limit 413, and the 404 fallback alike.
- File read — entry at `agentd/src/fs.rs:1118`. Streams the bytes, or a 1-based inclusive line range with an `end_line` past EOF reading through rather than erroring; 404 only when the path is genuinely absent.
- File write — entry at `agentd/src/fs.rs:1208`. Not confined to a root, since the path is the caller's; the mode is parsed before a single byte lands, because validating after writing left a file behind with the wrong permissions.
- Tar download — entry at `agentd/src/fs.rs:1396`. Refuses a non-directory with 400 and an absent one with 404, estimates the tree against both caps, then packs with symlinks preserved into a rewound spool.
- Proxy-token mint in the request path — entry at `microvms-core/src/session/mod.rs:120`. `Transport::request` mints inside the path every request takes, because a token minted once at construction expires mid-run and the resulting rejection looks like a dead daemon.
- Control-plane send with retry — entry at `microvms-core/src/control/transport.rs:275`. Exponential backoff with jitter over five attempts, gated on `Error::retryable`; mutating calls are safe to retry because each carries a `clientToken`.
- Session readiness wait — entry at `microvms-core/src/session/mod.rs:437`. Polls unauthenticated health through connection errors, returns at once on a fatal one, and names the last retryable error in the timeout.
- Client-side wait-then-ack — entry at `microvms-core/src/session/exec.rs:690`. Returns the ack's own result rather than a post-ack poll, because the poll reports `acked` with no output.
- CLI dispatch — entry at `microvms-cli/src/main.rs:67`. Reads `--json` and `--dense` off the raw tokens before the parse so an argument error still produces an envelope, and returns `ExitCode` rather than calling `exit` so `Sandbox`'s drop warning still runs.
- CLI run — entry at `microvms-cli/src/commands/lifecycle.rs:489`. Build, launch, exec, report, tear down in one invocation, with the interrupt future passed in so the teardown guard is testable.
- CLI build — entry at `microvms-cli/src/commands/lifecycle.rs:1352`. Builds without launching; `--reuse` content-keys the image name by hash, because recreating an image under a previously-used fixed name can serve a stale snapshot.
- CLI attached suspend — entry at `microvms-cli/src/commands/lifecycle.rs:1964`. Spends one `GetMicrovm` to refuse locally from anything but RUNNING, which is how STATE-5's local half holds on a path that did not send the launch.
- CLI attached resume — entry at `microvms-cli/src/commands/lifecycle.rs:2034`. Skips the suspended-window check, since a process that did not send the launch cannot know `suspendedDurationSeconds`, and relies on `fail_on: DEAD_STATES` instead.
- CLI attached terminate — entry at `microvms-cli/src/commands/lifecycle.rs:2105`. VM, then image, then the log group last, and never fails on a teardown failure — it reports the identifier, which is the only remedy for a resource that would not delete.
- CLI exec — entry at `microvms-cli/src/commands/attached.rs:178`. Four shapes over one subcommand — start and wait, `--stream`, `--stdin`, `--poll` — because they are one question asked at different points in an exec's life.
- CLI exec stream — entry at `microvms-cli/src/commands/attached.rs:378`. Drives core's callback loop rather than a `Stream`, so the crate needs no `futures-util` dependency, and reports `nextOffset` from core's own cursor.
- CLI health — entry at `microvms-cli/src/commands/attached.rs:779`. Warns about a degraded identity and disk pressure on stderr while keeping exit 0, because the daemon's contract is to serve anyway and draining is the operator's decision.
- CLI ack — entry at `microvms-cli/src/commands/attached.rs:984`. Issues the ack on its own for a detached caller; both 409 shapes collapse onto `ERR_PROTOCOL` while the daemon's `still_running` or `already_acked` detail rides in the message.
- CLI stdin — entry at `microvms-cli/src/commands/attached.rs:1168`. Reads `-` from this process's stdin before writing, and surfaces the daemon's 409-versus-410 split through `data.kind`.
- CLI cp — entry at `microvms-cli/src/commands/attached.rs:1335`. Resolves direction from the `vm:` prefix and inspects no archive, so the daemon's confined extractor stays the only extractor in the system and the only one under test.
- CLI doctor — entry at `microvms-cli/src/commands/doctor.rs:32`. Region, credentials, managed bases, infra, Terraform, and the binary's ELF machine — the check that turns a host-architecture binary from a 45-minute mystery into a line of output.
- CLI ls — entry at `microvms-cli/src/commands/local.rs:52`. Reads the local ledger and lists what this CLI created and could not confirm it deleted.
- CLI logs — entry at `microvms-cli/src/commands/local.rs:696`. Derives and names the build log group and exits `ERR_PRECONDITION` with the `aws logs` invocation, because `lines: []` is the wire shape for "the group exists and is empty".
- CLI manifest — entry at `microvms-cli/src/commands/local.rs:785`. Derived from clap introspection and the exit table, so it cannot drift from what the binary accepts; a command with no response-type row fails `microvms-cli/tests/manifest.rs`.
- CLI constants — entry at `microvms-cli/src/commands/local.rs:808`. Emits `microvms_core::constants::as_json()` verbatim for comparison against the pinned botocore model, with `--emit-json` writing the bare object the drift gate reads.
- CLI dockerfile — entry at `microvms-cli/src/commands/local.rs:843`. Emits the stanza with both platform traps as comments — the `FROM` that must pair with `baseImageArn`, and the `WORKDIR` the managed al2023 base does not declare.
- CLI cost — entry at `microvms-cli/src/commands/cost.rs:27`. Renders a report from pinned rates, optionally beside the residency comparison, with unpriced line items kept distinct from zero.

## See also

- [debugging guide](../insights/debugging-guide.md) — 15 shared source citations
- [business logic](../insights/business-logic.md) — 14 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 14 shared source citations
- [data flow](../architecture/data-flow.md) — 13 shared source citations
- [sequences](../diagrams/behavioral/sequences.md) — 11 shared source citations
