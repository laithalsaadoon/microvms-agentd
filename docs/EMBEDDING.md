# Embed agentd in a sandbox workflow

Use the Rust client, Python package, or Node package to build an image, launch a
VM, execute work, and collect results. The shared implementation manages the
AWS control plane and the daemon connection. A custom HTTP client also works,
but must implement the [protocol](PROTOCOL.md), proxy authentication, and
reconnection behavior described below.

Start with the [SDK tutorial](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/)
for a complete program. [Harness capabilities](HARNESS-CAPABILITIES.md) maps
integration requirements to the current API.

## Prepare an image

The image must contain the ARM64 Linux daemon and start it before any task
workload. Generate a Dockerfile with the same defaults as the client:

```bash
microvm dockerfile --workdir /workspace > Dockerfile
microvm build --dockerfile Dockerfile --name task-tools --json
```

Add runtime packages and other image layers before the generated environment
and startup lines. Preserve the managed base image, the daemon's port, an
explicit working directory, `ENTRYPOINT []`, and `CMD ["/agentd"]`. Run task
commands through the session after readiness. An image's startup program can
otherwise begin work before per-VM initialization has finished.

The client validates the Dockerfile and daemon architecture before building.
It cannot prove what arbitrary startup scripts or base-image programs do.
See [Write a guest Dockerfile](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/write-a-guest-dockerfile/)
for supported customization and build diagnostics.

Keep shared tools and data in the image. Upload each task's source after launch,
and install its credentials after bootstrap. Every VM restored from an image
receives that image's files and captured memory.

## Preserve a task image's user

To keep the final Dockerfile `USER` as the default for workload execs, opt in
when wrapping it:

```bash
microvm dockerfile --wrap Task.Dockerfile --preserve-user > Dockerfile
```

The Rust function is `wrap_dockerfile_preserving_user`. Python exposes the
same name and Node exposes `wrapDockerfilePreservingUser`. They take the same
options as the existing wrapper. The existing wrapper retains its root default.

The builder resolves the final stage's user, including a user inherited from a
base or previous stage, variables expanded by `USER`, and numeric `uid:gid`
values. A build-only daemon invocation records its effective uid and primary
gid. The wrapper then restores `USER 0:0` and seals the record. It clears the task's
entrypoint and sets `CMD ["/agentd"]`. It requires a builder that supports
`COPY --chmod`. Workloads still begin only after daemon readiness.

Later edits to `/etc/passwd` do not change the captured uid or gid.
A matching passwd row supplies `HOME`, `USER` and `LOGNAME` at each request.
A numeric uid with no row still works. The daemon clears supplementary groups.

An image's explicit `AGENTD_DEFAULT_USER` takes precedence, including an empty
value, which refuses implicit-user execs. A request's `user` overrides the image
default, and its `group` overrides the default primary gid. A missing or bad
record, or one writable by another user, causes implicit-user execs to fail.
The control API remains available for an explicit-user repair.

## Throttle retries and launch timing

`ControlPlane` and `Sandbox` accept an optional throttle retry budget. Python takes
`retry=(max_attempts, max_elapsed_seconds)`. Node takes
`{ maxAttempts, maxElapsedSeconds }`. Rust uses
`ControlPlane::with_retry(RetryPolicy::new(attempts, elapsed)?)` before passing the
plane to `Sandbox::with_control_plane`. Both limits must be positive. Attempts
include the initial request, and elapsed time includes requests and backoff.
An in-flight request may finish after the deadline, but no new retry starts
after it. The option applies to HTTP 429 and header-named `ThrottlingException`
responses. Other transient failures keep their existing retry budget.
Omitting it preserves the default: an initial request and up to five retries.
Retries reuse the original request and client token.

The returned session exposes `launch_metrics` in Python, `launchMetrics()` in
Node, and `launch_metrics()` in Rust. Its snapshot includes the launch request's
attempt count, the total duration of completed scheduled backoff sleeps, and the
first observed RUNNING and ready times as Unix seconds. A launch with `wait=False` (`wait: false` in Node) initially
leaves unobserved timestamps empty. Later readiness waits update the
shared snapshot, including snapshots read through a cloned session.

