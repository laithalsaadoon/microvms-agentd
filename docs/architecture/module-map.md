# microvms-agentd · Module map

The workspace declares its members (`Cargo.toml:2-14`), and the sections below run in the
dependency order of the `system-overview.md` flowchart, bottom-up: the wire contract first,
then `agentd` and the client's layers (`microvms-domain`, `microvms-app`, `microvms-edges`, and
`microvms-core` over them), which compile against it, then the CLI and the bindings over the
client, then the checked model (`docs/architecture/system-overview.md:83-112`). Each crate's
file list names its main `src/` files, and the `agentd` list is every module `crates/agentd/src/lib.rs`
declares; a crate's `tests/` tier is excluded so the source files a reader is looking for are
not crowded out by large test files such as `crates/agentd/tests/turmoil_transport.rs`. Files
belonging to no crate are collected under `Supporting code` at the end.

## protocol

`protocol` is the wire contract expressed as Rust types, split into `exec`, `fs`, `health`, and
`hook` (`crates/protocol/src/lib.rs:29-32`). The daemon and every Rust client of it compile against the
same definitions, so a renamed field breaks compilation on whichever side has not caught up
rather than surfacing as a consumer's runtime bug (`crates/protocol/src/lib.rs:10-12`). Membership is
decided by one rule — pure data that travels on the wire is admitted and machinery for making it
travel is not, which is why the SSE event payloads live here and the stream that emits them does
not (`crates/protocol/src/lib.rs:16-21`). Every type derives both halves of serde even where one side
needs only one, because the missing half is what a client would otherwise hand-write, and
`docs/schema.json` is generated from those same attributes under both contracts and byte-compared
in CI (`crates/protocol/src/lib.rs:23-27`).

- `crates/protocol/src/exec.rs`
- `crates/protocol/src/lib.rs`
- `crates/protocol/src/hook.rs`
- `crates/protocol/src/health.rs`
- `crates/protocol/src/fs.rs`

## agentd

`agentd` is the in-VM daemon supplying the exec and file-transfer APIs AWS Lambda MicroVMs does
not have (`crates/agentd/src/lib.rs:4-7`). Its modules divide by defect class rather than by HTTP
surface: `state` owns the one-shot bootstrap, `auth` decides authorization before a body byte is
read, `exec` owns idempotent start with ack-gated release, and `fs` owns streaming tar
(`crates/agentd/src/lib.rs:37-52`). The trust boundary is the crate's organizing fact — the platform's
own `/run` hook arrives from `127.0.0.1`, indistinguishable at the socket level from a request
sent by a process inside the VM, so source-address filtering would reject a legitimate bootstrap
and the one-shot property is the only defense left (`crates/agentd/src/lib.rs:11-16`). `routes.rs`
assembles the router by walking `surface_docs`, the same endpoint list `/v1/schema`
publishes, so a documented route with no handler panics at startup, and each endpoint's declared
auth mode decides which of the two routers it joins (`crates/agentd/src/routes.rs:31-35`,
`crates/agentd/src/routes.rs:48-58`, `crates/agentd/src/routes.rs:371`, `docs/schema.json:497-1154`).

- `crates/agentd/src/auth.rs`
- `crates/agentd/src/config.rs`
- `crates/agentd/src/disk.rs`
- `crates/agentd/src/exec.rs`
- `crates/agentd/src/exec_start.rs`
- `crates/agentd/src/fs.rs`
- `crates/agentd/src/hook_handlers.rs`
- `crates/agentd/src/identity.rs`
- `crates/agentd/src/routes.rs`
- `crates/agentd/src/schema.rs`
- `crates/agentd/src/serve.rs`
- `crates/agentd/src/state.rs`
- `crates/agentd/src/tunnel.rs`
- `crates/agentd/src/tunnel_identity.rs`
- `crates/agentd/src/exec_start_fuzz.rs` (private, compiled only under `#[cfg(test)]`)

## microvms-domain

