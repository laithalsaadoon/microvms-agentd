---
title: Run coding agents on Bedrock inside a MicroVM
description: Bring up a VM with Claude Code or Codex CLI in it with one command, hand it a task with another, against Bedrock, with credentials minted from your own AWS identity and no vendor API key anywhere.
editUrl: false
sidebar:
  order: 3
---

```bash
microvm agent-up --vm-name dev --agent claude-code --agent codex
microvm agent-prompt --name dev --agent claude-code \
  "Write a one-line bash command that counts files in /usr/bin, run it, and report the number."
microvm terminate dev
```

`agent-up` builds an image carrying the agent CLIs, launches a VM with outbound network, mints a short-lived Bedrock bearer token from your AWS credentials, installs it as a file inside the VM, and registers the name `dev`. `agent-prompt` runs the agent headless as a non-root user over your task and prints what it did. At the end of this page you will have run both agents inside a VM, refreshed their credentials without relaunching, moved a project in and out, and know why the helper does each thing it does. [Agent VMs](/internals/agent-vms/) is the specification.

## 1. Prerequisites

The `microvm` CLI on your `PATH`; the `MICROVM_BUCKET`, `MICROVM_BUILD_ROLE_ARN`, and `MICROVM_EXECUTION_ROLE_ARN` values from [your first run](/learn/tutorial/first-run/); and AWS credentials that can call `lambda-microvms` and `bedrock:InvokeModel`. The account needs Bedrock access to the models the profiles default to, `global.anthropic.claude-opus-5` for Claude Code and `global.openai.gpt-5.6-sol` for Codex; `--claude-model` and `--codex-model` override them. Nothing else is needed on your machine: the token is minted in-process, so there is no Python step.

## 2. Bring the VM up

```bash
microvm agent-up --vm-name dev --agent claude-code --agent codex --json
```

`--agent` is repeatable and defaults to `claude-code` alone. With `--json` the command prints one envelope of type `microvm.agent`; without it, a short text summary ending in the prompt and teardown lines to copy. Step by step, on a name this machine has not registered:

1. **The image.** The Dockerfile is the client's own agentd stanza plus three layers: `dnf install nodejs22 nodejs22-npm python3 git tar gzip which findutils procps-ng`, `npm install -g` of each agent's package, and a uid and gid 1000 appended to `/etc/passwd` and `/etc/group` directly, because `useradd` is not in the minimal base. `/workspace` is created and handed to that user and becomes the `WORKDIR`. The image is named `agent-vm-<agents>-<hash12>`, the hash over the daemon binary and the Dockerfile text, so an unchanged agent set reuses its image in seconds and a `--claude-version` or `--codex-version` pin builds a fresh one under a new name. The image contains no secret of any kind.
2. **The launch.** A kept VM with egress, because neither agent reaches Bedrock without it; both would install fine and then fail on their first model call. `--memory` defaults to `1024` rather than `run`'s `2048`. The minimum you request is your bill floor, and four times it is the guest's always-present ceiling, with no scaling event. Agent sessions are peaky, long stretches of a small steady state punctuated by bursts of build and test work, so a 4 GiB ceiling at half the floor cost fits them, and the peaks bill only by what is consumed. A steadier or heavier workload passes `--memory 2048`.
3. **The token.** A Bedrock bearer token is a SigV4 query presign of `POST https://bedrock.amazonaws.com/` with `Action=CallWithBearerToken`, base64-encoded and prefixed `bedrock-api-key-`. The CLI mints it from your default credential chain for `--token-ttl-hours` (default and ceiling 12). It is never printed, never an argument to anything, and never in the launch payload.
4. **The files.** Three uploads over the authenticated channel, then one root exec. `/workspace/.agent-env` (mode `0600`) is the file every agent sources:

   ```bash
   export HOME="/workspace"
   export PATH="/usr/local/bin:/usr/bin:/bin"
   export AWS_REGION="us-east-1"
   export CLAUDE_CODE_USE_BEDROCK="1"
   export ANTHROPIC_MODEL="global.anthropic.claude-opus-5"
   export AWS_BEARER_TOKEN_BEDROCK="<token>"
   export OPENAI_API_KEY="<token>"
   ```

   Claude Code has a native Bedrock mode: `CLAUDE_CODE_USE_BEDROCK=1` plus the bearer token, with the model chosen by `ANTHROPIC_MODEL` as an inference-profile id. Codex has no Bedrock mode, and `bedrock-runtime` exposes an OpenAI-compatible surface that serves the Responses wire API Codex speaks, so when Codex is installed a second file, `/workspace/.codex/config.toml`, defines a provider with the bearer token as its API key. Two lines are required on that host: the model is an inference-profile id (the bare `openai.gpt-5.6-sol` is refused with "on-demand throughput isn't supported"), and hosted web search is disabled, because Codex advertises that tool by default and Bedrock fails the turn with "web search is not supported for this request":

   ```toml
   model = "global.openai.gpt-5.6-sol"
   model_provider = "bedrock"
   model_reasoning_effort = "medium"
   web_search = "disabled"
   [model_providers.bedrock]
   name = "Amazon Bedrock"
   base_url = "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
   env_key = "OPENAI_API_KEY"
   wire_api = "responses"
   ```

   The `PATH` line matters. The daemon spawns execs from an empty environment, and Claude Code's Bash tool snapshots the shell it starts from: without an exported `PATH` the agent's subshells find no `ls`, `wc`, or `python3` (every command exits 127) even though the daemon's own execs resolve them. Codex probes absolute paths when lookup fails, so it limps through; Claude Code does not.

   The third file is the marker `/workspace/.agent-vm.json` (mode `0644`), naming the installed agents and their models, so a later `agent-prompt` can learn which agent to run from the VM itself. The daemon writes every upload as root, mode `0600`, which a demoted agent cannot read, so the command finishes with one root `chown -R 1000:1000 /workspace`.
