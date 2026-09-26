# microvms-agentd · Module map

The workspace declares its members (`Cargo.toml:2-9`), and the sections below run in the
dependency order of the `system-overview.md` flowchart, bottom-up: the wire contract first,
then `agentd` and `microvms-core`, which compile against it, then the CLI and the bindings over
the client, then the checked model (`docs/architecture/system-overview.md:79-97`). Each crate's
file list names its main `src/` files, and the `agentd` list is every module `agentd/src/lib.rs`
declares; a crate's `tests/` tier is excluded so the source files a reader is looking for are
not crowded out by large test files such as `agentd/tests/turmoil_transport.rs`. Files
belonging to no crate are collected under `Supporting code` at the end.

## protocol

`protocol` is the wire contract expressed as Rust types, split into `exec`, `fs`, `health`, and
`hook` (`protocol/src/lib.rs:29-32`). The daemon and every Rust client of it compile against the
same definitions, so a renamed field breaks compilation on whichever side has not caught up
rather than surfacing as a consumer's runtime bug (`protocol/src/lib.rs:10-12`). Membership is
decided by one rule — pure data that travels on the wire is admitted and machinery for making it
travel is not, which is why the SSE event payloads live here and the stream that emits them does
not (`protocol/src/lib.rs:16-21`). Every type derives both halves of serde even where one side
needs only one, because the missing half is what a client would otherwise hand-write, and
`docs/schema.json` is generated from those same attributes under both contracts and byte-compared
in CI (`protocol/src/lib.rs:23-27`).

- `protocol/src/exec.rs`
- `protocol/src/lib.rs`
- `protocol/src/hook.rs`
- `protocol/src/health.rs`
- `protocol/src/fs.rs`

## agentd

`agentd` is the in-VM daemon supplying the exec and file-transfer APIs AWS Lambda MicroVMs does
not have (`agentd/src/lib.rs:4-7`). Its modules divide by defect class rather than by HTTP
surface: `state` owns the one-shot bootstrap, `auth` decides authorization before a body byte is
read, `exec` owns idempotent start with ack-gated release, and `fs` owns streaming tar
(`agentd/src/lib.rs:31-46`). The trust boundary is the crate's organizing fact — the platform's
own `/run` hook arrives from `127.0.0.1`, indistinguishable at the socket level from a request
sent by a process inside the VM, so source-address filtering would reject a legitimate bootstrap
and the one-shot property is the only defense left (`agentd/src/lib.rs:11-16`). `routes.rs`
assembles the router by walking `surface_docs`, the same endpoint list `/v1/schema`
publishes, so a documented route with no handler panics at startup, and each endpoint's declared
auth mode decides which of the two routers it joins (`agentd/src/routes.rs:31-35`,
`agentd/src/routes.rs:48-58`, `agentd/src/routes.rs:371`, `docs/schema.json:497-1154`).

- `agentd/src/auth.rs`
- `agentd/src/config.rs`
- `agentd/src/disk.rs`
- `agentd/src/exec.rs`
- `agentd/src/exec_start.rs`
- `agentd/src/fs.rs`
- `agentd/src/hook_handlers.rs`
- `agentd/src/identity.rs`
- `agentd/src/routes.rs`
- `agentd/src/schema.rs`
- `agentd/src/serve.rs`
- `agentd/src/state.rs`
- `agentd/src/tunnel.rs`
- `agentd/src/tunnel_identity.rs`
- `agentd/src/exec_start_fuzz.rs` (private, compiled only under `#[cfg(test)]`)

## microvms-core

`microvms-core` is the client library and the workspace's largest crate, holding the control
plane, the in-VM session client, the cost engine, and every trap closure
(`microvms-core/src/lib.rs:2-3`). Its own doc comment splits it in two: `error`, `region`,
`sizing`, `hooks`, and `constants` are the foundation, while `cost`, `control`, `session`, and
`sandbox` are the product surface (`microvms-core/src/lib.rs:59-65`). Each measured platform
finding is spent once here so no caller has to measure it again, and every closure is ranked
on a strength ladder where S1 means the mistake cannot be written down at all
(`microvms-core/src/lib.rs:7-14`, `microvms-core/src/lib.rs:23-40`). `cost.rs` carries the rule
that makes the rest of it legible — unknown is not zero, so `Amount::Unpriced` is a distinct
variant a consumer has to match on rather than a $0.00
line (`microvms-domain/src/cost.rs:22-27`) — and the crate re-exports `protocol` so consumers name
wire types through here instead of depending on the contract crate
(`microvms-core/src/lib.rs:79-81`). The `agents` module sits deliberately above the generic
lifecycle: it is the L3 layer, a dated profile table (Claude Code, Codex), an `AgentVm` that
derives an image, launches with egress, and provisions Bedrock access, and `agents::bedrock`, which
mints the bearer token in process (`microvms-core/src/agents/mod.rs`, `docs/AGENT-VMS.md`). Its
free functions (`image_request_for`, `launch_request_for`, `install_access`, `prompt`) are what
the bindings drive, because their sandbox sits behind a lock one `AgentVm` cannot own.

