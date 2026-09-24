# Embedding agentd in your own image and driving it from your own harness

The platform has no exec API: a MicroVM exposes one HTTPS endpoint and forwards
it to whatever the image's `CMD` is listening on. Every harness that wants to
run commands inside a VM therefore ships a daemon in its task image, and before
agentd each harness wrote its own — evaluation harnesses and session servers
each carry a several-hundred-line stdlib Python daemon baked into their images
(`docs/HARNESS-CAPABILITIES.md`, gap 2). agentd supersedes those daemons. This
document is the recipe for appending it to an arbitrary task image, and the
orientation a harness client needs to drive it over the published wire
protocol. The protocol itself is in `docs/PROTOCOL.md` and, machine-readably,
at `GET /v1/schema` on any running daemon; nothing here duplicates either.

## The recipe

A task that brings its own Dockerfile needs two calls to become buildable, and
no string literal of the harness's own:

```python
import microvms

task = open("environment/Dockerfile").read()
# The task text, then the agentd stanza.
dockerfile = microvms.wrap_dockerfile(task)
# The managed base, paired with the Dockerfile's own FROM.
base = microvms.BaseImage.from_dockerfile(dockerfile)
# The daemon for this client's version: fetched, verified, and cached once.
agentd_bytes = microvms.provision_agentd()

image = sandbox.build_image(
    name="my-task-image",
    binary=agentd_bytes,
    code_artifact_uri=uploaded_uri,
    build_role_arn=build_role_arn,
    base_image=base,
    dockerfile=dockerfile,
)
```

The Node binding is the same pair: `wrapDockerfile(task, { workdir })` and
`baseImageFromDockerfile(dockerfile)`, passed as `buildImage({ dockerfile,
baseImage, … })`. Both are pure functions in
`microvms-core/src/control/artifact.rs` and make no AWS call.

`wrap_dockerfile` keeps the task text verbatim and appends the stanza the
default `microvm build` bakes, rendered by the same function, so the two cannot
drift: `wrap_dockerfile("FROM x\n")` *is* the default Dockerfile for a base
whose ref is `x`. The stanza is `COPY agentd /agentd`, its chmod, `ENV
AGENTD_PORT`, `EXPOSE`, `ENTRYPOINT []` and `CMD ["/agentd"]`, preceded by
`USER root` when the task's last `USER` is anyone else (the daemon demotes each
exec itself, which takes root). `port=` must match the sandbox's port, and
`workdir=` creates and sets a working directory the way `microvm dockerfile
--workdir` does.

It refuses, with `InvalidArgError` naming the cause, the task Dockerfiles the
build would accept and the guest would then fail on:

- no `FROM`;
- a last instruction that is unfinished: a trailing line continuation (in the
  escape character an `# escape=` directive selects) would join the stanza's
  `COPY agentd /agentd` into it, and an unterminated heredoc would swallow the
  whole stanza, so the image would build with no daemon in it;
- an `AGENTD_SSE_KEEPALIVE_SECS` at or over the client's stream idle timeout;
- a port of 0, or a `workdir` that is not one absolute path;
- `inherit_workdir=True` when neither the task nor `workdir` declares a
  `WORKDIR`. A task with no `WORKDIR` runs its commands in `/`, as it would
  under Docker, so this guard is opt-in: set it when your harness relies on the
  image `WORKDIR` being meaningful.

`BaseImage.from_dockerfile` answers the pairing the other way round.
`build_image` refuses a Dockerfile whose first `FROM` differs from the base's
`docker_ref`, which is the right guard where the client derives the Dockerfile
from the base (the build runs the Dockerfile on top of the base `baseImageArn`
names). A task Dockerfile chooses its own `FROM`, so the derived base keeps the
managed base's `name` — `baseImageArn` is unchanged — and takes the first
`FROM`'s ref whole, digest pin included; the guard then compares the ref with
itself. It raises `InvalidArgError` for a Dockerfile with no `FROM`.

