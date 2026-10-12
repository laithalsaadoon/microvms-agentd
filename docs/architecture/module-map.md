# microvms-agentd · Module map

The main sections follow the workspace crate boundaries, beginning with the shared contract
and ending with the adapters and portable models. `Cargo.toml:2-14`

## microvms-protocol

The shared wire contract defines execution, file-transfer, health, lifecycle-hook, identity, and tunnel types used by the daemon and client stack. `crates/protocol/src/lib.rs:29-34` Its module declarations are the starting point for a wire-format change; transport implementations live in their consumers. `crates/protocol/src/lib.rs:16-21`

- `crates/protocol/src/exec.rs:2`
- `crates/protocol/src/health.rs:2`
- `crates/protocol/src/hook.rs:2`
- `crates/protocol/src/identity.rs:2`
- `crates/protocol/src/lib.rs:2`
- `crates/protocol/src/tunnel.rs:2`
- `crates/protocol/src/fs.rs:2`

## agentd

The guest daemon owns command execution, file transfer, lifecycle hooks, guest identity setup, and TCP relay handlers. `crates/agentd/src/lib.rs:37-54` Its router assembles the endpoint descriptions into the served application, while the schema module describes request and response shapes. `crates/agentd/src/routes.rs:29-58` `crates/agentd/src/schema.rs:2-12`

- `crates/agentd/src/exec.rs:2`
- `crates/agentd/src/fs.rs:2`
- `crates/agentd/src/routes.rs:2`
- `crates/agentd/src/schema.rs:2`
- `crates/agentd/src/tunnel.rs:2`
- `crates/agentd/src/identity.rs:2`
- `crates/agentd/src/exec_start.rs:2`
- `crates/agentd/src/state.rs:2`

## microvms-domain

The domain layer owns the values and rules shared by client surfaces: sizing, cost, service constraints, regions, names, errors, hook timeouts, and identity material. `crates/microvms-domain/src/lib.rs:32-50` Rules receive ambient inputs as arguments, leaving production I/O to higher layers. `crates/microvms-domain/src/lib.rs:2-21`

- `crates/microvms-domain/src/cost.rs:2`
- `crates/microvms-domain/src/constants.rs:2`
- `crates/microvms-domain/src/sizing.rs:2`
- `crates/microvms-domain/src/error.rs:2`
- `crates/microvms-domain/src/names.rs:2`
- `crates/microvms-domain/src/region.rs:2`
- `crates/microvms-domain/src/hooks.rs:2`
- `crates/microvms-domain/src/identity.rs:2`

## microvms-app

The application layer owns the control-plane client, sandbox lifecycle, in-VM sessions, image preparation, and coding-agent recipes. `crates/microvms-app/src/lib.rs:2-5` It declares the ports used for transport, local storage, time, entropy, provisioning, and other external effects; production implementations come from the edges layer. `crates/microvms-app/src/lib.rs:6-12` `crates/microvms-app/src/lib.rs:29-34`

- `crates/microvms-app/src/control/mod.rs:2`
- `crates/microvms-app/src/agents/mod.rs:2`
- `crates/microvms-app/src/session/mod.rs:2`
- `crates/microvms-app/src/sandbox.rs:2`
- `crates/microvms-app/src/control/image.rs:2`
- `crates/microvms-app/src/control/microvm.rs:2`
- `crates/microvms-app/src/control/ops.rs:2`
- `crates/microvms-app/src/control/artifact.rs:2`

## microvms-edges

The edges layer implements the application ports with production I/O: signed control-plane requests, daemon HTTP calls, socket relays, local storage, release retrieval, clock, and entropy. `crates/microvms-edges/src/lib.rs:2-21` The files below lead to the connection-serving loops and provisioning paths; the crate root also identifies the smaller port implementations. `crates/microvms-edges/src/session/serve.rs:2-6` `crates/microvms-edges/src/provision.rs:2-3` `crates/microvms-edges/src/lib.rs:27-40`

- `crates/microvms-edges/src/session/forward.rs:2`
- `crates/microvms-edges/src/provision/release.rs:2`
- `crates/microvms-edges/src/provision.rs:2`
- `crates/microvms-edges/src/session/serve.rs:2`
- `crates/microvms-edges/src/session/tunnel.rs:2`
- `crates/microvms-edges/src/workspace.rs:2`
- `crates/microvms-edges/src/session/shell.rs:2`
- `crates/microvms-edges/src/control/transport.rs:2`

## microvms-core

The composition root presents the Rust client API used by the CLI and language bindings, re-exporting the domain, application, edges, and protocol layers. `crates/microvms-core/src/lib.rs:55-95` Its prelude supplies extension traits that construct application types with production ports. `crates/microvms-core/src/prelude.rs:2-17`

