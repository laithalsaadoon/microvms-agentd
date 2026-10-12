# The trust contract for a control daemon inside a Lambda MicroVM

`agentd` authenticates control requests inside a VM that may run hostile
workloads. It does not isolate itself from a root workload. This document
states the guarantees, deployment assumptions, and limits. [Protocol](PROTOCOL.md)
defines the wire behavior; [Platform](PLATFORM.md) records dated AWS evidence.
For deployment choices and task execution, use [Embedding](EMBEDDING.md) and
[Networking](NETWORKING.md) alongside this contract.

## The threat model

A guest process can connect to the daemon over loopback, including lifecycle
hook paths. A process without the agent token must not gain control through
an authentication bypass or a second bootstrap. The token lives in daemon
memory and is not written to disk or logged by the daemon.

An authorized caller can execute as root. A root workload can read
daemon memory with `ptrace` or `/proc/<pid>/mem`, change files, or exhaust guest
resources. User demotion changes the workload UID within the same sandbox. The model
and authentication tests do not prove isolation from guest root.

## Platform isolation guarantees

AWS documents two relevant properties:

- Endpoint requests require a proxy credential scoped to the VM and permitted
  ports, with a maximum lifetime of 60 minutes. This credential is separate
  from the agent token. Clients refresh it for later requests.
- External traffic starts only after the run hook returns HTTP 200. This
  protects launch-time bootstrap from external traffic, but not from a
  process already running inside the guest.

Port scope was measured on 2026-08-15, us-east-1, API `2025-09-09`: a token
for 9000 could not access 8080. Guest loopback traffic bypasses this proxy.

## Local forwards

`microvm port-forward` requires a `Host` header matching its bound address and
port. Loopback bindings accept `localhost` and loopback IP literals. It attaches
the session's proxy credential to the upstream request. The listener
is available to other local users. Keep the guest app's own user
authentication enabled; the proxy credential authenticates the upstream hop,
not the user of that app. The code-server example uploads a protected
password configuration before starting the IDE.

## Why source-address filtering is wrong, not merely weak

Measured 2026-08-04, us-east-1, API `2025-09-09`: platform hooks and proxied
control requests arrived from `127.0.0.1`. A loopback filter cannot identify
AWS and rejecting loopback rejects legitimate bootstrap. The platform does
not present an authentication credential to the run hook.

## The five defenses that remain

1. **One-shot bootstrap.** The first valid run hook installs the token and
   returns 200. An identical replay returns 200; a different token returns
   409 without modifying state. Replays must remain idempotent because a
   failed run hook can cause AWS to terminate the VM. Implemented in
   `crates/agentd/src/state.rs` and `crates/agentd/src/routes.rs`.
2. **Constant-time comparison on bytes.** Bootstrap and request guards compare
   equal-length byte strings with `subtle::ct_eq`, avoiding Unicode decoding
   errors. Token length remains observable. Implemented in `crates/agentd/src/auth.rs`.
3. **Authorization before request-body processing.** Protected routes reject
   unauthorized requests before parsing or buffering their body. A bounded
   drain (64 KiB by default) reduces connection resets; excess data closes the
   connection. The drain also has a two-second deadline. Hook bodies have a
   separate 32 KiB cap and five-second collection deadline. The server bounds
   active connections, header-read time, and idle time; [Protocol](PROTOCOL.md)
   lists these limits.
4. **Explicit child environments.** Exec uses `env_clear()` and adds only the
   launch/request environment and, for a user with a passwd row, that row's
   `HOME`, `USER` and `LOGNAME`. The installed agent token is never implicitly
   inherited. Caller-supplied environment values are intentionally available
   to child processes. User changes use `Command::uid`/`gid`; avoid Rust
   `pre_exec` closures in a multithreaded process because inherited locks can
   deadlock after fork. Implemented in `crates/agentd/src/exec.rs` and
   `crates/agentd/src/exec_start.rs`.

   **The one opt-in exception is `inherit_image_env`.** A start request that
   sets it starts the child from the environment the daemon inherited as the
   container `CMD`, snapshotted at startup, beneath everything else. That
   snapshot is the image's `ENV` plus whatever the platform set for the
   process, minus every `AGENTD_*` variable. It never holds the token, which
   arrives in the run hook after the snapshot is taken and is never written to
   the process environment. Treat it like the image: anything an image author
   put in an `ENV` line reaches every child that asks, so keep secrets out of
   images (they already reach anyone who can pull the image). Health reports
   only the snapshot's key count, never its values, because health is
   unauthenticated. The default stays off, and with it off no variable from the
   daemon's own environment reaches a child.