- `microvms-domain/src/cost.rs`
- `microvms-core/src/control/image.rs`
- `microvms-core/src/session/exec.rs`
- `microvms-core/src/control/microvm.rs`
- `microvms-core/src/sandbox.rs`
- `microvms-core/src/agents/mod.rs`
- `microvms-core/src/control/ops.rs`
- `microvms-core/src/control/mod.rs`
- `microvms-core/src/session/mod.rs`

## microvms-cli

`microvms-cli` builds the `microvm` binary: its subcommands in lifecycle order over
`microvms-core`, and nothing the library does not do (`microvms-cli/src/cli.rs:83`,
`microvms-cli/src/main.rs:2`). Thinness is checked rather than intended — the direct
dependency set contains none of the denylisted transport and signing crates, no source file here names a transport or a
control-plane operation, and every AWS-touching command must fail when the library seam is made
to refuse (`microvms-cli/src/main.rs:10-13`, `microvms-cli/tests/thinness.rs:66`). A coding agent
is a first-class consumer, so `microvm manifest` emits the whole command tree with its option
domains, exit codes, and envelope schema generated from the parser, and every command writes
exactly one envelope object to stdout with progress on stderr
(`microvms-cli/src/main.rs:17-21`). There is no lib target, which is why the modules are declared
in `main.rs`, and `guards.rs` — the crate's largest file — holds the guards that have to
inject a refusing seam from inside the crate and so compiles only under `cfg(test)`
(`microvms-cli/src/main.rs:23-28`, `microvms-cli/src/guards.rs:12-20`).

- `microvms-cli/src/guards.rs`
- `microvms-cli/src/cli.rs`
- `microvms-cli/src/exit.rs`
- `microvms-cli/src/commands/attached.rs`
- `microvms-cli/src/commands/lifecycle.rs`
- `microvms-cli/src/render.rs`
- `microvms-cli/src/seam.rs`
- `microvms-cli/src/envelope.rs`

## microvms-py

`microvms-py` is the PyO3 binding over `microvms-core`: a total, thin mapping where every public
core constructor gets one binding constructor and no arithmetic or coercion surface the core does
not have (`microvms-py/src/lib.rs:6-11`). No validation lives here — no range check, no state
check, no region check, no size check — because a guard added in a binding is the copy every
Python caller hits and the copy nothing else tests (`microvms-py/src/lib.rs:12-18`). The
closures a binding could give away for free are each stopped by an absent surface rather than an
added check: no `__float__` on a dollar amount, no `__new__` on a duration, no region string on
any method, and the run-hook and build-hook timeouts as separate `#[pyclass]`es so transposing
them is a `TypeError` before any Rust runs (`microvms-py/src/lib.rs:20-40`). Methods are
synchronous over
the async core, blocking on one shared multi-thread tokio runtime with the GIL released first
(`microvms-py/src/lib.rs:42-46`), and module membership is declared inside the `#[pymodule] mod`
so the committed `microvms.pyi` is a function of this file and `mise run stubs:check` fails when
the two disagree (`microvms-py/src/lib.rs:98-101`). `agents.rs` is the L3 layer as Python sees
it: `AgentVm`, `AgentSpec`, and `BearerToken` over the same `Arc<Mutex<Sandbox>>` every session
shares, driving the core's free functions with the specs kept beside the lock
(`microvms-py/src/agents.rs`).

- `microvms-py/src/cost.rs`
- `microvms-py/src/sandbox.rs`
- `microvms-py/src/agents.rs`
- `microvms-py/src/exec.rs`
- `microvms-py/src/session.rs`
- `microvms-py/src/errors.rs`
- `microvms-py/src/lib.rs`
- `microvms-py/src/hooks.rs`
- `microvms-py/src/runtime.rs`

## microvms-js

