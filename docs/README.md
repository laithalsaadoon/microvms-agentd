# Use microvms-agentd

Run agents and their tools in remote AWS Lambda MicroVM sandboxes. Start with
the [90-second path in the README](../README.md#start-in-90-seconds): configure
existing AWS resources, launch a sandbox, run a task, collect the result, and
terminate the VM. First-time AWS setup and image builds take longer.

| I want to… | Start here |
|---|---|
| Install the CLI and configure AWS | [First run](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/) |
| Run Claude Code or Codex on a project | [Coding agents in sandboxes](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/) |
| Add sandboxes to a Python application | [Python package quickstart](../microvms-py/README.md) |
| Add sandboxes to a Node application | [JavaScript / TypeScript quickstart](../microvms-js/README.md) |
| Use the Rust client | [Rust quickstart](../microvms-core/README.md) |
| Upload a project and retrieve artifacts | [Project guide](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/run-a-project/) |
| Look up a CLI flag or response | [CLI reference](https://laithalsaadoon.github.io/microvms-agentd/reference/) or `microvm manifest` |

The [SDK tutorial](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/)
includes programs you can save and run. For VM isolation, credentials, and
internet restrictions, read [Trust](TRUST.md) and [Networking](NETWORKING.md).
For a running daemon's wire contract, use `/v1/schema`.

## Current references

| Document | Purpose |
|---|---|
| [Networking](NETWORKING.md) | VPC connectors and internet isolation |
| [Protocol](PROTOCOL.md) | Daemon routes, authentication, execution, files, and streaming |
| [Embedding](EMBEDDING.md) | Image requirements and integration with a custom harness |
| [Trust](TRUST.md) | Bootstrap, credentials, workload boundaries, and networking |
| [Platform](PLATFORM.md) | Dated observations and corrections to AWS behavior |
| [Suspend and resume](SUSPEND-RESUME.md) | What survives a suspend, what triggers it, and what is unknown |
| [Agent VMs](AGENT-VMS.md) | Agent lifecycle and Bedrock integration |
| [Strategy](STRATEGY.md) | Project scope and priorities |
| [Harness capabilities](HARNESS-CAPABILITIES.md) | Integration requirements and remaining gaps |
| [Wire schema](schema.json) | Generated daemon contract |
| [CLI manifest](manifest.json) | Generated commands, flags, responses, and exit codes |

## Source analyses and history

These pages were generated from a particular source revision. Confirm their
citations against the current code; a line number alone does not establish
freshness. Current source and generated executable contracts take precedence.

- Architecture: [overview](architecture/system-overview.md),
  [modules](architecture/module-map.md), [data flow](architecture/data-flow.md).
- Reference: [CLI](reference/cli.md), [Rust and bindings](reference/public-api.md),
  [daemon routes](reference/rpc-tools.md).
- Behavior: [processes](behavior/processes.md), [state machines](behavior/state-machines.md).
- Analysis: [risks](analysis/risk-hotspots.md), [ownership](analysis/ownership.md),
  [dead code](analysis/dead-code.md).
- Diagrams: [components](diagrams/architecture/components.md),
  [dependencies](diagrams/structural/dependency-graph.md),
  [sequences](diagrams/behavioral/sequences.md).
- Maintenance: [impact](insights/impact-analysis.md),
  [debugging](insights/debugging-guide.md), [contracts](insights/contract-map.md),
  [business rules](insights/business-logic.md), [debt](insights/tech-debt.md).
- History: [implemented CLI coverage plan](CLI-COVERAGE-PLAN.md).

Edit user guides in `site/authored/` and technical documents here. The site
build copies them into `site/src/content/docs/`; do not edit that output.