5. **Distinct status codes.** Protected routes return 503 before bootstrap and
   401 for an invalid token afterward. Unknown routes return 404. `/v1/health`
   and `/v1/schema` remain unauthenticated; health exposes bootstrap state so
   callers can check readiness.

`crates/model/` explores bootstrap interleavings and tests both compliant and broken
deployments. `crates/model/src/tunnel.rs` explores the verified tunnel's handshake, frames
and end of stream against a path that drops, replays, swaps or forges frames and hangs up,
and with each pin and check switched off in turn. Unit and conformance tests exercise the
implementation. The models do not cover identity repair, filesystem confinement, or all
Linux process behavior.

## Threats and the tests that guard them

Each row names a threat, the requirement key that states the defense (in
`verify/spec/agentd.symspec.json` or `verify/spec/core.symspec.json`), and a test that fails
when the defense is removed. `mise run trace:check` reads this table: it fails
on a key neither spec defines, on a guard that isn't a running test at that path
or doesn't name one of its row's keys, and on a known gap that names no issue,
and it renders the table in [Traceability](TRACEABILITY.md). A guard is written
`path::test`.

| Threat | Requirement | Guard | Status |
|---|---|---|---|
| An in-VM process races the platform to the bootstrap hook | `AGENTD-1`, `AGENTD-3` | `crates/agentd/tests/model_conformance.rs::slice_0_of_the_walk_replays_against_the_daemon`, `crates/agentd/src/state.rs::identical_replay_succeeds_and_a_different_token_conflicts` | guarded; a process that starts before the daemon still wins, which is the unenforced invariant below |
| A caller holding the agent token but not the host key opens a verified tunnel | `AGENTD-17` | `crates/agentd/src/tunnel_identity.rs::only_the_pinned_host_key_completes_a_handshake`, `crates/agentd/tests/tunnel_relay.rs::a_valid_token_with_the_wrong_host_key_is_refused`, `crates/model/src/tunnel.rs::agentd_17_a_token_holder_without_the_host_key_is_refused_before_any_dial` | guarded; under KK no one-sided change turns the pin off, so its seeded fault turns it off in the tunnel model |
| A verified tunnel's handshake fails, or the VM has no key, and the guest service is reached anyway | `AGENTD-18` | `crates/agentd/tests/tunnel_relay.rs::a_refused_caller_never_causes_a_guest_connection`, `crates/agentd/tests/tunnel_relay.rs::identity_against_a_seedless_vm_is_refused_not_downgraded`, `crates/model-conformance/tests/tunnel_confirmation.rs::a_handshake_without_confirmation_leaves_the_guest_listener_idle`, `crates/model-conformance/tests/tunnel_end_of_stream.rs::a_verified_tunnel_requires_the_requested_guest_port` | guarded |
| A guest answers a verified tunnel's handshake with a key other than the pinned VM key | `BIND-21` | `crates/microvms-core/tests/tunnel_end_to_end.rs::a_wrong_pin_fails_closed_with_a_diagnosis`, `crates/microvms-core/tests/tunnel_end_to_end.rs::a_reply_that_does_not_verify_against_the_pin_fails_the_tunnel` | guarded |
| A guest or the path replays, reorders or forges tunnel frames after the handshake | `AGENTD-21`, `BIND-24` | `crates/agentd/tests/tunnel_relay.rs::agentd_21_a_replayed_frame_ends_the_tunnel_unrelayed`, `crates/agentd/tests/tunnel_relay.rs::agentd_21_a_forged_frame_ends_the_tunnel_unrelayed`, `crates/microvms-edges/tests/tunnel_end_of_stream.rs::bind_24_a_replayed_frame_fails_the_tunnel_unwritten`, `crates/microvms-edges/tests/tunnel_end_of_stream.rs::bind_24_a_forged_frame_fails_the_tunnel_unwritten`, `crates/model/src/tunnel.rs::bind_24_the_model_finds_a_replay_when_frames_carry_no_position`, `crates/agentd/src/tunnel_fuzz.rs::frames_open_in_order_and_the_callers_end_only_after_every_chunk`, `crates/microvms-edges/src/session/tunnel_fuzz.rs::frames_open_in_order_and_the_daemons_end_only_after_every_chunk` | guarded |
| A guest streams hostile server-sent events to the client | `BIND-22` | `crates/microvms-app/src/session/sse.rs::an_unterminated_stream_is_refused_at_the_pending_ceiling`, `crates/microvms-app/src/session/sse.rs::an_unrecognized_or_unparseable_frame_is_dropped_rather_than_raised`, `crates/microvms-app/src/session/sse_fuzz.rs::hostile_stream_bytes_stay_bounded_and_every_event_round_trips` | guarded |
| A replaced or tampered daemon release asset | `BIND-18` | `crates/microvms-edges/src/provision/release.rs::another_signer_identity_is_refused`, `crates/microvms-edges/src/provision/release.rs::one_flipped_byte_in_the_asset_is_refused` | guarded |
| A release directory (`$MICROVM_RELEASE_DIR`) holding an asset and a matching `SHA256SUMS` but no bundle, or another release's files under the requested tag | `BIND-18` | `crates/microvms-edges/src/provision/release.rs::a_release_directory_without_its_bundle_is_refused_despite_a_matching_checksum`, `crates/microvms-edges/src/provision/release.rs::another_releases_files_under_the_requested_tag_are_refused` | guarded |
| An on-path party ends a verified tunnel early, with a plaintext close frame or by dropping the connection | `BIND-23`, `AGENTD-19`, `AGENTD-20` | `crates/microvms-edges/tests/tunnel_end_of_stream.rs::a_verified_tcp_consumer_observes_the_stream_verdict`, `crates/model-conformance/tests/tunnel_end_of_stream.rs::bind_23_a_close_frame_forged_mid_stream_ends_truncated`, `crates/model-conformance/tests/tunnel_end_of_stream.rs::bind_23_a_connection_dropped_mid_stream_ends_truncated`, `crates/model-conformance/tests/tunnel_end_of_stream.rs::bind_23_the_daemons_end_of_stream_withheld_ends_truncated`, `crates/model-conformance/tests/tunnel_end_of_stream.rs::agentd_20_the_clients_end_of_stream_withheld_resets_the_guest`, `crates/agentd/tests/tunnel_relay.rs::agentd_19_a_guest_eof_reaches_the_caller_as_the_end_of_stream` | guarded. A protocol 2 peer that omits the end-of-stream offer ends `ClosedUnproven`. Each side refuses a protocol 1 handshake |

