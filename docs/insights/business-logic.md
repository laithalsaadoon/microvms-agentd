# microvms-agentd · Business logic

This index covers domain validation and calculations in `microvms-domain`, orchestration policies in `microvms-app`, and request and execution rules in `agentd`. It documents the checks this repository implements; service-side behavior beyond those checks is outside its scope. The sources separate pure sizing rules, image preflight, lifecycle transitions, and daemon handlers. `crates/microvms-domain/src/sizing.rs:153`, `crates/microvms-app/src/control/image.rs:273`, `crates/microvms-app/src/sandbox.rs:1587`, `crates/agentd/src/exec.rs:341`.

## Validations

### microvms-domain

| Rule | Domain | Citation | Failure mode |
| --- | --- | --- | --- |
| A baseline memory request must match an entry in the size table. | Sizing | `crates/microvms-domain/src/sizing.rs:153` | Invalid-argument error; no rounding to a neighboring class. |
| CPU requests must be finite and nonnegative; the selected class must cover both requested baseline resources. Zero or absent resources impose no requirement. | Sizing | `crates/microvms-domain/src/sizing.rs:179` | Invalid-argument error when no class covers the request; no requirements selects the default. |
| A VM name must be nonempty, at most 128 bytes, use ASCII letters, digits, hyphen, or underscore, and avoid the `microvm-` and `mvm-` prefixes. | Names | `crates/microvms-domain/src/names.rs:33` | Descriptive validation error. |
| Ordinary region parsing accepts the names in `MICROVM_REGIONS`. | Regions | `crates/microvms-domain/src/region.rs:168` | Invalid-argument error for an unlisted name. |
| Run-family hook timeouts are 1 to 60 seconds; build-family timeouts are 1 to 3600 seconds. Hook ports are 1 to 65535. | Hooks | `crates/microvms-domain/src/hooks.rs:64`, `crates/microvms-domain/src/hooks.rs:87`, `crates/microvms-domain/src/hooks.rs:138` | Invalid-argument error naming the accepted range. |
| A cost size must be finite, nonnegative, and convertible from its decimal string. | Cost | `crates/microvms-domain/src/cost.rs:138` | Invalid-argument error. |
| Calendar inputs must describe a real civil date; Unix seconds must fit the calendar's supported range. | Cost | `crates/microvms-domain/src/cost.rs:229`, `crates/microvms-domain/src/cost.rs:254` | Invalid-argument error instead of normalization or clamping. |
| A rate catalog must provide an unambiguous product with the expected unit for every required billing line. | Cost | `crates/microvms-domain/src/cost.rs:1198` | Invalid-argument error for absent, ambiguous, or differently denominated rates. |

### microvms-app

