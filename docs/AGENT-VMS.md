# Run coding agents in a MicroVM

`AgentVm` composes the shared sandbox and session APIs for Claude Code and
Codex. The CLI exposes that workflow as `agent-up` and `agent-prompt`; Python,
Node, and Rust expose the same underlying recipes. Scheduling, task queues,
and coordination across VMs belong to the calling application.

For a complete walkthrough, use
[Run coding agents on Bedrock](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/).
The examples below assume the CLI, AWS credentials, artifact bucket, build
role, and execution role are configured. They create billable AWS resources.
The caller also needs access to the selected Bedrock model.

## Start a workspace and run a task

```bash
microvm agent-up --vm-name review --agent claude-code --project .
microvm agent-prompt --name review --agent claude-code \
  "Review this project and write your findings to REVIEW.md."
microvm cp --name review vm:/workspace/REVIEW.md ./REVIEW.md
microvm terminate review --wait
```

`agent-up` builds or reuses an image, launches a named VM, uploads the project
when requested, and installs model access. The project is available in
`/workspace`. Prompts run as uid and gid 1000. Read the files or test results
the task was meant to produce; a successful agent process is not proof that
the requested work happened.

The VM remains available after a prompt. Terminate it when finished, including
after a task failure, and inspect cleanup results. Its image can be reused by
later launches.

## Choose profiles, models, and versions

Pass `--agent codex` for Codex or repeat `--agent` to install both profiles.
When both are installed, `agent-prompt` requires the agent to be named. With a
single installed profile it can read that choice from the guest marker.

The maintained defaults are defined in
[`agents/profile.rs`](../crates/microvms-app/src/agents/profile.rs):

| Profile | npm package | Default model |
|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` | `global.anthropic.claude-opus-5` |
| `codex` | `@openai/codex` | `global.openai.gpt-5.6-sol` |

Model access depends on the account and region. Use `--claude-model` or
`--codex-model` to choose an available model. Use `--claude-version` or
`--codex-version` to pin a CLI package version. Without a pin, an image build
installs the package version available then; reusing that image does not
update the package inside it.

Image reuse depends on the build inputs, including the generated Dockerfile
and version pins. A reused image and a reused VM are separate outcomes,
reported as `imageReused` and `vmReused` in the `microvm.agent` envelope.

## Refresh an existing workspace

Run `agent-up` again with the registered VM name to attach and refresh model
credentials. This path retains the VM instead of building and launching a new
one. If `--project` is supplied, it uploads that project again.

The Bedrock token is minted from the caller's AWS credential chain and written
to guest files after launch. It is not included in the shared image. The
credential environment and provider configuration are under `/workspace`;
treat that directory as containing credentials when deciding what to download.

The marker `/workspace/.agent-vm.json` records the installed profiles and
models. It lets another session discover which profile to prompt. The local
CLI name record remains specific to the machine and state directory that
created it; use `attach` to adopt an existing VM elsewhere.

## Set deadlines and permissions

Use `microvm manifest` for the installed CLI's flags and defaults. The prompt
controls distinguish the caller's wait from the guest execution budget:

- `--timeout` bounds how long the CLI waits for the result.
- `--execution-timeout` bounds execution in the daemon.
- `--detach` returns the execution ID for later polling or streaming.
- `--exec-id` preserves the identity of a start that may need reconciliation.
- `--reap-group-on-exit` requests cleanup of residual child processes.
- `--permission-mode` selects the agent's own tool-approval mode.

The SDK's `prompt_sync` convenience uses its timeout for both execution and
waiting. The asynchronous execution-handle path lets a caller manage the
result separately. In either case, inspect the execution outcome.

An unrestricted agent mode changes the agent CLI's approval behavior. It
does not change the VM's IAM permissions or network configuration. Agents
share the guest environment; user demotion is not isolation from other work
in that VM.

## Use the libraries

Start with the language-specific examples:

- [Python](../bindings/microvms-py/README.md)
- [JavaScript and TypeScript](../bindings/microvms-js/README.md)
- [Rust](../crates/microvms-core/README.md)

`AgentVm` provides image preparation, launch, access installation, prompt
execution, and termination. An application that already holds a `Session`
can use the corresponding free functions, including `installed_agents`,
`install_agent_access`, and `prompt_agent` in Python, or their camelCase names
in Node. The Rust helpers are re-exported through `microvms_core::agents`.

These calls share the same implementation as the CLI. Keep the returned
execution IDs and cleanup reports in the application's lifecycle records.
For another agent tool, compose `Sandbox` and `Session` directly rather than
copying client transport or bootstrap logic.

## Implementation and evidence

The recipes and prompt requests live in
[`microvms-app/src/agents`](../crates/microvms-app/src/agents/mod.rs).
The production Bedrock signer lives in
[`microvms-edges/src/agents`](../crates/microvms-edges/src/agents/mod.rs).
Core wires the implementations together, and the CLI and bindings adapt them.

Local tests cover image derivation, profile validation, request construction,
credential-file generation, and client behavior through test ports. The live
suite separately exercises agent launches and tasks against Bedrock. Profile
verification dates are in the source table, and each records when that
profile was last verified against AWS.

See [Trust](TRUST.md), [Networking](NETWORKING.md), and
[Suspend and resume](SUSPEND-RESUME.md) before choosing credentials, idle
policies, or a long-running workflow.
