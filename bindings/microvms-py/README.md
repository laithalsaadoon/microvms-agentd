# microvms

Run coding agents and their tools in sandboxed AWS Lambda MicroVMs. Give each
task its own Linux workspace, execute commands, collect files, and terminate
the VM from Python. `AgentVm` runs Claude Code or Codex through Amazon
Bedrock; `Sandbox` supports your own agent or command runner.

## Install

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install microvms
```

Requires **CPython 3.9+**. On Windows, activate with
`.venv\Scripts\Activate.ps1` in PowerShell. Wheels ship for Linux x64/ARM64
(glibc), macOS Intel/Apple Silicon, and Windows x64. Type declarations are included.

## Run your first command

With an existing image and AWS credentials, this is the 90-second path to a
working SDK example. One-time AWS setup and image builds take longer.

You need Lambda MicroVMs access, your normal AWS credential configuration,
and an execution role. Follow [AWS setup](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/)
to set `AWS_REGION` and `MICROVM_EXECUTION_ROLE_ARN`. Then use the
[CLI](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/install/)
to prepare an image containing `agentd`:

```sh
microvm build --name agent-tools --json
export MICROVM_IMAGE='paste data.imageIdentifier from the result'
```

Use the **image ARN**, in the same account and region as your credentials.
The CLI can resolve image names; the SDK example takes the ARN directly.
The CLI provisions `agentd` and uploads the build artifact for you. Shell
examples use Bash or Zsh; in PowerShell, set variables with `$env:NAME='value'`.

Save as `hello.py`:

```python
import os
import sys
from microvms import Region, Sandbox

image = os.environ["MICROVM_IMAGE"]
role = os.environ["MICROVM_EXECUTION_ROLE_ARN"]
vm = Sandbox(Region.parse(os.environ.get("AWS_REGION", "us-east-1")))
try:
    session = vm.run(image_identifier=image, execution_role_arn=role)
    result = session.run_sync(["echo", "hello from a sandbox"])
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    if not result.ok:
        raise RuntimeError(f"Command exited with {result.exit_code}")
finally:
    cleanup = vm.terminate()
    if cleanup.failures or cleanup.undeleted:
        print("Cleanup needs attention:", cleanup.to_dict(), file=sys.stderr)
```

```sh
python hello.py
```

Expected output: `hello from a sandbox`. The VM is terminated after the
command; the image stays available for reuse. `terminate()` returns once
termination is accepted by default. Pass `wait_for_terminated=True` to
wait for the final state. Inspect `failures` and `undeleted` because cleanup
reports failures in its result.

## Run a coding agent

First prepare a Claude Code image using the same AWS setup plus
[Bedrock permissions](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/).
This builds or reuses the agent image and starts a temporary VM. Capture
only its image ARN, then terminate that temporary VM:

```sh
MICROVM_AGENT_IMAGE="$(microvm agent-up --vm-name sdk-image --agent claude-code --json |
  python -c 'import json, sys; print(json.load(sys.stdin)["data"]["imageIdentifier"])')"
export MICROVM_AGENT_IMAGE
microvm terminate sdk-image --wait
```

Save as `agent.py`. The agent writes a file inside its own VM; the SDK
downloads that file before cleanup:

```python
import os
import sys
from microvms import AgentVm, Region

