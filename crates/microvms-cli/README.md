# microvms-cli

Run coding agents, shell commands, and test jobs in **sandboxed AWS Lambda
MicroVMs**. Give Claude Code or Codex a copy of your project, let it work in
`/workspace` as a non-root user, and bring the results back with `microvm`.

## Install

Download a [prebuilt CLI](https://github.com/laithalsaadoon/microvms-agentd/releases/latest)
for Linux, macOS, or Windows, extract it, and put `microvm` on your `PATH`.
No Rust compiler is needed. If you already have
[cargo-binstall](https://github.com/cargo-bins/cargo-binstall):

```sh
cargo binstall microvms-cli --no-confirm
microvm --version
```

Or compile from crates.io with `cargo install microvms-cli --locked`.
The CLI downloads and verifies the matching `agentd` binary automatically
when it builds an image, checking its Sigstore attestation in-process.

## Start an agent in 90 seconds

With the CLI installed and [AWS configured](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/),
these are the commands to start using it. Your first image build takes several
minutes; subsequent agent launches reuse that image. The 90 seconds is setup
time, not a guarantee about AWS builds or the agent's task.

Prerequisites: AWS CLI v2, AWS credentials with MicroVMs and Bedrock model
access, and `MICROVM_BUCKET`, `MICROVM_BUILD_ROLE_ARN`, and
`MICROVM_EXECUTION_ROLE_ARN` set to your infrastructure values.

From your project directory:

```sh
microvm agent-up --vm-name review --agent claude-code --project .
microvm agent-prompt --name review \
  "Review this project and write your findings to REVIEW.md."
microvm cp --name review vm:/workspace/REVIEW.md ./REVIEW.md
microvm terminate review --wait
```

Use `--agent codex` on `agent-up` to run Codex instead. Both profiles use
Amazon Bedrock with a short-lived token minted from your AWS credentials.
The project is uploaded to the VM; files on your machine change only when
you copy results back. The upload skips `.git`, `target`, `node_modules`,
and `.venv`; other project files are included.

`agent-up` leaves the VM running. Terminate it when finished; its reusable
image remains. By default the VM has a one-hour lifetime ceiling, suspends
after ten minutes of inbound inactivity, and terminates after ten more
minutes suspended. See the [agent guide](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/)
for both agents together, longer sessions, and credential refresh.

## Run commands or connect an SDK

Build a general-purpose image once, then use it repeatedly:

```sh
microvm build --name agent-tools
microvm run --image agent-tools --exec "uname -m"
```

This prints `aarch64` from the guest. `run` tears down its VM by default;
`--keep --vm-name dev` keeps it for later `exec`, file transfer, or SDK calls.
The CLI accepts image names; SDKs need the full image ARN returned by the
build as `imageIdentifier`. [Python, Node.js/TypeScript, and Rust examples](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/)
show how to run an agent's tools in a fresh sandbox from your application.

## Networking and automation

Agent VMs have outbound access so they can call Bedrock. Omitting `--egress`
on a general `run` does not block outbound traffic. For no egress, use
`--egress-network-connector ARN` with an existing VPC connector in a VPC
without an internet gateway, NAT gateway, or other internet route. `--deny-egress` sets advisory
proxy variables that workloads can bypass. Keep the guest's execution role
minimal: the workload can access that role's credentials.

Use `microvm doctor` for setup diagnostics, `--json` for structured output,
and `microvm manifest` for the installed CLI's machine-readable command
reference. Check `data.leaked` after a failed cleanup; `microvm ls --remote`
helps locate remaining resources.

[Documentation](https://laithalsaadoon.github.io/microvms-agentd/) · Apache-2.0