**The end of stream.** A verified tunnel's WebSocket close frame is sent in the
clear, so anything on the path can send one or drop the connection. The end of a
verified stream is therefore a Noise message inside the session, sent before the
close, and a side that offered it in the handshake is held to it
([Protocol](PROTOCOL.md), "The verified tunnel's end of stream"). Without it the
client reports the tunnel `Truncated` rather than closed. Its local TCP serving
loop resets the local connection, and the daemon resets the guest connection, so a stream cut short on the path reads as
a failure at both ends instead of a finished transfer. That's detection, not
prevention: an on-path party can still end any tunnel early. Protocol 2 peers must agree on the handshake context and ready message.
Within that version, a peer that omits the optional end-of-stream offer ends
`ClosedUnproven`. Check a transfer's length or digest where it matters.

## The unenforced invariant

**Run the daemon as the image's `CMD`, and start workloads only after bootstrap
and readiness.** Use `ENTRYPOINT []` and `CMD ["/agentd"]`. Review the base image
and startup behavior for processes that could run before the daemon.

A pre-existing hostile process could win the first run-hook request and
install its own token. One-shot bootstrap then preserves the wrong principal.
The daemon cannot verify the image's full startup history. The model includes
this misconfiguration and confirms an attacker can win that race.

## Identity repair for derived VMs

