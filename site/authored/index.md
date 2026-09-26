---
title: microvms-agentd
description: Run AI agents in sandboxed AWS MicroVMs from the CLI or Python, JavaScript, TypeScript, and Rust SDKs.
---

<div class="rfc-title-block">
<p class="rfc-memo-title">Run AI agents in sandboxed MicroVMs</p>
<p class="rfc-brand">LAUNCH · RUN · COLLECT · CLEAN UP</p>
</div>

Give an agent a copy of your project, let it edit files and run tools in a
remote AWS Lambda MicroVM, and bring back the results. Use the `microvm` CLI
or a Python, JavaScript/TypeScript, or Rust SDK. No local Docker daemon or
hypervisor is needed.

## Start in 90 seconds

With the CLI installed, AWS configured, and Bedrock model access enabled for
your caller, start a coding agent from your project directory:

```bash
microvm agent-up --vm-name review --agent claude-code --project .
microvm agent-prompt --name review --agent claude-code \
  "Review this project and write your findings to REVIEW.md."
microvm cp --name review vm:/workspace/REVIEW.md ./REVIEW.md
microvm terminate review --wait
```

The agent runs as a non-root user inside the VM, using a short-lived Bedrock
token minted from your AWS credentials. Replace `claude-code` with `codex` in both commands
to use Codex CLI.

**First time here?** [Install the CLI](/learn/tutorial/install/), then
[configure AWS](/learn/tutorial/first-run/). You need AWS CLI v2, AWS credentials,
Lambda MicroVMs access, an artifact bucket, and build/execution roles.
The first image build takes several minutes; 90 seconds is the path to
starting the workflow, not a promise that AWS setup or the build has finished.
VMs, builds, and stored images create AWS charges.

## Pick your interface

| Use | Install | Start here |
|---|---|---|
| Terminal | `cargo binstall microvms-cli --no-confirm` | [CLI and AWS setup](/learn/tutorial/first-run/) |
| Python 3.9+ | `pip install microvms` | [Python SDK](/learn/tutorial/from-code/#python) |
| Node 22.13+ | `npm install @theagenticguy/microvms` | [JavaScript / TypeScript SDK](/learn/tutorial/from-code/#javascript--typescript) |
| Rust | `cargo add microvms-core` | [Rust SDK](/learn/tutorial/from-code/#rust) |

The CLI install command needs `cargo-binstall`. [Installation](/learn/tutorial/install/)
also covers standalone binaries and Cargo source builds. All interfaces can
launch a VM, run commands, transfer files, stream output, and terminate it.
The SDK guide includes complete programs you can save and run.

## Start with a command

For a sandbox that needs no Bedrock model access, run `microvm quickstart`
after AWS setup. It builds an image, runs hello-world, reports the result and
estimated cost, and attempts cleanup. Then reuse a named image:

```bash
microvm build --name agent-tools
microvm run --image agent-tools --exec "uname -m"
```

The command prints `aarch64` from inside the VM. Use
[project uploads](/learn/tutorial/run-a-project/) for your code and
[custom images](/learn/operations/write-a-guest-dockerfile/) for dependencies.

## Choose the next task

| Task | Guide |
|---|---|
| Run Claude Code or Codex against a project | [Coding agents in sandboxes](/learn/operations/run-coding-agents-on-bedrock/) |
| Use a sandbox in your application | [SDK examples](/learn/tutorial/from-code/) |
| Keep working in the same VM | [Long-lived VMs](/learn/tutorial/long-lived-vm/) |
| Restrict internet access | [Networking](/learn/operations/configure-networking/) |
| Find a flag, response, or error | [CLI reference](/reference/) |
| Recover resources after failed cleanup | [Recovery](/learn/operations/recover-a-leaked-vm/) |

The VM separates the agent's work from your local machine. `agent-up` enables
outbound networking, and workloads can obtain the execution role's credentials.
Keep that role minimal. Internet isolation needs a VPC with no internet route;
omitting `--egress` does not enforce it. See [Trust](/internals/trust/).

For automation, `microvm manifest` describes the installed CLI and
[For agents](/agents/) explains JSON and streaming. Every page has a Markdown
twin at its path ending in `.md`; [llms.txt](/llms.txt) indexes them.
[Internals](/internals/) covers the daemon, wire protocol, and platform behavior.
