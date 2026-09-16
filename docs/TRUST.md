# The trust contract for a control daemon inside a Lambda MicroVM

`agentd` authenticates control requests inside a VM that may run hostile
workloads. It does not isolate itself from a root workload. This document
states the guarantees, deployment assumptions, and limits. [Protocol](PROTOCOL.md)
defines the wire behavior; [Platform](PLATFORM.md) records dated AWS evidence.

## The threat model

A guest process can connect to the daemon over loopback, including lifecycle
hook paths. A process without the agent token must not gain control through
an authentication bypass or a second bootstrap. The token lives in daemon
memory and is not written to disk or logged by the daemon.

An authorized caller can execute as root. A root workload can potentially read
daemon memory with `ptrace` or `/proc/<pid>/mem`, change files, or exhaust guest
resources. User demotion is a convenience, not a separate sandbox. The model
and authentication tests do not prove isolation from guest root.

## What the platform gives you for free

AWS documents two relevant properties:

- Endpoint requests require a proxy credential scoped to the VM and permitted
  ports, with a maximum lifetime of 60 minutes. This credential is separate
  from the agent token. Clients refresh it for later requests.
- External traffic starts only after the run hook returns HTTP 200. This
  protects launch-time bootstrap from external traffic, but not from a
  process already running inside the guest.

Port scope was measured on 2026-08-15, us-east-1, API `2025-09-09`: a token
for 9000 could not access 8080. Guest loopback traffic bypasses this proxy.

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
   `agentd/src/state.rs` and `agentd/src/routes.rs`.
2. **Constant-time comparison on bytes.** Bootstrap and request guards compare
   equal-length byte strings with `subtle::ct_eq`, avoiding Unicode decoding
   errors. Token length remains observable. Implemented in `agentd/src/auth.rs`.
3. **Authorization before request-body processing.** Protected routes reject
   unauthorized requests before parsing or buffering their body. A bounded
   drain (64 KiB by default) reduces connection resets; excess data closes the
   connection rather than causing an unbounded allocation.
4. **Explicit child environments.** Exec uses `env_clear()` and adds only the
   launch/request environment. The installed agent token is never implicitly
   inherited. Caller-supplied environment values are intentionally available
   to child processes. User changes use `Command::uid`/`gid`; avoid Rust
   `pre_exec` closures in a multithreaded process because inherited locks can
   deadlock after fork. Implemented in `agentd/src/exec.rs`.
5. **Distinct status codes.** Protected routes return 503 before bootstrap and
   401 for an invalid token afterward. Unknown routes return 404. `/v1/health`
   and `/v1/schema` remain unauthenticated; health exposes bootstrap state so
   callers can check readiness.

`model/` explores bootstrap interleavings and tests both compliant and broken
deployments. Unit and conformance tests exercise the implementation. The
model does not cover identity repair, filesystem confinement, or all Linux
process behavior.

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
on disk or cached by applications. Optional identity repair runs during
bootstrap and reports its result through health.

`agentd/src/identity.rs` uses a fresh 128-bit seed to:

- Rewrite `/etc/machine-id` and set the hostname.
- Remove `/var/lib/systemd/random-seed` rather than sharing its snapshot value.
- Attempt to shadow `/proc/sys/kernel/random/boot_id` with a bind mount.
- Remove configured cached identity files, including `/var/lib/dbus/machine-id`
  by default.

Repair cannot revoke values a process already read, update arbitrary
application caches, or override missing kernel capabilities. A bind mount is
namespace-local. Failures leave the daemon serving and set `identity_degraded`;
callers that require repaired identity must check it. Opting out is reported
separately through `identity_repaired`.

The August 2026 measurement succeeded with `additionalOsCapabilities: ["ALL"]`.
September measurements found a restricted capability set even when repair was
requested. Those observations are both retained in [Platform](PLATFORM.md);
requesting `ALL` is not proof that every repair operation succeeded.

## Tunnel identity: proving which VM answered

`microvm run --keep --identity` provisions keys for
`microvm tunnel --verify-identity`. A Noise KK handshake runs inside the
WebSocket and terminates in the daemon. The host supplies the VM seed and
host public key through one-shot bootstrap; both sides pin the other's public
key. Subsequent tunnel data is encrypted with ChaCha20-Poly1305, beyond the
proxy's TLS termination. See `protocol/src/identity.rs`.

The local name record stores the host secret and VM public pin; it does not
retain the VM secret. A stolen record can authorize the same tunnels its
agent token already permits, but does not provide the VM key needed to
impersonate it. Protect local records accordingly.

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
conformance role grants CloudWatch logging; its policy is checked during live
verification. Deliver additional workload credentials with their own scope
and lifetime instead of broadening the shared VM role.

Tested in-guest metadata blocks failed: the capability set lacked
`CAP_NET_ADMIN`, and route/rule/link changes returned `EPERM`. Relevant sysctl
paths were read-only. No tested guest-side block was effective. VPC internet
isolation does not remove metadata access or make an overprivileged role safe.

## What this contract does not cover

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

**Workload confinement.** There is no seccomp or user-namespace boundary
between workload and daemon. Single-file APIs intentionally accept arbitrary
guest paths because the same authorized principal can execute as root. Tar
uploads are confined because member names originate in an archive and may
not be paths its uploader intended; see [Protocol](PROTOCOL.md).