`microvms-domain` holds the rules and values every surface has to agree on: the size classes,
the cost engine and its rate table, region parsing, the service constraints, name validation,
the error kinds and the tunnel identity's derivation (`crates/microvms-domain/src/lib.rs:4-6`). It
reads no ambient state, so each input a rule needs is a parameter
(`crates/microvms-domain/src/lib.rs:6-11`). `cost.rs` carries the rule that makes the rest of the client
legible: unknown is not zero, so `Amount::Unpriced` is a distinct variant a consumer has to
match on rather than a $0.00 line (`crates/microvms-domain/src/cost.rs:22-27`).

- `crates/microvms-domain/src/cost.rs`
- `crates/microvms-domain/src/sizing.rs`
- `crates/microvms-domain/src/region.rs`
- `crates/microvms-domain/src/error.rs`
- `crates/microvms-domain/src/identity.rs`

## microvms-app

`microvms-app` holds the use cases: the control-plane client, `Sandbox`, `Session`,
`ensure_image`, the agent recipes and the daemon release's verification policy
(`crates/microvms-app/src/lib.rs:4-6`). Everything they do outside the process goes through a trait this
crate declares, from the control-plane `Transport` to the `Clock`, the `Entropy` source and the
`ReleaseSource` (`crates/microvms-app/src/lib.rs:5-12`). It can't reach the network, AWS, the
filesystem, a subprocess, the wall clock or the OS random pool, and its dependency set, its
`clippy.toml` and its crate root hold that (`crates/microvms-app/src/lib.rs:14-27`). The `agents`
module sits deliberately above the generic lifecycle: it is the L3 layer, a dated profile table
(Claude Code, Codex), and an `AgentVm` that derives an image, launches with egress, and
provisions Bedrock access (`crates/microvms-app/src/agents/mod.rs`, `docs/AGENT-VMS.md`). Its free
functions (`image_request_for`, `launch_request_for`, `install_access`, `prompt`) are what the
bindings drive, because their sandbox sits behind a lock one `AgentVm` cannot own.

- `crates/microvms-app/src/control/image.rs`
- `crates/microvms-app/src/session/exec.rs`
- `crates/microvms-app/src/control/microvm.rs`
- `crates/microvms-app/src/sandbox.rs`
- `crates/microvms-app/src/agents/mod.rs`
- `crates/microvms-app/src/control/ops.rs`
- `crates/microvms-app/src/control/mod.rs`
- `crates/microvms-app/src/session/mod.rs`
- `crates/microvms-app/src/provision.rs`

## microvms-edges

`microvms-edges` implements those ports over the real thing: the signed transport and build
services over the default credential chain, SigV4 and reqwest, the daemon's HTTP backend, the
port forwarder, the tunnel and the shell, the name registry on disk, the Bedrock token mint, the
verified `agentd` fetch, and the tokio clock and OS entropy (`crates/microvms-edges/src/lib.rs:4-16`).
It's the one library crate that may depend on a crate doing I/O
(`crates/microvms-edges/src/lib.rs:18-21`).

- `crates/microvms-edges/src/control/transport.rs`
- `crates/microvms-edges/src/control/services.rs`
- `crates/microvms-edges/src/session/http.rs`
- `crates/microvms-edges/src/session/tunnel.rs`
- `crates/microvms-edges/src/session/forward.rs`
- `crates/microvms-edges/src/provision.rs`
- `crates/microvms-edges/src/provision/release.rs`

## microvms-core

`microvms-core` is the library crate the CLI and the bindings depend on, and it's the
composition root: it holds no logic of its own beyond wiring and re-exports
(`crates/microvms-core/src/lib.rs:62-73`). Each measured platform finding is spent once in the client
so no caller has to measure it again, and every closure is ranked on a strength ladder where S1
means the mistake cannot be written down at all (`crates/microvms-core/src/lib.rs:8-15`,
`crates/microvms-core/src/lib.rs:22-41`). Its `prelude` puts the production transport, clock, entropy
and adapters into each type's port-taking constructor, so a 0.10 call still compiles
(`crates/microvms-core/src/lib.rs:76-81`), and it re-exports `protocol` so consumers name wire types
through here instead of depending on the contract crate (`crates/microvms-core/src/lib.rs:98-100`).