image = os.environ["MICROVM_AGENT_IMAGE"]
role = os.environ["MICROVM_EXECUTION_ROLE_ARN"]
vm = AgentVm(Region.parse(os.environ.get("AWS_REGION", "us-east-1")))
try:
    session = vm.launch(image_identifier=image, execution_role_arn=role)
    vm.install_access()
    result = vm.prompt_sync(
        "claude-code",
        "Create /workspace/hello.py that prints hello from a sandbox. Run it.",
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    if not result.ok:
        raise RuntimeError(f"Agent exited with {result.exit_code}")
    with open("hello-from-agent.py", "wb") as artifact:
        artifact.write(session.download_file("/workspace/hello.py"))
finally:
    cleanup = vm.terminate()
    if cleanup.failures or cleanup.undeleted:
        print("Cleanup needs attention:", cleanup.to_dict(), file=sys.stderr)
```

```sh
python agent.py
```

For Codex, prepare the image with `--agent codex`, import `AgentSpec`, create
`AgentVm(region, [AgentSpec.codex()])`, and prompt `"codex"`.
Agent images must contain the agent you select. Agents run as UID/GID 1000
in `/workspace`; `install_access()` installs a short-lived Bedrock token
after launch. `AgentVm` enables internet egress for model calls.

## Next steps

For a `Sandbox` named `vm` and its `session`:

| Need | API |
| --- | --- |
| Start a task and poll or stream later | `session.run(argv)` → `ExecHandle` |
| Read stdout and stderr as separate bytes | `session.spawn(argv)` → `ExecProcess` |
| Wait for and release saved output | `handle.wait_and_ack(timeout=60)` |
| Keep the VM awake while an exec runs | `with session.keep_awake(while_busy=True): ...` |
| Upload input or download results | `session.upload_file(path, bytes)`, `session.download_file(path)` |
| Sync a directory in, bring files back | `session.sync_dir(local_dir)`, `session.download_dir(path, local_dir, ["**"])` |
| Move a tar archive as bytes | `session.upload_tar(path, tar_bytes)`, `session.download_tar(path)` |
| Freeze and restore a workspace | `vm.suspend()`, `vm.resume()` |

Each method that calls AWS or the VM blocks, and has an awaitable twin with the
same arguments and an `_async` suffix for asyncio code (see below). `run_sync`
starts a command, waits, and acknowledges its saved output. A nonzero exit is a result, so check `result.ok` or
`result.exit_code`. Use `shell=True` when passing a shell script string.
Library exceptions expose `code`, `kind`, `wire_kind`, and `retryable`.

Omitting `egress` does not block outbound traffic. For no egress, use
`egress_network_connectors=[vpc_connector_arn]` with a VPC without an internet
gateway, NAT gateway, or other internet route. `deny_egress` sets advisory proxy variables that
workloads can bypass. Keep the guest execution role limited to the task's needs.

## Use it from asyncio

Every method that calls AWS or the VM has an awaitable twin named with an
`_async` suffix, and the constructors have `create_async`. A twin takes the same
arguments and returns the same result, and its work runs on the library's own
runtime, so the event loop keeps running and many calls overlap:

```python
import asyncio
import os
from microvms import Region, Sandbox, Session


async def main() -> None:
    region = Region.parse(os.environ.get("AWS_REGION", "us-east-1"))
    async with await Sandbox.create_async(region) as vm:
        session = await vm.run_async(
            image_identifier=os.environ["MICROVM_IMAGE"],
            execution_role_arn=os.environ["MICROVM_EXECUTION_ROLE_ARN"],
        )
        proc = await session.spawn_async(["sh", "-c", "echo out; echo err >&2"])
        async for chunk in proc.stdout:
            print(chunk.decode(), end="")
        # An attached session has a transport of its own, so its calls overlap.
        worker = await Session.attach_async(
            region, vm.microvm_id, vm.endpoint, session.agent_token
        )
        results = await asyncio.gather(
            *(worker.run_sync_async(["echo", str(n)]) for n in range(3))
        )
        print([result.stdout.strip() for result in results])


asyncio.run(main())
```

`async with` terminates the VM on the way out, as `with` does. Cancelling a
read, wait, transfer or command start stops it. Cancelling `run_async`,
`build_image_async`, `ensure_image_async`, `suspend_async`, `resume_async` or
`terminate_async` lets the call finish, so a VM a cancelled launch started is
still the sandbox's to terminate. Calls through the sandbox's own session take
turns on the sandbox, as the blocking methods do, so a lifecycle call never
interleaves with them; `Session.attach_async` gives a session whose calls run at
once. A property such as `vm.lifecycle` waits for a transition
in flight, so read it before or after awaiting one.

[SDK tutorial](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/)
· [API reference](https://laithalsaadoon.github.io/microvms-agentd/reference/public-api/)
· [Source](https://github.com/laithalsaadoon/microvms-agentd) · Apache-2.0
