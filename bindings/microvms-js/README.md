# @theagenticguy/microvms

Run coding agents and their tools in sandboxed AWS Lambda MicroVMs. Give each
task its own Linux workspace, execute commands, collect files, and terminate
the VM from JavaScript or TypeScript. `AgentVm` runs Claude Code or Codex
through Amazon Bedrock; `Sandbox` supports your own agent or command runner.

## Install

```sh
npm install @theagenticguy/microvms
```

Requires Node.js **22.13.0+**. Native addons ship for Linux x64/ARM64 (glibc),
macOS Apple Silicon, and Windows x64. TypeScript declarations are included.

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
The CLI provisions `agentd` and uploads the build artifact for you.
Shell examples use Bash or Zsh; in PowerShell, set variables with `$env:NAME='value'`.

Save as `hello.mjs`:

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

Expected output: `hello from a sandbox`. The VM is terminated after the
command; the image stays available for reuse. `terminate()` returns once
termination is accepted by default. Pass `{ waitForTerminated: true }` to
wait for the final state. Inspect `failures` and `undeleted` because cleanup
reports failures in its result.

## Run a coding agent

First prepare a Claude Code image using the same AWS setup plus
[Bedrock permissions](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/).
This builds or reuses the agent image and starts a temporary VM. Capture
only its image ARN, then terminate that temporary VM:

```sh
MICROVM_AGENT_IMAGE="$(microvm agent-up --vm-name sdk-image --agent claude-code --json |
  node -pe 'JSON.parse(require("node:fs").readFileSync(0, "utf8")).data.imageIdentifier')"
export MICROVM_AGENT_IMAGE
microvm terminate sdk-image --wait
```

Save as `agent.mjs`. The agent writes a file inside its own VM; the SDK
downloads that file before cleanup:

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

For Codex, prepare the image with `--agent codex`, create the VM with
`AgentVm.create(region, [{ agent: 'codex' }])`, and prompt `'codex'`.
Agent images must contain the agent you select. Agents run as UID/GID 1000
in `/workspace`; `installAccess()` installs a short-lived Bedrock token
after launch. `AgentVm` enables internet egress for model calls.

## Next steps

For a `Sandbox` named `vm` and its `session`:

| Need | API |
| --- | --- |
| Start a task and poll or stream later | `session.run(argv)` → `ExecHandle` |
| Read live output as byte streams | `session.spawn(argv)` → `ExecProcess` |
| Keep the VM awake while an exec runs | `await session.keepAwake({ whileBusy: true })` → `KeepAwake` |
| Upload input or download results | `session.uploadFile(path, bytes)`, `session.downloadFile(path)` |
| Transfer a directory | `session.uploadTar(path, tarBytes)`, `session.downloadTar(path)` |
| Freeze and restore a workspace | `vm.suspend()`, `vm.resume()` |

`runSync` returns a Promise: it starts a command, waits, and acknowledges
its saved output. A nonzero exit is a result, so check `result.ok` or
`result.exitCode`. Use `{ shell: true }` when passing a shell script string.
Async library errors expose their `ERR_*` code through `error.cause.message`.

Omitting `egress` does not block outbound traffic. For no egress, use
`egressNetworkConnectors: [vpcConnectorArn]` with a VPC without an internet
gateway, NAT gateway, or other internet route. `denyEgress` sets advisory proxy variables that
workloads can bypass. Keep the guest execution role limited to the task's needs.

[SDK tutorial](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/)
· [API reference](https://laithalsaadoon.github.io/microvms-agentd/reference/public-api/)
· [Source](https://github.com/laithalsaadoon/microvms-agentd) · Apache-2.0