- `crates/microvms-core/src/lib.rs`
- `crates/microvms-core/src/prelude.rs`
- `crates/microvms-core/src/identity.rs`

## microvms-cli

`microvms-cli` builds the `microvm` binary: its subcommands in lifecycle order over
`microvms-core`, and nothing the library does not do (`crates/microvms-cli/src/cli.rs:83`,
`crates/microvms-cli/src/main.rs:2`). Thinness is checked rather than intended — the direct
dependency set contains none of the denylisted transport and signing crates, no source file here names a transport or a
control-plane operation, and every AWS-touching command must fail when the library seam is made
to refuse (`crates/microvms-cli/src/main.rs:10-13`, `crates/microvms-cli/tests/thinness.rs:71`). A coding agent
is a first-class consumer, so `microvm manifest` emits the whole command tree with its option
domains, exit codes, and envelope schema generated from the parser, and every command writes
exactly one envelope object to stdout with progress on stderr
(`crates/microvms-cli/src/main.rs:17-21`). There is no lib target, which is why the modules are declared
in `main.rs`, and `verify/guards/`, one file per command area, holds the guards that have to
inject a refusing seam from inside the crate and so compiles only under `cfg(test)`
(`crates/microvms-cli/src/main.rs:23-28`, `crates/microvms-cli/src/guards/mod.rs:12-25`).

- `crates/microvms-cli/src/guards/`
- `crates/microvms-cli/src/cli.rs`
- `crates/microvms-cli/src/exit.rs`
- `crates/microvms-cli/src/commands/attached.rs`
- `crates/microvms-cli/src/commands/lifecycle.rs`
- `crates/microvms-cli/src/render.rs`
- `crates/microvms-cli/src/seam.rs`
- `crates/microvms-cli/src/envelope.rs`

## microvms-py

`microvms-py` is the PyO3 binding over `microvms-core`: a total, thin mapping where every public
core constructor gets one binding constructor and no arithmetic or coercion surface the core does
not have (`bindings/microvms-py/src/lib.rs:6-11`). No validation lives here — no range check, no state
check, no region check, no size check — because a guard added in a binding is the copy every
Python caller hits and the copy nothing else tests (`bindings/microvms-py/src/lib.rs:12-18`). The
closures a binding could give away for free are each stopped by an absent surface rather than an
added check: no `__float__` on a dollar amount, no `__new__` on a duration, no region string on
any method, and the run-hook and build-hook timeouts as separate `#[pyclass]`es so transposing
them is a `TypeError` before any Rust runs (`bindings/microvms-py/src/lib.rs:20-40`). Methods are
synchronous over
the async core, blocking on one shared multi-thread tokio runtime with the GIL released first
(`bindings/microvms-py/src/lib.rs:42-46`), and module membership is declared inside the `#[pymodule] mod`
so the committed `microvms.pyi` is a function of this file and `mise run stubs:check` fails when
the two disagree (`bindings/microvms-py/src/lib.rs:98-101`). `agents.rs` is the L3 layer as Python sees
it: `AgentVm`, `AgentSpec`, and `BearerToken` over the same `Arc<Mutex<Sandbox>>` every session
shares, driving the core's free functions with the specs kept beside the lock
(`bindings/microvms-py/src/agents.rs`).

- `bindings/microvms-py/src/cost.rs`
- `bindings/microvms-py/src/sandbox.rs`
- `bindings/microvms-py/src/agents.rs`
- `bindings/microvms-py/src/exec.rs`
- `bindings/microvms-py/src/session.rs`
- `bindings/microvms-py/src/errors.rs`
- `bindings/microvms-py/src/lib.rs`
- `bindings/microvms-py/src/hooks.rs`
- `bindings/microvms-py/src/runtime.rs`

## microvms-js

