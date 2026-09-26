# AWS Lambda MicroVMs: measured platform behavior

A compact record of runtime observations. Unless stated otherwise, measurements
used us-east-1, MicroVM API `2025-09-09`, and an ARM64 `al2023-minimal` guest.
Dates apply to observations, not guarantees about current service behavior.
Original experiments and full narratives remain in git history.

For current operation shapes, see the [AWS MicroVM documentation](https://docs.aws.amazon.com/lambda/latest/microvm-api/Welcome.html).
For internet isolation, use [Networking](NETWORKING.md): a VPC without an IGW
or NAT gateway is required; connector omission and proxy variables do not seal
the default network. The September 2026 correction below distinguishes the
separate Lambda core API from the MicroVM API.

## The service provides no exec and no file transfer

Lambda MicroVMs have no addressable command-execution or file-transfer API;
`agentd` supplies those operations. The service does offer a programmatically
usable PTY through `CreateMicrovmShellAuthToken` (measured 2026-08-15), which
corrects the earlier claim that its shell was console-only. A PTY does not
provide detached exec IDs, separated output streams, or exit-status records.

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

Measured 2026-08-07; model rechecked 2026-09-16 with botocore 1.43.95.
The inclusive limit is **4096 bytes of the serialized payload**. The model's
member documentation still says 16,384, but its referenced shape says 4096.
The client checks the smaller, measured limit before calling AWS.

| Payload size | Measured response with an invalid image identifier |
|---|---|
| 4096 bytes | Passed length validation, then rejected the image ARN |
| 4097 bytes | Rejected `runHookPayload` length |

This corrects the earlier 16 KB claim. The budget includes the token, launch
environment, identity material, and JSON escaping. See the
[AWS RunMicrovm documentation](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_RunMicrovm.html) for current shape constraints.

## Calling an unpriced region returns `AccessDeniedException` with a null message

Measured 2026-08-07 with `ListMicrovms`. `us-east-1`, `us-east-2`,
`us-west-2`, `eu-west-1`, and `ap-northeast-1` succeeded. `eu-central-1`,
`ap-southeast-2`, and `sa-east-1` returned `AccessDeniedException` with a null
message. This can resemble an IAM problem; a null message alone is not proof
of its cause.

The shared Lambda endpoint resolver can list ordinary Lambda regions that do
not support MicroVMs. SDK endpoint availability is not a MicroVM service
availability check. The package validates its known regions and permits an
explicit `--unlisted-region` override.

## Network connectors are ARNs

Measured 2026-08-05. Managed connectors use ARNs such as
`arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:ALL_INGRESS`.
The bare value `ALL_INGRESS` fails with `Malformed network connector ARN`.
`INTERNET_EGRESS`, `HTTP_INGRESS`, and `SHELL_INGRESS` use the same form.

**Correction, 2026-09-16:** custom VPC connectors are created by the separate
`lambda-core` service and attached through `egressNetworkConnectors`. The
old claim that omitting a managed connector disables egress was disproved in
September. Use a VPC without an IGW or NAT gateway for internet isolation;
see [Networking](NETWORKING.md).

## `CreateMicrovmAuthToken` returns a header map

Measured 2026-08-05. `authToken` is a header map, not a string. Read
`authToken["X-aws-proxy-auth"]`, and send `X-aws-proxy-port` with the target
port. Preserve the header-map contract rather than assuming the response can
never contain another header.

## MicroVM states, and terminal states reached before `RUNNING`

Typical progression is `PENDING → RUNNING → SUSPENDING → SUSPENDED`, with
resume and termination transitions. Poll for the desired state and stop on
terminal states, reporting `stateReason`. A VM that terminates before
`RUNNING` failed startup; continuing to poll hides the useful error. Use the
[AWS GetMicrovm documentation](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_GetMicrovm.html) for the complete state enum.

## The build log group survives Terraform

Measured 2026-08-05. AWS creates `/aws/lambda-microvms/<image-name>` outside
the Terraform stack. `terraform destroy` does not remove it. Verify log
groups separately after VM/image cleanup.

## Root in the guest is not enough: `sethostname` and bind mounts need `additionalOsCapabilities`

Measured 2026-08-06 with `al2023-1`. Without `additionalOsCapabilities`,
writing `/etc/machine-id` succeeded while `sethostname` and the bind mount over
`/proc/sys/kernel/random/boot_id` returned `EPERM`. Requesting `["ALL"]` made
all three succeed in that run; `ALL` is the model's only capability value.

**Later evidence, 2026-09-12:** the daemon and child capability bounding sets
lacked `CAP_SYS_ADMIN` and `CAP_NET_ADMIN`, including with identity repair
requested. Do not treat the earlier success or `["ALL"]` as a portable
privilege guarantee. Inspect `identity_degraded` on health and the current
capability mask. The metadata section below records the later measurements.

## Identity repair at daemon start is captured by the image snapshot

Measured 2026-09-23, us-east-1, API `2025-09-09`, live request (one sample, an
image whose daemon repaired identity at startup). Two VMs launched from one
image had the same `/proc/sys/kernel/random/boot_id`, `/etc/machine-id`, and
hostname (`localhost`), while `/proc/sys/kernel/random/uuid` and a fresh
`random.random()` differed. Both reported `identity_repaired: true` and
`identity_degraded: true`. The daemon starts in the image-build VM, so what it
wrote at startup was in the snapshot every VM restores.

**Correction, 2026-09-24,** us-east-1, API `2025-09-09`, live request (the
conformance suite's `--repair-identity` build): with repair moved to the first
successful run hook, two VMs from one image had distinct machine-ids, and
`identity_steps` reported `machine-id`, `hostname`, and `boot-id` repaired on
both. The same run installed `suspend` and `resume` handlers under
`/etc/agentd/hooks.d`; across a 40-second suspend both ran in order, exited 0,
and were reported on their hook entries (2 ms and 111 ms). Without
`--repair-identity`, expect the hostname and `boot-id` steps to fail with
`EPERM` as recorded above.

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
baseline while running, plus consumption above it. This corrects the earlier
inference that the reported peak was the billing floor. Memory-pressure tests
must use the guest's ceiling, not the requested baseline. Swap was absent.

## What actually costs money

Queried 2026-08-07 from AWS Pricing in us-east-1, `ServiceCode="AWSLambda"`.
These are dated USD rates, not a current invoice:

| Usage | Rate |
|---|---|
| ARM vCPU-second | 0.0000276944 |
| ARM memory GiB-second | 0.0000036667 |
| Snapshot read GiB | 0.0015467699 |
| Snapshot write GiB | 0.0037977138 |
| Snapshot storage GiB-hour | 0.0001111111 |

The API also returned non-ARM compute rates; MicroVMs support `ARM_64` only.
Rates existed in five regions. us-east-2/us-west-2 matched us-east-1; eu-west-1
and ap-northeast-1 were higher. Regional usage types have prefixes that must
be removed before comparing the same dimension.

Image storage has a one-week minimum. Running idle VMs still incur baseline
charges; suspended VMs incur snapshot storage and transitions incur reads and
writes. Data transfer is separate. Server-side build compute billing remains
unverified and is reported as unpriced, not zero. The old $0.08/GiB-month
storage estimate was rounded low; the API rate gives $0.081111103 at 730 hours.
Use `microvm cost` and `scripts/check-live-rates.py` rather than copying rates
from this record.

## Seeing an OOM: the process case works, the VM case is still unmeasured

Measured 2026-08-07. `dmesg` was readable and
`/sys/fs/cgroup/memory.events` exposed `oom`, `oom_kill`, and `oom_group_kill`,
all zero in the tested VM. No actual OOM was induced: the first probe required
an absent Python interpreter; a second hit the `/dev/shm` limit instead of
RAM pressure. A guest-wide OOM's `stateReason` therefore remains unmeasured.

The daemon remained reachable while processing 64 MiB of output and reported
`truncated: true`. Unit tests cover signal reporting, but do not establish
what AWS reports after a guest-wide OOM.

## Suspend/resume is a freeze and restore, not a stop and start

Measured 2026-08-05, `al2023-1`, 1024 MiB baseline, held suspended for 45
seconds. Token, files, exec records, unread output, background process, and
endpoint URL all survived. A one-second ticker had a 51-second gap, then
advanced six times in six seconds after resume.

Resume continues frozen memory and processes; it does not require bootstrap
again. Wall-clock leases and credentials can expire during suspension. These
observations corrected the earlier claim that an in-memory token was lost.

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
`Ready hook invocation timed out after PT5M`. CloudWatch logs provide the
container-level detail; a summary reason does not replace them.

## `idlePolicy`

AWS documents idleness as inbound endpoint traffic, not guest CPU activity.
Set `maxIdleDurationSeconds`, `suspendedDurationSeconds`, and
`autoResumeEnabled` deliberately. The suspended timeout can terminate a VM
before a later manual resume; the maximum VM duration also applies.

Measured 2026-08-15: `GetMicrovm` echoed all three policy fields unchanged in
`RUNNING` and `SUSPENDED`. The older claim that the suspended timeout existed
only in requests was wrong. External health polls kept a VM running; guest
loopback requests do not traverse the endpoint's idle accounting.

## Pagination cursors are URL-safe base64, and the padding still has to be encoded

Measured 2026-08-15 over 28 cursors. Tokens were 688–800 bytes of URL-safe
base64, including `=` padding in six samples. An encoded `%3D` request
succeeded; the otherwise identical raw `=` request returned HTTP 400 with a
null message. Treat cursors as opaque and percent-encode query values before
signing. The absence of `+` or `/` does not make encoding optional.

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

## A WebSocket reaches a guest server through the endpoint, and the proxy strips its own subprotocols

The independent 2026-08-15 run confirmed the same handshake, stripping,
application negotiation, and opaque failure behavior. Its observations are
consolidated in the earlier WebSocket section; this heading remains for
existing links.

## An outside poll of `/v1/health` does reset the idle timer, and the control half proves it

Measured 2026-08-15 with two VMs running detached `sleep 300`, a 60-second
idle timeout, and a 900-second suspended timeout. The VM polled through its
endpoint every roughly 20 seconds remained `RUNNING` through 311 seconds.
The unpolled control was `SUSPENDED` at 66 seconds and remained so.

External polling keeps a busy VM alive; guest work alone does not. Poll health
at an interval below the idle timeout when preserving an active exec is the
caller's intention. A local ledger watcher does not have this effect.

Measured again 2026-09-24 in us-east-1 (API `2025-09-09`, live requests) with a
CPU-busy exec instead of `sleep`, through the supported keepalive
(`conformance/run_rs.py`, `drive_keepalive_helper`, and a Python binding run).
With a 60-second idle timeout, `microvm keepalive --for 170` polled every 20
seconds (9 polls) and the VM was `RUNNING` at 170 seconds. Once the keepalive
stopped, the same VM, its exec still busy, was `SUSPENDED` within 150 seconds.
`Session.keep_awake` held a second such VM `RUNNING` for 170 seconds at a
15-second interval (12 polls). On that VM, launched with `autoResumeEnabled`, a
`Sandbox.suspend()` issued while a keepalive ran ended the keepalive as
`not-running`, and the VM was still `SUSPENDED` 20 seconds later: the keepalive
did not wake it. The JavaScript binding's `session.keepAwake` held a third busy VM
`RUNNING` for 170 seconds at a 15-second interval (12 polls); after `stop()` the
same VM, still busy, was `SUSPENDED` within 150 seconds.

## The 4096-byte `runHookPayload` ceiling is on the whole string, env map included

Measured 2026-08-15 using a payload containing both `agent_token` and an
`env` map. Exactly 4096 serialized bytes passed length validation; 4097 failed
before image resolution, matching the token-only measurement. Validate the
whole serialized string after combining fields.

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
There is no structured per-command exit-status channel. This corrects the
original claim that the shell could not be driven programmatically; the
package now exposes it through `microvm shell`.

## Tagging works on images and not on MicroVMs, and `RunMicrovm` takes no tags

Measured 2026-08-15. Image tags could be created, accumulated, listed,
and removed. `GetMicrovmImage` echoed them. Attempts to tag a running
MicroVM by ARN or bare ID failed. `RunMicrovm` has no tags field in the
2025-09-09 model, still true in the 2026-09-16 SDK refresh. Do not assume
image tags provide per-instance compute attribution.

Re-measured 2026-09-24, us-east-1, API 2025-09-09, live request against a
RUNNING MicroVM. `TagResource` and `ListTags` both refused
`arn:aws:lambda:<region>:<account>:microvm:<id>` and the bare id with
`ValidationException` before any authorization: the `resource` pattern the
service enforces admits `function`, `layer`, `code-signing-config`,
`event-source-mapping`, `capacity-provider`, and `network-connector` ARNs and
no MicroVM form. So tag-based lookup of VMs is blocked by the platform, and
finding a VM by name needs the caller's own record; `microvms_core::names`
is that record, shared by the CLI and both bindings.

## Build introspection returns snapshot sizes and a chipset generation, not logs

Measured 2026-08-15. `ListMicrovmImageBuilds` requires an image identifier
and version and returned two builds, for Graviton generations 3 and 4.
`GetMicrovmImageBuild` added `snapshotBuild` sizes: 579080192 memory bytes,
2357084160 code-install bytes, and 24297472 disk-snapshot bytes for the tested
image. It did not return logs or `stateReason`; use build summaries and
CloudWatch for failure details.

`GetMicrovmImageVersion` echoes build configuration, including resources,
hooks, connectors, and base version. `state` (build outcome) and `status`
(launch eligibility) are separate. Use snapshot dimensions for estimates and
preserve absent dimensions as unknown rather than zero.

## The managed base image has two versions, and its versions are bare integers

Measured 2026-08-15. The managed-image listing returned `al2023-1`, with
versions `"0"` and `"1"`. The version readback of a derived image normalized
its base version to `"1.0"`. This is a dated listing, not a claim that AWS
will always offer one image or two versions. Discover current versions and
retain their service-provided strings rather than treating every version as
an integer or a common semantic-version format.

## Every field `GetMicrovm` returns for a running VM

Measured 2026-08-15. A healthy response contained `microvmId`, `state`,
`endpoint`, `imageArn`, `imageVersion`, `executionRoleArn`, `idlePolicy`,
`maximumDurationInSeconds`, `startedAt`, `ingressNetworkConnectors`, and
`egressNetworkConnectors`.

`runHookPayload` was not echoed. `stateReason` was absent on the healthy VM.
Memory sizing belongs to the image version, not the instance response. The
idle policy echoed `autoResumeEnabled`; the package exposes it through
`--auto-resume`. See [AWS GetMicrovm](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_GetMicrovm.html) for
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

## `GetMicrovmImageBuild`'s `snapshotBuild` is absent on a container-build failure and partial on a hook timeout

Measured 2026-08-16 across successful and deliberately failed images.
Preserve this optional structure and its optional fields:

| Outcome | `snapshotBuild` |
|---|---|
| Successful | Memory, code-install, and disk-snapshot sizes present |
| Ready-hook timeout | Only `codeInstallSizeInBytes` present |
| Container build failure | Entire member absent |

Filling missing values with zero erases the distinction between an unbuilt
image and installed code whose daemon never became ready. Both tested
failures appeared in builds for Graviton 3 and 4.

## `baseImageVersion` is accepted, validated, and normalised on the way back

Measured 2026-08-16. `baseImageVersion: "999"` for `al2023-1` failed with
HTTP 400 before creating an image. A build pinned to `"1"` read back as
`"1.0"`. Discover valid request versions through the managed-version listing;
do not compare echoed strings literally with listing strings.

An unpinned build also reports a base version, so readback alone does not
prove the caller pinned one. Retain the original request for reproducibility.

## A guest listening on the wrong port fails the build with a clean build log

Measured 2026-08-16, `al2023-1`, 8192 MiB baseline. A mismatch between
`AGENTD_PORT` and `hooks.port` produced `CREATE_FAILED` despite successful
Docker layers and clean startup logs: AWS called hooks on the wrong port.
The same happens when the daemon defaults to 9000 but the client selects
another port. Compare version readback with the Dockerfile; the client now
rejects known mismatches before building.

## A baked environment layer removes the guest's env init, measured with `build --project`

Measured 2026-09-02, `al2023-1`, 1024 MiB baseline, a Python project with
one `attrs` dependency:

| Operation | Time |
|---|---|
| First `build --project --reuse` | 126.6 s |
| Same dependency files, reused image | 0.48 s |
| Lockfile-only edit, new build | 125.1 s |
| Fresh VM importing from baked environment | 11.13–12.44 s running |
| Plain VM installing dependencies then importing | 31.85 s running |

The lockfile edit changed the image hash and installed dependency version.
This small sample saved roughly 20 seconds per launch; it is not a general
benchmark. The earlier description of a launch without `--egress` as having
no network was incorrect: the measurement showed dependency reuse, not
network isolation.

Exec starts with a minimal environment. In this run no `PATH` or `HOME` was
present; uv downloaded another interpreter despite one being installed. Use
the baked venv's absolute executable path or supply the required environment.

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

**Correction, 2026-09-16:** the earlier model review incorrectly concluded
that no VPC control existed because it inspected only `lambda-microvms`.
Boto3/botocore 1.43.95 also exposes `lambda-core` API `2026-04-30`, whose
`CreateNetworkConnector` accepts VPC subnets and security groups. AWS documents
attaching the active connector ARN through `RunMicrovm.egressNetworkConnectors`.
No internet egress requires a VPC without an IGW or NAT gateway, with no
alternative internet path. These are SDK/documentation findings; a VPC-isolated
launch was not live-measured in this refresh. See [Networking](NETWORKING.md).

## The guest reaches the execution role's credentials through MMDS, and no in-guest block works

Measured 2026-09-11 and 2026-09-12, `al2023-1`, 512 MiB baseline.
`169.254.169.254` served the execution role through Firecracker MMDS:

| Request | Response |
|---|---|
| Credential GET without token | 401 |
| IMDSv2 token PUT | 200 |
| Credential GET with token | 200, full temporary credential document |
| Token PUT as uid 1000 | 200 |

Both managed-egress and connector-less VMs behaved alike. Credential values
were neither printed nor retained.

The daemon and child reported `CapBnd 00000000a80425fb`, lacking
`CAP_NET_ADMIN` and `CAP_SYS_ADMIN`, including with identity repair requested.
After installing `iproute`, route/rule/link changes still returned `EPERM`;
relevant `/proc/sys` writes were read-only. No tested in-guest metadata block
worked. This limits the earlier August capability observation.

Use least privilege on the execution role. VPC internet isolation does not
remove metadata credentials. The conformance role permits CloudWatch logging
and its policy is checked by `drive_platform_posture`.

## Suspend/resume across a two-minute hold: clocks, sockets, and auto-resume

Measured 2026-09-23, us-east-1, API `2025-09-09`, `al2023-1`, two VMs from one
image, one sample per observation. Live requests; the summary is in
[Suspend and resume](SUSPEND-RESUME.md).

| Observation | Result |
|---|---|
| Explicit suspend to `SUSPENDED` | about 1–2 s; `stateReason` null |
| Resume call to `RUNNING` / first health | about 1.5 s / 1.9 s |
| Process, PID, in-memory nonce, `boot_id` | unchanged; ticker sequence had no gap |
| `/tmp`, `/dev/shm`, `/workspace` files; endpoint; original agent token | unchanged and accepted |
| `time.time()`, `CLOCK_MONOTONIC`, `CLOCK_BOOTTIME` across a 124 s hold | each jumped +124 s, equal within microseconds |
| Four held outbound TLS sockets | all aborted on resume (`ECONNABORTED`); three of the same peers kept an idle socket 210 s without a suspend |
| New outbound request after resume | succeeded |
| CPU-busy exec, no inbound traffic, 60 s idle window | `SUSPENDED` between 60 and 70 s after the last request |
| First request to a suspended VM with auto-resume | succeeded in about 1.2 s; `GetMicrovm` showed `PENDING`, then `RUNNING` |
| Held-open exec output stream with steady output, no other requests | stayed `RUNNING` for 150 s |
| Two VMs from one image | identical `boot_id` and `machine-id`; kernel random UUID differed |

**Correction candidate, 2026-09-23:** with `suspendedDurationSeconds=60`, a VM
explicitly suspended was still `SUSPENDED` 240 s later, and `ResumeMicrovm`
was accepted. The service model documents termination after the window. One
sample does not overturn that; repeat with a longer hold before relying on
either behavior.

## A client-token retry after a suspend returns the same VM, and it resumes

Measured 2026-09-24, us-east-1, API 2025-09-09, live (`microvms-core/tests/
live_lifecycle.rs`, run by `drive_lifecycle_by_id`). A `RunMicrovm` retried with the
same `clientToken` and identical parameters after the VM had been suspended answered with
the original VM, and the client's retry reached RUNNING on it. A client that reads
SUSPENDED before RUNNING as a startup death reports that healthy VM as dead. Two `run
--client-token` calls with the same key from the CLI returned one VM.

## `GetMicrovm` reports `terminatedAt`, and `ListMicrovms` filters by image

Measured 2026-09-24, us-east-1, API 2025-09-09, live. A RUNNING VM carried `startedAt`
and `maximumDurationInSeconds`; the same VM carried `terminatedAt` once TERMINATED.
`ListMicrovms` with `imageIdentifier` set to the image ARN listed the VM. An explicit
suspend reached SUSPENDED in about 1.2 s and a resume reached RUNNING in about 1.2 to
1.3 s (two samples each, one-second polling).

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
probe allows 60 s (`microvms-app/src/agents/mod.rs`, `VERSION_PROBE_TIMEOUT`).

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
