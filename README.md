# microvms-agentd

**Run AI agents in sandboxed AWS MicroVMs from your terminal or application.**
Give a coding agent a copy of your project, let it edit files and run tools in a
remote VM, and bring back the results. Use the `microvm` CLI or the Python,
JavaScript/TypeScript, and Rust SDKs to launch sandboxes, execute commands,
transfer files, stream output, and tear everything down.

VMs run in AWS Lambda MicroVMs; no local Docker daemon or hypervisor is needed.
The bundled `agentd` daemon handles commands inside each VM.

[Get started](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/) ·
[CLI reference](https://laithalsaadoon.github.io/microvms-agentd/reference/) ·
[SDK examples](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/) ·
[Documentation](https://laithalsaadoon.github.io/microvms-agentd/)

## Install

| Interface | Install | First example |
|---|---|---|
| CLI | `cargo binstall microvms-cli --no-confirm` | [Run an agent below](#run-a-coding-agent-in-a-sandbox) |
| Python 3.9+ | `pip install microvms` | [Python quickstart](microvms-py/README.md) |
| Node 22.13+ | `npm install @theagenticguy/microvms` | [JavaScript / TypeScript quickstart](microvms-js/README.md) |
| Rust | `cargo add microvms-core` | [Rust quickstart](microvms-core/README.md) |

The CLI command requires [cargo-binstall](https://github.com/cargo-bins/cargo-binstall).
Without it, download a CLI binary for your OS from
[Releases](https://github.com/laithalsaadoon/microvms-agentd/releases/latest),
or compile with `cargo install microvms-cli --locked`.
The CLI downloads and verifies its matching ARM64 Linux daemon automatically.
See [installation](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/install/)
for supported hosts and source builds.

## Start in 90 seconds

With the CLI installed and AWS resources ready, copy the configuration below
and start a sandbox. **The first image build takes several minutes; the
90-second path gets the workflow started, not AWS provisioning completed.**

You need AWS CLI v2, `gh` or `curl` for the daemon download, configured AWS
credentials, Lambda MicroVMs access, an
S3 artifact bucket, and build/execution IAM roles. Replace these example values:

```bash
export AWS_REGION=us-east-1
export MICROVM_BUCKET=your-artifact-bucket
export MICROVM_BUILD_ROLE_ARN=arn:aws:iam::123456789012:role/microvm-build
export MICROVM_EXECUTION_ROLE_ARN=arn:aws:iam::123456789012:role/microvm-execution
microvm doctor
```

Need those resources first? The
[AWS setup guide](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/)
includes a Terraform path and required permissions. Commands below create
billable AWS resources.

## Run a coding agent in a sandbox

Your AWS caller needs permission to invoke the selected Bedrock model, and
your account needs access to it. From a project directory, run Claude Code:

```bash
microvm agent-up --vm-name review --agent claude-code --project .
microvm agent-prompt --name review --agent claude-code \
  "Review this project and write your findings to REVIEW.md."
microvm cp --name review vm:/workspace/REVIEW.md ./REVIEW.md
microvm terminate review --wait
```

The CLI builds or reuses an image, copies your project into `/workspace`, and
runs the agent as a non-root user in the VM. Inspect the downloaded `REVIEW.md`
for its findings. Terminate the VM when done, including after a failed task;
`agent-up` keeps it alive for follow-up prompts. The reusable image is retained.

The CLI mints a short-lived Bedrock token from your AWS identity; no
separate model-provider API key is needed. For Codex CLI, replace
`--agent claude-code` with `--agent codex` in both commands. See the
[agent guide](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/)
for model selection, both agents in one VM, and credential refresh.

## Run any command

No model access is needed for a regular sandbox:

```bash
microvm quickstart
```

This builds an image, runs hello-world, reports the result and estimated cost,
and attempts cleanup. For your own commands, build once and reuse the image:

```bash
microvm build --name agent-tools --json
microvm run --image agent-tools --exec "uname -m"
microvm run . --image agent-tools --exec "ls -la"
```

Expected architecture output: `aarch64`; the second run lists your uploaded
project. Add your runtimes and dependencies with a
[custom image](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/write-a-guest-dockerfile/)
before running tests. `run` cleans up the VM by default; add
`--keep --vm-name dev` to keep working with `microvm exec --name dev "..."`,
then `microvm terminate dev --wait`.

## Use a sandbox from code

Copy `data.imageIdentifier` from the build output above into `MICROVM_IMAGE`.
If you skipped that step, first run `microvm build --name agent-tools --json`.
SDKs need the image ARN, rather than a name resolved by the CLI. They use the same AWS credentials and
`MICROVM_EXECUTION_ROLE_ARN` configured above.

```bash
export MICROVM_IMAGE='paste-the-image-ARN-here'
```

**JavaScript / TypeScript:** after installing the npm package, save as
`sandbox.mjs` and run `node sandbox.mjs`:

```js
import { Region, Sandbox } from '@theagenticguy/microvms';

const vm = await Sandbox.create(Region.parse(process.env.AWS_REGION ?? 'us-east-1'));
try {
  const session = await vm.run({
    imageIdentifier: process.env.MICROVM_IMAGE,
    executionRoleArn: process.env.MICROVM_EXECUTION_ROLE_ARN,
  });
  const result = await session.runSync(['echo', 'hello from a sandbox']);
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
  if (!result.ok) process.exitCode = result.exitCode ?? 1;
} finally {
  console.error('Cleanup:', await vm.terminate());
}
```

**Python:** after installing `microvms`, save as `sandbox.py` and run
`python sandbox.py`:

```python
import os
import sys
from microvms import Region, Sandbox

vm = Sandbox(Region.parse(os.environ.get("AWS_REGION", "us-east-1")))
try:
    session = vm.run(
        image_identifier=os.environ["MICROVM_IMAGE"],
        execution_role_arn=os.environ["MICROVM_EXECUTION_ROLE_ARN"],
    )
    result = session.run_sync(["echo", "hello from a sandbox"])
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    if not result.ok:
        raise SystemExit(result.exit_code or 1)
finally:
    print("Cleanup:", vm.terminate().to_dict(), file=sys.stderr)
```

Both print `hello from a sandbox` and request VM termination. Check the cleanup
report for failures or undeleted resources. For complete setup, agent prompts
via `AgentVm`, file transfer, and streaming, see the
[SDK guide](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/).
Rust has a [complete Cargo example](microvms-core/README.md).

## Sandbox boundaries and cleanup

The agent works in a remote VM on the project copy you upload. `agent-up`
enables outbound networking so agents can reach Bedrock. **VM isolation does
not mean internet isolation:** omitting `--egress` does not disable internet
access, and `--deny-egress` sets proxy variables that workloads can bypass.
For enforced internet isolation, use a custom VPC connector with no IGW, NAT,
or other internet route; see [Networking](docs/NETWORKING.md).

Workloads can access the VM execution role's credentials through metadata, so
give that role only permissions every workload may use. `agentd` does not
isolate itself from a root workload. Agent credentials live inside the VM;
copy back selected results instead of archiving the entire workspace.
See [Trust](docs/TRUST.md) and [Security](SECURITY.md).

Cleanup can fail. Inspect `leaked` in CLI JSON output or SDK cleanup reports,
and use `microvm ls --remote` to check remaining resources. Images have a
one-week minimum retention charge; reuse them. See
[costs](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/read-the-cost-report/)
and [recovery](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/recover-a-leaked-vm/).

## Next steps

- [Run a project and collect artifacts](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/run-a-project/)
- [Use JSON and streaming in automation](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/drive-it-from-a-script-or-an-agent/); `microvm manifest` describes the installed CLI.
- [Build a custom image](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/write-a-guest-dockerfile/) or [embed agentd](docs/EMBEDDING.md).
- [Contribute](CONTRIBUTING.md): `mise run check` checks code; `mise run docs:check` checks documentation.

[Documentation index](docs/README.md) · [Apache-2.0 license](LICENSE)