From a shell, `microvm dockerfile` prints the same stanza for a base of your
choice, and `microvm build --dockerfile` takes the result:

```
microvm dockerfile --workdir /workspace > Dockerfile
# edit: insert your RUN layers between the chmod line and the ENV lines
microvm build --dockerfile Dockerfile --name my-task-image
```

With no binary, `build` provisions the daemon itself: the release asset for
the CLI's own version, verified and cached under the state directory. The
`agentd_bytes` above come from the same chain through the bindings
(`microvms-core/src/provision.rs`). `provision_agentd_report()` returns the
bytes together with `.source`, `.verification`, `.path`, and `.sha256`, and
Node has the same pair:

```js
import { provisionAgentd, provisionAgentdReport } from '@theagenticguy/microvms';

const agentd = await provisionAgentd(); // Buffer, for this client's version
const { source, verification } = await provisionAgentdReport();
```

The call answers from, in order, a `binary` you pass (or `$MICROVM_AGENTD`),
the cache entry for the version, and a fetch of the GitHub release asset. The
version defaults to `core_version()`, so the daemon you bake always speaks the
protocol of the client that drives it. A fetch is verified by
`gh attestation verify` (the release workflow's Sigstore attestation) or, when
`gh` cannot download, by the release's `SHA256SUMS`; one that cannot be
verified raises `PreconditionError` rather than warning. Every binary it
returns, including one you supplied, is checked for an aarch64 ELF header
first, because a wrong-architecture daemon fails 45 minutes later as a run-hook
timeout. A cache entry is served only while it still matches the digest
recorded when it was verified.

The worked example is
[`examples/coding-agents-on-bedrock/Dockerfile`](../examples/coding-agents-on-bedrock/Dockerfile):
the stanza's lines, plus `dnf install` and `npm install -g` layers that put two
coding-agent CLIs in the image, plus a `/workspace` WORKDIR.

Two lines in the stanza are load-bearing and must survive any edit.
`ENTRYPOINT []` plus `CMD ["/agentd"]` is the deployment invariant the trust
boundary rests on: it guarantees no task workload runs before the platform's
run hook lands, and it is what makes an omitted `cwd` inherit the image
`WORKDIR` (`docs/PROTOCOL.md`, "Trust boundary"). `wrap_dockerfile` writes them
last, so a task's own `ENTRYPOINT` or `CMD` cannot displace them. A base image
that starts its own background process before bootstrap breaks the invariant,
and enforcing it belongs to whoever builds the image — the daemon cannot.

One thing never goes in the image: a secret. The image becomes a shared
snapshot, so every VM launched from it sees the same bytes; per-VM credentials
travel through `runHookPayload` at launch instead (the module docs of
`microvms-core/src/control/artifact.rs`).

## The wire contract a harness client implements

The full route table, request shapes, and the defect-driven rules are in
`docs/PROTOCOL.md`; the same contract is served as JSON Schema at
`GET /v1/schema`, unauthenticated, so a client can fetch it before it holds a
token. What follows is the shape of the client, not the contract itself.

**Bootstrap.** The platform delivers your `runHookPayload` string to the
daemon's `/run` hook; agentd parses it as JSON and installs `agent_token`
(`agentd/src/routes.rs:166-216`). The install is one-shot: a replay of the
identical token answers 200 (the platform may retry its own hook), a different
token answers 409 and changes nothing. Until it lands, every control route
answers 503 — not 404, not a dropped connection — so a client can distinguish
"not yet bootstrapped" from "broken" (`agentd/src/auth.rs:62-80`). The payload
is capped at 4096 bytes (`microvms-core/src/constants.rs:61`).

**Auth.** Every `/v1/` route except `/v1/health` and `/v1/schema` takes
`Authorization: Bearer <agent_token>` — the same token the payload delivered.
Comparison is constant-time over bytes (`agentd/src/auth.rs:28`).

**Exec.** The client mints the `exec_id` and sends it in `POST /v1/exec/start`.
That is what makes a retry safe: a start carrying a known id returns success
without spawning a second child, decided under the registry lock
(`agentd/src/exec.rs:364-367`), so a harness whose process died between sending
the start and reading the answer sends the identical start again and gets the
original exec. `GET /v1/exec/{id}` polls, read-only, repeatable. `POST
/v1/exec/{id}/ack` releases the buffered output and starts the collection
clock; a second ack is 409, because the first released it and a 200 with an
empty body would read as "the command produced no output". Output lives until
the ack, so nothing a slow reader has not seen is destroyed. `POST
/v1/exec/{id}/kill` signals the process group, SIGTERM then SIGKILL after a
grace period (`agentd/src/exec.rs:900-931`). `POST /v1/exec/{id}/stdin` writes
to a child that was started with `stdin: true` and carries the explicit EOF
signal; an exec that never asked for stdin answers 409.

**Streaming.** `GET /v1/exec/{id}/stream?offset=N` follows output as SSE from
a byte cursor. A reconnecting client passes the offset it read to and receives
exactly what it has not seen; a reattach past the retained window gets an
explicit `gap` event naming the missing byte range rather than silently
skipping (`agentd/src/exec.rs:436-524`). The stream ends with a typed `exit`
event, which is what distinguishes a finished command from a cut connection —
the reason this is SSE and not a chunked byte stream.

**Files.** `PUT`/`GET /v1/fs/file` move one file, streamed, with a mode
applied at open. `PUT`/`GET /v1/fs/tar` move directory trees; extraction is
confined by lexical resolution with symlink and bomb defenses and member/size
caps (`agentd/src/fs.rs:4-41`), and a write that would push the filesystem
under the disk reserve is refused with 507 naming the real free space
(`agentd/src/fs.rs:66-91`).

**Health.** `GET /v1/health` is unauthenticated and reports version, bootstrap
state, disk pressure, and the identity-repair flags — the conditions that are
reasons to drain a VM rather than schedule more work onto it.

## Running one command to one result

Most harness `exec` methods are one composition over the exec routes: start
with a caller-minted id, stream output to a callback, fall back to polling when
the stream is cut, kill the process group when the harness's own deadline
passes, and turn what came back into a shell exit code. The bindings provide
it as one call, `Session.run_to_completion` in Python and
`Session.runToCompletion` in Node, over `Session::run_to_completion` in
`microvms-core` (`microvms-core/src/session/complete.rs`). The behavior is
specified as BIND-6 through BIND-10 in `spec/core.symspec.json` and checked by
the Stateright model in `model/src/run.rs`.

```python
def exec(self, command: str, timeout_sec: int | None = None) -> tuple[str, str, int]:
    result = session.run_to_completion(
        ["bash", "-c", command],  # bash semantics; see below
        on_output=lambda chunk: stream_to_log(chunk.stream, chunk.text()),
        cwd=workdir,
        env=env,
        timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
        client_grace_sec=60.0,
    )
    stderr = "\n".join([result.stderr, *result.notes])
    return result.stdout, stderr, result.posix_exit_code
```

What the call does, in order:

1. **Start.** With `exec_id` omitted a fresh id is minted; pass your own to make
   a retried call address the same exec.
2. **Stream.** With `on_output`, each output chunk reaches the callback as it
   arrives, reconnecting at the byte cursor through ordinary cuts. The
   terminal `exit` event means the output is final, and one ack returns it.
3. **Fall back to wait and ack.** Without a callback, or when the stream ends
   without its `exit` event (the reconnect budget ran out, the stream failed,
   or the callback raised), the call polls until the exec finishes and then
   acks it. A callback that raises stops delivery; the exec is still collected,
   and then the exception is raised.
4. **Client deadline.** The client waits `timeout_sec + client_grace_sec`, or
   the VM's maximum lifetime when there is no `timeout_sec`. If that passes
   first, it kills the process group and then waits and acks for up to
   `client_grace_sec` more. The daemon enforces `timeout_sec` itself, so this
   path runs only when the daemon's escalation outlasts the grace: a command
   that ignores SIGTERM for longer than the daemon's ten-second `kill_grace`,
   a grandchild holding the pipes, or a stalled proxy. The daemon answers the
   kill only once the group is gone, sending SIGKILL after `kill_grace` if it
   has to (measured in us-east-1 on 2026-09-24), so the kill itself can take
   up to ten seconds before the grace starts.
5. **Synthesize.** If that wait and ack fails too, the result is synthesized:
   `synthesized` is true, `posix_exit_code` is 124, `phase` is `running`, and
   the output is unknown rather than empty. Because a killed group is gone by
   the time the kill returns, this happens when something outside the group
   holds the output pipes past the grace (the daemon waits a five-second
   linger for them), when the kill could not be sent, or when the daemon is
   unreachable.

`posix_exit_code` is what `$?` would say. It is **124** when a deadline ended
the command: the daemon's own (`timed_out`), the client's kill of a live
process group, or a synthesized result. It is **128 plus the signal** for any
other signal death, so an out-of-memory kill reads 137 rather than as a
timeout, and otherwise it is the exit code. A child that traps SIGTERM and
exits 0 after the daemon's deadline reports 124, not 0, which agrees with `ok`
being false.

`notes` has one human-readable line per condition that changes how the output
reads: truncation at the daemon's output cap, an expired daemon deadline,
writers left alive past the linger deadline, and the client deadline (a kill,
or a synthesized result naming both failures). A clean result has none.
Append them to stderr as they are.

**The `["bash", "-c", command]` idiom.** The daemon's `shell=True` runs the
string through `/bin/sh -c`, and on Debian-family images `/bin/sh` is dash,
which rejects `set -o pipefail`, arrays, and `[[`. A harness whose contract is
bash semantics passes the command as an argv with `shell=False` (the default),
so bash parses it and no other shell is in between. The image must ship bash;
the managed al2023 base and every Harbor-built main image do.

## The proxy-token reality

The daemon's endpoint sits behind the platform's proxy, and the proxy wants two
headers on every request: `X-aws-proxy-auth` carrying a minted JWE, and
`X-aws-proxy-port` naming which allowed port this request targets — omitting
the second is rejected in a way that reads like a bad token
(`microvms-core/src/session/proxy.rs:5-13`). The token comes from
`CreateMicrovmAuthToken`, and the response's `authToken` is a **map of header
name to value**, not a string; read it as a string and every request fails.

The service caps a token at sixty minutes
(`microvms-core/src/session/proxy.rs:63`). That is not a choice, and it is
shorter than a long agent run, so a client that mints once at construction
expires mid-run with a rejection indistinguishable from a dead daemon. The
pattern that works is minting inside the request path with a refresh interval
well under the ceiling — this repo's clients refresh at half of it, thirty
minutes, so a request in flight across the rollover still holds a token with
about thirty minutes of life (`microvms-core/src/session/proxy.rs:29-37`). A
mint failure is retryable; treat it that way, because a control-plane throttle
at minute thirty must not kill a healthy run.

Token rotation costs nothing on the daemon side. All exec state — the records,
the buffered output, the stream cursors — lives in the daemon, keyed by
`exec_id`, so a detached exec started under one proxy token is polled and acked
under the next one. Start, rotate, poll, ack is a normal sequence, not a
recovery path. This is a tested contract, not an inference: the live suite's
`reattach after token rotation` section starts a detached exec, drops every
piece of client state except the endpoint, the agent token, and the MicroVM id,
reattaches under freshly minted proxy tokens, and asserts that the output
produced *before* the reattach comes back whole — nothing buffered under one
token is lost to the next (`conformance/run_rs.py`, `drive_token_rotation`).

## The idle keepalive is yours, and it must run outside the VM

The platform measures idleness by inbound traffic through the endpoint proxy
and suspends a VM whose window elapses without any. Your harness — the
orchestrator outside the VM — owns the keepalive: poll `GET /v1/health` on an
interval well under the launch's `maxIdleDurationSeconds`, and each poll is the
inbound traffic that resets the timer. Measured, both halves: a polled VM
outlives its idle window and the same VM suspends once the polling stops
(`docs/PLATFORM.md`, "An outside poll of `/v1/health` does reset the idle
timer"; asserted every live run by `conformance/run_rs.py`,
`drive_idle_keepalive`).

An in-guest keepalive **cannot** work, and it is worth knowing why before
someone builds one: the endpoint proxy terminates *outside* the VM and forwards
over loopback, so a request a guest process sends to the daemon's own port is
generated on the far side of the meter and never crosses it. A guest-side
keepalive route would answer 200 and change nothing, and the failure would
surface as a suspend during exactly the long run it was added to protect
(`docs/HARNESS-CAPABILITIES.md`, gap 6). Neither does in-guest *work*: a VM
running a multi-hour exec with no outside traffic is suspended mid-work at the
idle window. The process survives — suspend is a freeze, not a kill — but
nothing external can reach it until someone resumes it. What survives the
freeze, including clocks and outbound connections, is in
[Suspend and resume](SUSPEND-RESUME.md).

`/v1/health` is the right route for the poll: unauthenticated, one small
request, and it carries `busy` and `execs` so the poll is informed rather than
unconditional — an orchestrator can stop keeping a drained VM alive instead of
billing it to the duration ceiling.

### The supported keepalive

The core ships that loop as `KeepAwake`, and every surface exposes it:

| Surface | Call |
| --- | --- |
| Rust | `session.keep_awake(&KeepAwake::new(window).while_busy(true), stop).await` or `KeepAwake::spawn` for a background task |
| Python | `with session.keep_awake(while_busy=True) as keepalive: ...` (also `wait()` and `stop()`) |
| JavaScript | `const keepalive = await session.keepAwake({ whileBusy: true }); await keepalive.done();` |
| CLI | `microvm keepalive --name NAME --while-busy` |

It polls `/v1/health` at an interval of at most half the idle window, so one
missed poll cannot let the VM suspend. The default interval is a third of the
window, at most 20 seconds. When the caller does not know the window, the policy
assumes the platform minimum of 60 seconds. A sandbox-held session knows its own
window, and the CLI reads it from `GetMicrovm`. Retryable poll failures are
retried after one second, up to three in a row. `while_busy` ends the loop once
no exec is running.

A keepalive on a sandbox-held session ends as soon as that sandbox suspends or
terminates, because the next poll would otherwise auto-resume the VM the caller
just suspended. It reads the sandbox's lifecycle, not its lock, so it keeps
polling while a long `run_sync` holds the lock. Stop a keepalive yourself before
suspending the VM any other way, for example from the CLI.

Measured 2026-09-23 in us-east-1: with `maxIdleDurationSeconds=60`, a CPU-busy
exec was suspended between 60 and 70 seconds after the last inbound request,
and a held-open exec output stream with steady output kept the VM running for
150 seconds with no other requests (`docs/PLATFORM.md`).

### Choosing `maxIdleDurationSeconds`

- **An orchestrator that stays connected** (a CLI session, a notebook, a
  service): keep the short default and run the keepalive for the length of the
  work. The VM suspends soon after the orchestrator goes away, which is the
  cheap failure.
- **An orchestrator that disconnects between polls** (a durable workflow that
  suspends between steps): the orchestrator's own poll cadence is the
  keepalive. Set the window comfortably above the longest gap between polls,
  including retries, or accept that the VM suspends and auto-resumes on the next
  poll.
- **Streaming output**: a client holding an exec stream open receives traffic
  continuously, which kept a VM awake in the measurement above. It is not a
  substitute for a keepalive: the stream ends when the exec does, and a reconnect
  gap longer than the window suspends the VM.

## Lifecycle from a process that did not launch the VM

A durable workflow step, a reaper, or another machine often holds only an identifier.
`ControlPlane` is lifecycle by ID in both bindings, one core call per method, with no
lifecycle state of its own and therefore none of a `Sandbox`'s STATE guards:

```python
plane = microvms.ControlPlane(microvms.Region.us_east_1())
vm = plane.get(microvm_id)  # state, state_reason, endpoint, idle_policy, started_at
plane.suspend(microvm_id)
plane.wait_for_state(microvm_id, ["SUSPENDED"])
for item in plane.list(image_identifier=vm.image_arn):
    print(item.id, item.state)
```

Pair `vm.endpoint` with the agent token in `Session.attach` for exec and files.

The process that launches a VM for later steps hands it off with `detach()` rather than
dropping the sandbox, which would warn that a live VM was abandoned:

```python
sandbox = microvms.Sandbox(region)
sandbox.run(image_identifier=image, wait=False)
record = sandbox.detach().to_dict()  # microvm_id, endpoint, region, port, agent_token
save_privately(record)  # encrypted: agent_token is a credential
```

`detach()` (JS `sandbox.detach()`, `AgentVm.detach()` in both) makes no AWS call and leaves
the VM running. It returns a `Detached` record whose token is absent from repr and
`toString()`, and it leaves the sandbox inert: its session is gone, and `run`,
`wait_until_running`, `suspend`, `resume`, and `terminate` are refused (`terminate` reports
the refusal in its report rather than raising). It is refused without a live VM.

To drive the VM with a `Sandbox`'s guards instead, adopt it from its private record:

```python
sandbox = microvms.Sandbox.adopt(region, microvm_id, endpoint, agent_token)
if sandbox.lifecycle == "SUSPENDED":
    sandbox.resume()
sandbox.terminate(wait_for_terminated=True)
```

`Sandbox.adopt` (JS `Sandbox.adopt`, and `AgentVm.adopt` with the agent specs) reads the
lifecycle from `GetMicrovm`, so suspend, resume, and terminate keep STATE-5, STATE-7,
STATE-11, and STATE-12, with the suspended window from the reported `idlePolicy`. The VM
was bootstrapped by the launch that made it, so an adopted sandbox refuses `run` and never
re-sends a run-hook payload (STATE-3), and dropping it prints no leak warning: the
launching record owns the teardown. A VM adopted while already SUSPENDED has no suspend
time this client saw, so the service answers a late resume. The `endpoint` must match the
one `GetMicrovm` reports.

Two launch options make a launch step safe to retry. `Sandbox.run(wait=False)` returns
once `RunMicrovm` is accepted and `wait_until_running()` finishes the wait later. A
persisted `client_token` with an explicit `agent_token` makes a retried launch adopt the
VM the first attempt made; if that VM idle-suspended in between, the launch resumes it
rather than reporting a startup death (`docs/PLATFORM.md`, "A client-token retry after a
suspend returns the same VM"). The CLI's `run --client-token` reads the agent token from
`$MICROVM_AGENT_TOKEN`.

To find a VM by name instead of carrying the triple, register it and adopt it by name
later, from any process:

```python
registry = microvms.NameRegistry()  # the CLI's registry: $MICROVM_STATE_DIR/names
registry.register("ci-runner", sandbox)
# ...in another process, or `microvm exec --name ci-runner` from a shell
vm = microvms.Sandbox.from_name(region, "ci-runner", registry)
vm.terminate(wait_for_terminated=True)
registry.release_by_vm(vm.microvm_id)
```

`NameRegistry(state_dir)` is the CLI's file registry, so names cross freely between the
CLI and both bindings. A record holds the agent token, a bearer credential: files are
owner-only, and `NameRecord` keeps the token out of `repr` (JS: out of `toString` and
`JSON.stringify`). To keep names in your own database, store `record.to_dict()` (JS
`toObject()`) privately and rebuild it with `NameRecord.from_dict`, then call
`Sandbox.adopt` with its fields. `from_name` refuses a missing name, and a record from
another region, before any AWS call. The platform itself offers no lookup: `RunMicrovm`
takes no tags and tagging a MicroVM fails (`docs/PLATFORM.md`).

`log_group` (with an optional exact `log_stream`) sends a VM's own logs to a group you
choose, and `disable_logging` turns them off; the execution role must be allowed to write
there. The CLI spells these `--vm-log-group`, `--vm-log-stream`, and `--no-vm-logs`, since
`--log-group` already names the image build's logs.

## What the hand-rolled daemons needed, and where agentd covers it

The two daemon shapes this supersedes are described in
`docs/HARNESS-CAPABILITIES.md`; neither project is a dependency of this repo,
so the rows are the generic needs.

| Need | Who had it | agentd |
| --- | --- | --- |
| Start/poll/ack exec that outlives an auth-token ceiling | evaluation harnesses | caller-minted `exec_id`, idempotent start, read-only poll, explicit ack, TTL only after ack (`agentd/src/exec.rs`) |
| Idempotent start under retry | evaluation harnesses | a known id returns success without spawning a second child (`agentd/src/exec.rs:364-367`) |
| Per-exec env, cwd, user/group, timeout | evaluation harnesses | in the wire protocol and applied by the daemon; the child's environment starts empty, so the token never leaks into it |
| A user by name, with its `HOME` | evaluation harnesses | `user`/`group` take a name the daemon resolves against the guest's `/etc/passwd` and `/etc/group`, and a user with a row gets `HOME`, `USER`, `LOGNAME` beneath the caller's env; an unknown name is `400 unknown_user` before anything spawns (`agentd/src/exec_start.rs`) |
| The image's `ENV` (a venv `PATH`, a `JAVA_HOME`) in the child | evaluation harnesses | opt-in `inherit_image_env`: the daemon's startup snapshot of its own environment, minus `AGENTD_*`, as the lowest layer; `Health.image_env_keys` says whether the daemon honours it |
| Bash semantics without guessing the image | evaluation harnesses | `shell: "bash"`, resolved in the guest; a missing shell is `400 unknown_shell` rather than exit 127 |
| File and directory-tree transfer with tar fidelity | evaluation harnesses | streamed file routes plus confined tar extraction (`agentd/src/fs.rs`) |
| Per-instance credential bootstrap, no secret in the shared image | both | one-shot `runHookPayload` bootstrap with replay semantics (`agentd/src/routes.rs:166-216`) |
| Lifecycle hooks answered so the platform can manage the VM | session servers | ready/validate/run/suspend/resume/terminate all served (`agentd/src/routes.rs:112-118`) |
| A liveness probe cheaper than an exec | session servers | unauthenticated `GET /v1/health` |
| Live output streaming with resume | neither had it | SSE with byte-cursor resume and explicit gap events (`agentd/src/exec.rs:436-524`) |

### A harness exec in one call

A harness whose contract is bash semantics, a task user by name, and the image's own
`ENV` sends all three on the start request and resolves nothing itself:

```python
result = session.run_sync(
    command,
    shell="bash",  # resolved in the guest; unknown_shell if absent
    user=task.agent_user,  # int or str; unknown_user if the guest has no such row
    inherit_image_env=True,  # the image's ENV beneath the launch env and `env`
    env=task.env,
    timeout=timeout,
)
```

The Node binding takes the same fields in `ExecOptions` (`shell: "bash"`, `user: "agent"`,
`inheritImageEnv: true`), and the CLI as `exec --shell bash --user agent
--inherit-image-env`. Precedence, lowest first, is image env, the passwd row's `HOME`,
`USER` and `LOGNAME`, the launch env, the request's `env`. A daemon built before these
fields refuses a string `user` or `shell` as `malformed_request` and ignores
`inherit_image_env`; check `Health.image_env_keys` (not `None`) before relying on the
last one.

## Coding agents over the daemon

The one opinionated layer this repo ships over the recipe above is `docs/AGENT-VMS.md`:
`microvm agent-up` derives a Dockerfile from your agentd stanza plus three layers
(Node 22 with `nodejs22-npm`, `npm install -g` of Claude Code and/or Codex, a uid
1000), launches with egress, mints a Bedrock bearer token from the caller's own
credentials, and installs it as `/workspace/.agent-env` for the agent to source;
`microvm agent-prompt` runs the agent headless as that user. A harness that already
embeds agentd gets the same steps from the bindings' `AgentVm`, or piecewise from
`install_agent_access` and `prompt_agent` over any `Session`, so the credential file,
the demotion, the `PATH` line, and the read-back-the-effect discipline are the
library's rather than each harness's to rediscover.

## Configuration knobs

Every `AGENTD_*` variable is read at startup by `Config::from_env`
(`agentd/src/config.rs:116-152`); an unset or unparseable value keeps the
default rather than refusing to boot, because a daemon that will not start
strands the VM with no way in. Set them as `ENV` lines in your Dockerfile —
the stanza already sets the first two.

| Variable | Default | What it bounds |
| --- | --- | --- |
| `AGENTD_PORT` | `9000` | the port the control API and hooks listen on (`agentd/src/config.rs:15`) |
| `AGENTD_LOG` | `info` | the tracing filter, standard `EnvFilter` syntax (`agentd/src/main.rs:91`) |
| `AGENTD_MAX_BODY_BYTES` | 512 MiB | largest request body accepted on the wire (`agentd/src/config.rs:17-19`) |
| `AGENTD_MAX_OUTPUT_BYTES` | 8 MiB | per-stream cap on captured exec output; exceeding it truncates and marks the result (`agentd/src/config.rs:25-27`) |
| `AGENTD_OUTPUT_LINGER_SECS` | `5` | how long to keep reading pipes after the child exits, for grandchildren holding them (`agentd/src/config.rs:28-31`) |
| `AGENTD_EXEC_TTL_SECS` | `900` | how long an acked exec entry is retained before collection (`agentd/src/config.rs:32-33`) |
| `AGENTD_STREAM_BUFFER_BYTES` | 1 MiB | bytes of recent output kept for stream replay; a reattach past it gets a gap event (`agentd/src/config.rs:41-45`) |
| `AGENTD_STREAM_CHANNEL_CAPACITY` | `256` | slots in an exec's live fan-out channel; a lagging subscriber re-reads the ring instead of losing output (`agentd/src/config.rs:46-49`) |
| `AGENTD_SSE_KEEPALIVE_SECS` | `15` | interval between SSE keep-alive comments, so a silent exec does not look like a dead connection (`agentd/src/config.rs:50-53`) |
| `AGENTD_MAX_STDIN_WRITE_BYTES` | 1 MiB | largest single decoded stdin write (`agentd/src/config.rs:54-57`) |
| `AGENTD_DISK_RESERVE_BYTES` | 256 MiB | free bytes a write target must keep; a write that would cross it is refused with 507. Zero disables the guard (`agentd/src/config.rs:63-69`) |
| `AGENTD_REPAIR_IDENTITY` | `true` | whether to replace image-derived identity at the first successful run hook, because N VMs restored from one snapshot share machine-id, hostname, and boot_id. `0`/`false`/`no`/`off` opt out (`agentd/src/config.rs:70-78`) |
| `AGENTD_HOOK_HANDLER_TIMEOUT_SECS` | `20` | how long one handler may run before its process group is killed; clamped to 1–55 so it stays under the image's hook timeout (`agentd/src/config.rs:84-88`) |
