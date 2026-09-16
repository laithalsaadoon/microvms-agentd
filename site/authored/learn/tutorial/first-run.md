---
title: Configure AWS and run your first sandbox
description: Set up AWS once, give an agent a project in a MicroVM, and copy its results back.
editUrl: false
sidebar:
  order: 2
---

Give a coding agent a copy of your project in its own AWS Lambda MicroVM.
It can inspect files, edit code, and run commands in `/workspace`; you
choose which results to bring back to your machine.

With the [CLI installed](/learn/tutorial/install/) and AWS configured,
you can start the workflow below in 90 seconds. A first image build takes
several minutes, and the agent's task takes additional time. These commands
create billable AWS resources.

## Configure AWS

You need AWS CLI v2, your normal AWS credential configuration, and a region
where your account can use Lambda MicroVMs. Supported region names are
`us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, and `ap-northeast-1`.
Use `gh` or `curl` for the CLI's automatic daemon download.

Image builds need an S3 artifact bucket, a build role, and an execution
role in your account. Your caller needs permission to manage MicroVMs,
upload the artifact, and pass those roles. For coding agents, it also
needs Bedrock model invocation permissions and access to the chosen model.
The default Claude Code profile uses `global.anthropic.claude-opus-5`;
the [agent guide](/learn/operations/run-coding-agents-on-bedrock/) covers
Codex and model overrides.

Export your existing infrastructure values, replacing the examples:

```bash
export AWS_REGION=us-east-1
export MICROVM_BUCKET=your-artifact-bucket
export MICROVM_BUILD_ROLE_ARN=arn:aws:iam::123456789012:role/microvm-build
export MICROVM_EXECUTION_ROLE_ARN=arn:aws:iam::123456789012:role/microvm-execution
```

Use `AWS_PROFILE` if your credentials are in a named profile. These examples
use Bash-compatible shell syntax; in PowerShell, set environment variables
with `$env:NAME = "value"`. Matching `--bucket`, `--build-role-arn`,
`--execution-role-arn`, and `--region` flags are also available.

If you need the bucket and roles, the repository includes a Terraform
example. With Git and Terraform 1.6+ installed, run this once:

```bash
git clone https://github.com/laithalsaadoon/microvms-agentd.git
cd microvms-agentd
terraform -chdir=conformance/infra init
terraform -chdir=conformance/infra apply -var="region=us-east-1"
export AWS_REGION=us-east-1
export MICROVM_BUCKET=$(terraform -chdir=conformance/infra output -raw s3_bucket)
export MICROVM_BUILD_ROLE_ARN=$(terraform -chdir=conformance/infra output -raw build_role_arn)
export MICROVM_EXECUTION_ROLE_ARN=$(terraform -chdir=conformance/infra output -raw execution_role_arn)
```

This is an example infrastructure stack; it does not grant your caller
Bedrock model access. Keep the guest execution role minimal because the
workload can access its credentials. The stack also creates a separate
build-log reader policy for the operator.

```bash
microvm doctor
```

`doctor` reports setup findings and remedies. Check those before launching;
it does not prove that your chosen Bedrock model is available to your account.

## Run an agent on your project

Change to the project directory you want the agent to review, then run:

```bash
microvm agent-up --vm-name review --agent claude-code --project .
microvm agent-prompt --name review \
  "Review this project and write your findings to REVIEW.md."
microvm cp --name review vm:/workspace/REVIEW.md ./REVIEW.md
microvm terminate review --wait
```

`agent-up` prepares the image, starts the VM, uploads your project, and
installs a short-lived Bedrock token. `agent-prompt` runs as uid 1000 in
`/workspace`. The copy command retrieves the actual file the agent wrote.
Your project upload skips `.git`, `target`, `node_modules`, and `.venv`;
other files are included. Dependencies excluded from the upload may need
to be installed inside the VM.

`agent-up` keeps the VM until you terminate it or its lifetime policy
expires. The default is a one-hour maximum, suspension after ten minutes
of inbound inactivity, and termination after ten minutes suspended.
Its image remains for reuse on the next launch.

The VM has outbound access to reach Bedrock. Omitting `--egress` on a
general `run` does not disable outbound traffic. For no egress, use a
custom VPC connector in a VPC without an internet gateway, NAT gateway, or
other internet route;
`--deny-egress` only sets proxy variables that workloads can bypass.
See [Networking](/internals/networking/).

## Run a command without an agent

For a hello-world with automatic VM and image cleanup:

```bash
microvm quickstart
```

For repeated CLI runs or the [SDK examples](/learn/tutorial/from-code/),
build a general-purpose image once:

```bash
microvm build --name agent-tools --json
microvm run --image agent-tools --exec "uname -m"
```

The guest prints `aarch64`. Each `run` tears down its VM by default, while
the existing image stays available. The CLI accepts an image name;
SDKs need its ARN. Copy `data.imageIdentifier` from the build's JSON into
`MICROVM_IMAGE` for the SDK examples:

```bash
export MICROVM_IMAGE='paste-the-image-ARN-here'
```

Replace the example value with the exact ARN the build returned. With
`--reuse`, `build` appends a content hash to the name; use the returned
`imageIdentifier` or complete `imageName` in CLI commands instead of the
`agent-tools` prefix.

## Check cleanup and cost

`--json` returns structured results. On cleanup failures, inspect
`data.leaked`; `microvm ls --remote` helps find remaining resources.
Cost totals are estimates and are lower bounds when some items are
unpriced. Images have a one-week minimum retention charge, so reusing
them avoids repeated builds and snapshots.

Next: [run both coding agents](/learn/operations/run-coding-agents-on-bedrock/),
[use an SDK](/learn/tutorial/from-code/), or
[keep a VM and work by name](/learn/tutorial/long-lived-vm/).
