# Wire protocol v1

The lifecycle hook paths are fixed by the platform, so they stay unversioned.
Everything this project owns lives under `/v1/`. Every response carries a
`microvms-agentd-version` header.

## Routes

Hooks are served under `/aws/lambda-microvms/runtime/v1/`, abbreviated `HOOKS`
below. That prefix is fixed by the service, so a daemon serving a bare `/run`
never gets bootstrapped.

| Route | Auth | Purpose |
| --- | --- | --- |
| `POST HOOKS/ready` | none (platform hook) | image-build readiness probe |
| `POST HOOKS/validate` | none (platform hook) | image-build validation probe |
| `POST HOOKS/run` | none (platform hook) | one-shot token bootstrap from `runHookPayload`, plus the optional launch environment; the winning bootstrap repairs identity and runs the workload's `run` handler |
| `POST HOOKS/suspend` | none (platform hook) | runs the workload's `suspend` handler, then acknowledges |
| `POST HOOKS/resume` | none (platform hook) | runs the workload's `resume` handler, then acknowledges; memory, processes, and the token survive a suspend ([Suspend and resume](SUSPEND-RESUME.md)) |
| `POST HOOKS/terminate` | none (platform hook) | runs the workload's `terminate` handler, then acknowledges; server shutdown is signal-driven |
| `POST /v1/exec/start` | bearer | start a command under a caller-minted `exec_id` |
| `GET /v1/exec/{id}` | bearer | poll status and output; never mutates |
| `GET /v1/exec/{id}/stream?offset=` | bearer | follow output as SSE from a byte offset |
| `POST /v1/exec/{id}/stdin` | bearer | write to a child's stdin, or signal EOF |
| `POST /v1/exec/{id}/ack` | bearer | release output, enter TTL collection |
| `POST /v1/exec/{id}/kill` | bearer | signal escalation to the process group |
| `GET /v1/procs` | bearer | process accounting: every exec's group and its live pids, read from `/proc` |
| `GET /v1/tcp?port=&identity=` | bearer | WebSocket relay to `127.0.0.1:<port>` in the guest; close codes carry the outcome, and a verified tunnel's clean end is its [end of stream](#the-verified-tunnels-end-of-stream) |
| `PUT /v1/fs/tar` | bearer | streaming tar upload and confined extraction |
| `GET /v1/fs/tar?path=` | bearer | streaming tar download |
| `PUT /v1/fs/file` | bearer | write one file |
| `GET /v1/fs/file?path=&start_line=&end_line=` | bearer | read one file, or a 1-based inclusive line range of it |
| `GET /v1/health` | none | liveness, version, bootstrap state, exec-activity, hook observations, image-environment key count |
| `GET /v1/schema` | none | this contract as a JSON Schema document: every route, shape, status, and limit |

**`/v1/health` reports hook requests kept in a capped log.** The daemon records
the hook's name and timestamp in memory before running its workload handler or
choosing a status. `GET /v1/health` reports kept entries as `hooks`
(`[{hook, fired_at}]`, oldest first), beside `hooks_dropped`. The earliest entries
stay. Once the log is full, the daemon counts later requests in `hooks_dropped`
instead of storing them. This applies regardless of the hook or request source.
A later request does not run a workload handler. The route still performs its
other work and returns its normal status. A run
hook that arrives malformed or answers 409 can leave a `run` entry if space
remains: the entry records receipt of the request, not its success.

The platform writes no CloudWatch logs for the validate hook. Build-time hooks
(`ready`, `validate`) fire in the snapshot VM before the snapshot is taken, so
their stored in-memory records ride the memory image into every VM launched
from it and are visible on the first health poll.

The hook routes are unauthenticated and can be called over loopback from inside
the guest (see "Trust boundary" below). An entry shows that the daemon received
a request on that hook route. It does not prove that the platform sent it.
Later platform requests are subject to the same cap as any other request.
Both fields are `#[serde(default)]` like `busy` and `execs`, and for the same
reason: an older daemon omits them, and empty/zero is the honest reading.

## Compatibility between releases

Every upgrade puts an older client in front of a newer daemon, or a newer client
in front of an older one, so each release has to work with the one before it in
both directions. `mise run schema:compat` holds `docs/schema.json` to the copy at
the previous release tag. It follows serde's two contracts, because the daemon
reads a request and the client reads a response, and it compares each route's
shapes where the route uses them, so a route that gains a body or points at
another definition is held like an edited one:

- A route, a status (its code and error) or a stream event the previous release
  had doesn't go away.