`microvms-js` is the napi-rs binding over the same core under the same thin-mapping and
no-validation rules as the Python side, plus a module the Python side has no twin for —
`process`, the same exec seen as two byte streams for a consumer shaped like the AI SDK's
`SandboxProcess` (`microvms-js/src/lib.rs:6-17`, `microvms-js/src/lib.rs:72-74`). Its single most
important decision is `#[napi]` classes rather than `#[napi(object)]` for anything carrying a
closure: `#[napi(object)]` converts by structure, so `{ seconds: 3600 }` would satisfy a
`RunHookTimeout` and `{ amount: 1.5 }` an `EstimatedUsd`, which is precisely the coercion those
types exist to prevent (`microvms-js/src/lib.rs:19-35`). JS coerces more eagerly than Python, so
the money type carries no `valueOf`, no `toJSON`, and no `Symbol.toPrimitive` — the figure comes
out only through `.amount`, a string (`microvms-js/src/lib.rs:39-42`). Async maps straight through
with no `block_on` bridge, at the cost of a divergence from the Python twin — napi's async
rejection path is typed over its own closed `Status` enum, so a caller branches on
`err.cause.message` rather than `err.code` (`microvms-js/src/lib.rs:58-67`) — and the generated
`index.js`, `index.d.ts`, and `.node` addon are untracked, so they are absent from the list below
(`.gitignore:27-29`). `agents.rs` is the L3 layer as JS sees it, the twin of the Python file
over tokio's mutex; `BearerToken` is a `#[napi]` class rather than an object because it carries a
secret, so `JSON.stringify` gives `{}` and a look-alike object is rejected by napi's conversion
(`microvms-js/src/agents.rs`).

- `microvms-js/src/cost.rs`
- `microvms-js/src/session.rs`
- `microvms-js/src/exec.rs`
- `microvms-js/src/sandbox.rs`
- `microvms-js/src/agents.rs`
- `microvms-js/src/process.rs`
- `microvms-js/src/region.rs`
- `microvms-js/src/lib.rs`
- `microvms-js/src/errors.rs`

## model

`model` builds the `agentd-model` crate, an executable specification rather than daemon code: a
state machine whose reachable states stateright enumerates exhaustively, plus the safety
properties the real daemon must uphold (`model/Cargo.toml:2`, `model/src/lib.rs:3-9`). Its only
dependency is `stateright`, and it has no edge to any workspace member, because it models
the protocol instead of importing it (`model/Cargo.toml:9-10`). The question it settles is
whether an in-VM process can
hijack the unauthenticated `/run` bootstrap hook, and it prices the unenforced invariant instead
of asserting it: `Config::attacker_before_bootstrap` toggles the assumption that no in-VM workload
runs before bootstrap, so the model reports both that the attacker never obtains authority while
the assumption holds and the concrete path by which it does once the assumption breaks
(`model/src/lib.rs:20-34`). `client.rs` is the deliberate sibling covering what `microvms-core`'s
`Sandbox` may do from outside the VM, where `State::wire` counts the calls the client issued so a
property can say no resume ever fires once `was_terminated` holds (`model/src/client.rs:2-9`,
`model/src/client.rs:23-30`).

- `model/src/client.rs`
- `model/src/lib.rs`
- `model/Cargo.toml`

## Supporting code

Verification tooling, generated surfaces, and requirements data. None of it is a workspace crate
(`Cargo.toml:2-9`), and none of it is a module under the enumeration rule that skips
tooling-only paths.

- `conformance/run_rs.py`
- `microvms-py/microvms.pyi`
- `scripts/check-model-drift.py`
- `spec/core.symspec.json`
- `scripts/check-live-rates.py`
- `scripts/check-live-wiring.py`
- `scripts/generate-py-stubs.py`
- `scripts/check-lint-coverage.py`
- `conformance/infra/main.tf`
- `spec/microvms-core-kickoff.md`
- `scripts/verify-clean.py`
- `examples/coding-agents-on-bedrock/run.sh`
- `spec/agentd.symspec.json`
- `scripts/check-license-headers.py`
- `examples/coding-agents-on-bedrock/Dockerfile`

Related: [System overview](system-overview.md) ·
[Data flow](data-flow.md) ·
[Contract map](../insights/contract-map.md) ·
[Impact analysis](../insights/impact-analysis.md) ·
[Tech debt](../insights/tech-debt.md) ·
[CLI reference](../reference/cli.md)

## See also

- [system overview](system-overview.md)
- [business logic](../insights/business-logic.md)
- [contract map](../insights/contract-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [dependency graph](../diagrams/structural/dependency-graph.md)