- `crates/microvms-core/src/lib.rs:2`
- `crates/microvms-core/src/prelude.rs:2`
- `crates/microvms-core/src/identity.rs:2`

## microvms-cli

The `microvm` binary parses the command tree, dispatches handlers through its library seam, and renders command results. `crates/microvms-cli/src/main.rs:81-108` `crates/microvms-cli/src/main.rs:182-240` Command modules group lifecycle work, operations on existing VMs, local operations, and coding-agent workflows; project configuration supplies another source of run options. `crates/microvms-cli/src/commands/mod.rs:25-39` `crates/microvms-cli/src/config.rs:2-6`

- `crates/microvms-cli/src/cli.rs:2`
- `crates/microvms-cli/src/commands/attached.rs:2`
- `crates/microvms-cli/src/commands/lifecycle.rs:2`
- `crates/microvms-cli/src/commands/mod.rs:2`
- `crates/microvms-cli/src/commands/local.rs:2`
- `crates/microvms-cli/src/main.rs:2`
- `crates/microvms-cli/src/commands/agent.rs:2`
- `crates/microvms-cli/src/config.rs:2`

## microvms-py

The Python binding maps the core client into PyO3 classes for values, lifecycle operations, sessions, execution, and coding-agent workflows. `bindings/microvms-py/src/lib.rs:44-57` Blocking methods and awaitable `_async` twins use a shared Tokio runtime; the runtime module owns that bridge and its cancellation policies. `bindings/microvms-py/src/runtime.rs:4-40`

- `bindings/microvms-py/src/session.rs:2`
- `bindings/microvms-py/src/sandbox.rs:2`
- `bindings/microvms-py/src/agents.rs:2`
- `bindings/microvms-py/src/cost.rs:2`
- `bindings/microvms-py/src/control.rs:2`
- `bindings/microvms-py/src/exec.rs:2`
- `bindings/microvms-py/src/lib.rs:2`
- `bindings/microvms-py/src/serve.rs:2`

## microvms-js

The Node binding exposes the core client through napi-rs classes, result objects, and options. `bindings/microvms-js/src/lib.rs:17-35` Async exports return promises, and execution also has a process-style interface with separate output streams, waiting, and termination. `bindings/microvms-js/src/lib.rs:62-78` `bindings/microvms-js/src/process.rs:2-8`

- `bindings/microvms-js/src/session.rs:2`
- `bindings/microvms-js/src/sandbox.rs:2`
- `bindings/microvms-js/src/cost.rs:2`
- `bindings/microvms-js/src/agents.rs:2`
- `bindings/microvms-js/src/control.rs:2`
- `bindings/microvms-js/src/exec.rs:2`
- `bindings/microvms-js/src/process.rs:2`
- `bindings/microvms-js/src/lib.rs:2`

## agentd-model

The portable model crate expresses daemon and client behavior as state machines checked with Stateright. `crates/model/src/lib.rs:2-6` Its modules cover bootstrap and execution, client lifecycle, image preparation, output, launch posture, preflight, provisioning, completion, tunnels, and Dockerfile wrapping. `crates/model/src/lib.rs:36-47` These specifications remain separate from the implementation exercised by the model-conformance crate. `crates/model-conformance/src/lib.rs:2-10`

- `crates/model/src/lib.rs:2`
- `crates/model/src/exec_start.rs:2`
- `crates/model/src/tunnel.rs:2`
- `crates/model/src/client.rs:2`
- `crates/model/src/run.rs:2`
- `crates/model/src/provision.rs:2`
- `crates/model/src/posture.rs:2`
- `crates/model/src/output.rs:2`

## Supporting code

- `crates/model-conformance/src/lib.rs:2-14`
- `crates/model-conformance/tests/client_lifecycle.rs:2`
- `conformance/run_rs.py:90-94`
- `conformance/lanes/suite.py:2`
- `conformance/selftest/suite.py:2`
- `site/astro.config.ts:1`
- `site/src/content.config.ts:1`
- `site/src/lib/citation-links.ts:2`
- `site/src/styles/rfc.css:1`
- `examples/background-coding-agent/handler.py:1`
- `examples/background-coding-agent/cli.py:1`
- `examples/coding-agents-on-bedrock/run.sh:1`
- `examples/code-server-remote-dev/run.sh:1`
- `examples/s3-prefetch-at-build/run.sh:1`

## See also

- [contract map](../insights/contract-map.md): 41 shared source citations
- [impact analysis](../insights/impact-analysis.md): 31 shared source citations
- [processes](../behavior/processes.md): 20 shared source citations
- [business logic](../insights/business-logic.md): 18 shared source citations
- [risk hotspots](../analysis/risk-hotspots.md): 12 shared source citations
