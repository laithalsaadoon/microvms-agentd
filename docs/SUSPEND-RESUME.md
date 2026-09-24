# Suspend and resume

**Suspend always keeps the VM's full memory and disk, and nothing about that is
configurable.** `SuspendMicrovm` and `ResumeMicrovm` take only the VM ID. What
a caller controls is *when* suspension happens (the idle policy and explicit
calls) and *what the workload does around it* (the image's `/suspend` and
`/resume` lifecycle hooks).

Each statement below is marked by its source:

- **AWS**: AWS documentation or the service model.
- **Measured**: a live observation recorded in [Platform](PLATFORM.md), with
  its date. Measurements are single samples unless stated otherwise.
- **Unknown**: not documented and not measured.

## What survives

| Guest state | After resume | Source |
|---|---|---|
| Memory and running processes | Continue where they stopped: same boot, same PIDs, same in-memory values | AWS; measured 2026-08-05 and 2026-09-23 |
| Disk, `/tmp`, `/dev/shm`, `/workspace` | Preserved | AWS; measured 2026-09-23 |
| agentd state: agent token, exec records, unread output | Preserved; no second bootstrap | Measured 2026-08-05 and 2026-09-23 |
| Endpoint URL | Unchanged | Measured 2026-08-05 and 2026-09-23 |
| Exec output stream | Reconnects at its byte offset | Measured 2026-08-15 |
| Held outbound TCP and TLS connections | **Aborted** (`ECONNABORTED`) | AWS says they may end; measured 2026-09-23 |
| New outbound connections | Work | Measured 2026-09-23 |

AWS: [`SuspendMicrovm`](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_SuspendMicrovm.html)
"preserv[es] its full memory and disk state", and
[`ResumeMicrovm`](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_ResumeMicrovm.html)
restores the VM "with all state intact". The
[launch guide](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html)
recommends the `/suspend` hook to close network connections and the `/resume`
hook to re-establish them and refresh credentials.

## Time jumps

Measured 2026-09-23: wall-clock time (`time.time()`), `CLOCK_MONOTONIC`, and
`CLOCK_BOOTTIME` all advanced by the full suspended time (124 seconds), equal
to within microseconds. Any timeout, lease, or deadline inside the guest, including
those measured with a monotonic clock, expires at once on resume. Tokens and
credentials keep expiring in wall-clock time while the VM is suspended.

## What triggers suspend and resume

- **Idle means no inbound requests through the endpoint.** AWS (the service
  model's `IdlePolicy`). Work inside the guest does not count, and neither do
  loopback requests from the guest. Measured 2026-08-15 and 2026-09-23: a
  CPU-busy exec was suspended 60–70 seconds after the last request with
  `maxIdleDurationSeconds=60`. External `/v1/health` polls kept a VM running,
  and so did a held-open exec output stream with steady output (150 seconds,
  2026-09-23). Whether the open connection or the data flowing through it is
  what counts is unknown.
- **Explicit suspend is a control-plane call** (`SuspendMicrovm`), not a
  daemon route. Measured 2026-09-23: `SUSPENDED` about 1–2 seconds after the call, and
  `RUNNING` about 1.5 seconds after a resume call.
- **Auto-resume holds the request.** AWS: with `autoResumeEnabled`, Lambda holds
  an inbound request while the VM resumes, including the `/resume` hook.
  Measured 2026-09-23: the first request succeeded in about 1.2 seconds, and
  `GetMicrovm` briefly reported `PENDING` before `RUNNING`.
- **The suspended window ends in termination.** AWS: `suspendedDurationSeconds`
  is the maximum time a VM stays suspended before it is automatically
  terminated. Measured 2026-09-23, one sample: with a 60-second window, the VM
  was still `SUSPENDED` 240 seconds after an explicit suspend, and
  `ResumeMicrovm` was accepted. This needs a repeated, longer run before either
  behavior is relied on.
- **The lifetime cap includes suspended time.** AWS: `maximumDurationInSeconds`
  (at most 28,800) covers running and suspended time.

The idle policy is set at `RunMicrovm` and cannot be changed afterward.

## Lifecycle hooks: the only control point

Images declare `suspend` and `resume` hooks, each with a 1–60 second timeout
(AWS, `MicrovmHooks`). agentd serves both and records each call on `/v1/health`.
It does not yet run workload code on them;
[#198](https://github.com/laithalsaadoon/microvms-agentd/issues/198) tracks
that. Until then, a workload should treat every outbound connection as possibly
dead after any pause and reconnect on error.

## Capacity and cost

- Suspended VMs count against the account's MicroVM memory quota (AWS,
  [launch guide](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html)).
- A suspended VM pays no compute. It pays snapshot storage, one snapshot write
  per suspend, and one snapshot read per resume
  ([pricing](https://aws.amazon.com/lambda/pricing/); dated rates are in
  [Platform](PLATFORM.md#what-actually-costs-money)). Whether suspend bills the
  configured or the used memory is unknown.
- AWS's [FAQ](https://aws.amazon.com/lambda/faqs/) states suspended state is kept
  for up to eight hours.

## VMs from one image share a snapshot

AWS's [image snapshot guidance](https://docs.aws.amazon.com/lambda/latest/dg/microvms-images-snapshots.html)
says to generate unique IDs, secrets, and random values after a VM starts, in
the `/run` hook, because every VM from an image starts from one memory
snapshot. Measured 2026-09-23: two VMs from one image had an identical
`boot_id` and `machine-id`, while the kernel random UUID differed. [#205](https://github.com/laithalsaadoon/microvms-agentd/issues/205)
tracks per-VM identity.

## Unknowns

- Whether an open WebSocket or SSE connection with no data counts as activity.
- Whether a request with a missing or invalid token triggers auto-resume.
- What happens when a `/suspend` or `/resume` hook fails or times out.
- Whether guest metadata credentials refresh on resume.
- Resume latency at larger memory sizes, and anything outside us-east-1.