5. **The name.** `dev` is registered last, only over a VM every step succeeded on. A failure between the launch and the registration tears the VM down and names anything it could not remove in the failure envelope's `data.leaked`, because a running VM with no credentials and no name is one nobody can use.

Envelope keys worth reading: `imageReused` says whether the image was built or found; `vmReused` is `false` on this path; `agents` lists each installed agent with its `model` and its `headlessCommand`, the exact template `agent-prompt` runs with `<TASK>` where the quoted task goes; `credentialExpiresAt` is the token's expiry in epoch seconds; `microvmId`, `endpoint`, and `agentToken` are the identifier triple every attached command can take instead of `--name`.

## 3. Prompt an agent

```bash
microvm agent-prompt --name dev --agent codex \
  "Create hello.py that prints hello from a microvm, run it, and show the output."
microvm exec "cat /workspace/hello.py" --name dev
```

The command runs the agent's headless line, `claude -p <TASK> --allowedTools Bash,Read,Edit,Write,Grep,Glob` or `codex exec --skip-git-repo-check -s workspace-write <TASK>`, as uid 1000 and gid 1000, in `/workspace`, with `. /workspace/.agent-env &&` in front. It waits up to `--timeout` (default 900 seconds, because agent tasks run minutes, not seconds), then prints the agent's output and acks the exec. The envelope is `microvm.agent.prompt` with `exec`'s keys plus `agent` and `model`; a non-zero agent exit is `ERR_EXEC_FAILED`, as for `exec`. The `cat` afterwards is how you prove the agent did filesystem work inside the VM rather than reporting that it had. That proof is not decoration: in one of five identical runs on 2026-09-10, Codex answered "I can't create or run files in this environment", made no tool call, and exited 0, so the prompt envelope alone said `ok`. The config sets `model_reasoning_effort = "medium"` because Codex has no metadata for a Bedrock model id and otherwise sends no effort at all; whether that lowers the decline rate is unmeasured. Read the effect back, and re-prompt once if it is missing.

Why uid 1000 and not root: Claude Code's `--dangerously-skip-permissions` refuses to run as root, and the agent then denies its own Bash, Grep, and WebFetch calls and returns a confident report built on zero tool calls. Measured on this task shape: as uid 0 the agent made no shell calls at all; as uid 1000 it made 147. The helper never offers a root option.

