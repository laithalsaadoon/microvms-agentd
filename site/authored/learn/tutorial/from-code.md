---
title: Use the SDKs
description: Install the Python, JavaScript, TypeScript, or Rust SDK; run commands and coding agents in sandboxed MicroVMs.
editUrl: false
sidebar:
  order: 5
---

Use the SDKs to give your agent a disposable Linux workspace: upload inputs,
run tools, read their output, download results, and terminate the VM.
`Sandbox` runs your own commands; `AgentVm` adds Claude Code and Codex through
Amazon Bedrock.

With AWS configured and an existing image, pick a language below and run
your first command in about 90 seconds. One-time AWS setup and image builds
take longer.

## Before you start

Complete [AWS setup](/learn/tutorial/first-run/) and export
`AWS_REGION` and `MICROVM_EXECUTION_ROLE_ARN`. You need Lambda MicroVMs access
and AWS credentials on the machine running the SDK. SDKs use the normal AWS
credential chain; they do not read the CLI's `microvm.toml` configuration.

Use the [CLI](/learn/tutorial/install/) to build an image containing `agentd`:

```sh
microvm build --name agent-tools --json
export MICROVM_IMAGE='paste data.imageIdentifier from the result'
```

Use the image **ARN**, in the same account and region as your credentials.
The CLI resolves image names; these SDK calls take the ARN directly.
The CLI handles daemon provisioning and artifact upload. If your team has
already built a compatible image, set `MICROVM_IMAGE` to its ARN and skip
the build.

The shell examples use Bash or Zsh. In PowerShell, set environment variables
with `$env:NAME='value'`.

## Python

Requires CPython 3.9+. Install in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install microvms
```

On Windows, activate with `.venv\Scripts\Activate.ps1`.
Save this as `hello.py`:

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

Expected output: `hello from a sandbox`.

Methods are synchronous. `session.run_sync()` starts a command, waits for
completion, returns stdout/stderr and an exit code, and acknowledges the
saved output. Nonzero command exits are results; library failures raise
exceptions with `code`, `kind`, `wire_kind`, and `retryable` attributes.

## JavaScript / TypeScript

Requires Node.js 22.13.0+. The package includes TypeScript declarations.

```sh
npm install @theagenticguy/microvms
```

Save this as `hello.mjs`:

```js
import { Region, Sandbox } from '@theagenticguy/microvms';

const imageIdentifier = process.env.MICROVM_IMAGE;
const executionRoleArn = process.env.MICROVM_EXECUTION_ROLE_ARN;
if (!imageIdentifier || !executionRoleArn) {
  throw new Error('Set MICROVM_IMAGE and MICROVM_EXECUTION_ROLE_ARN first');
}
const vm = await Sandbox.create(Region.parse(process.env.AWS_REGION ?? 'us-east-1'));
try {
  const session = await vm.run({ imageIdentifier, executionRoleArn });
  const result = await session.runSync(['echo', 'hello from a sandbox']);
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
  if (!result.ok) process.exitCode = 1;
} finally {
  const cleanup = await vm.terminate();
  if (cleanup.failures.length || cleanup.undeleted.length) {
    console.error('Cleanup needs attention:', cleanup);
    process.exitCode = 1;
  }
}
```

```sh
node hello.mjs
```

Expected output: `hello from a sandbox`.

`runSync()` returns a Promise, like other methods that contact AWS or the
guest. It starts a command, waits, and acknowledges the saved output.
A nonzero exit is a result; an async library failure rejects with its
`ERR_*` code in `error.cause.message`.

Both examples terminate the VM in `finally`, including when a command
fails. Cleanup reports failures through `failures` and `undeleted`.
By default it returns when termination is accepted; use
`terminate(wait_for_terminated=True)` in Python or
`terminate({ waitForTerminated: true })` in JavaScript to observe completion.
The image remains available for reuse.

## Run a coding agent

`AgentVm` installs model access after launch and runs the agent as UID/GID
1000 in `/workspace`. Each task can have its own VM and files. This example
asks Claude Code to write and run a Python script, then downloads that
specific file to your machine.

First configure [Bedrock access](/learn/operations/run-coding-agents-on-bedrock/).
Prepare an agent image once, keeping its ARN for later SDK launches.
The command below starts a temporary VM, so terminate it after capturing
the image ARN. For Python users:

```sh
MICROVM_AGENT_IMAGE="$(microvm agent-up --vm-name sdk-image --agent claude-code --json |
  python -c 'import json, sys; print(json.load(sys.stdin)["data"]["imageIdentifier"])')"
export MICROVM_AGENT_IMAGE
microvm terminate sdk-image --wait
```

For Node users, replace the `python -c ...` part with
`node -pe 'JSON.parse(require("node:fs").readFileSync(0, "utf8")).data.imageIdentifier'`.

### Python agent

Save as `agent.py`:

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

### JavaScript agent

Save as `agent.mjs`:

```js
import { writeFile } from 'node:fs/promises';
import { AgentVm, Region } from '@theagenticguy/microvms';