| Rule | Domain | Citation | Failure mode |
| --- | --- | --- | --- |
| Maximum VM duration must be 1 to 28800 seconds; an explicit idle duration must be at least 60 seconds. | Launch | `crates/microvms-app/src/control/mod.rs:560`, `crates/microvms-app/src/control/mod.rs:928` | Invalid-argument error. |
| Versions and nonblank strings must be nonempty, at most 2048 bytes, and contain no ASCII whitespace. Identifiers must be nonempty and at most 256 bytes. | Request shapes | `crates/microvms-app/src/control/mod.rs:595`, `crates/microvms-app/src/control/mod.rs:648`, `crates/microvms-app/src/control/mod.rs:704` | Invalid-argument error naming the member. |
| Role ARNs must satisfy the length limits and the structural role-ARN predicate; ports represented as `u16` must be nonzero. | Request shapes | `crates/microvms-app/src/control/mod.rs:753`, `crates/microvms-app/src/control/mod.rs:895` | Invalid-argument error; the ARN check does not establish that a role exists. |
| Image names must be nonempty, at most 64 bytes, and use ASCII letters, digits, hyphen, or underscore. | Image build | `crates/microvms-app/src/control/mod.rs:1014`, `crates/microvms-domain/src/constants.rs:509` | Invalid-argument error. |
| Tag keys must be nonempty and at most 128 bytes; values may be empty and are limited to 256 bytes. Both use the shared tag-character predicate. | Image build | `crates/microvms-app/src/control/mod.rs:961` | Invalid-argument error identifying the key or value. |
| A serialized run-hook payload must fit within 4096 bytes. | Launch | `crates/microvms-app/src/control/microvm.rs:201`, `crates/microvms-domain/src/constants.rs:80` | Invalid-argument error. |
| A configured log stream requires a log group. Its caller-supplied prefix reserves room for the per-attempt suffix and cannot contain `:` or `*`. | Image build | `crates/microvms-app/src/control/image.rs:311`, `crates/microvms-app/src/control/mod.rs:837` | Invalid-argument error during preflight. |
| Inheriting the work directory requires the base image or Dockerfile to declare one. | Image build | `crates/microvms-app/src/control/artifact.rs:854` | Invalid-argument error. |
| A supplied Dockerfile must agree with the selected base image and daemon port; its keepalive interval must be below the stream idle timeout. | Image build | `crates/microvms-app/src/control/image.rs:279`, `crates/microvms-app/src/control/artifact.rs:940`, `crates/microvms-app/src/control/artifact.rs:1027` | Invalid-argument error during preflight. |
| Dockerfile validation rejects a nonempty `ENTRYPOINT` and requires a nonempty `CMD`. | Image build | `crates/microvms-app/src/control/artifact.rs:1105` | Invalid-argument error; this predicate checks command shape. |
| A nonempty supplied daemon binary must pass the aarch64 binary check. | Image build | `crates/microvms-app/src/control/image.rs:355`, `crates/microvms-domain/src/provision.rs:48` | Precondition error during preflight. |
| Keep-awake configuration requires an idle window of at least 60 seconds and a polling interval from one second through half that window. | Session | `crates/microvms-app/src/session/keepalive.rs:152` | Invalid-argument error before polling. |
| Proxy-token refresh must occur before the 60-minute lifetime limit. | Session | `crates/microvms-app/src/session/proxy.rs:343` | Invalid-argument error at construction. |

### agentd

| Rule | Domain | Citation | Failure mode |
| --- | --- | --- | --- |
| Exec start requires a valid request body and a nonempty exec ID. An explicit timeout must be positive, finite, and representable as a duration. | Execution | `crates/agentd/src/exec.rs:341`, `crates/agentd/src/exec.rs:1171` | HTTP 400 with a malformed-request error. |
| Direct execution requires a nonempty argument vector; named users, groups, and shells must resolve under the guest lookup rules. | Execution | `crates/agentd/src/exec_start.rs:218`, `crates/agentd/src/exec_start.rs:250`, `crates/agentd/src/exec_start.rs:275`, `crates/agentd/src/exec_start.rs:370` | HTTP 400 with the corresponding request error. |
| A run-hook payload must be a JSON object with a nonempty string token; supplied environment values and identity fields must have their expected string shapes. | Bootstrap | `crates/protocol/src/hook.rs:175`, `crates/agentd/src/routes.rs:247` | HTTP 400 for an invalid payload. |
| Archive extraction requires an absolute destination; escaping members, absolute or escaping links, device entries, and FIFOs are refused. | Files | `crates/agentd/src/fs.rs:1450`, `crates/agentd/src/fs.rs:714`, `crates/agentd/src/fs.rs:739`, `crates/agentd/src/fs.rs:755` | HTTP 400; configured archive-size and member limits return HTTP 413. `crates/agentd/src/fs.rs:158` |
| Protected requests require the installed bearer token. | Authorization | `crates/agentd/src/auth.rs:61` | HTTP 503 before bootstrap; HTTP 401 for missing or mismatched credentials after bootstrap. |

## Invariants

