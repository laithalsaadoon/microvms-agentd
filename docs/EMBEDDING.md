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

## Choose execution semantics

| Need | API behavior |
|---|---|
| Wait for a command's complete result | `Session.run_sync` waits and acknowledges the execution. Check its outcome and exit code. |
| Stream or detach | `Session.run` returns an execution handle; retain its ID to poll, stream, signal, and acknowledge later. |
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
AWS behavior requires a separate live conformance run. This documentation
refresh was verified offline only; it introduces no new AWS measurement.
