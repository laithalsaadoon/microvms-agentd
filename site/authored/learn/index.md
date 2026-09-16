---
title: Learn
description: Tutorials that take you from an empty machine to a project running inside a Lambda MicroVM, and task-shaped pages for operating one.
editUrl: false
sidebar:
  order: 0
---

Run a coding agent or your own tools in a remote AWS MicroVM. Pick the path
that gets you to a working sandbox; you do not need to read every tutorial in
order.

## Start here

| Your starting point | Next step |
|---|---|
| I have not installed anything | [Install the CLI](/learn/tutorial/install/) |
| I need AWS resources or my first sandbox | [First run](/learn/tutorial/first-run/) |
| I want an agent to work on my project | [Run Claude Code or Codex in a sandbox](/learn/operations/run-coding-agents-on-bedrock/) |
| I want to use Python, Node, or Rust | [Run a sandbox from code](/learn/tutorial/from-code/) |
| I already have a VM running | [Keep working by name](/learn/tutorial/long-lived-vm/) |
| I want to upload code and collect outputs | [Run a project](/learn/tutorial/run-a-project/) |

With the CLI installed and AWS configured, the quickstart takes less than
90 seconds to read and start. The first image build takes several minutes.
VMs and images create AWS charges. The first-run guide covers prerequisites,
expected output, image reuse, and cleanup.

## More tasks

These guides assume the CLI is installed and AWS is configured.

| Page                                                                                         | Answers                                                                                        |
| -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| [Write a guest Dockerfile](/learn/operations/write-a-guest-dockerfile/)                      | How to start from the generated stanza, and which traps cost a server-side build cycle          |
| [Embed agentd in your own image](/learn/operations/embed-agentd-in-your-image/)              | How to append the daemon to a task image your own harness drives                                |
| [Run coding agents on Bedrock](/learn/operations/run-coding-agents-on-bedrock/)              | How `agent-up` and `agent-prompt` run Claude Code and Codex CLI headless in a VM with no vendor API key |
| [Remote dev with code-server](/learn/operations/remote-dev-with-code-server/)                | How to reach VS Code in a browser through `port-forward`, on a VM that suspends when you leave   |
| [Prefetch S3 content at image build](/learn/operations/prefetch-s3-at-build/)                | How to bake an S3 prefix into the snapshot so a launched VM makes no S3 call                     |
| [Configure networking](/learn/operations/configure-networking/) | Attach a VPC connector and restrict internet access |
| [Read the cost report](/learn/operations/read-the-cost-report/)                              | What each line means, why a total may read "at least", and how to plan with `microvm cost`       |
| [Debug a failed build](/learn/operations/debug-a-failed-build/)                              | Where the reason lives, how to read the build log, and what `ERR_BUILD_WEDGED` means             |
| [Recover a leaked VM](/learn/operations/recover-a-leaked-vm/)                                | What `ls` and `history` say you left behind, and how to ask the account directly                 |
| [Configure the project file](/learn/operations/configure-the-project-file/)                  | Every `microvm.toml` key, which source wins, and what the loader refuses                         |
| [Drive it from a script or an agent](/learn/operations/drive-it-from-a-script-or-an-agent/)  | The one-envelope rule, the exit codes, the manifest, and the streaming exception                 |
| [Run the live suite](/learn/operations/run-the-live-suite/)                                  | What `mise run check` proves, what `mise run live` adds, what it costs, and how to leave the account clean |

## Automation and cleanup

Every command takes `--json` and then writes exactly one JSON envelope on stdout; progress goes to stderr. A success envelope carries `type` and `data`. A failure envelope carries a stable `code`, an `exitCode` that matches `$?`, a `finding` naming the section of [Platform](/internals/platform/) that measured the behavior, and `suggestions`. Branch on `code`, never on the `error` text. The one exception is `exec --stream`, which writes NDJSON events and the envelope last, under its own `type`. [Drive it from a script or an agent](/learn/operations/drive-it-from-a-script-or-an-agent/) develops this.

Teardown is attempted by default. `microvm run` builds an image, launches a VM, runs your command, reports the cost, and attempts cleanup. Inspect `leaked` for failures; interruption can leave resources behind. `--keep` opts out and hands you the identifiers you have just taken responsibility for. The image is the durable artifact: its snapshot has a one-week minimum retention, so deleting it early saves nothing and reusing it with `--image` is the economical habit.

If you are an AI agent rather than a person reading a page, start with `microvm manifest`. It prints every command, flag, response type, and exit code this binary accepts, as JSON, on a machine with no credentials and no network. [For agents](/agents/) names which surface answers which question. Come back here for the worked paths.
