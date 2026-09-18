# microvms-agentd

[![ci](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/ci.yml/badge.svg)](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/ci.yml)
[![live conformance](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/live-conformance.yml/badge.svg)](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/live-conformance.yml)
[![release](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/release.yml/badge.svg)](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/release.yml)
[![docs](https://github.com/laithalsaadoon/microvms-agentd/actions/workflows/docs.yml/badge.svg)](https://laithalsaadoon.github.io/microvms-agentd/)
[![OpenSSF Scorecard](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.scorecard.dev%2Fprojects%2Fgithub.com%2Flaithalsaadoon%2Fmicrovms-agentd&query=%24.score&label=openssf%20scorecard)](https://scorecard.dev/viewer/?uri=github.com/laithalsaadoon/microvms-agentd)

[![crates.io](https://img.shields.io/crates/v/microvms-cli.svg?label=crates.io)](https://crates.io/crates/microvms-cli)
[![docs.rs](https://img.shields.io/docsrs/microvms-core?label=docs.rs)](https://docs.rs/microvms-core)
[![PyPI](https://img.shields.io/pypi/v/microvms.svg?label=PyPI)](https://pypi.org/project/microvms/)
[![npm](https://img.shields.io/npm/v/%40theagenticguy%2Fmicrovms.svg?label=npm)](https://www.npmjs.com/package/@theagenticguy/microvms)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

[![Open in Claude Code](https://img.shields.io/badge/Open_in-Claude_Code-D97757?logo=claude&logoColor=white)](https://claude.ai/code?prompt=I%20want%20to%20use%20AWS%20Lambda%20MicroVMs%3A%20on-demand%2C%20isolated%20Firecracker%20VMs%20that%20I%20pay%20for%20per%20use%2C%20with%20no%20Docker%20daemon%20or%20hypervisor%20to%20run%20myself.%20Help%20me%20understand%20the%20service%20and%20get%20productive%20with%20it%20using%20microvms-agentd.%0A%0AThe%20service%20launches%20a%20VM%20from%20a%20container%20image%20and%20forwards%20one%20HTTPS%20endpoint%20to%20the%20image%27s%20CMD.%20It%20has%20no%20exec%20API%20and%20no%20file-transfer%20API%2C%20so%20on%20its%20own%20you%20cannot%20run%20a%20command%20in%20the%20VM%20or%20move%20files%20in%20and%20out.%20microvms-agentd%20fills%20that%20gap%3A%20%60agentd%60%20is%20a%20small%20daemon%20baked%20into%20the%20image%2C%20and%20the%20%60microvm%60%20CLI%20plus%20Python%2C%20JavaScript%2FTypeScript%2C%20and%20Rust%20SDKs%20talk%20to%20it.%20Together%20they%20build%20images%2C%20launch%20VMs%2C%20run%20commands%2C%20stream%20output%2C%20copy%20files%20both%20ways%2C%20run%20coding%20agents%20such%20as%20Claude%20Code%20or%20Codex%20against%20Amazon%20Bedrock%20inside%20the%20VM%2C%20report%20cost%2C%20and%20tear%20everything%20down%20while%20flagging%20leaked%20resources.%0A%0AResources%2C%20in%20order%3A%0A1.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fllms.txt%20indexes%20the%20docs%20as%20raw%20Markdown.%20Follow%20its%20links%20rather%20than%20scraping%20HTML.%0A2.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fagents.md%20has%20the%20automation%20rules%3A%20%60microvm%20manifest%60%2C%20%60--json%60%20envelopes%2C%20error%20codes%2C%20and%20cleanup%20checks.%0A3.%20https%3A%2F%2Fdocs.aws.amazon.com%2Flambda%2Flatest%2Fmicrovm-api%2FWelcome.html%20is%20the%20AWS%20API%20reference%20for%20the%20service%20itself.%0A%0AThen%3A%20explain%20the%20concepts%20I%20need%20%28images%2C%20VMs%2C%20sessions%2C%20execution%20roles%2C%20egress%2C%20cost%29%20in%20a%20few%20paragraphs.%20Install%20the%20CLI%20%28%60cargo%20binstall%20microvms-cli%20--no-confirm%60%29%2C%20run%20%60microvm%20doctor%60%2C%20and%20help%20me%20satisfy%20the%20AWS%20prerequisites%20it%20reports.%20Once%20%60doctor%60%20passes%2C%20choose%20the%20CLI%20or%20the%20SDK%20that%20fits%20my%20stack%2C%20get%20one%20sandbox%20running%2C%20and%20show%20me%20how%20to%20run%20a%20command%2C%20copy%20files%20in%20and%20out%2C%20and%20terminate%20the%20VM.%20Commands%20create%20billable%20AWS%20resources%2C%20so%20tell%20me%20before%20running%20anything%20that%20costs%20money.)
[![Open in Cursor](https://img.shields.io/badge/Open_in-Cursor-000000?logo=cursor&logoColor=white)](https://cursor.com/link/prompt?text=I%20want%20to%20use%20AWS%20Lambda%20MicroVMs%3A%20on-demand%2C%20isolated%20Firecracker%20VMs%20that%20I%20pay%20for%20per%20use%2C%20with%20no%20Docker%20daemon%20or%20hypervisor%20to%20run%20myself.%20Help%20me%20understand%20the%20service%20and%20get%20productive%20with%20it%20using%20microvms-agentd.%0A%0AThe%20service%20launches%20a%20VM%20from%20a%20container%20image%20and%20forwards%20one%20HTTPS%20endpoint%20to%20the%20image%27s%20CMD.%20It%20has%20no%20exec%20API%20and%20no%20file-transfer%20API%2C%20so%20on%20its%20own%20you%20cannot%20run%20a%20command%20in%20the%20VM%20or%20move%20files%20in%20and%20out.%20microvms-agentd%20fills%20that%20gap%3A%20%60agentd%60%20is%20a%20small%20daemon%20baked%20into%20the%20image%2C%20and%20the%20%60microvm%60%20CLI%20plus%20Python%2C%20JavaScript%2FTypeScript%2C%20and%20Rust%20SDKs%20talk%20to%20it.%20Together%20they%20build%20images%2C%20launch%20VMs%2C%20run%20commands%2C%20stream%20output%2C%20copy%20files%20both%20ways%2C%20run%20coding%20agents%20such%20as%20Claude%20Code%20or%20Codex%20against%20Amazon%20Bedrock%20inside%20the%20VM%2C%20report%20cost%2C%20and%20tear%20everything%20down%20while%20flagging%20leaked%20resources.%0A%0AResources%2C%20in%20order%3A%0A1.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fllms.txt%20indexes%20the%20docs%20as%20raw%20Markdown.%20Follow%20its%20links%20rather%20than%20scraping%20HTML.%0A2.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fagents.md%20has%20the%20automation%20rules%3A%20%60microvm%20manifest%60%2C%20%60--json%60%20envelopes%2C%20error%20codes%2C%20and%20cleanup%20checks.%0A3.%20https%3A%2F%2Fdocs.aws.amazon.com%2Flambda%2Flatest%2Fmicrovm-api%2FWelcome.html%20is%20the%20AWS%20API%20reference%20for%20the%20service%20itself.%0A%0AThen%3A%20explain%20the%20concepts%20I%20need%20%28images%2C%20VMs%2C%20sessions%2C%20execution%20roles%2C%20egress%2C%20cost%29%20in%20a%20few%20paragraphs.%20Install%20the%20CLI%20%28%60cargo%20binstall%20microvms-cli%20--no-confirm%60%29%2C%20run%20%60microvm%20doctor%60%2C%20and%20help%20me%20satisfy%20the%20AWS%20prerequisites%20it%20reports.%20Once%20%60doctor%60%20passes%2C%20choose%20the%20CLI%20or%20the%20SDK%20that%20fits%20my%20stack%2C%20get%20one%20sandbox%20running%2C%20and%20show%20me%20how%20to%20run%20a%20command%2C%20copy%20files%20in%20and%20out%2C%20and%20terminate%20the%20VM.%20Commands%20create%20billable%20AWS%20resources%2C%20so%20tell%20me%20before%20running%20anything%20that%20costs%20money.)
[![Open in ChatGPT](https://img.shields.io/badge/Open_in-ChatGPT-412991?logo=openai&logoColor=white)](https://chatgpt.com/?q=I%20want%20to%20use%20AWS%20Lambda%20MicroVMs%3A%20on-demand%2C%20isolated%20Firecracker%20VMs%20that%20I%20pay%20for%20per%20use%2C%20with%20no%20Docker%20daemon%20or%20hypervisor%20to%20run%20myself.%20Help%20me%20understand%20the%20service%20and%20get%20productive%20with%20it%20using%20microvms-agentd.%0A%0AThe%20service%20launches%20a%20VM%20from%20a%20container%20image%20and%20forwards%20one%20HTTPS%20endpoint%20to%20the%20image%27s%20CMD.%20It%20has%20no%20exec%20API%20and%20no%20file-transfer%20API%2C%20so%20on%20its%20own%20you%20cannot%20run%20a%20command%20in%20the%20VM%20or%20move%20files%20in%20and%20out.%20microvms-agentd%20fills%20that%20gap%3A%20%60agentd%60%20is%20a%20small%20daemon%20baked%20into%20the%20image%2C%20and%20the%20%60microvm%60%20CLI%20plus%20Python%2C%20JavaScript%2FTypeScript%2C%20and%20Rust%20SDKs%20talk%20to%20it.%20Together%20they%20build%20images%2C%20launch%20VMs%2C%20run%20commands%2C%20stream%20output%2C%20copy%20files%20both%20ways%2C%20run%20coding%20agents%20such%20as%20Claude%20Code%20or%20Codex%20against%20Amazon%20Bedrock%20inside%20the%20VM%2C%20report%20cost%2C%20and%20tear%20everything%20down%20while%20flagging%20leaked%20resources.%0A%0AResources%2C%20in%20order%3A%0A1.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fllms.txt%20indexes%20the%20docs%20as%20raw%20Markdown.%20Follow%20its%20links%20rather%20than%20scraping%20HTML.%0A2.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fagents.md%20has%20the%20automation%20rules%3A%20%60microvm%20manifest%60%2C%20%60--json%60%20envelopes%2C%20error%20codes%2C%20and%20cleanup%20checks.%0A3.%20https%3A%2F%2Fdocs.aws.amazon.com%2Flambda%2Flatest%2Fmicrovm-api%2FWelcome.html%20is%20the%20AWS%20API%20reference%20for%20the%20service%20itself.%0A%0AThen%3A%20explain%20the%20concepts%20I%20need%20%28images%2C%20VMs%2C%20sessions%2C%20execution%20roles%2C%20egress%2C%20cost%29%20in%20a%20few%20paragraphs.%20Install%20the%20CLI%20%28%60cargo%20binstall%20microvms-cli%20--no-confirm%60%29%2C%20run%20%60microvm%20doctor%60%2C%20and%20help%20me%20satisfy%20the%20AWS%20prerequisites%20it%20reports.%20Once%20%60doctor%60%20passes%2C%20choose%20the%20CLI%20or%20the%20SDK%20that%20fits%20my%20stack%2C%20get%20one%20sandbox%20running%2C%20and%20show%20me%20how%20to%20run%20a%20command%2C%20copy%20files%20in%20and%20out%2C%20and%20terminate%20the%20VM.%20Commands%20create%20billable%20AWS%20resources%2C%20so%20tell%20me%20before%20running%20anything%20that%20costs%20money.)
[![Open in Claude](https://img.shields.io/badge/Open_in-Claude-D97757?logo=claude&logoColor=white)](https://claude.ai/new?q=I%20want%20to%20use%20AWS%20Lambda%20MicroVMs%3A%20on-demand%2C%20isolated%20Firecracker%20VMs%20that%20I%20pay%20for%20per%20use%2C%20with%20no%20Docker%20daemon%20or%20hypervisor%20to%20run%20myself.%20Help%20me%20understand%20the%20service%20and%20get%20productive%20with%20it%20using%20microvms-agentd.%0A%0AThe%20service%20launches%20a%20VM%20from%20a%20container%20image%20and%20forwards%20one%20HTTPS%20endpoint%20to%20the%20image%27s%20CMD.%20It%20has%20no%20exec%20API%20and%20no%20file-transfer%20API%2C%20so%20on%20its%20own%20you%20cannot%20run%20a%20command%20in%20the%20VM%20or%20move%20files%20in%20and%20out.%20microvms-agentd%20fills%20that%20gap%3A%20%60agentd%60%20is%20a%20small%20daemon%20baked%20into%20the%20image%2C%20and%20the%20%60microvm%60%20CLI%20plus%20Python%2C%20JavaScript%2FTypeScript%2C%20and%20Rust%20SDKs%20talk%20to%20it.%20Together%20they%20build%20images%2C%20launch%20VMs%2C%20run%20commands%2C%20stream%20output%2C%20copy%20files%20both%20ways%2C%20run%20coding%20agents%20such%20as%20Claude%20Code%20or%20Codex%20against%20Amazon%20Bedrock%20inside%20the%20VM%2C%20report%20cost%2C%20and%20tear%20everything%20down%20while%20flagging%20leaked%20resources.%0A%0AResources%2C%20in%20order%3A%0A1.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fllms.txt%20indexes%20the%20docs%20as%20raw%20Markdown.%20Follow%20its%20links%20rather%20than%20scraping%20HTML.%0A2.%20https%3A%2F%2Flaithalsaadoon.github.io%2Fmicrovms-agentd%2Fagents.md%20has%20the%20automation%20rules%3A%20%60microvm%20manifest%60%2C%20%60--json%60%20envelopes%2C%20error%20codes%2C%20and%20cleanup%20checks.%0A3.%20https%3A%2F%2Fdocs.aws.amazon.com%2Flambda%2Flatest%2Fmicrovm-api%2FWelcome.html%20is%20the%20AWS%20API%20reference%20for%20the%20service%20itself.%0A%0AThen%3A%20explain%20the%20concepts%20I%20need%20%28images%2C%20VMs%2C%20sessions%2C%20execution%20roles%2C%20egress%2C%20cost%29%20in%20a%20few%20paragraphs.%20Install%20the%20CLI%20%28%60cargo%20binstall%20microvms-cli%20--no-confirm%60%29%2C%20run%20%60microvm%20doctor%60%2C%20and%20help%20me%20satisfy%20the%20AWS%20prerequisites%20it%20reports.%20Once%20%60doctor%60%20passes%2C%20choose%20the%20CLI%20or%20the%20SDK%20that%20fits%20my%20stack%2C%20get%20one%20sandbox%20running%2C%20and%20show%20me%20how%20to%20run%20a%20command%2C%20copy%20files%20in%20and%20out%2C%20and%20terminate%20the%20VM.%20Commands%20create%20billable%20AWS%20resources%2C%20so%20tell%20me%20before%20running%20anything%20that%20costs%20money.)
[![llms.txt](https://img.shields.io/badge/llms.txt-available-2ea44f)](https://laithalsaadoon.github.io/microvms-agentd/llms.txt)
[![For agents](https://img.shields.io/badge/for_agents-agents.md-2ea44f)](https://laithalsaadoon.github.io/microvms-agentd/agents.md)

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

## Start here

**Humans:** copy the prompt below into your coding agent of choice, or press an
**Open in** badge above to prefill it in Claude Code, Cursor, ChatGPT, or
Claude. Nothing runs until you send it. The prompt explains what AWS Lambda
MicroVMs are, the gap this project fills, and where the documentation lives,
then asks the agent to guide you from install to a first sandbox.

```text
I want to use AWS Lambda MicroVMs: on-demand, isolated Firecracker VMs that I pay for per use, with no Docker daemon or hypervisor to run myself. Help me understand the service and get productive with it using microvms-agentd.

The service launches a VM from a container image and forwards one HTTPS endpoint to the image's CMD. It has no exec API and no file-transfer API, so on its own you cannot run a command in the VM or move files in and out. microvms-agentd fills that gap: `agentd` is a small daemon baked into the image, and the `microvm` CLI plus Python, JavaScript/TypeScript, and Rust SDKs talk to it. Together they build images, launch VMs, run commands, stream output, copy files both ways, run coding agents such as Claude Code or Codex against Amazon Bedrock inside the VM, report cost, and tear everything down while flagging leaked resources.

Resources, in order:
1. https://laithalsaadoon.github.io/microvms-agentd/llms.txt indexes the docs as raw Markdown. Follow its links rather than scraping HTML.
2. https://laithalsaadoon.github.io/microvms-agentd/agents.md has the automation rules: `microvm manifest`, `--json` envelopes, error codes, and cleanup checks.
3. https://docs.aws.amazon.com/lambda/latest/microvm-api/Welcome.html is the AWS API reference for the service itself.

Then: explain the concepts I need (images, VMs, sessions, execution roles, egress, cost) in a few paragraphs. Install the CLI (`cargo binstall microvms-cli --no-confirm`), run `microvm doctor`, and help me satisfy the AWS prerequisites it reports. Once `doctor` passes, choose the CLI or the SDK that fits my stack, get one sandbox running, and show me how to run a command, copy files in and out, and terminate the VM. Commands create billable AWS resources, so tell me before running anything that costs money.
```

**Agents:** start with
[llms.txt](https://laithalsaadoon.github.io/microvms-agentd/llms.txt), which
indexes the documentation as raw Markdown. Read
[agents.md](https://laithalsaadoon.github.io/microvms-agentd/agents.md) for the
automation rules, and the
[AWS Lambda MicroVMs API reference](https://docs.aws.amazon.com/lambda/latest/microvm-api/Welcome.html)
for the service this project drives.

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