Control-plane errors expose `attempt_count`, `backoff_seconds`, and `throttled`
in Python. Node's `retryDetails(error)` returns the corresponding camel-case
fields, or `null` for an error without telemetry. CLI JSON failures include
`attemptCount`, `backoffSeconds`, and `throttled` in `data`. These values describe
one control-plane call, not a caller's own outer retry loop.

Canceling Python's `run_async` stops before another launch request. If a request
is already in flight, it finishes and any accepted VM remains recorded on the
sandbox for cleanup. Rust callers can use `run_cancellable` with a
`RetryCancellation` token for the same behavior.

## Obtain the daemon without the CLI

The bindings expose the same release verification used by the CLI:

```python
import microvms

agentd = microvms.provision_agentd()
```

```js
import { provisionAgentd } from 'microvms';

const agentd = await provisionAgentd();
```

The result is the daemon's bytes. The client considers a supplied binary,
`MICROVM_AGENTD`, and the versioned local cache before downloading its matching
release. Release verification is performed in the client. The report variants,
`provision_agentd_report` and `provisionAgentdReport`, include the selected
source, verification result, path, and SHA-256. Use the report when diagnosing
installation failures.

A supplied binary still needs to be an ARM64 ELF. To check a file before an
image build, run `microvm doctor --binary ./agentd`.

## Launch, execute, and clean up

A `Sandbox` tracks a VM's lifecycle and any image it built. `run` accepts an
image ARN or resolves an image name in the sandbox's region. It waits for the
running state and daemon readiness by default, then returns a `Session`.
An execution role grants permissions to guest workloads; choose it for the
workload rather than reusing an operator's permissions.

```python
import os
from microvms import Region, Sandbox

vm = Sandbox(Region.parse(os.environ.get("AWS_REGION", "us-east-1")))
try:
    session = vm.run(
        image_identifier=os.environ["MICROVM_IMAGE"],
        execution_role_arn=os.environ["MICROVM_EXECUTION_ROLE_ARN"],
    )
    result = session.run_sync(["echo", "hello"])
    print(result.stdout, end="")
    if not result.ok:
        raise RuntimeError(f"Command failed: {result.exit_code}")
finally:
    cleanup = vm.terminate(wait_for_terminated=True)
    if cleanup.failures or cleanup.undeleted:
        print("Cleanup needs attention:", cleanup.to_dict())
```

This creates billable AWS resources. `terminate` returns a report even when
cleanup is incomplete. Inspect it on success and failure. Dropping an object
or ending a process does not guarantee termination. Images remain available
for reuse unless cleanup explicitly deletes an eligible image.

For workspace transfer, use the session's file or tar methods, or the CLI's
`run <DIR>` and `sync` workflows. The CLI packs project files with exclusions
and can collect configured artifacts after the command finishes. A raw tar
download is an archive; it does not choose which files an application should
restore locally.

## Running inside Lambda

The Node package can manage MicroVMs from a Lambda function. Match the native
addon to the function architecture: an arm64 function uses
`microvms.linux-arm64-gnu.node`. An x86_64 function uses
`microvms.linux-x64-gnu.node`.

A bundler such as esbuild cannot bundle the native `.node` addon. Mark
`microvms` as external and include its loader, package manifest, and matching
addon in the deployment zip. For an arm64 function, after installing the package:

```bash
npx esbuild handler.mjs --bundle --platform=node --target=node24 --format=esm \
  --external:microvms --outfile=dist/handler.mjs
mkdir -p dist/node_modules/microvms
cp node_modules/microvms/index.js node_modules/microvms/package.json \
  node_modules/microvms/microvms.linux-arm64-gnu.node dist/node_modules/microvms/
(cd dist && zip -r ../function.zip .)
```

The loader tries the local addon first. Leave the other platform binaries out
of this zip. The published package includes every supported platform.

`Sandbox.create` and `ControlPlane.create` use the function's execution role
through the default credential chain. Let that chain read Lambda's environment
instead of passing a profile or embedding keys. Give the function role the
permissions the handler needs. Pass the VM's own execution role to
`sandbox.run`.

If the VM should keep running after the handler returns, call
`sandbox.detach()` first and save the VM identifier for later control-plane
calls. You own cleanup after detaching. Returning from Lambda leaves the VM
running.

Lambda can freeze its execution environment between invocations. Background
work such as tunnels and token refresh stops during a freeze. Finish required client work within the invocation and reconnect when
a later invocation needs the VM.

