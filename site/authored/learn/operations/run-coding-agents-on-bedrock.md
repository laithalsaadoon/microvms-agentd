---
title: Run coding agents in sandboxed MicroVMs
description: Give Claude Code or Codex a project in its own VM, run a task through Bedrock, and retrieve the results.
editUrl: false
sidebar:
  order: 3
---

Run Claude Code or Codex in an AWS Lambda MicroVM with a copy of your
project. The agent can edit files and run tools as a non-root user in
`/workspace`. Both profiles call Amazon Bedrock using a short-lived token
minted from your AWS credentials.

## Start with one agent

First [install the CLI](/learn/tutorial/install/) and
[configure AWS](/learn/tutorial/first-run/#configure-aws). You need AWS CLI
v2, MicroVMs permissions, the artifact bucket and build/execution roles,
and Bedrock invocation permissions with access to your chosen model.
With those prerequisites ready, you can start this workflow in 90 seconds.
The first image build takes several minutes; later launches reuse it
when its inputs are unchanged.

From your project directory:

```bash
microvm agent-up --vm-name review --agent claude-code --project .
microvm agent-prompt --name review \
  "Review this project and write your findings to REVIEW.md."
microvm cp --name review vm:/workspace/REVIEW.md ./REVIEW.md
microvm terminate review --wait
```

The file download verifies that the agent produced an artifact and saves
it locally. A successful model response alone does not prove that files
were changed. `agent-up` keeps the VM running; `terminate --wait` waits
for termination. Its image stays available for the next agent VM.

## Choose an agent and model

| Agent | Launch flag | Default Bedrock model | Override |
| --- | --- | --- | --- |
| Claude Code | `--agent claude-code` | `global.anthropic.claude-opus-5` | `--claude-model MODEL_ID` |
| Codex | `--agent codex` | `global.openai.gpt-5.6-sol` | `--codex-model MODEL_ID` |

Omitting `--agent` installs Claude Code. Repeat it to put both agents in
the same VM, then select one on every prompt:

```bash
microvm agent-up --vm-name dev --agent claude-code --agent codex --project .
microvm agent-prompt --name dev --agent codex \
  "Create hello.py that prints hello from a microvm, run it, and show the output."
microvm cp --name dev vm:/workspace/hello.py ./hello.py
microvm terminate dev --wait
```

Use `--claude-version VERSION` or `--codex-version VERSION` on a fresh
launch to pin the npm package. A changed version pin produces a different
image name. To install a different agent set or package version, launch
a new VM under a new name, or terminate the old VM first.

## Move code and results

The following examples address a running VM named `review`; run
`agent-up` again if you terminated it after the quickstart.

`--project .` uploads a copy into `/workspace`. It skips `.git`, `target`,
`node_modules`, and `.venv`; other files are included. Install project
dependencies in the guest as needed. The agent image already includes
Node.js 22, npm, Python 3, Git, and basic shell tools.

While a named VM is running, upload an updated project with
`microvm agent-up --vm-name review --project .`. This refreshes credentials
and uploads files into the existing workspace. It does not rebuild the VM
or reinstall the agent packages.

Copy individual outputs with `microvm cp`, as in the quickstart. For a
directory of generated outputs, download an archive:

```bash
microvm cp --name review --tar vm:/workspace/dist ./dist.tar
```

The local destination is a tar file. Downloading all of `/workspace`
also includes `.agent-env`, which contains the Bedrock bearer token;
select your output files or output directory when collecting results.

## Long tasks and VM lifetime

`agent-prompt` waits and collects output by default, with a 900-second
timeout. Use `--timeout SECONDS` for another task budget. To return
immediately, start a detached task and poll its exec ID (this example
uses `jq`):

```bash
ID=$(microvm agent-prompt --name review --detach --json \
  "Run the test suite and write a summary to TESTS.md." | jq -r .data.execId)
microvm exec --name review --poll "$ID"
# After the exec finishes and you have collected its output:
microvm ack --name review "$ID"
```

Repeat the poll until the task finishes. Add `--agent` when the VM carries
both agents. A detached task keeps the same task timeout.

| Setting | Default | Effect |
| --- | --- | --- |
| `--memory` | `1024` | 1 GiB billing baseline, 4 GiB guest ceiling |
| `--max-idle-sec` | `600` | Suspend after this many seconds without inbound traffic |
| `--suspended-sec` | `600` | Terminate after this many seconds suspended |
| `--max-duration-sec` | `3600` | Maximum VM lifetime; upper limit is 28800 seconds (8 hours) |
| `--auto-resume` | off | Let an inbound request wake a suspended VM |
| `--token-ttl-hours` | `12` | Maximum Bedrock token lifetime; signing credentials may expire sooner |

Choose lifetime settings on the initial `agent-up`. For multi-hour work,
raise `--max-duration-sec` and keep inbound traffic active by polling
`microvm health --name review` at intervals below `--max-idle-sec`.
An idle suspended VM terminates after `--suspended-sec` even with
`--auto-resume`. A busy process inside the guest does not count as inbound
traffic. [Suspend and resume](/learn/tutorial/long-lived-vm/) explains the
lifecycle in more detail.

If credentials expire while the VM is still alive, refresh them:

```bash
microvm agent-up --vm-name review
```

This reads the installed-agent marker and rewrites the credential files.
It keeps the existing agents and models, builds nothing, and does not
extend the VM's lifetime. For a model change on an installed agent, name
that agent explicitly with its model override; include both agent flags
when keeping both profiles. Refresh does not install missing packages.

## Isolation and credentials

The agent runs as uid/gid 1000 in the guest. Claude Code's headless
profile permits `Bash,Read,Edit,Write,Grep,Glob`; Codex uses
`workspace-write`. The token is installed after launch in
`/workspace/.agent-env` with mode `0600`, so it is absent from the shared
image snapshot.

The agent VM has internet egress to reach Bedrock. VM isolation does not
make it a network-isolated workload. Keep the execution role minimal:
guest processes can access its credentials. For workloads requiring no
internet egress, use the general sandbox flow with an existing VPC
connector in a VPC without an internet gateway or NAT gateway.
Omitting `--egress` does not block traffic, and `--deny-egress` is advisory.
See [Networking](/internals/networking/).

## Cleanup, automation, and SDKs

Always terminate a kept VM when finished:

```bash
microvm terminate review --wait
```

Add `--delete-image` when you also want to delete its image. Snapshot
storage has a one-week minimum charge even if you delete earlier. Inspect
cleanup reports and `data.leaked` on failures; `microvm ls --remote` helps
find remaining resources. See [Costs](/learn/operations/read-the-cost-report/).

Use `--json` to automate the same flow: `agent-up` returns a
`microvm.agent` envelope and `agent-prompt` returns
`microvm.agent.prompt`. [Script and agent integration](/learn/operations/drive-it-from-a-script-or-an-agent/)
covers errors, polling, and streaming.

For programmatic use, start with the complete
[Python, Node.js/TypeScript, and Rust examples](/learn/tutorial/from-code/).
The Python and Node packages also expose `AgentVm` for building agent
images, launching, installing credentials, and prompting. The
[Agent VMs specification](/internals/agent-vms/) documents those methods,
profiles, and provisioning details. The
[shell example](https://github.com/laithalsaadoon/microvms-agentd/tree/main/examples/coding-agents-on-bedrock)
shows the individual build, launch, file-transfer, and exec steps.