Restored images can share files and cached userspace state. Firecracker
VMGenID and Linux kernel reseeding do not repair identifiers already stored
on disk or cached by applications. Optional identity repair runs at the first
successful run hook and reports each step through health (`identity_steps`).

It cannot run earlier. The daemon starts in the image-build VM, so repair at
daemon start is captured by the snapshot and every VM launched from one image
inherits the same "repaired" machine-id (measured 2026-09-23, two VMs from one
image). The run hook is the first per-VM moment, and nothing has read the
identifiers by then: the platform forwards no traffic until the hook answers,
and workloads start only after readiness.

`crates/agentd/src/identity.rs` uses a fresh 128-bit seed to:

- Rewrite `/etc/machine-id` and set the hostname.
- Remove `/var/lib/systemd/random-seed` rather than sharing its snapshot value.
- Attempt to shadow `/proc/sys/kernel/random/boot_id` with a bind mount.
- Remove configured cached identity files, including `/var/lib/dbus/machine-id`
  by default.

Repair cannot revoke values a process already read, update arbitrary
application caches, or override missing kernel capabilities. A bind mount is
namespace-local. Failures leave the daemon serving and set `identity_degraded`;
callers that require repaired identity must check it, and `identity_steps` names
the failed step. `identity_repaired` is false until repair runs on this VM, and
stays false when repair is switched off.

`boot_id` is fixed by the kernel at boot. `procfs` refuses writes to it even for
root, so it can only be shadowed by a bind mount, which needs `CAP_SYS_ADMIN`;
`sethostname` needs it too. Without those capabilities both steps fail with
`EPERM`, and VMs from one image keep the same `boot_id` and hostname while their
machine-ids differ. Do not key uniqueness on either.

An image built with `--repair-identity` requests
`additionalOsCapabilities: ["ALL"]`, and its guest gets every capability
(measured 2026-10-12). All three steps succeeded on such a guest (measured
2026-08-06 and 2026-09-24, [Platform](PLATFORM.md)). A guest from an image built
without it lacks `CAP_SYS_ADMIN`, and passing `--repair-identity` to
`microvm run --image` doesn't add it, because the flag applies only to an image
`run` builds. `identity_steps` reports each step either
way, so a caller that needs repaired identity checks it and `identity_degraded`.

## Workload hook handlers

An executable at `<hooks dir>/<hook>` runs when the platform posts that
lifecycle hook ([Protocol](PROTOCOL.md), "Workload hook handlers"). It is
image-owned code with the daemon's privileges:

- It runs as root, like the daemon, with the daemon's environment, the launch
  environment, and `AGENTD_HOOK`. The agent token is never in either
  environment. Keep secrets out of shared images; a handler is part of the image.
- The hook routes are unauthenticated and reachable over loopback from inside
  the guest, so any guest process can make a handler run. A handler must be
  idempotent and safe to run when no real lifecycle event happened. The hook
  log's cap bounds the number of runs, and handlers run one at a time.
- Output goes to the daemon's log, not to `/v1/health`, because health needs no
  agent token. The outcome (exit code, signal, timeout, duration) is on health.
  The handler budget covers its leader and output collection. At the deadline
  the daemon signals the process group and stops collecting output. Processes
  that start a separate group are outside group termination.
- A handler failure does not change the successful hook acknowledgment.
  Request-body validation and bootstrap checks run before the handler.

## Tunnel identity: proving which VM answered

`microvm run --keep --identity` provisions keys for
`microvm tunnel --verify-identity`. A Noise KK handshake runs inside the
WebSocket and terminates in the daemon. The host supplies the VM seed and
host public key through one-shot bootstrap; both sides pin the other's public
key. The handshake binds the protocol version, route, and guest port. The daemon
waits for an authenticated session confirmation before connecting to the guest,
including when the guest protocol sends first. Subsequent tunnel data is encrypted
with ChaCha20-Poly1305, beyond the proxy's TLS termination, and the stream's end is
a message inside the same session. See `crates/protocol/src/identity.rs`.