`microvms-js` is the napi-rs binding over the same core under the same thin-mapping and
no-validation rules as the Python side, plus a module the Python side has no twin for —
`process`, the same exec seen as two byte streams for a consumer shaped like the AI SDK's
`SandboxProcess` (`bindings/microvms-js/src/lib.rs:6-17`, `bindings/microvms-js/src/lib.rs:72-74`). Its single most
important decision is `#[napi]` classes rather than `#[napi(object)]` for anything carrying a
closure: `#[napi(object)]` converts by structure, so `{ seconds: 3600 }` would satisfy a
`RunHookTimeout` and `{ amount: 1.5 }` an `EstimatedUsd`, which is precisely the coercion those
types exist to prevent (`bindings/microvms-js/src/lib.rs:19-35`). JS coerces more eagerly than Python, so
the money type carries no `valueOf`, no `toJSON`, and no `Symbol.toPrimitive` — the figure comes
out only through `.amount`, a string (`bindings/microvms-js/src/lib.rs:39-42`). Async maps straight through
with no `block_on` bridge, at the cost of a divergence from the Python twin — napi's async
rejection path is typed over its own closed `Status` enum, so a caller branches on
`err.cause.message` rather than `err.code` (`bindings/microvms-js/src/lib.rs:58-67`) — and the generated
`index.js`, `index.d.ts`, and `.node` addon are untracked, so they are absent from the list below
(`.gitignore:27-29`). `agents.rs` is the L3 layer as JS sees it, the twin of the Python file
over tokio's mutex; `BearerToken` is a `#[napi]` class rather than an object because it carries a
secret, so `JSON.stringify` gives `{}` and a look-alike object is rejected by napi's conversion
(`bindings/microvms-js/src/agents.rs`).

- `bindings/microvms-js/src/cost.rs`
- `bindings/microvms-js/src/session.rs`
- `bindings/microvms-js/src/exec.rs`
- `bindings/microvms-js/src/sandbox.rs`
- `bindings/microvms-js/src/agents.rs`
- `bindings/microvms-js/src/process.rs`
- `bindings/microvms-js/src/region.rs`
- `bindings/microvms-js/src/lib.rs`
- `bindings/microvms-js/src/errors.rs`

## model

`model` builds the `agentd-model` crate, an executable specification rather than daemon code: a
state machine whose reachable states stateright enumerates exhaustively, plus the safety
properties the real daemon must uphold (`crates/model/Cargo.toml:2`, `crates/model/src/lib.rs:3-9`). Its only
dependency is `stateright`, and it has no edge to any workspace member, because it models
the protocol instead of importing it (`crates/model/Cargo.toml:9-10`). The question it settles is
whether an in-VM process can
hijack the unauthenticated `/run` bootstrap hook, and it prices the unenforced invariant instead
of asserting it: `Config::attacker_before_bootstrap` toggles the assumption that no in-VM workload
runs before bootstrap, so the model reports both that the attacker never obtains authority while
the assumption holds and the concrete path by which it does once the assumption breaks
(`crates/model/src/lib.rs:20-34`). `client.rs` is the deliberate sibling covering what `microvms-core`'s
`Sandbox` may do from outside the VM, where `State::wire` counts the calls the client issued so a
property can say no resume ever fires once `was_terminated` holds (`crates/model/src/client.rs:2-9`,
`crates/model/src/client.rs:23-30`).

- `crates/model/src/client.rs`
- `crates/model/src/lib.rs`
- `crates/model/Cargo.toml`

## Supporting code

Verification tooling, generated surfaces, and requirements data. None of it is a workspace crate
(`Cargo.toml:2-9`), and none of it is a module under the enumeration rule that skips
tooling-only paths.

- `conformance/run_rs.py`
- `bindings/microvms-py/microvms.pyi`
- `tools/check-model-drift.py`
- `verify/spec/core.symspec.json`
- `tools/check-live-rates.py`
- `tools/check-live-wiring.py`
- `tools/generate-py-stubs.py`
- `tools/check-lint-coverage.py`
- `conformance/infra/main.tf`
- `verify/spec/microvms-core-kickoff.md`
- `tools/verify-clean.py`
- `examples/coding-agents-on-bedrock/run.sh`
- `verify/spec/agentd.symspec.json`
- `tools/check-license-headers.py`
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