With two agents installed, `--agent` is required: the command reads the marker, finds two names, and refuses with `ERR_PRECONDITION` listing both. With one installed, omit it and the marker chooses. A typed `--agent` still reads the marker, so the prompt uses the model the VM was provisioned with.

For a long task, detach and poll:

```bash
ID=$(microvm agent-prompt --name dev --agent claude-code --detach --json "Refactor …" | jq -r .data.execId)
microvm exec --poll "$ID" --name dev
microvm ack "$ID" --name dev
```

`--detach` starts the agent and returns `phase: running` with the exec id. `exec --poll` reads the record without acking, so you can poll on an interval; `ack` releases it when you have what you need. To watch output as it arrives, take the `headlessCommand` from the `agent-up` envelope and run it through `microvm exec --stream --name dev --user 1000 --group 1000`, with your task in place of `<TASK>`.

## 4. Refresh the credentials

The token lives at most twelve hours. When it expires the VM is still running and still yours; re-run the same command against the same name:

```bash
microvm agent-up --vm-name dev
```

Because `dev` is registered, the command builds nothing and launches nothing. It attaches, reads the marker to keep the agents and models the VM already has, mints a fresh token, rewrites the same files, runs the same `chown`, and reports `vmReused: true` with a later `credentialExpiresAt`; the image keys are `null` because no image was touched. Typing `--agent` on a refresh is how you change the agents or models, and `--project` on a refresh uploads a tree the same way it does on a fresh launch.

## 5. Bring a project in and results out

```bash
microvm agent-up --vm-name dev --project ./my-app
microvm agent-prompt --name dev "Run the test suite and fix the first failing test."
microvm cp --tar vm:/workspace ./after.tar --name dev
```

`--project` packs the directory the way `run <DIR>` does, with `.git`, `target`, `node_modules`, and `.venv` skipped whole, under the same size budgets, and before any AWS call, so an unreadable tree costs nothing. The archive is uploaded into `/workspace` before the `chown`, so the agent owns every file it finds there. Codex's `--skip-git-repo-check` is on the headless line for exactly this reason: a synced tree arrives without `.git`. `cp --tar` brings the whole workspace back as one archive.

## 6. The recipe by hand

[examples/coding-agents-on-bedrock/run.sh](https://github.com/laithalsaadoon/microvms-agentd/tree/main/examples/coding-agents-on-bedrock) is the same recipe as a shell script, one `microvm` call per step: `build --reuse` with its own Dockerfile, `run --keep --egress` with `memory = 1024` in a `microvm.toml`, a token from the `aws-bedrock-token-generator` package through `uvx`, `cp --mode 0600` for the environment file, the root `chown` exec, and one `exec --user 1000 --group 1000` per agent. Read it when you want to see each decision on its own line or drive a step differently; the two commands above are that script moved into the library, with the same measured values.

## 7. Cost and cleanup

An agent VM is kept by definition, so nothing tears it down for you except the launch's own idle policy: `--max-idle-sec` (default 600) suspends it after that much inbound idleness, `--suspended-sec` (default 600) terminates it after that long suspended, and `--max-duration-sec` (default 3600) is the hard ceiling. A multi-hour session needs an outside keepalive, because idleness is measured outside the VM: poll `microvm health --name dev` on an interval under `--max-idle-sec`, or pass `--auto-resume` so the next request wakes it. [Keep a VM running and work inside it](/learn/tutorial/long-lived-vm/) covers suspend and resume. When you are done:

```bash
microvm terminate dev
```

The image persists deliberately: its snapshot has a one-week minimum retention, so keeping and reusing it is cheaper than rebuilding, and the next `agent-up` with the same agents finds it by name. Delete old images with `aws lambda-microvms delete-microvm-image`.

Two open-source harnesses run coding agents inside Lambda MicroVMs the same way, each carrying its own hand-rolled daemon. [Harness capabilities](/internals/harness-capabilities/) maps their contracts onto this platform and ranks what is still missing; the agent VM layer is what such a harness class would call.