The trust root includes the AWS control plane and run-hook delivery. The VM's
private seed goes in the `RunMicrovm` launch payload. The endpoint proxy also
handles the agent token for authorized root control. Protecting the pinned key
depends on trusted control-plane and bootstrap delivery, authorized control, and
guest-root access. The Noise proof ends in the daemon and protects session data
under those assumptions. It does not establish independence from those trusted
components.

The local name record stores the host secret and VM public pin; it does not
retain the VM secret. A stolen record can authorize the same tunnels its
agent token already permits, but does not provide the VM key needed to
impersonate it. Protect local records accordingly.

Use `tunnel --name <NAME> --verify-identity` with the local record. To transfer
a record, pipe it into `microvm attach --from -`, then use its registered name.
For explicit connection details, `--agent-token-file` and
`--identity-host-seed-file` read a protected file or stdin with `-`.
The group and other permission bits must be clear on Unix. Only one input can read stdin
per invocation. The literal secret flags remain supported and
print a warning recommending these input paths.

This proves key possession by the endpoint, not that the guest remains
uncompromised. A root workload may read daemon keys from memory. It also does
not authenticate arbitrary plain HTTP or unverified tunnel traffic.

## The execution role is the boundary

Measured 2026-09-11 and 2026-09-12, us-east-1, API `2025-09-09`: the guest
could retrieve the execution role's temporary credentials from Firecracker
MMDS at `169.254.169.254`. Both root and uid 1000 could do so, with and without
the managed internet connector. An empty child environment does not hide
metadata credentials.

Grant the execution role only permissions every workload may use. The
conformance role grants CloudWatch logging within `/aws/lambda-microvms/` in
its region and account; live verification checks both actions and resource
scope. Deliver additional workload credentials with their own scope and lifetime
instead of broadening the shared VM role.

Blocking MMDS inside the guest takes `CAP_NET_ADMIN`. A guest from an image
built without `--repair-identity` lacks it, and route, rule, and link changes
return `EPERM`. On a guest from an image built with it, root can replace the guest's host route for `169.254.169.254`
with a blackhole. Afterward, a process that isn't root can neither reach MMDS
nor remove the route ([Platform](PLATFORM.md), measured 2026-10-12). Any root
process can remove it, including an exec that doesn't ask for another user. The
block protects only workloads that run as another user. VPC internet isolation
does not remove metadata access or make an overprivileged role safe.

## Outside this contract

**Internet isolation.** No internet egress requires a VPC without an internet
gateway or NAT gateway, attached with a custom VPC connector. Check subnet
routes for other paths, including IPv6, transit networks, and proxies.
[Networking](NETWORKING.md) gives the boto3 setup. Connector lifecycle belongs
to the separate Lambda core API; a MicroVM launch supplies its ARN.

Omitting `--egress` only omits the managed connector. Default-network tests
still reached public sites. `--deny-egress` sets proxy variables and cannot
constrain a workload that ignores them. The package does not audit custom
connector routing; its posture values are conservative:

| `egressPosture` | Meaning |
|---|---|
| `open` | Managed internet connector requested |
| `unsealed` | Internet isolation has not been established by the client |
| `best-effort` | Guest proxy variables discourage outbound HTTP clients |
| `sealed` | Retained for stored labels; not inferred from a launch request |

**Credential rotation.** The serialized `runHookPayload` has a measured
4096-byte limit shared by the token, environment, and identity material. It is
delivered once and is not a rotation channel. The client validates the full
payload before calling AWS. Use an authenticated session or an external
credential broker for subsequent delivery and refresh.

**Workload confinement.** The workload and daemon share the same seccomp and user namespace; these
do not provide a boundary between them. Single-file APIs intentionally accept arbitrary
guest paths because the same authorized principal can execute as root. Tar
uploads are confined because member names originate in an archive and may
not be paths its uploader intended; see [Protocol](PROTOCOL.md).