`mise run npm:lambda` checks the minimal package in the Lambda Node.js 24
container image. It builds with the release addon compiler configuration, and
CI runs it on arm64. It makes a control-plane call without
networking or AWS credentials. The call must fail with `ERR_CREDENTIALS`. This checks
runtime loading and credential resolution. A deployed Lambda invocation still
needs live verification.

## Choose execution semantics

| Need | API behavior |
|---|---|
| Wait for a command's complete result | `Session.run_sync` waits and acknowledges the execution. Check its outcome and exit code. |
| Stream or detach | `Session.run` returns an execution handle; retain its ID to poll, stream, signal, and acknowledge later. |
| Power off after an execution group exits | Set Python `poweroff_on_exit=True` or Node `poweroffOnExit: true` on the execution call. Requires a supporting daemon with `CAP_SYS_BOOT`. All VM work ends, and acknowledgment or output delivery can race shutdown. |
| Retry an uncertain start | Reuse the original execution ID and request. A duplicate ID refers to the existing execution. |
| Bound guest execution | Set the request's execution timeout. A client wait deadline alone does not stop the guest process. |
| End residual child processes | Request process-group cleanup when the workload requires it. |
| Resume output after a broken connection | Reconnect at the last consumed byte offset and handle an explicit gap. |

Execution output has separate stdout and stderr channels. Polling reports
terminal output; streaming exposes progress. Acknowledge only after consuming
the result: acknowledgment releases buffered output and permits collection.
The daemon applies retention and admission limits rather than offering an
unbounded output store. [Protocol](PROTOCOL.md) defines the exact fields and
limits.

A launch idempotency key and an execution ID solve different problems. A
persisted `client_token` reconciles the same AWS launch, using the same launch
parameters. It is not a reusable name for future VMs. The sandbox requires an
explicit agent token and disabled generated tunnel identity for that replay
path. A new task that needs a new VM needs a new launch key.

## Reconnect from another process

Persist the endpoint, MicroVM ID, region, and agent token only in a store
appropriate for credentials. A session can connect to an existing daemon;
connecting is distinct from launching or bootstrapping it again. The CLI's
`--vm-name`, `attach`, and `history` commands manage local records for this
workflow.

The daemon's agent token and the platform's proxy token serve different
purposes. A daemon request needs its bearer token and the proxy headers minted
by `CreateMicrovmAuthToken`, scoped to the daemon port. The shared client
refreshes proxy authorization while retaining the daemon execution IDs and
stream cursors. A custom client must do the same; a proxy token's expiration
does not mean an execution disappeared.

## Keep a VM available between tasks

The idle policy is fixed at launch. Inbound endpoint traffic resets the idle
timer; guest work and requests to the daemon from inside the guest do not.
Choose the idle window for the workload. If the VM must stay available, send
health requests from outside it at an interval below that window and monitor
failures. Stop polling when you intend it to suspend.

Suspend preserves the guest's state, but connections and time-sensitive
credentials may need renewal after resume. Use workload hook scripts when
necessary and treat reconnect failures explicitly. The configured suspended
window and maximum lifetime still bound availability. Consult
[Suspend and resume](SUSPEND-RESUME.md) for the distinction between documented
behavior, measurements, and unknowns.

## Integrate coding agents

`AgentVm` composes image preparation, launch, credential installation, and
prompt execution for the supported agent profiles. It does not schedule work
across VMs. Use [Agent VMs](AGENT-VMS.md) for that workflow, or build your own
recipe on `Sandbox` and `Session` for another tool.

Internet access and guest credentials remain properties of the VM. A custom
VPC connector must have suitable routing to enforce isolation; omitting the
managed connector or setting proxy-deny variables does not do that. Read
[Networking](NETWORKING.md) and [Trust](TRUST.md) before choosing a deployment
policy.

## Verify an integration

Test command failure, interrupted connections, repeated starts, missing output,
and incomplete cleanup as well as the successful path. Compare the installed
CLI's `microvm manifest` and the daemon's `/v1/schema` with the versions your
application uses. Do not infer an AWS guarantee from a serializer or mock.

The repository's local checks exercise client policies and daemon behavior.
AWS behavior requires a separate live conformance run.