### microvms-domain

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| Baseline, peak, and headroom accessors read the size table; headroom is the difference between its peak and baseline. | `SizeClass` accessors | `crates/microvms-domain/src/sizing.rs:218`, `crates/microvms-domain/src/sizing.rs:243` |
| Run and build hook timeouts are distinct types with separately checked constructors. | `RunHookTimeout` and `BuildHookTimeout` | `crates/microvms-domain/src/hooks.rs:44`, `crates/microvms-domain/src/hooks.rs:50` |
| Duration values carry measured or projected provenance. Planned usage is wrapped as projected before report generation. | `DurationP` and `estimate_run` | `crates/microvms-domain/src/cost.rs:391`, `crates/microvms-domain/src/cost.rs:1935` |
| Unknown prices remain unpriced; a total containing them is a lower bound with the unpriced items attached. | `build_line` and `Total::of` | `crates/microvms-domain/src/cost.rs:1700`, `crates/microvms-domain/src/cost.rs:732` |

### microvms-app

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| Image creation uses the same preflight method available to callers. | `ControlPlane::create_image` | `crates/microvms-app/src/control/image.rs:188` |
| Suspend is accepted locally only from `Running`; its settled result records either `Suspended` or `Terminated`. | `Sandbox::suspend` | `crates/microvms-app/src/sandbox.rs:1587` |
| Resume requires `Suspended`, refuses a previously terminated VM, and rebinds the session after reaching `Running`. | `Sandbox::resume` | `crates/microvms-app/src/sandbox.rs:1671` |
| Teardown records termination intent before calling the service. Waiting for completion changes the recorded lifecycle to `Terminated` only after that state is observed. | `Sandbox::terminate` | `crates/microvms-app/src/sandbox.rs:1791` |

### agentd

| Invariant | Where enforced | Citation |
| --- | --- | --- |
| First bootstrap installs the token and launch state; identical token replays succeed without replacing them; conflicting replays fail. | State bootstrap and run-hook handler | `crates/agentd/src/state.rs:336`, `crates/agentd/src/routes.rs:277` |
| When an exec ID is already present, a validated repeat start returns without spawning another process. | Exec start handler | `crates/agentd/src/exec.rs:382` |
| TTL collection removes only acknowledged exec records; an unacknowledged record remains eligible for retrieval. | `collect_expired` | `crates/agentd/src/exec.rs:975` |

## Calculations

### Sizing and session timing

| Calculation | Inputs | Output | Citation |
| --- | --- | --- | --- |
| Select the first size class whose baseline covers both resource requests. | Optional CPU and memory requests | Smallest covering class, or the default when neither resource is requested | `crates/microvms-domain/src/sizing.rs:179` |
| Choose the lesser of one-third of the idle window and the default keep-awake interval. | Idle window, default polling interval | Initial polling interval | `crates/microvms-app/src/session/keepalive.rs:105` |

### Cost

| Calculation | Inputs | Output | Citation |
| --- | --- | --- | --- |
| Convert whole seconds plus nanoseconds divided by one billion into a decimal. | `Duration` | Decimal seconds | `crates/microvms-domain/src/cost.rs:120` |
| Multiply baseline vCPU and baseline memory by seconds and their rates. | Size class, duration, rate table | Separate compute and memory line items | `crates/microvms-domain/src/cost.rs:1585` |
| Use the greater of actual hold and minimum retention, then convert GB-seconds into GB-months. | Stored GB, hold, retention floor, monthly rate | Snapshot storage estimate | `crates/microvms-domain/src/cost.rs:1628` |
| Multiply GB transferred by transfer count and the selected read or write rate. | Snapshot size, operation count, rate table | Snapshot transfer estimate | `crates/microvms-domain/src/cost.rs:1661` |
| Multiply a per-hour catalog price by the configured hours-per-month conversion when needed. | Catalog line and unit | Rate-table value | `crates/microvms-domain/src/cost.rs:1234` |
| Sum estimated amounts and retain every unpriced phase. | Report line items | Exact estimated total or an estimated lower bound | `crates/microvms-domain/src/cost.rs:732` |
| Solve the running-versus-suspended break-even equation in the applicable retention regime. | Running rate, storage rate, retention floor, cycle transfer cost | Suspended hold in decimal seconds | `crates/microvms-domain/src/cost.rs:2048` |
| Subtract the rate retrieval date from the supplied current date. | Civil dates | Rate-table age in days | `crates/microvms-domain/src/cost.rs:959` |