const imageIdentifier = process.env.MICROVM_AGENT_IMAGE;
const executionRoleArn = process.env.MICROVM_EXECUTION_ROLE_ARN;
if (!imageIdentifier || !executionRoleArn) {
  throw new Error('Set MICROVM_AGENT_IMAGE and MICROVM_EXECUTION_ROLE_ARN first');
}
const vm = await AgentVm.create(Region.parse(process.env.AWS_REGION ?? 'us-east-1'));
try {
  const session = await vm.launch({ imageIdentifier, executionRoleArn });
  await vm.installAccess();
  const result = await vm.promptSync(
    'claude-code',
    'Create /workspace/hello.py that prints hello from a sandbox. Run it.',
  );
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
  if (!result.ok) throw new Error(`Agent exited with ${result.exitCode}`);
  await writeFile('hello-from-agent.py', await session.downloadFile('/workspace/hello.py'));
} finally {
  const cleanup = await vm.terminate();
  if (cleanup.failures.length || cleanup.undeleted.length) {
    console.error('Cleanup needs attention:', cleanup);
    process.exitCode = 1;
  }
}
```

```sh
node agent.mjs
```

Both examples create `hello-from-agent.py` locally. The download checks that
the agent actually produced its artifact: an agent process can exit zero
without completing the requested task.

For Codex, build with `--agent codex` and match the SDK's agent selection:

| Language | Create | Prompt |
| --- | --- | --- |
| Python | `AgentVm(region, [AgentSpec.codex()])` (import `AgentSpec`) | `vm.prompt_sync("codex", task)` |
| JavaScript | `AgentVm.create(region, [{ agent: 'codex' }])` | `vm.promptSync('codex', task)` |

`prompt_sync` / `promptSync` defaults to a 900-second task deadline.
Override with `timeout=...` in Python or `{ timeoutSec: ... }` in JavaScript.
The selected agent must be installed in the image.
`AgentVm` enables internet egress for Bedrock calls.

## Connect your own agent tools

Keep your orchestrator in your application and point its command and file
tools at a session. For example, use these calls inside the `try` block
after obtaining `session`:

| Task | Python | JavaScript |
| --- | --- | --- |
| Start a command without waiting | `session.run(["python3", "job.py"])` | `await session.run(['python3', 'job.py'])` |
| Upload an input | `session.upload_file("/workspace/input.txt", b"hello")` | `await session.uploadFile('/workspace/input.txt', Buffer.from('hello'))` |
| Download a result | `session.download_file("/workspace/result.txt")` | `await session.downloadFile('/workspace/result.txt')` |
| Upload a project tar archive | `session.upload_tar("/workspace", tar_bytes)` | `await session.uploadTar('/workspace', tarBytes)` |

`run()` returns an `ExecHandle` for polling, streaming, or reattaching by
exec ID. Use `wait_and_ack()` / `waitAndAck()` when collecting its final
output. JavaScript also offers `spawn()` with readable byte streams.
Pass `shell=True` / `{ shell: true }` for shell-script strings; argv
arrays need no shell option.

For arbitrary agent-generated commands, set the exec's `user` and `group`
to a non-root UID/GID and make its workspace writable by that user.
The built-in `AgentVm` prompt methods already select UID/GID 1000.
Keep the VM execution role limited to the task's needs.

VM isolation does not imply blocked outbound networking. Omitting
`egress` does not block traffic, and `deny_egress` / `denyEgress` only sets
advisory proxy variables. For a network boundary, use a customer-managed
VPC connector and a VPC without internet or NAT gateways; see
[Networking](/learn/operations/configure-networking/).

## Rust

```sh
cargo add microvms-core
```

`microvms-core` is the Rust SDK behind the CLI and bindings.
Use `Sandbox::new(region).await`, `RunRequest` for launch,
`Session::run_sync` for command results, and `Sandbox::terminate`
for cleanup. The async API also includes `agents::AgentVm`, file transfer,
streaming, suspend/resume, and cost estimates.

[Run the complete Rust example](https://github.com/laithalsaadoon/microvms-agentd/tree/main/microvms-core#run-your-first-command)
to launch the same image and execute a command.
[docs.rs](https://docs.rs/microvms-core) documents the Rust methods and
request types. [Public API](/reference/public-api/) maps the available
surfaces. The [Python package guide](https://github.com/laithalsaadoon/microvms-agentd/tree/main/microvms-py)
and [Node package guide](https://github.com/laithalsaadoon/microvms-agentd/tree/main/microvms-js)
cover supported hosts.

## Build and operate beyond the quickstart

The SDKs expose `build_artifact` / `buildArtifact` and
`build_image` / `buildImage` for custom images. Upload the artifact bytes
to S3 before calling the build method; the SDK does not perform that upload.
Build once and reuse the resulting image ARN across fresh sandboxes.

- [Run coding agents on Bedrock](/learn/operations/run-coding-agents-on-bedrock/)
  covers model access and repeated agent tasks.
- [Read the cost report](/learn/operations/read-the-cost-report/) covers
  estimates, measured durations, and retained resources.
- [Embedding](/internals/embedding/) describes the daemon contract for a
  custom harness or transport.