- In a request or query body, no field becomes required (a body or query a
  route didn't have counts as one with no fields), no field is removed, and no
  required field becomes optional. An older client doesn't send the new required
  field, a field it still sends would be ignored, and a newer client that leaves
  out a field an older daemon requires is refused.
- In a response or stream event, a body the route had doesn't go away, and a
  field an older client requires with no default isn't removed or left out of
  `required`, unless it's nullable (serde reads an absent `Option` as `None`). A
  field becomes required only with a serde default, since an older daemon
  doesn't send it.

A new route, an optional request field, a response field with a default and a
definition renamed with its shape kept are all compatible. A change that has to
break the rules bumps `PROTOCOL_VERSION` in `crates/protocol/src/lib.rs` and lists each
break in `docs/schema-breaks.toml`, as the line the check prints and with the
reason. While the version differs from the previous tag's, the check passes only
when that list and the breaks it finds are the same, so a later change that
breaks something else before the next release still fails. No client reads
`protocol_version` before every HTTP call yet, so an older client that meets a
changed HTTP shape fails the way any undecodable answer does. Verified tunnels
bind the version into the handshake and refuse a different version. Type changes
(a field that accepts less than it did, an enum that loses a variant, a body's
media type), `limits`, and encrypted tunnel framing aren't checked by
`schema:compat`. Review holds those. Encrypted framing changes also appear beside
the schema break declarations, with their protocol version.

## Workload hook handlers

The platform always keeps a suspended VM's full memory and disk, and
`SuspendMicrovm` takes no option to change what survives. What a workload can
control is what runs at the boundary. When the image carries an executable at
`/etc/agentd/hooks.d/<hook>` (fixed; the directory is image-owned and not read from the
environment, because it decides what the daemon executes as root), the
daemon runs it for the `run`, `suspend`, `resume`, and `terminate` hooks:

- **Before answering.** A `suspend` handler runs before the freeze; a `resume`
  handler runs before the platform forwards traffic it held during the resume.
  The `run` handler runs once, after the winning bootstrap installs the token;
  a replayed or conflicting run hook runs none.
- **Within a budget.** `AGENTD_HOOK_HANDLER_TIMEOUT_SECS` (default 20, clamped
  to 1 to 55) is kept below the image's hook timeout, which the client sets to 30
  seconds. The budget covers the leader and reads from its output streams. At
  the deadline the daemon kills the handler's process group, even if the leader
  already exited, and stops reading output. The daemon does not stop a process
  in a new group. It still stops reading its output at the deadline.
- **Handler results do not change the status.** After the request is valid and,
  for `run`, bootstrap succeeds, the hook answers 200 whatever the handler did.
  Check the recorded outcome for handler failures.
- **One at a time,** and only for invocations the hook log kept, so the log's cap
  bounds how many handlers an in-guest caller can trigger.

The outcome lands on the hook's `/v1/health` entry as `handler`:
`{exit_code, signal, timed_out, duration_ms, error}`. `error` says why the
handler could not run at all, such as `not executable`. The key is absent when
the image has no handler for that hook, so an entry reads exactly as it did
before handlers existed. Handler output is logged by the daemon (the image's
log group), never returned on `/v1/health`, which needs no agent token.
A service that needs to keep running after its handler exits should redirect
its output streams so the daemon can finish reading them.

A handler runs as the daemon's user (root), whatever user `AGENTD_DEFAULT_USER`
holds, with the daemon's environment, the launch environment, and
`AGENTD_HOOK=<hook>`; never the agent token. Any process
in the guest can post a hook path, so a handler must be safe to run spuriously.
See [Trust](TRUST.md).

## Identity repair, per VM

`/v1/health` reports `identity_repaired` (repair ran on this VM),
`identity_degraded` (a step failed), and `identity_steps`, each step's
`{name, outcome, error}`. Repair runs at the winning run hook, not at daemon
start: the daemon starts in the image-build VM, so startup repair was captured
by the snapshot, and every VM launched from one image shared its machine-id
(measured 2026-09-23). Before the run hook, `identity_repaired` is false and
`identity_steps` is empty.

## Rules that exist because a defect proved them necessary

These rules describe the daemon contract and are covered by the repository's
model and regression tests. A client must preserve them when reconnecting or
retrying an operation.

**Bootstrap is one-shot, and a replay of the identical token succeeds.** A first
`/run` installs the token and returns 200. A later `/run` carrying the same token
returns 200, because the platform may retry its own hook and must not be told the
VM is broken. A later `/run` carrying a different token returns 409 and changes
nothing. The model in `crates/model/` checks this over every interleaving, including a
racing in-VM caller.

**Control routes answer 503 before bootstrap.** They do not answer 404, and
they do not drop the connection.

**A missing or malformed body key is 400, never 404.** Clients map 404 onto
"file not found". Because of that mapping, returning 404 for a protocol typo
makes the client believe an artifact is absent when it is not. One defect went
undetected this way.

**Protected routes check authorization before they collect a body.**
Rejected requests drain at most the configured byte cap within two seconds so
clients can reuse pooled connections. Reaching either bound closes the connection.

**Connections and hook bodies have fixed limits.** The daemon admits at most
128 connections, including upgraded tunnels. Headers must arrive within five
seconds. This also applies to the next request on an idle keep-alive connection.
A slot stays in use while a response stream is active or a tunnel is open. Hook
envelopes have a 32 KiB cap and a five-second read deadline. The size cap allows
for JSON escapes around the platform payload. Larger envelopes receive 413.
Unfinished bodies receive 408. Both responses close the connection.

**Token comparison happens on bytes, in constant time.** Comparing `str` values
raises on non-ASCII input in some languages. Any caller controls the header, so
a non-ASCII header value could crash the connection.

**No exception on the parse, auth, or routing path may drop a connection.** A
catch-all returns 500. Raw TLS handshake bytes get a 400 and a debug log, since
something in the platform's path probes the port with TLS first.

**`cwd` is omitted when unset.** When the client sends no working directory, the
daemon emits no `cd` prefix, and the child inherits the daemon's own working
directory. Because the daemon is the container `CMD`, that directory is the
image `WORKDIR`. Forcing `/` breaks prebuilt-image tasks and defeats any harness
that discovers the image workdir with `pwd`.

**Exec is idempotent on a caller-minted `exec_id`.** A retried `/exec/start`
returns success without spawning a second child. Polling is read-only. Output
lives until the caller acks, and only acked entries are collected. The Python
predecessor unlinked output files at child exit, which destroyed anything a
backgrounded grandchild wrote afterward.

**A shell wraps the command only when the caller asks for one.** An argv array
execs directly. `shell: true` wraps in
`sh -c` with the command as a single argument, and `shell: "bash"` does the same
with a shell the daemon resolves in the guest (see
[Naming the user, group and shell](#naming-the-user-group-and-shell)). The
predecessor's brace-group wrapper turned empty and comment-terminated commands
into syntax errors and let an unbalanced `}` escape the group.

**Tar extraction mirrors the CPython `data` filter contract.** In-tree symlinks
are preserved, because harnesses legitimately pack them. Absolute link targets
are refused. Relative targets must resolve under the root, using `normpath`
semantics rather than `realpath`. Symlinks resolve relative to their own
directory, while hard links resolve against the archive root. Member count and
total size are capped. Modes are applied after content lands. A regular-file
member replaces the old entry before creation, so other hard links keep their
bytes and modes. Extraction drops the archive's owners, so everything it
creates is root's, as [Upload ownership](#upload-ownership) describes.

**A symlink an archive wrote cannot redirect a later member, and the kernel is
what enforces that.** The `normpath` rule above judges a member at the depth its
name implies, and that is not the same as the depth the write reaches once a
symlink is in the path. The daemon opens the extraction root once and creates
every member relative to that descriptor with `openat2`, using
`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`. A member whose
path would traverse a symlink is refused with 400 rather than written somewhere
else. `openat2` needs Linux 5.6 or newer. On an older kernel the syscall answers
`ENOSYS` and extraction answers 500, which is a refusal rather than a silent
fall back to the weaker check.

**Bodies stream to disk, and caps are enforced on the wire.** The predecessor
buffered whole archives in memory on a VM whose baseline can be 512 MiB, where an
OOM-killed daemon is unrecoverable. It also measured archive size inside the gzip
`with` block, where the stream is unflushed. There, `tell()` reported 10 bytes
for a 327-byte archive, so the size guard almost never fired.

**Output is bounded, and truncation is marked explicitly.** A post-exit linger
deadline bounds how long the daemon waits on grandchildren still holding the pipe.

**A grandchild outlives its exec unless the caller asks otherwise, and
`/v1/procs` is how anyone finds it.** Each exec runs in its own process group,
and until now only `/v1/exec/{id}/kill` ever signaled that group: a command that
backgrounded something and exited left it running with nothing able to enumerate
it (issue #157, measured 2026-09-11, a ticker still counting six seconds after
its exec reported exit 0). `GET /v1/procs` answers `{procs: [{exec_id, pgid,
started_at, child_exited, reap, pids}]}`, one entry per registered exec in any
phase. `pids` are the live pids whose process group is `pgid`, read from
`/proc/<pid>/stat` inside the guest, so the route needs no `ps` in the image;
zombie leaders remain listed while any of their worker threads are alive.
Fully exited zombies are not listed, and an entry whose `pgid` was never
captured lists none. `child_exited` is read from the same write-once marker
`busy` uses, never from the polled result, so an acked exec still reads exited.
`started_at` is epoch seconds on the daemon's clock, like a hook's `fired_at`.
Inspect the row with `child_exited: true` with a non-empty `pids`: that is
the survivor, and its `exec_id` is what the kill route takes.

`reap_group_on_exit: true` on `/v1/exec/start` is the opt-in that closes the gap
at the source. When set, the daemon runs the kill route's SIGTERM-then-SIGKILL
escalation against the group as soon as the child's exit is observed and before
the linger begins, so the survivors die, the pipe closes, and
`writers_may_be_alive` reads false because the linger saw EOF rather than its
deadline. A timed-out exec is already escalated and is not escalated twice. The
escalation's grace ends as soon as `/proc` shows no live member of the group, not when `killpg(pgid, 0)` fails, because the daemon is PID 1 in the guest and a
signaled grandchild stays a zombie that still answers that probe. The
flag defaults to false and the default is the contract above: a backgrounded
grandchild that keeps writing is a feature for the caller who did not set it.
`reap` on a procs entry echoes what the start request asked.

## The launch environment

The run-hook payload may carry an `env` map alongside `agent_token`. It becomes
the base environment of every later exec.

```json
{"runHookPayload": "{\"agent_token\": \"...\", \"env\": {\"KEY\": \"VALUE\"}}"}
```

**The per-request `env` wins on a key both set.** The daemon applies the launch
env first and the request's own map second, so a launch env is a default for the
whole VM and a request is the specific thing happening now. A caller who never
sends a launch env sees no change: the child's environment is the request's map
and nothing else, exactly as before.

**The agent token never becomes part of it.** `env_clear()` still runs, so the
daemon's own environment reaches no child, and the launch env and the token are
separate parameters through the whole install path, there is no field a refactor
could forward one into. Proven by a test that spawns `/usr/bin/env` with a launch
env installed and asserts the child's environment is *exactly* that map: an extra
variable of any name fails it, which is what a leak would look like.

**Only the first successful bootstrap sets it.** A replay of the identical token
answers 200 and leaves the installed env alone, and a conflicting token answers
409 and leaves it alone. Without that, a caller who cannot win the token could
still rewrite the environment every later child runs in.

**Every value is a string, and a malformed `env` is 400 naming the problem.** A
non-object `env`, or a value that is a number or a nested object, is refused with
a body that names the key or the shape. The body never quotes a value, because
the payload carries the token.

**Unknown payload keys are ignored.** A 400 at this hook makes the platform
terminate the VM before forwarding any traffic, so a newer client sending a field
this daemon has never heard of must still be able to bootstrap it. Forward
compatibility here is the difference between an ignored field and a dead launch.

**The token, the env, and the tunnel identity, when a launch has one, share one
4096-byte budget, measured in UTF-8 bytes and inclusive** (`PLATFORM.md`). The daemon cannot enforce what the platform already
rejected, an over-ceiling payload never reaches the guest, so the check is on
the *client* side, in `microvms-core`'s `RunHookPayload::for_launch`, and it fires
before any control-plane call. botocore does not enforce the ceiling either, so
without that local check there is no signal at all until AWS answers with a
`ValidationException` on a member the caller did not know they were filling. The
refusal names the byte count, the ceiling, and how much of it the env is, because
"4142 bytes, ceiling 4096" alone does not say whether to shorten the token or drop
a variable. One bearer token fits with room to spare; a set of AWS session
credentials does not, and that is what makes this ceiling reachable in practice.

## Naming the user, group and shell

Three fields of `POST /v1/exec/start` accept a second JSON type, and one flag is
new. All four are backward compatible in both directions that matter: a request
that sends integers and booleans is byte-identical to what it always was, and a
daemon that predates them refuses a string it cannot read as `malformed_request`
rather than misreading it. `PROTOCOL_VERSION` does not change for these fields.

| Field | Accepts | Refused before spawn with |
| --- | --- | --- |
| `user` | a uid (integer) or a name (string) | `400 unknown_user` |
| `group` | a gid (integer) or a name (string) | `400 unknown_group` |
| `shell` | `false`, `true` (`/bin/sh -c`), or a shell name or absolute path | `400 unknown_shell` |
| `inherit_image_env` | `false` (default) or `true` | never |

**A name is resolved in the guest, before anything is spawned.** The daemon reads
the guest's `/etc/passwd` and `/etc/group` on each request that names one, so a
user created by an earlier exec resolves on the next. A name that matches no row
answers 400 with the stable slug and a `detail` naming the value, and no child
exists: the exec id is not registered, so a poll of it is 404. A string of ASCII
digits that matches no name is read as that id, the way `docker exec -u 1000`
reads it. Supplementary groups are not set; a root daemon's demotion clears them.

**A user with a passwd row gets `HOME`, `USER` and `LOGNAME` from it.** That holds
however the user was given, name or uid. A uid with no row sets none of them,
as before. A user given by *name* also gets the row's primary gid when `group` is
omitted, the way `su` does; a user given as an integer keeps the daemon's group,
which is what protocol 1 always did.

**An image can set the user a start without `user` runs as.** The image's
`AGENTD_DEFAULT_USER` holds a user name or a string of digits. A start request
that omits `user` resolves that string to a row of the guest's `/etc/passwd`:
the row a name matches, or the row whose uid the digits spell. Either way the
child gets the row's uid, primary gid, `HOME`, `USER` and `LOGNAME`. That's
stricter than `user`, where digits keep the daemon's gid and don't need a row. The
daemon resolves it at each request, so a user an earlier exec created resolves
on the next. A request's own `user` always wins, `root` and `0` included, and
`group` still overrides the primary gid. If no row matches, the daemon answers
each start that omits `user` with `400 unknown_user`, and the `detail` mentions
`AGENTD_DEFAULT_USER`. For digits, the `detail` also says the image needs a
passwd row for that uid. The daemon refuses an empty value the same way. It still
boots, and a request that sets `user` still runs. With the variable unset, a
start that omits `user` runs as the daemon's own user, root, as it always has.
Uploads don't follow the variable: what they create stays root's, as
[Upload ownership](#upload-ownership) describes.
Workload hook handlers ignore the variable, and the image snapshot drops it with
every other `AGENTD_*` variable. `/v1/health` doesn't report it, because a new
`Health` field would break the protocol crate's API. The daemon logs the value
at startup instead.

**A named shell is resolved on the child's `PATH`, the image's `PATH`, `/bin`,
then `/usr/bin`.** The first executable regular file wins and runs as
`<shell> -c <command>`. An absolute path is checked as given. A shell the guest
does not have answers `400 unknown_shell` naming it, instead of the exit 127 a
caller cannot tell from the command failing. `shell: true` stays `/bin/sh`,
which is dash on Debian-family images and rejects `set -o pipefail`, arrays and
`[[`; send `"bash"` for bash semantics.

**`inherit_image_env` makes the image's `ENV` the lowest layer.** At startup the
daemon snapshots the environment it inherited as the container `CMD`, minus
every `AGENTD_*` variable, and keeps it immutable. The snapshot is taken in the
image-build VM and rides the memory image into every VM, like the hook
observations. The child's environment is built one layer at a time, each key
from its highest layer:

1. the image snapshot, only with `inherit_image_env: true`;
2. the passwd identity (`HOME`, `USER`, `LOGNAME`);
3. the launch environment;
4. the request's `env`.

The identity is stored above the image layer so a user demoted from root does not keep the
image's root `HOME`, and below the launch and the request so a caller who sets
one of the three wins. Without the flag the first layer is empty and the
child's environment is exactly what it was: the launch map, the request map, and
the identity of a user with a row.

**Whether a child has `PATH` and `HOME` depends on the request.** An exec that
names no `user` and leaves `inherit_image_env` off gets only the launch and
request maps, so by default it has neither (measured 2026-09-02,
[Platform](PLATFORM.md)). A `user` with a passwd row adds that row's `HOME`.
`inherit_image_env: true` adds the daemon's startup environment, which on the
conformance image holds both `PATH` and `HOME` (measured 2026-09-24). A launch
or request `env` that sets either one takes precedence over both.

**The token is never in the snapshot.** It arrives in the run-hook payload,
after the snapshot is taken, into a slot that is not an environment. The daemon
cannot write its own environment (`set_var` needs `unsafe`, which the crate
forbids), and the `AGENTD_` filter would drop anything under that prefix
regardless. `GET /v1/health` reports `image_env_keys`, the snapshot's key count,
and never its values; `null` means no snapshot, which is also what a daemon that
predates the flag reports. Such a daemon ignores `inherit_image_env`, as serde
ignores any unknown key, so a caller that depends on it checks the count first.

The model in `crates/model/src/exec_start.rs` checks these rules over every request
shape (AGENTD-7 through AGENTD-16 in `verify/spec/agentd.symspec.json`), including that
no refused request ever has a child and that neither the token nor `AGENTD_*`
configuration reaches one.

## Upload ownership

**Uploads are root's, whatever `AGENTD_DEFAULT_USER` holds.** `PUT /v1/fs/tar`
and `PUT /v1/fs/file` write as the daemon's user, root, so each entry they
create belongs to root: files, directories, symlinks, and the parent
directories made for them. Tar extraction masks each member's mode to `0755` at
most, so it doesn't set a group or other write bit on a file or directory.
`PUT /v1/fs/file` gives the file the `mode` the request sets, or `0644` without
one, and the parent directories it creates are `0755`. Direct uploads need a
separate ownership transfer before a non-root default user can write into
those directories.

Health returns `microvms-default-user-configured: 1` when the image sets a default
user. Clients use direct uploads when this header is absent, so older images
work without a shell. With the header, the shared client runs `id -u; id -g` once per
session to read the default user's UID and GID.

For a non-root default, the client uploads into a private directory owned by
root and runs `chown` there as `user: 0`. Only root can access the stage until
the ownership transfer ends. The client then installs the contents as the
default user. It refuses destination symlinks. Path traversal during install
and sync deletion runs with the default user's permissions, even through a
parent symlink. The client changes the owner of `/workspace` to the default
user so the command-line tool and both bindings can write the project. Other destinations
need write permission for that user. Root sessions use direct uploads.

An invalid default stops the identity probe before upload. These operations
need `/bin/sh` and the guest's `id`, `mktemp`, `chown`, `find`, `cp`, `mv`, `stat`,
and `rm` in the system command directories. The helpers use a fixed system
`PATH` and clear shell startup-file variables. The staging parent `/tmp` must
belong to root. It must be sticky or writable only by root. After a lost handoff
response, the client cleans up as the default user. An authorized root caller
can inspect any remaining stage.

## Line-ranged text reads

`GET /v1/fs/file` takes optional `start_line` and `end_line`. The semantics are
the AI SDK harness contract's `readTextFile`, copied rather than chosen, because
this route is what that method is implemented on top of:

**Both bounds are 1-based and inclusive.** `start_line=2&end_line=4` is three
lines. `start_line` absent means 1 and `end_line` absent means through EOF.

**An `end_line` past the last line reads through EOF without an error.** Lines
1..1000 of a twelve-line file is a 200 carrying twelve lines, never a 416. A
`start_line` past the last line is an empty 200, not a 404: the file is there and
the window is empty, which is a different fact from the file being absent.

**A line owns its terminating newline.** Lines 1..2 and lines 3..5 concatenate
back into the file rather than losing a separator at the seam. A last line with no
trailing newline has none to own and is returned as it is.

**`start_line=0` and `end_line < start_line` are 400.** Neither is 416: 416 is
about a range the file cannot satisfy, and both of these are ranges no file could,
so a client sent to look at the file would be looking in the wrong place. A
non-integer bound is also 400, and it does not return the missing-`path`
refusal.

**A range still streams.** The read is filtered chunk by chunk and stops reading
once the window closes, so lines 1..5 of a large file cost the first chunk. Nothing
buffers a file to slice it, for the same reason nothing buffers an upload: an
OOM-killed daemon in a MicroVM is unrecoverable.

**With no range the response is byte-identical to what it always was.** The
un-ranged read hands back the reader stream untouched, so the path every existing
caller uses does not acquire the range feature's bug surface.

## Idle policy, and why liveness is a field rather than a route

The platform measures idleness by inbound traffic through the endpoint proxy
(`PLATFORM.md`, "`idlePolicy`"). A workload holding an outbound connection, or one
simply computing for hours, receives none, so auto-suspend can freeze a VM
mid-work. Multi-hour agent runs are the real case.

**A guest-side request cannot fix this, and the daemon does not pretend
otherwise.** The endpoint proxy terminates *outside* the VM and forwards over
loopback, measured, `PLATFORM.md`, "The platform's own hook arrives over
loopback". A request an in-VM process sends to the daemon's own port therefore
never reaches the thing counting traffic. A "keep myself alive" route would be a
keepalive that keeps nothing alive, and it would be discovered as broken by a
multi-hour run auto-suspending mid-work, which is the failure it was added to
prevent.

**`GET /v1/health` carries `busy` and `execs`, for an orchestrator outside the
VM.** The orchestrator's own poll is the inbound traffic, and `busy` is what
makes that poll informed rather than unconditional. The assertion of liveness is
therefore repeated and is explicitly the caller's, which is the property that rules
out the daemon self-keepaliving: a hung process would then bill to the 8-hour
`maximumDurationInSeconds` ceiling with nobody having asked.

**`busy` means producing, not unfinished.** An exec whose child has exited and
whose result is waiting to be acked is not busy: nothing is running, and holding a
VM alive at baseline billing for a command that is over is the mistake this
distinction exists to prevent. `execs` counts every registered entry in any phase,
so `busy: false` with a non-zero count is a VM holding unacked output somebody
still has to collect before terminating it.

Both fields default to `false` and `0` when absent, unlike every other field on
the response. The daemon is baked into an image while a client is installed
separately, so a current client routinely talks to a daemon from whenever that
image was built; a required field would make a health call fail outright against an
older daemon, turning a missing signal into an unreachable VM.

## Streaming and stdin

Both features serve one consumer, an agent harness running inside the VM. The
harness emits output for minutes and may need a prompt written to it. Polling
serves neither need well, because it re-sends the whole buffer each time and
truncates at the output cap.

**The stream is a read-only view of the exec.** An exec is a server-side record
keyed by its caller-minted `exec_id`. Attaching, detaching, or dropping a
connection must not affect the command. Both views must keep working, so poll
returns the buffer, stream follows it, and neither disturbs the other.

**Resume is by byte offset.** `?offset=N` yields exactly the bytes after N, so a
client that reconnects can pick up where it left off. For comparison, E2B's
reattach takes no offset, so a reconnecting E2B client loses everything produced
during the gap. A reattach past the retained window gets an explicit `gap` event
naming the missing range. Without that event, the client would keep streaming
and never learn that bytes were skipped.

**SSE is used because it can carry a typed terminal event.** A raw chunked byte
stream cannot distinguish a finished command from a dropped connection, because
the bytes are identical in both cases. The stream therefore emits a typed `exit`
event carrying the status and *then* ends. Keep-alive comments fill silences, so
an agent harness thinking for two minutes does not look like a dead connection.

**stdin is opt-in and a separate request.** A command that does not ask for stdin
gets `Stdio::null()`, so nothing inherits a surprise descriptor. Writing to a
command that did not request stdin returns 409. Writes go to
`POST /v1/exec/{id}/stdin` and are never multiplexed onto the output connection.
Because the two connections are separate, a dropped attach cannot corrupt stdin.
EOF is an explicit signal rather than inferred, because a child reading stdin
cannot exit until the daemon drops its own handle. `Child::wait()` drops the
child's copy of the handle, not the daemon's.

## Reconnect-at-cursor across a real suspend, measured

The claim above, that resume by byte offset is what distinguishes this from a backend
whose reattach loses the gap, was asserted from the daemon's side only. The cut under
unit test is a response body ending without an exit frame, which is the condition the
reconnect keys on, but nothing had put a real platform suspend across a live exec.

Measured 2026-08-15, us-east-1, through `Session.spawn` in `microvms-js` against a
guest ticking once a second and also appending each tick to a file, so the guest's own
record is an oracle independent of the stream under test:

| | Observation |
| --- | --- |
| Before the suspend | `tick-1`, `tick-2`, `tick-3` delivered on the handle's `stdout` |
| Suspend / resume | `SUSPENDED`, held ~20s, `RUNNING` |
| The handle already held | Resumed at **`tick-4`**: contiguous, no restart, no duplicate |
| Gaps recorded | **None**, on either the held handle or a fresh one |
| A fresh handle at `offset: 0` | Replayed **200 of 200** ticks, indices contiguous `1..200` |
| The guest's own file | Agreed with the stream at every point |

The property holds against the thing it was designed for, and it holds in both
shapes: the handle a caller was already reading recovers at its cursor, and a *new*
handle for the same `exec_id` replays the whole output across the suspension. The exec
itself is untouched by the freeze, which is the `PLATFORM.md` finding this depends on.

The other three `ExecProcess` properties were measured on the same VM. `stdout` and
`stderr` arrive as separate streams carrying only their own bytes, from one interleaved
SSE channel with a per-frame discriminator. `wait()` reads the exit code from
the exec record: 0 for success, or 42 for `exit 42`. It does not infer completion by
the stream ending. `kill()` terminates the process group and a second
call succeeds rather than 404ing, and `wait()` afterwards reports `signal: 15` with no
exit code, so a killed build cannot read as passing.

## Credentials for a caller's own connection

`Session::connect_headers(port)` and `connect_subprotocols(port)` hand a caller what it
needs to open a connection to another port on the same VM: the header pair for
HTTPS or the subprotocols for a WebSocket.

**Both mint a token scoped to the port they name, and that is required rather than an
implementation detail.** The service scopes a proxy token at mint time, so a token minted
for the agent port answers 403 `Access to port denied` for any other port and close code
1006 with no reason on a WebSocket. An implementation that reused the session's cached
token would therefore return a *correct-looking* port value behind a credential that does
not authorize it, which is what this client did until the live run of 2026-08-15 caught
it. See `PLATFORM.md`, "Endpoint authentication".

The scope is extended rather than replaced: a mint for a new port asks for every port
already covered plus that one, so warming the cache for a workload port does not cost the
session access to its own. One extra control-plane call per new port, cached thereafter,
and `proxy_mint_count()` is the observable that says so.

**A caller debugging a failed WebSocket should retry the same port over HTTPS with
`connect_headers`.** Every handshake failure is 1006 and none of them says why; the HTTPS
request distinguishes 403 (wrong scope) from 502 (right scope, nothing listening).

## The verified tunnel's end of stream

A tunnel opened with `identity=true` runs a Noise KK handshake before it relays data
(`crates/protocol/src/identity.rs`). Protocol 2 binds a shared prologue to this handshake.
Its fields are `microvms-tunnel`, the Noise pattern, the protocol version, and `/v1/tcp`,
each followed by a NUL byte. The guest port follows as a 16-bit unsigned value in big-endian
order. Both sides must agree on these fields and the pinned keys. The client checks the
reply and sends `microvms-tunnel-ready` as the plaintext in the first Noise transport
message. The daemon reads this ready message before it dials the guest and never passes
it on as user data. The client sends this message before any user data. A guest can send first
as soon as the daemon dials it. The daemon refuses a missing or different ready message,
with a ten-second deadline. Each chunk of user data then goes in one Noise transport
message per binary frame. The close frame that ends a tunnel is plaintext, because it has to
reach a caller whose handshake failed, so anything on the path can send one or drop the
connection. Before #342 both ends read either as a finished stream, so a transfer cut short
looked complete. The end of a verified stream is now a message inside the session:

- **The end of stream is a transport message with an empty plaintext.** Neither side relays an
  empty read, so it can't be a chunk. It decrypts only at its own position in the stream (the
  nonce is a counter), so it also proves every message before it arrived, in order, with none
  added, which is why it carries no byte counts.
- **The daemon sends it when the guest's side reaches EOF**, after the last byte the guest sent
  and before its close frame (code 1000). A failed dial, a failed guest read and a guest that
  stops accepting bytes end without one.
- **The client sends it when its local side ends**, at EOF or when the local client goes away,
  before its close frame. A WebSocket close is a whole close: the tunnel ends at the first
  side's end, and bytes the other side still had in flight aren't relayed.
- **Each side offers it in its handshake message's payload**, a flags byte of `0x01`
  (`protocol::identity::HANDSHAKE_PAYLOAD`), and holds the other side to it only when the other
  side offered it. The payloads are inside the handshake's encryption and its hash, so an
  on-path party can't strip the offer the way it could strip a query flag.

What each side does when the tunnel ends without the end of stream of a peer that offered it:

| Side | The tunnel ends with | Outcome |
| --- | --- | --- |
| Client | a close frame with code 1000 or no code, a transport error, or a hangup | `TunnelEnd::Truncated`, a failure |
| Client | a close frame with one of the daemon's failure codes | `TunnelEnd::Refused` with the code, as before |
| Daemon | a close frame, a transport error, a hangup, or a frame that doesn't authenticate | the guest connection is reset rather than closed, so a guest reading to its EOF sees a broken stream |

**Version compatibility.** Protocol 2 peers must use the shared prologue and ready message.
A peer refuses a protocol 1 handshake. Both peers stop if the handshake fails. They do not retry
with an unbound handshake or switch to a plain tunnel. Rebuild older guest images and update their
clients together before using verified tunnels. Plain tunnels and unchanged HTTP shapes keep their existing behavior.
A protocol 2 peer may omit the end-of-stream offer and end with `ClosedUnproven`. That
optional offer does not relax the rules for the prologue and ready message.

A plain tunnel (no `identity`) has no end of stream. None of its frames are authenticated, so its
close is its end.

The model in `crates/model/src/tunnel.rs` checks these rules, the two pins, and the frames'
positions over every interleaving with a path that drops, replays, swaps or forges frames and
hangs up (AGENTD-17 to AGENTD-21, BIND-21, BIND-23 and BIND-24).

## Trust boundary

The platform's `/run` hook arrives from `127.0.0.1` and is indistinguishable at
the socket level from a request sent by a process inside the VM (measured; see
`PLATFORM.md`). Filtering by source address therefore cannot separate the
platform from an in-VM process, so it provides no protection here.

The remaining defenses, all checked in `crates/model/`, are the following:

1. Bootstrap is one-shot, so a losing racer never replaces the winner's token.
2. A post-bootstrap hijack attempt is refused at the hook with 409 and at the
   control API with 401.
3. The agent token never enters an exec'd child's environment.

One risk remains. The design assumes the daemon is the container `CMD` and that
the harness issues its first exec only after readiness. The daemon does not
enforce this invariant. A base image that starts its own background process
before bootstrap breaks it. `crates/model/` includes that configuration and reports the
counterexample path, so the consequence of breaking the invariant is a checked
result rather than a prediction. Enforcing the invariant is the responsibility
of whoever builds the image, not of this daemon.

## Execution deadlines and cancellation (2026-09-17 implementation)

An exec's daemon deadline runs independently of the attached client. Polled
outcomes and streamed exit events include `timed_out: true` when that deadline
fires, preserving the raw exit code and signal. A process that handles SIGTERM
and exits zero after its deadline still did not complete within budget; clients
must not report it as successful. Older daemons omit this additive field and
cannot distinguish a deadline from other signal deaths.

The deadline, explicit kill route, and opt-in `reap_group_on_exit` all inspect
live process-group members while escalating SIGTERM to SIGKILL. The default kill
grace is 10 seconds. Completion of the leader or closure of its pipes does not
prove its descendants stopped. Cancellation after a leader's exit still kills
SIGTERM-ignoring survivors. Processes that change process group or session may
escape this mechanism; external platform VM termination supplies the final cap.
These changes have local Linux regression coverage; AWS evidence must be recorded
separately for the image and deployment being used.

## Client response handling

The production client does not follow redirects from the daemon. A local HTTP
port forward returns the redirect's status, headers, and body to the caller. It
does not request the new URL.

The client caps bodies it buffers at 512 MiB for success and 64 KiB for errors.
It checks both the declared length and the bytes read from the body. These caps
do not apply to successful streams. If a request to open a stream fails, the
client reads the error body with the 64 KiB cap and an idle timeout.