Compute cost is duration multiplied by the sum of baseline vCPU times its per-second rate and baseline GB times its per-second rate. Storage uses GB multiplied by the greater of held seconds and the retention floor, divided by 2,628,000 seconds per billing month, then multiplied by the monthly storage rate. These are the estimator's formulas. `crates/microvms-domain/src/cost.rs:1585`, `crates/microvms-domain/src/cost.rs:1634`, `crates/microvms-domain/src/cost.rs:80`.

For break-even, let `r` be running cost per second, `s` storage cost per second, `f` minimum retention in seconds, and `c` the write-plus-read cost of one cycle. The first candidate is `(c + f × s) / r`. When that candidate exceeds `f`, the method uses `c / (r − s)` instead because storage now grows with the hold. `crates/microvms-domain/src/cost.rs:2048`.

The report charges snapshot reads on launch, compute for a recorded running phase, storage for a suspended phase, and writes and reads for suspend/resume cycles. A supplied image size adds image storage and an unpriced image-build line; the default snapshot size is baseline memory. These are explicit estimation choices in the report builder. `crates/microvms-domain/src/cost.rs:1805`.

## Policy and gates

- **Regions:** Environment resolution checks `AWS_REGION`, then `AWS_DEFAULT_REGION`, then defaults to `us-east-1`; an explicit `Region::unlisted` opts into another name and normalizes recognized names. `crates/microvms-domain/src/region.rs:139`, `crates/microvms-domain/src/region.rs:107`.
- **Rates:** Missing ARM catalog entries are errors; an available x86 entry is not substituted. `crates/microvms-domain/src/cost.rs:1266`.
- **Rate freshness:** Reports carry a warning when the rate-table age exceeds 90 days. `crates/microvms-domain/src/cost.rs:964`, `crates/microvms-domain/src/cost.rs:95`, `crates/microvms-domain/src/cost.rs:1890`.
- **Image waits:** After the stall grace, the wait probes once; a readable, nonempty build listing containing only `PENDING` builds produces `BuildWedged`, while missing listing evidence leaves the ordinary wait running. `crates/microvms-app/src/control/image.rs:429`, `crates/microvms-app/src/control/image.rs:464`.
- **Control-plane retries:** Requests use bounded exponential backoff with jitter only when the resulting error is retryable; throttling and server faults receive that classification. `crates/microvms-app/src/control/transport.rs:290`, `crates/microvms-app/src/control/transport.rs:374`.
- **Session errors:** Transport, token-mint, not-bootstrapped, request-timeout, and server errors are retryable; authorization failures map to credential errors. `crates/microvms-domain/src/error.rs:377`.
- **Proxy-token cache:** Minting is serialized and rechecks the cache under the lock; a refresh retains previously requested ports and adds the current port. Credential failures retain their classification. `crates/microvms-app/src/session/proxy.rs:502`.
- **Keep-awake polling:** The loop stops on cancellation, a nonrunning probe, an enabled idle condition, or its duration limit; retryable errors consume the configured tolerance and other errors propagate. `crates/microvms-app/src/session/keepalive.rs:188`.
- **Teardown:** `Sandbox::terminate` returns a report containing failures and undeleted resources, attempts VM termination before optional image deletion, and reports a requested log-group deletion for external cleanup. `crates/microvms-app/src/sandbox.rs:1775`.
- **Route authorization:** Route metadata places bearer-protected endpoints behind token middleware and keeps open endpoints and platform hooks on their declared path. `crates/agentd/src/routes.rs:52`.
- **Child environment:** Execution composes the optional image environment, passwd-derived identity, launch environment, and request environment in that order, with later layers overriding earlier values. `crates/agentd/src/exec_start.rs:350`, `crates/agentd/src/exec_start.rs:191`.
- **Response behavior:** The router installs the version-stamping middleware and panic-response layer around the combined routes. `crates/agentd/src/routes.rs:78`.

## See also

- [module map](../architecture/module-map.md): 18 shared source citations
- [contract map](contract-map.md): 17 shared source citations
- [impact analysis](impact-analysis.md): 14 shared source citations
- [processes](../behavior/processes.md): 11 shared source citations
- [components](../diagrams/architecture/components.md): 7 shared source citations
