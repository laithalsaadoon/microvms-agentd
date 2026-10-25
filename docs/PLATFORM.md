# AWS Lambda MicroVMs: measured platform behavior

A compact record of runtime observations, with one current answer to each
question. Unless stated otherwise, measurements used us-east-1, MicroVM API
`2025-09-09`, and the managed ARM64 base image `al2023-1`, built from
`amazonlinux:2023-minimal`. Each entry carries its measurement date, which dates
an observation rather than guaranteeing current service behavior. A newer
measurement replaces the entry it contradicts, and git history holds the entry
it replaced. Use [Protocol](PROTOCOL.md) for the current daemon contract and the
installed `microvm manifest` for the CLI surface.

For current operation shapes, see the [AWS MicroVM documentation](https://docs.aws.amazon.com/lambda/latest/microvm-api/Welcome.html).
For internet isolation, use [Networking](NETWORKING.md): a VPC without an IGW
or NAT gateway is required; connector omission and proxy variables do not seal
the default network. VPC connectors belong to the separate Lambda core API, not
the MicroVM API.

## The service provides no exec and no file transfer

Lambda MicroVMs have no addressable command-execution or file-transfer API;
`agentd` supplies those operations. The service's shell is a PTY reached through
`CreateMicrovmShellAuthToken`, described under the shell endpoint below. A PTY
does not provide detached exec IDs, separated output streams, or exit-status
records.

## Hooks are served under a fixed prefix, and two of them are build-time

Measured 2026-08-05. Hooks are
`POST /aws/lambda-microvms/runtime/v1/<hook>` for `ready`, `validate`, `run`,
`resume`, `suspend`, and `terminate`. `ready` and `validate` run during image
build, before token delivery, and must succeed without bootstrap. The model
allows 3600-second build-hook timeouts and 60-second runtime-hook timeouts.

## `runHookPayload` arrives wrapped, not as the body

Measured 2026-08-05. The platform sends an outer JSON object containing the
string supplied to `RunMicrovm`:

```json
{"runHookPayload": "{\"agent_token\": \"...\"}"}
```

Decode both layers. Reading `agent_token` from the outer object fails the run
hook; AWS can terminate the VM before forwarding traffic. Read `stateReason`
from `GetMicrovm` when launch fails.

## The `runHookPayload` ceiling is 4096 bytes, and the service model states it twice, differently

Measured 2026-08-07 with a token-only payload and 2026-08-15 with a payload
holding both `agent_token` and an `env` map; model read 2026-10-12 with botocore
1.43.106. The inclusive limit is **4096 bytes of the complete serialized
payload**, whatever fields it combines. The model's member documentation says
16,384, but its referenced shape says 4096. The client checks the smaller,
measured limit against the full serialized string before calling AWS.

| Payload size | Measured response with an invalid image identifier, both payload kinds |
|---|---|
| 4096 bytes | Passed length validation, then rejected the image ARN |
| 4097 bytes | Rejected `runHookPayload` length before image resolution |

The budget includes the token, launch environment, identity material, and JSON
escaping. See the
[AWS RunMicrovm documentation](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_RunMicrovm.html) for current shape constraints.

## A null-message `AccessDeniedException` can mean region availability or IAM

Measured 2026-10-17 in `us-east-1`, API version `2025-09-09`, with
`CreateMicrovmImage` using tags on an `al2023-1` aarch64 base image. A role
permitted to create the image but lacking `lambda:TagResource` received HTTP
403 with a null message. CloudTrail recorded `AccessDenied` and identified
`lambda:TagResource` as the denied action on `*`. Granting that action with an
`aws:RequestTag` condition allowed the next call. Evidence: the API response
and CloudTrail event recorded in [issue #673](https://github.com/laithalsaadoon/microvms-agentd/issues/673).

A null message alone does not distinguish this IAM denial from a region
without MicroVMs. Measured 2026-10-24 at about 01:05Z, API version
`2025-09-09`, with `aws lambda-microvms list-microvms --max-results 1` in each
region the test account has enabled: `ListMicrovms` answered in `us-east-1`,
`us-east-2`, `us-west-2`, `ca-central-1`, `sa-east-1`, `eu-west-1`,
`eu-west-2`, `eu-west-3`, `eu-central-1`, `eu-north-1`, `ap-south-1`,
`ap-southeast-1`, `ap-southeast-2`, `ap-northeast-1`, and `ap-northeast-2`.
`us-west-1` and `ap-northeast-3` returned the null-message
`AccessDeniedException`. AWS hasn't launched MicroVMs in `ca-central-1`,
`sa-east-1`, `eu-west-2`, `eu-west-3`, or `ap-northeast-2`, even though they
answered: no AWS launch post names them, the Pricing API prices nothing there,
and AWS's regional availability data lists the service as planned there, as it
does for `us-west-1` and `ap-northeast-3`. The Pricing API and AWS's launch
posts list the same regions: `us-east-1`, `us-east-2`, `us-west-2`,
`eu-west-1`, `ap-northeast-1`, `ap-south-1`, `ap-southeast-1`,
`ap-southeast-2`, `eu-central-1`, `eu-north-1`, `ap-east-1`, `ap-southeast-5`,
`ap-southeast-7`, and `eu-south-2`. A listing that answers doesn't prove that
a region runs MicroVMs. Evidence: the listing responses, Pricing API query,
launch posts, and availability data summarized in
[issue #743](https://github.com/laithalsaadoon/microvms-agentd/issues/743).

In a known supported region, check the policy, including implied actions for
tags. Retrieve recent events in that region with:

```bash
aws cloudtrail lookup-events --region us-east-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateMicrovmImage \
  --query 'Events[].CloudTrailEvent' --output text
```

Inspect the matching event's `errorCode` and `errorMessage` for the denied
action. [CloudTrail event lookup](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/view-cloudtrail-events-cli.html)
returns the event record as JSON text. The shared Lambda endpoint resolver
can list ordinary Lambda regions that do not support MicroVMs. SDK endpoint
availability is not a MicroVM service availability check. Since 1.3.0 the
package takes any well-formed region name. When a call in a region outside the
14 measured ones gets the null-message denial, its error says to verify that
AWS runs MicroVMs there as well as the IAM policy.

## Network connectors are ARNs

Measured 2026-08-05. Managed connectors use ARNs such as
`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:ALL_INGRESS`.
The bare value `ALL_INGRESS` fails with `Malformed network connector ARN`.
`INTERNET_EGRESS`, `HTTP_INGRESS`, and `SHELL_INGRESS` use the same form.

Custom VPC connectors are created by the separate `lambda-core` service and
attached through `egressNetworkConnectors`. Omitting a managed connector does
not disable egress (measured below). Use a VPC without an IGW or NAT gateway
for internet isolation; see [Networking](NETWORKING.md).

## `CreateMicrovmAuthToken` returns a header map

Measured 2026-08-05. `authToken` is a map of headers. Read
`authToken["X-aws-proxy-auth"]`, and send `X-aws-proxy-port` with the target
port. Preserve the header-map contract rather than assuming the response can
never contain another header.

## MicroVM states, and terminal states reached before `RUNNING`

Typical progression is `PENDING → RUNNING → SUSPENDING → SUSPENDED`, with
resume and termination transitions. Poll for the desired state and stop on
terminal states, reporting `stateReason`. A VM that terminates before
`RUNNING` failed startup; continuing to poll hides the useful error. Use the
[AWS GetMicrovm documentation](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_GetMicrovm.html) for the complete state enum.

## Guest power-off ends a privileged VM

Measured 2026-10-14, us-east-1, API `2025-09-09`, `al2023-1`, using the
[targeted conformance probe at `fd548af`](https://github.com/laithalsaadoon/microvms-agentd/blob/fd548af/conformance/drivers/platform_probe.py).
A root exec scheduled `reboot(LINUX_REBOOT_CMD_POWER_OFF)` after a three-second
delay and returned before the syscall. Control-plane polling observed:

| Image | Syscall or control-plane observation |
|---|---|
| Without identity repair | The syscall returned `-1` with `EPERM`. The VM was `RUNNING` at the final 7.672-second observation. |
| With identity repair | The VM was `RUNNING` at 21.992 seconds and first observed `TERMINATED` at 24.430 seconds. |

Times start when the client began the scheduling exec, not when the delayed
syscall ran. The default capability set lacks `CAP_SYS_BOOT`. The repaired
image has it. This measures the guest syscall and later service state,
not a daemon option to power off after a workload exits. Billing cessation
was not measured: `TERMINATED` alone does not establish when charges stop.

## The build log group survives Terraform

Measured 2026-08-05. AWS creates `/aws/lambda-microvms/<image-name>` outside
the Terraform stack. `terraform destroy` does not remove it. Verify log
groups separately after VM/image cleanup.

## Root in the guest is not enough: `sethostname` and bind mounts need `additionalOsCapabilities`

Measured 2026-10-12, us-east-1, API `2025-09-09`, `al2023-1`, kernel 6.1.166
aarch64, live request (one VM per image, the two images of
`examples/strands-box-codex-sdk`, built with and without `--repair-identity`).
`--repair-identity` is what requests `additionalOsCapabilities: ["ALL"]`, the
model's only capability value:

| Image | Capability bounding set | Mounts over `/proc` |
|---|---|---|
| Without identity repair | `00000000a80425fb`: the 14 capabilities Docker grants a root container, with no `CAP_SYS_ADMIN` and no `CAP_NET_ADMIN` | `/proc/kcore`, `/proc/keys`, `/proc/timer_list` and `/proc/scsi` masked; `/proc/bus`, `/proc/fs`, `/proc/irq`, `/proc/sys` and `/proc/sysrq-trigger` read-only |
| With identity repair | `000001ffffffffff`: every capability | agentd's `boot_id` bind only |

Without the capabilities, root can still rewrite `/etc/machine-id`. The
`sethostname` call and the bind mount over `/proc/sys/kernel/random/boot_id`
return `EPERM` (measured 2026-08-06). Both need `CAP_SYS_ADMIN`, which the
set without identity repair lacks. With them, all three identity-repair steps succeed (measured 2026-08-06
and 2026-09-24, the next section). agentd reports each step in
`identity_steps` and sets `identity_degraded` when one fails, so a caller that
needs repaired identity checks those fields.

The capabilities come from the image build. `microvm run --image <arn>
--repair-identity` applies the flag only when `run` builds the image, so a VM
it launched from an image built without the flag kept `00000000a80425fb`, and
root's route changes returned `EPERM` (measured 2026-09-12).

On both images, uid 1000 could create a user namespace. A fresh procfs inside
one returned `EPERM` on both, because the kernel refuses that mount while every
procfs the namespace can see has a mount over part of it. With identity repair,
root could mount a fresh procfs at `/proc` in a private mount namespace, and a
user namespace below that one could mount its own.

## A reduced capability bounding set persists across an image snapshot

Measured 2026-10-14, us-east-1, API `2025-09-09`, `al2023-1`, using the
[targeted conformance probe at `fd548af`](https://github.com/laithalsaadoon/microvms-agentd/blob/fd548af/conformance/drivers/platform_probe.py)
and the [snapshot marker at `217b0ea`](https://github.com/laithalsaadoon/microvms-agentd/blob/217b0ea/conformance/drivers/platform_probe.py).
The default daemon and root exec had `CapBnd`, `CapPrm`, and `CapEff` of
`00000000a80425fb`. The repaired image had `000001ffffffffff` in those sets.
Both had zero inheritable and ambient sets, `NoNewPrivs: 1`, and `Seccomp: 0`.

A root exec called `PR_CAPBSET_DROP` for `CAP_SYS_MODULE`, `CAP_SYS_RAWIO`,
`CAP_SYS_PTRACE`, `CAP_SYS_ADMIN`, `CAP_SYS_BOOT`, `CAP_PERFMON`, and `CAP_BPF`.
Every call succeeded. These bits were already absent from the default set.
In the repaired exec, `CapBnd` became `0000013fff94ffff`, while `CapPrm` and
`CapEff` stayed `000001ffffffffff`. This shows a process can reduce its
bounding set while keeping its permitted and effective capabilities.

The snapshot probe used `CAP_MKNOD` (bit 27), which was present before the
drop, as a marker. It dropped that bit in the image-build process, then execed
agentd. The launched daemon and its root execs kept the smaller bounding set:

| Stage | `CapBnd` | `CapPrm` and `CapEff` |
|---|---|---|
| Build process before the marker drop | `00000000a80425fb` | `00000000a80425fb` |
| Build process after the successful drop | `00000000a00425fb` | `00000000a80425fb` |
| Launched daemon and root execs | `00000000a00425fb` | `00000000a00425fb` |

The wrapper's exec also removed the marker from its permitted and effective
sets. This marker experiment measures persistence of the reduced bounding set
across the snapshot.

In both control images, a probe running with effective uid 0 could open the
daemon's `/proc/<pid>/mem`, while effective uid 4242 received `EACCES`. It did
not read memory or attempt a ptrace attach. `/proc/sys/kernel/yama/ptrace_scope`
was absent. These observations do not establish the access of a fresh exec
after an in-process daemon drop.

After the repaired exec dropped the selected bounding bits, it could still
replace the metadata host route with a blackhole. Its TCP connect changed
from success to `EINVAL` (22), and route removal succeeded. The default
image's route command exited 2, so no post-route connectivity observation was
available there. This probe did not make a metadata HTTP request or read a
token or credential.

## Startup capability policy retains daemon authority and limits fresh children

Measured 2026-10-15, us-east-1, API `2025-09-09`, `al2023-1`, using the
[`capability_policy` conformance lane at `9eef8f0`](https://github.com/laithalsaadoon/microvms-agentd/blob/9eef8f0/conformance/lanes/capability_policy.py)
and its [guest probe](https://github.com/laithalsaadoon/microvms-agentd/blob/9eef8f0/conformance/drivers/capability_policy_probe.py).
With `AGENTD_DROP_EXEC_CAPABILITIES=true`, the daemon applied the policy in
process while retaining its permitted and effective capabilities:

| Image build | Daemon `CapBnd` | Daemon `CapPrm` and `CapEff` | Fresh root exec and run hook: `CapBnd`, `CapPrm`, and `CapEff` |
|---|---|---|---|
| Default | `00000000a80425fb` | `00000000a80425fb` | `00000000a80425fb` |
| `--repair-identity` | `0000013fff94ffff` | `000001ffffffffff` | `0000013fff94ffff` |

The runtime and blocking worker had the same sets as the daemon. The probe
seeded nonzero inheritable and ambient sets before startup; both were zero in
the daemon, its workers, the fresh root exec, and the run hook. The default
image repaired `machine-id`; the image built with `--repair-identity` repaired
`machine-id`, `hostname`, and `boot-id`.

Memory and ptrace access after this startup policy remain unverified.

## Identity repair runs at the first run hook, because the image snapshot captures daemon start

The daemon starts in the image-build VM, and every VM launched from the image
restores that VM's snapshot. Anything the daemon writes when it starts is in the
snapshot, and each VM gets the same copy. Measured 2026-09-23, us-east-1, API
`2025-09-09`, live request (one sample): with a daemon that repaired identity at
startup, two VMs from one image had the same `/proc/sys/kernel/random/boot_id`,
`/etc/machine-id`, and hostname (`localhost`), while
`/proc/sys/kernel/random/uuid` and a fresh `random.random()` differed.

agentd repairs identity at the first successful run hook instead, the first
moment that belongs to one VM. Measured 2026-09-24, us-east-1, API `2025-09-09`,
live request (the conformance suite's `--repair-identity` build): two VMs from
one image had distinct machine-ids, and `identity_steps` reported `machine-id`,
`hostname`, and `boot-id` repaired on both. The same run installed `suspend` and
`resume` handlers under `/etc/agentd/hooks.d`; across a 40-second suspend both
ran in order, exited 0, and were reported on their hook entries (2 ms and
111 ms). Without `--repair-identity`, the hostname and `boot-id` steps fail with
`EPERM`, as the previous section says.

## `minimumMemoryInMiB` selects a *baseline*, and the guest reports the *peak*

Measured 2026-08-07 with `al2023-1`. A 512 MiB baseline produced
`MemTotal: 2037648 kB`; 2048 MiB produced `8209056 kB`. These match the
documented size classes:

| Baseline memory / vCPU | Provisioned ceiling memory / vCPU |
|---|---|
| 0.5 GiB / 0.25 | 2 GiB / 1 |
| 1 GiB / 0.5 | 4 GiB / 2 |
| 2 GiB / 1 | 8 GiB / 4 |
| 4 GiB / 2 | 16 GiB / 8 |
| 8 GiB / 4 | 32 GiB / 16 |

The service team confirmed in August 2026 that the ceiling is provisioned at
launch; there is no resize event. AWS documents billing at the requested
baseline while running, plus consumption above it; the reported peak is not the
billing floor. Memory-pressure tests must use the guest's ceiling, not the
requested baseline. Swap was absent.

## What actually costs money

Queried 2026-08-07 and again 2026-10-24 from AWS Pricing in us-east-1, API
version `2017-10-15`, `ServiceCode="AWSLambda"`. Both queries returned the same us-east-1 USD rates,
which support estimates:

| Usage | Rate |
|---|---|
| ARM vCPU-second | 0.0000276944 |
| ARM memory GiB-second | 0.0000036667 |
| Snapshot read GiB | 0.0015467699 |
| Snapshot write GiB | 0.0037977138 |
| Snapshot storage GiB-hour | 0.0001111111 |

The API also returned non-ARM compute rates; MicroVMs support `ARM_64` only.
On 2026-10-24 it returned MicroVM rates for each region that
[the null-message section](#a-null-message-accessdeniedexception-can-mean-region-availability-or-iam)
names as priced. `us-east-2` and `us-west-2` match `us-east-1` on every line.
`ap-south-1` and `eu-south-2` match its compute rates to within rounding. They
charge 10% to 14% more for snapshot storage and 4.9% to 9.6% more for snapshot
reads and writes. Every other region charges more on every line. Compute costs
from under 0.1% more in `eu-north-1` to 47.8% more in `ap-east-1`. Snapshot
lines cost from 4.5% more (storage in `eu-north-1`) to 59.8% more (writes in
`ap-east-1`). Regional usage types have prefixes that must be removed before
comparing the same dimension.

Image storage has a one-week minimum. Running idle VMs still incur baseline
charges; suspended VMs incur snapshot storage and transitions incur reads and
writes. Data transfer is separate. Server-side build compute billing remains
unverified and is reported as unpriced, not zero. At 730 hours a month, the
storage rate is $0.081111103 per GiB-month. Use `microvm cost` and
`tools/check-live-rates.py` rather than copying rates from this record.

## Seeing an OOM: the process case works, the VM case is still unmeasured

Measured 2026-08-07. `dmesg` was readable and
`/sys/fs/cgroup/memory.events` exposed `oom`, `oom_kill`, and `oom_group_kill`,
all zero in the tested VM. No actual OOM was induced: the first probe required
an absent Python interpreter; a second hit the `/dev/shm` limit instead of
RAM pressure. A guest-wide OOM's `stateReason` therefore remains unmeasured.

The daemon remained reachable while processing 64 MiB of output and reported
`truncated: true`. Unit tests cover signal reporting, but do not establish
what AWS reports after a guest-wide OOM.

## Suspend and resume preserve execution state

Measured 2026-08-05 (`al2023-1`, 1024 MiB baseline, a 45-second hold),
2026-09-23 (us-east-1, API `2025-09-09`, `al2023-1`, two VMs from one image, a
124-second hold, one sample per observation), and 2026-09-24 (suspend and resume
latency, two samples each, one-second polling). Live requests; the summary is in
[Suspend and resume](SUSPEND-RESUME.md).

Resume continues frozen memory and processes; it does not require bootstrap
again. Wall-clock leases and credentials can expire during suspension.

| Observation | Result |
|---|---|
| Agent token, exec records, unread output, background process | survived; the original token was still accepted |
| Process, PID, in-memory nonce, `boot_id` | unchanged |
| `/tmp`, `/dev/shm`, `/workspace` files; endpoint URL | unchanged |
| A ticker printing epoch seconds once a second | no tick lost; its timestamps skipped the suspended time (51 s across the 45 s hold), then advanced once a second |
| `time.time()`, `CLOCK_MONOTONIC`, `CLOCK_BOOTTIME` across a 124 s hold | each jumped +124 s, equal within microseconds |
| Four held outbound TLS sockets | all aborted on resume (`ECONNABORTED`); three of the same peers kept an idle socket 210 s without a suspend |
| New outbound request after resume | succeeded |
| Explicit suspend to `SUSPENDED` | about 1 to 2 s, three samples; `stateReason` null |
| Resume call to `RUNNING` / first health | about 1.2 to 1.5 s, three samples / 1.9 s |
| CPU-busy exec, no inbound traffic, 60 s idle window | `SUSPENDED` between 60 and 70 s after the last request |
| First request to a suspended VM with auto-resume | succeeded in about 1.2 s; `GetMicrovm` showed `PENDING`, then `RUNNING` |
| Held-open exec output stream with steady output, no other requests | stayed `RUNNING` for 150 s |

Whether the suspended window ends a suspended VM depends on what suspended it;
see `idlePolicy` below.

## Traffic ordering around the `/run` hook

AWS documents that external traffic is forwarded only after `/run` returns
HTTP 200. This permits launch-time secret delivery without baking secrets into
a shared image. It does not protect bootstrap from a process already running
inside the guest. See [Trust](TRUST.md).

## The platform's own hook arrives over loopback

Measured 2026-08-04. Lifecycle hooks and proxied control requests both
arrived from `127.0.0.1` on ephemeral ports. A loopback-address filter cannot
distinguish AWS from a guest process, and rejecting loopback rejects legitimate
bootstrap. Use the one-shot bootstrap contract and prevent workloads from
starting before it completes.

## Something probes the port with TLS before bootstrap

Measured 2026-08-04. TLS ClientHello bytes reached the daemon's plaintext
port before bootstrap and produced HTTP 400. The source component was not
identified. Reject malformed traffic without terminating the listener.

## Endpoint authentication

AWS documents proxy JWEs scoped to a VM, allowed ports, and an expiry of at
most 60 minutes. Clients must mint fresh credentials for later requests.
Measured 2026-08-15 against a listener on port 8080:

| `allowedPorts` | HTTPS request to 8080 |
|---|---|
| `[{"port":9000}]` | 403, `Access to port denied` |
| `[{"port":9000},{"port":8080}]` | 200 |
| `[{"allPorts":{}}]` | 200 |
| `[{"range":{"startPort":8000,"endPort":9100}}]` | 200 |

These are tagged-union wire forms, with one member per item. A permitted port
with no listener returned 502. WebSocket failures instead appeared as close
code 1006 without a reason; use an authenticated HTTPS request to diagnose
port scope versus an unavailable listener.

## `clientToken` is a permanent idempotency key

Measured 2026-08-02. Reusing a content-derived create token after deleting
an image replayed the original creation rather than scheduling new builds.
Two images remained `CREATING` for roughly 15 hours, with builds stuck
`PENDING` and unchanged timestamps.

Use a fresh token for a new logical build, retaining it only for retries of
that attempt. Detect stalled builds with `ListMicrovmImageBuilds` after a
grace period. This observation concerns MicroVM image creation; it is not a
claim about every AWS service's idempotency lifetime.

## Build logs go to `/aws/lambda-microvms/<image-name>`

Measured 2026-08-05. The log prefix is `/aws/lambda-microvms/`, not
`/aws/lambda/microvms/`. Build roles need CloudWatch permissions on the correct
group and ECR access for private source images. Incorrect logging permissions
can hide the underlying container error.

## An image build is three VMs and three log streams, and `logStream` is an exact name

Measured August 2026. A build used a docker-build VM and snapshot VMs for
Graviton 3 and 4; application startup logs came from the snapshot VMs.
Default logging used a per-image group and separate randomly named streams.

The API's configured `logStream` is an exact name, so all build phases write
to that stream. This client adds `/<16 hex>` to a configured prefix for each
create attempt and returns the resolved name. User prefixes are capped at
495 characters to fit the 512-character shape; `:` and `*` are forbidden.
The configured group must be writable by the build role.

## A failed build's `stateReason` lives on the **build**, not on the version or the image

Measured 2026-08-15 across three failed builds. `GetMicrovmImage` had no
reason field; version summaries returned `stateReason: null`; build summaries
from `ListMicrovmImageBuilds` contained the reason. Follow
`latestFailedImageVersion`, list its builds, and inspect every failed build.

Observed reasons included `The container image build failed.` and
`Ready hook invocation timed out after PT5M`. The record of that timeout does
not include the build's configured `readyTimeoutInSeconds`, so it does not
establish a five-minute platform limit; the model allows up to 3600 seconds.
CloudWatch logs provide the container-level detail; a summary reason does not
replace them.

## `idlePolicy`

AWS documents idleness as inbound endpoint traffic, not guest CPU activity.
Set `maxIdleDurationSeconds`, `suspendedDurationSeconds`, and
`autoResumeEnabled` deliberately; the maximum VM duration also applies.

Measured 2026-08-15: `GetMicrovm` echoed all three policy fields unchanged in
`RUNNING` and `SUSPENDED`. External health polls kept a VM running; guest
loopback requests do not traverse the endpoint's idle accounting.

`suspendedDurationSeconds` ends a VM that the idle policy suspended. VMs
suspended by an explicit `SuspendMicrovm` call, under the same idle policy,
stayed `SUSPENDED` long past the window, and `ResumeMicrovm` still worked.
AWS describes the field as the longest a MicroVM can remain suspended before it
ends, and the description doesn't exempt an explicit suspend. Treat the
explicit case as measured behavior that AWS doesn't promise. Measured 2026-10-12,
us-east-1, API `2025-09-09`, from one image, `autoResumeEnabled` false,
`maximumDurationInSeconds` 3600, with `GetMicrovm` echoing each policy as set:

| Case | Result |
|---|---|
| Idle suspend, one VM: `maxIdleDurationSeconds` 60, `suspendedDurationSeconds` 60, no inbound traffic | `SUSPENDED` about 60 s after launch, then `TERMINATED` about 60 s later with `stateReason` `MicroVM exceeded maximum lifetime.`; a later `ResumeMicrovm` returned `ValidationException`, naming the VM as terminated |
| Explicit `SuspendMicrovm` within 10 s of `RUNNING`, two VMs: `maxIdleDurationSeconds` 60, `suspendedDurationSeconds` 60 | still `SUSPENDED` 906 s and 917 s later, polled every 15 s with `stateReason` null; `ResumeMicrovm` was accepted, and both were `RUNNING` within 20 s |
| Explicit `SuspendMicrovm`, one VM: `maxIdleDurationSeconds` 3600, `suspendedDurationSeconds` 60 | still `SUSPENDED` 1283 s later; `ResumeMicrovm` was accepted, and the VM was `RUNNING` within 20 s |

On 2026-09-23 a VM with a 60-second window was still `SUSPENDED` 240 s after
an explicit suspend, and it resumed.

## Pagination cursors are URL-safe base64, and the padding still has to be encoded

Measured 2026-08-15 over 28 cursors. Tokens were 688 to 800 bytes of URL-safe
base64, including `=` padding in six samples. An encoded `%3D` request
succeeded; the otherwise identical raw `=` request returned HTTP 400 with a
null message. Treat cursors as opaque and percent-encode query values before
signing. The absence of `+` or `/` does not make encoding optional.

## A throttle can arrive as HTTP 400, named only by `x-amzn-ErrorType`

Measured 2026-10-02, us-east-1, API 2025-09-09, live, in an account listing 140 MicroVMs (14
pages of 10). Read back to back, `ListMicrovms` pages were throttled with HTTP 400, header
`x-amzn-ErrorType: ThrottlingException:http://internal.amazon.com/coral/com.amazon.coral.availability/`,
and body `{"message":null,"throttlingReasons":null}`; the service model declares
`ThrottlingException` as 429. Signed raw GETs of one page were throttled 3 times in 25. The
same pagination tokens succeeded when sent again, and botocore, which reads the header,
retried and finished every listing. Classify by the header's error name before the status:
read by status alone this throttle is a validation refusal that nothing retries, which is
what failed live run 37060855703 (#514).

## `maxResults` is applied before `nameFilter`, so a page can be empty while matches remain

Measured 2026-08-15. With 22 images, ten matching `nameFilter=bonk`, and
`maxResults=1`, the first page was empty and the complete listing took 26
pages. Follow `nextToken` until absent even when a page contains no items.
`nameFilter` is a substring filter; exact-name lookup must compare names
across the complete listing.

## A second `CreateMicrovmImage` under an existing name is refused, so a client without `UpdateMicrovmImage` cannot make a second version

Measured 2026-08-15. Creating an existing image name returned HTTP 400,
`ValidationException: A MicroVM image with the name '<name>' already exists
in this account`. Use `UpdateMicrovmImage` to create another version. Its
PUT request requires `codeArtifact`, `baseImageArn`, and `buildRoleArn`.
A client limited to create calls cannot produce a multi-version image.

## The image ARN separator is a colon, and the slash form fails as `AccessDeniedException`

Measured 2026-08-15. Customer image ARNs use
`arn:aws:lambda:<region>:<account>:microvm-image:<name>`. The colon form
returned 200; an encoded slash form returned 403 `AccessDeniedException`.
An unencoded slash created extra path segments and returned HTML 404.

One transient gateway 502 with an HTML body preceded consistent 403 responses.
Do not interpret a gateway error as service validation, or widen IAM solely
because a malformed ARN produced an authorization error.

## Most public ARM64 base images have no WORKDIR

Measured 2026-08-05. The inspected `al2023-minimal`, `python:3.12-slim`,
and `node:20-slim` images left `WorkingDir` empty. Set `WORKDIR` explicitly
and ensure the workload user can write there.

## A WebSocket reaches a guest server through the endpoint, and the proxy strips its own subprotocols

Measured in two independent runs on 2026-08-15. Both reached a guest echo
server with this offered subprotocol list:

```text
lambda-microvms
lambda-microvms.authentication.<jwe>
lambda-microvms.port.<port>
```

One run obtained credentials through the built Node binding's
`Session.connect_subprotocols` and `connect_headers`, verifying the helper as
well as the protocol. Text frames round-tripped in order. An 899-byte auth
subprotocol remained token-legal without escaping.

The proxy consumed its three subprotocols; none reached the guest. A fourth
application subprotocol did reach it and could be negotiated. When the guest
selected none, the client still observed `lambda-microvms`, supplied by the
proxy. Client-visible `ws.protocol` alone is therefore not evidence of guest
negotiation. HTTPS similarly stripped the proxy auth and port headers.

Missing credentials, missing marker, and wrong-port tokens all produced
opaque 1006 closes. Diagnose using the HTTPS status as described under endpoint
authentication.

## Binary frames survive a port-scoped WebSocket, and an upgrade cannot be replayed over HTTPS

Measured 2026-08-29 against a guest echo server on 8090. A real port-scoped
`wss://` connection returned 101 and preserved binary frames byte-for-byte,
including `00 FF FE 80 7F 00` and a 300-byte extended-length frame.

Forwarding an upgrade through an ordinary HTTPS request returned 400 and
never reached the guest; a tunnel must perform a real WebSocket handshake.
For `/v1/tcp?port=5432`, scope the proxy token to the daemon's listening port
(default 9000): the daemon makes the onward connection inside the guest.
Scoping the token to 5432 instead produced the same opaque 1006 failure.

## The guest kernel is 6.1, which `openat2` needs

Measured 2026-08-14 with `al2023-1`: kernel
`6.1.166-24.303.amzn2023.aarch64`. Tar extraction uses `openat2` with
`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, available since Linux 5.6. Unsupported
kernels fail extraction rather than silently weakening confinement.

## An outside poll of `/v1/health` does reset the idle timer, and the control half proves it

Measured 2026-08-15 and 2026-09-24, us-east-1, API `2025-09-09`, live requests,
each with a 60-second idle timeout. External polling keeps a busy VM alive;
guest work alone does not, and a local ledger watcher does not either. Poll
health at an interval below the idle timeout when preserving an active exec is
the caller's intention.

| Date | Guest work | Outside traffic | Result |
|---|---|---|---|
| 2026-08-15 | detached `sleep 300`, 900 s suspended timeout | a health poll through the endpoint about every 20 s | `RUNNING` through 311 s |
| 2026-08-15 | the same, as the control | none | `SUSPENDED` at 66 s, and stayed suspended |
| 2026-09-24 | a CPU-busy exec | `microvm keepalive --for 170`, a poll every 20 s (9 polls) | `RUNNING` at 170 s; `SUSPENDED` within 150 s of the keepalive stopping, its exec still busy |
| 2026-09-24 | a CPU-busy exec | Python `Session.keep_awake`, a poll every 15 s (12 polls) | `RUNNING` for 170 s |
| 2026-09-24 | a CPU-busy exec | JavaScript `session.keepAwake`, a poll every 15 s (12 polls) | `RUNNING` for 170 s; `SUSPENDED` within 150 s of `stop()`, its exec still busy |

The 2026-09-24 runs went through `conformance/run_rs.py` (`drive_keepalive_helper`)
and a Python binding run. On the `Session.keep_awake` VM, launched with
`autoResumeEnabled`, a `Sandbox.suspend()` issued while the keepalive ran ended
the keepalive as `not-running`, and the VM was still `SUSPENDED` 20 seconds
later: the keepalive did not wake it.

## The shell endpoint is a real PTY over a WebSocket, and it is programmatically drivable

Measured 2026-08-15. A programmable shell requires `HTTP_INGRESS` plus
`SHELL_INGRESS`. `ALL_INGRESS` alone cannot mint shell credentials. Combining
`ALL_INGRESS` with `SHELL_INGRESS` launched a VM but failed later at token
creation, so validate that combination before launch.

`CreateMicrovmShellAuthToken` returns a proxy header map and has no
`allowedPorts` parameter. Connect to the VM's WebSocket endpoint with the
marker and authentication subprotocols; no port subprotocol is required.
Ordinary HTTPS with the shell token returned 502.

| Message | Meaning |
|---|---|
| Initial text `{"type":"session_init","session_id":"<uuid>"}` | Session identifier |
| Binary frames | Raw terminal input/output |
| Text `{"type":"resize","cols":120,"rows":40}` | Resize; `stty size` reported `40 120` |
| Close 1000, `shell exited` | Shell finished |

The session was a root PTY with job control; Ctrl-C produced status 130.
Unknown control messages became literal shell input rather than errors.
The shell channel provides no structured per-command exit status. The package
exposes the shell through `microvm shell`.

## Tagging works on images and not on MicroVMs, and `RunMicrovm` takes no tags

Measured 2026-08-15 (images) and 2026-09-24 (a RUNNING MicroVM), us-east-1, API
2025-09-09, live requests. Image tags could be created, accumulated, listed, and
removed, and `GetMicrovmImage` echoed them. `RunMicrovm` has no tags field in
the 2025-09-09 model (botocore 1.43.106, read 2026-10-12). Do not assume image
tags provide per-instance compute attribution.

`TagResource` and `ListTags` both refused
`arn:aws:lambda:<region>:<account>:microvm:<id>` and the bare id with
`ValidationException` before any authorization: the `resource` pattern the
service enforces admits `function`, `layer`, `code-signing-config`,
`event-source-mapping`, `capacity-provider`, and `network-connector` ARNs and
no MicroVM form. Tag-based lookup of VMs is blocked by the platform, and
finding a VM by name needs the caller's own record; `microvms_core::names`
is that record, shared by the CLI and both bindings.

## Build introspection returns snapshot sizes and a chipset generation, not logs

Measured 2026-08-15, with failed builds 2026-08-16. `ListMicrovmImageBuilds`
requires an image identifier and version and returned two builds, for Graviton
generations 3 and 4. `GetMicrovmImageBuild` added `snapshotBuild` sizes:
579080192 memory bytes, 2357084160 code-install bytes, and 24297472
disk-snapshot bytes for the tested image. It did not return logs or
`stateReason`; use build summaries and CloudWatch for failure details.

`snapshotBuild` and each of its fields are optional. Preserve them as the
service returns them:

| Outcome | `snapshotBuild` |
|---|---|
| Successful | Memory, code-install, and disk-snapshot sizes present |
| Ready-hook timeout | Only `codeInstallSizeInBytes` present |
| Container build failure | Entire member absent |

Filling missing values with zero erases the distinction between an unbuilt
image and installed code whose daemon never became ready. Both tested
failures appeared in builds for Graviton 3 and 4.

`GetMicrovmImageVersion` echoes build configuration, including resources,
hooks, connectors, and base version. `state` (build outcome) and `status`
(launch eligibility) are separate. Use snapshot dimensions for estimates and
preserve absent dimensions as unknown rather than zero.

## The managed base image has two versions, and its versions are bare integers

Measured 2026-08-15 and 2026-08-16. The managed-image listing returned
`al2023-1`, with versions `"0"` and `"1"`; availability can change. A build
pinned to `"1"` read back as `"1.0"`, and `baseImageVersion: "999"` for
`al2023-1` failed with HTTP 400 before creating an image. Discover valid request
versions through the managed-version listing, retain their service-provided
strings rather than treating every version as an integer or a common
semantic-version format, and do not compare echoed strings literally with
listing strings.

An unpinned build also reports a base version, so readback alone does not
prove the caller pinned one. Retain the original request for reproducibility.

## What `GetMicrovm` returns in each state, and `ListMicrovms` filters by image

Measured 2026-08-15 and 2026-09-24, us-east-1, API 2025-09-09, live. A healthy
`RUNNING` response contained `microvmId`, `state`, `endpoint`, `imageArn`,
`imageVersion`, `executionRoleArn`, `idlePolicy`, `maximumDurationInSeconds`,
`startedAt`, `ingressNetworkConnectors`, and `egressNetworkConnectors`, with no
`stateReason`. The same VM carried `terminatedAt` once `TERMINATED`. After an
explicit suspend, `stateReason` was null.

`runHookPayload` was not echoed. Read memory sizing from the image version. The
idle policy echoed `autoResumeEnabled`; the package exposes it through
`--auto-resume`. `ListMicrovms` with `imageIdentifier` set to the image ARN
listed the VM. See [AWS GetMicrovm](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_GetMicrovm.html) for
the complete current response shape, including optional fields.

## A detached exec survives the 60-minute proxy-token ceiling

Measured 2026-08-15. A 75-minute detached exec produced all 450 expected
ten-second ticks and exited zero without truncation. The tick gap across the
60-minute credential boundary was ten seconds. Fresh client processes minted
new proxy credentials; the guest exec record survived independently.

The VM had a 30-minute idle window and external traffic every eight minutes.
Without that traffic, idle suspension would remain possible. Polling a
running exec returned its phase without partial stdout; use streaming or a
file for progress. Recorded compute plus snapshot-read estimate: about $0.16.

## `INACTIVE` is a real retire: `RunMicrovm` refuses the version, pinned or not

Measured 2026-08-16. After setting the sole image version to `INACTIVE`,
both pinned and unpinned launches returned 404 `No active version found`.
Readback still showed `state: SUCCESSFUL`, `status: INACTIVE`. Restoring
`ACTIVE` allowed launch. `UpdateMicrovmImageVersion` changes eligibility
without deleting the version. Effects on already-running VMs were not measured.

## A launch with no `executionRoleArn` **succeeds**, so there is no free `RunMicrovm` probe

Measured 2026-08-16. Omitting `executionRoleArn` created a real VM;
the field is optional in the service model. Missing a required-looking field
is not a safe dry run. Invalid image identifiers can bracket earlier
validation, but every probe must account for the possibility of creating a
billable resource.

## A guest listening on the wrong port fails the build with a clean build log

Measured 2026-08-16, `al2023-1`, 8192 MiB baseline. A mismatch between
`AGENTD_PORT` and `hooks.port` produced `CREATE_FAILED` despite successful
Docker layers and clean startup logs: AWS called hooks on the wrong port.
The same happens when the daemon defaults to 9000 but the client selects
another port. Compare version readback with the Dockerfile; the client
rejects known mismatches before building.

## A baked environment layer removes the guest's env init, measured with `build --project`

Measured 2026-09-02, `al2023-1`, 1024 MiB baseline, a Python project with
one `attrs` dependency:

| Operation | Time |
|---|---|
| First `build --project --reuse` | 126.6 s |
| Same dependency files, reused image | 0.48 s |
| Lockfile-only edit, new build | 125.1 s |
| Fresh VM importing from baked environment | 11.13 to 12.44 s running |
| Plain VM installing dependencies then importing | 31.85 s running |

The lockfile edit changed the image hash and installed dependency version.
This small sample saved roughly 20 seconds per launch; it is not a general
benchmark. It measures dependency reuse, not network isolation: the VMs had
the default network, described in the next section.

The exec in this run had no `PATH` or `HOME`, the default for an exec without a
`user` that does not inherit the image environment ([Protocol](PROTOCOL.md)
lists what adds them). uv downloaded another interpreter despite one being
installed. Use the baked venv's absolute executable path or supply the required
environment.

## A VM launched without the egress connector still has outbound network

Measured 2026-09-11, 2026-09-12, and 2026-09-13, us-east-1, API
`2025-09-09`, `al2023-1`, baselines 512 and 1024 MiB. Launches omitting
`egressNetworkConnectors` reached public destinations:

| Destination | Result without managed egress connector |
|---|---|
| `example.com` | 200 |
| `github.com` | 200 |
| `sts.amazonaws.com` | 302 |
| `pypi.org` | 200 |
| `pypi.org` with an invalid HTTPS proxy | curl exit 7, HTTP status `000` |

The proxy-variable result is the basis for `--deny-egress`; it changes client
behavior without removing the network path.

The VPC control is in the separate `lambda-core` API, not in `lambda-microvms`.
Boto3/botocore 1.43.95 exposes `lambda-core` API `2026-04-30`, whose
`CreateNetworkConnector` accepts VPC subnets and security groups. AWS documents
attaching the active connector ARN through `RunMicrovm.egressNetworkConnectors`.
No internet egress requires a VPC without an IGW or NAT gateway, with no
alternative internet path. These are SDK and documentation findings, reviewed
2026-09-16; no VPC-isolated launch has been live-measured. See [Networking](NETWORKING.md).

## The guest reaches the execution role's credentials through MMDS, and only root with identity repair can block it

Measured 2026-09-11 and 2026-09-12, `al2023-1`, 512 MiB baseline.
`169.254.169.254` served the execution role through Firecracker MMDS:

| Request | Response |
|---|---|
| Credential GET without token | 401 |
| IMDSv2 token PUT | 200 |
| Credential GET with token | 200, full temporary credential document |
| Token PUT as uid 1000 | 200 |

Both managed-egress and connector-less VMs behaved alike. Credential values
were neither printed nor retained. On 2026-10-12 the token PUT as uid 1000 still
returned 200.

Blocking it takes `CAP_NET_ADMIN`, which only an image built with
`--repair-identity` has (the bounding sets above). Without it, route, rule, and
link changes returned `EPERM` after installing `iproute`, and the relevant
`/proc/sys` writes were read-only (measured 2026-09-12).

With it, root can block MMDS for every other process. Measured 2026-10-12,
us-east-1, API `2025-09-09`, Amazon Linux `2023.12.20260817`, kernel 6.1.166
aarch64, live request (one VM). The guest's main table holds a host route,
`169.254.169.254/32 dev eth0`, so adding a second route for that address fails
with `EEXIST`. Replacing it with a blackhole route over rtnetlink succeeded as
root. The minimal image has no `ip` command, so the measurement used a Python
rtnetlink call. After the replacement:

| Process | Action | Result |
|---|---|---|
| uid 1000 | TCP connect and IMDSv2 token PUT | `EINVAL`, no token |
| uid 1000 | Replace or delete the route | `EPERM` |
| Root | TCP connect | `EINVAL`, until root deletes the route |
| agentd | Serve an exec | served |
| uid 1000 | TCP connect to `bedrock-runtime.us-east-1.amazonaws.com:443` | connected |

`fd00:ec2::254` was unreachable before and after, because the guest has no IPv6
route to it. Any root process can delete the blackhole. The block protects only
against processes that don't run as root.

Use least privilege on the execution role. VPC internet isolation does not
remove metadata credentials. The conformance role permits CloudWatch logging
and its policy is checked by `drive_platform_posture`.

## A client-token retry after a suspend returns the same VM, and it resumes

Measured 2026-09-24, us-east-1, API 2025-09-09, live (`crates/microvms-core/tests/
live_lifecycle.rs`, run by `drive_lifecycle_by_id`). A `RunMicrovm` retried with the
same `clientToken` and identical parameters after the VM had been suspended answered with
the original VM, and the client's retry reached RUNNING on it. A client that reads
SUSPENDED before RUNNING as a startup death reports that healthy VM as dead. Two `run
--client-token` calls with the same key from the CLI returned one VM.

## A paginated `ListMicrovms` can return one VM on two of its pages

Measured 2026-10-01, us-east-1, API 2025-09-09, live (wave 4 on 47d5cca), from CloudTrail.
The CLI-8 check listed the account at 08:34:37 UTC through `ListMicrovms` and its
`nextToken` pages while the suite's other VMs were changing state, and the listing held
`microvm-5f158136-92b9-3e32-82f1-4906393d24ca` twice: the check's two `GetMicrovm` calls
that followed, at 08:34:38 (request IDs `0d5a0c2b-2791-4f87-9d9a-f3e506e0d45d` and
`10b67570-ad1c-4484-a2f1-045de44a8377`), both name that id. Listed again later, with every
VM TERMINATED, the account held each VM once. A reader that counts a listing's entries
counts such a VM twice, so a count dedupes by `microvmId`. What moved between the pages,
the order or the boundaries, was not measured.

## Per-VM `logging` delivers a VM's logs to the caller's group

Measured 2026-09-24, us-east-1, API 2025-09-09, live. `RunMicrovm` with
`logging.cloudWatch.logGroup` naming a new group under `/aws/lambda-microvms/` produced a
log stream in that group within 120 s for a VM that ran one command. The execution role
permitted log writes under that prefix; whether a group outside a granted prefix fails
the launch or drops the logs was not measured. The group is the caller's: teardown does
not delete it.

## The first exec of a large binary on a fresh VM pays for paging it in

Measured 2026-09-24, us-east-1, API 2025-09-09, live, one agent VM built by `agent-up`
for Claude Code and Codex. The first `codex --version` (Codex 0.156.1, a 328 MB npm
package with a native arm64 executable) took 13.0 s; every later run took 0.1 s.
`claude --version` took 7 ms. On a codex-only VM the first probe happened to finish in
under 10 s, so the delay varies from launch to launch. Consistent with AWS's statement that a
MicroVM's disk is paged in on demand after launch (grade: measured; the mechanism is
inferred). Budget the first exec of any large executable accordingly: the agent version
probe allows 60 s (`crates/microvms-app/src/agents/mod.rs`, `VERSION_PROBE_TIMEOUT`).

## The daemon's own environment holds the image `ENV` plus four platform variables

Measured 2026-09-24, us-east-1, API 2025-09-09, live, through `exec env
--inherit-image-env` on a VM from the conformance image (`al2023-minimal` with
`ENV MICROVMS_CONFORMANCE_IMAGE_ENV`, `ENV AGENTD_PORT`, `ENV AGENTD_LOG`). The daemon, as
the container `CMD`, inherited `AWS_LAMBDA_MICROVM_IMAGE_ARN`,
`AWS_LAMBDA_MICROVM_IMAGE_NAME`, `AWS_LAMBDA_MICROVM_IMAGE_VERSION`, `AWS_REGION`, `HOME`,
`PATH`, the image's own variable, and the two `AGENTD_*` lines. The startup snapshot
dropped the `AGENTD_*` pair and `/v1/health` reported `image_env_keys: 7`; the child also
showed `PWD`, `SHLVL` and `_`, which `/bin/sh` sets itself. No credential or token
variable was present. The snapshot is taken in the image-build VM, so these are the
build VM's values carried by the snapshot: key names only were recorded, and whether
`AWS_REGION` or the image variables differ per launch was not measured.
