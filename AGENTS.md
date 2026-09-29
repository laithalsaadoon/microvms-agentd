# Repository guide

Rust client stack and guest daemon for AWS Lambda MicroVMs. Start with
[README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md), and
[docs/README.md](docs/README.md). Wire behavior is specified in
[docs/PROTOCOL.md](docs/PROTOCOL.md); security constraints are in
[docs/TRUST.md](docs/TRUST.md).

## Commands

```bash
mise run check         # local code, security, tests, contracts, drift, packaging, build
mise run ci:local      # CI's Linux jobs in CI-shaped clones, before a push
mise run docs:check    # documentation build and checks
mise run guards:fire   # re-prove the seeded faults (bindings ones need --venv); CI runs it
mise run live          # billable AWS verification
mise run live:verify-clean
```

`check` does not create AWS resources. Some tools need network access for
installation or advisory/rule updates. It does not run the documentation,
formal requirements, or live AWS tiers, and it doesn't build or test the Python
and Node bindings. When a change reaches behavior a binding exposes, build each
binding and run its suite the way CI's `python and node bindings` job does
(`pytest microvms-py/tests`, `node --test "microvms-js/__test__/*.mjs"`), or run
`mise run ci:bindings`, which is that job.

`mise run ci:local` runs each Linux job of ci.yml and fuzz.yml the way CI does: the job's
own steps, in a clone of a snapshot of the worktree with the job's checkout depth and the
workflow's env, each job with its own target directory. It catches what only CI used to, such
as output parsed under `CARGO_TERM_COLOR=always` or a command that needs origin/main in a
shallow checkout. `mise run ci:<job>` runs one job, and `-- --apply <patch>` runs it over the
snapshot with a patch applied. It isn't in `check`, and each job's target takes tens of GB
under `$TMPDIR` (`$CI_LOCAL_DIR` moves it). It tests the branch as it stands, while CI tests
a pull request's merge with main, so rebase onto a fresh origin/main first. `ci/local.toml`
says what each job skips and why. A new step in ci.yml needs an entry there, or, for a setup
action, a reason in its `[actions]` table (`ci:parity` fails without one).
See CONTRIBUTING.md for targeted tests and setup; do not infer live verification
from local test results.

## Code map

- `protocol/`: shared types; package name `microvms-protocol`, import `protocol`.
- `agentd/`: lifecycle hooks, authenticated execution, files, tunnels.
- `microvms-domain/`: rules and values with no I/O: sizing, cost, regions,
  names, service constraints, error kinds.
- `microvms-app/`: use cases over ports: the control-plane client, sandboxes,
  sessions, image builds, agent recipes, and the daemon release's verification
  policy.
- `microvms-edges/`: the production port implementations: SigV4 transport,
  sockets, the name registry on disk, the release fetch and Sigstore check,
  clock and entropy.
- `microvms-core/`: the composition root the CLI and bindings depend on; wires
  the edges into the app and re-exports every layer.
- `microvms-cli/`: `microvm` commands and JSON envelopes.
- `microvms-py/`, `microvms-js/`: thin PyO3 and napi-rs bindings.
- `model/`, `spec/`, `conformance/`: portable model tests, formal requirements,
  and live AWS checks.
- `arch/placement.toml`, `ratchet/`: each crate's allowed dependencies and the
  drift count (see Architecture).

`microvms-domain`, `microvms-app`, `microvms-edges`, `microvms-cli`, both bindings,
`agentd` and `conformance/` each carry an `AGENTS.md` with the rules and commands for
working in that directory. Claude Code loads one when it reads a file there; Codex loads
only the files between the repository root and the directory it starts in, so read the
crate's file before editing it.

When `.codegraph/` exists, use `codegraph explore` before text searches to
locate or understand code. Confirm ambiguous cross-crate symbol matches from
the actual source. The daemon route census is `docs/schema.json` because
routes are generated from a schema.

## Architecture

Behavior lives once, in Rust, at or below `microvms-core`. The layers, lowest
first:

- `microvms-protocol`: the wire types the client shares with the daemon
  (ARCH-2). The daemon, `agentd`, depends on protocol and never on the client.
- `microvms-domain`: rules and values. It performs no network, filesystem,
  subprocess, environment, clock or entropy access (ARCH-6): its `clippy.toml`
  refuses those std calls and its dependencies' clock and entropy calls under a
  crate-root `forbid`, and its dependency set in `arch/placement.toml` and each
  dependency's features are asserted exactly. A rule that needs one of those
  inputs takes it as a parameter, the way `Region::from_env` takes a lookup.
- `microvms-app`: use cases (the control-plane client, `Sandbox`, `Session`,
  `ensure_image`, the agent recipes, the daemon release's verification policy),
  written only against ports it declares: `Transport`, `BuildServices`,
  `HttpBackend`, `TokenMinter`, `NameStore`, `Clock`, `Entropy`, `Adapters`,
  `ReleaseSource` and `AttestationVerifier`. It depends on no crate or tokio feature that
  does network, AWS, filesystem, subprocess or entropy I/O (ARCH-7), and its
  `clippy.toml` refuses the std and tokio I/O items under a crate-root `forbid`.
  The shared test doubles are its `testing` module, behind `test-support`.
- `microvms-edges`: the production port implementations: SigV4 over reqwest,
  the sockets, the name registry on disk, the daemon fetch, tokio's clock and
  the OS random pool. It's the one library crate the I/O crates belong in.
- `microvms-core`: the composition root (ARCH-8). It wires the edges into the
  app, keeps the 0.10 constructors in `microvms_core::prelude`, and re-exports
  every layer at the paths core always had (ARCH-1). The CLI and the bindings
  depend on it and on nothing below it but protocol.
- `microvms-cli`, `microvms-py`, `microvms-js`: parse input, convert types,
  bridge to the host runtime, render output. A default, retry, validation rule,
  wire call, file format or subprocess here belongs in a lower layer.

That's the rule, not a description of today's tree. The CLI still owns file
formats and file I/O, the run ledger and the sync manifest among them, and #260
moves directory sync into core. The ratchet's adapter-logic rules
(`ratchet/rules/`) refuse an operation name written as a literal and a retyped
default in the CLI's and both bindings' code; they don't read file formats, or
attribute defaults such as clap's `default_value_t` and PyO3's `signature`,
which #300 checks through the generated surfaces.

If an adapter needs something private to a lower crate, make it public there or
move the caller down. Never copy it.

`ratchet/drift.json` is the drift count: layering drift, parity gaps and
untraced requirements, each with the issue that removes it. `mise run
ratchet:check` fails on new drift and on a fix whose entry is still in the
file; `mise run ratchet:update` removes fixed entries. A PR can't add an entry:
fix the code, or record a permanent exception in `decisions` with its reason.
An untraced requirement takes no decision: list it in `TRACED` and waive there
any layer it can't carry, with its reason. The edges
between the workspace's crates are checked by
`microvms-cli/tests/dependency_direction.rs`. Each adapter's allowed
dependencies (`arch/placement.toml`) are checked by the ratchet, and
`dependency_direction.rs` asserts them exactly for each adapter the ratchet
holds no placement drift for (the CLI joins when #260 clears its entries). The
domain's, the app's and core's sets are there too, asserted exactly, and they
never carry drift. The ratchet's port-impl collector reads the app and core as
well as the adapters, so a port implemented anywhere but the edges is drift or
a recorded decision. Forbidden calls are refused by each adapter's
`clippy.toml`, and `scripts/test_ratchet.py` lists every site that turns those
lints off. The CLI's `clippy.toml` also refuses core's transport calls and its
production constructors outside `microvms-cli/src/seam.rs`, and the bindings refuse the
transport calls. `protocol::exec::StartRequest` is `#[non_exhaustive]`, so every
start request is built from `StartRequest::new`, which holds the wire's defaults.
The Python binding's `run` signatures still restate four of them as keyword
defaults, which #300 checks.

Core is the one implementation. The CLI, Python and TypeScript expose the same
capabilities, or `parity/capabilities.toml` says why one doesn't.

- A capability lands in core first. The change that adds a public name to any
  surface adds its row to the table, with the other surfaces implemented, or
  exempted with a reason. `mise run parity:check` fails on a public function, a
  method of a class the table names, or a command, when no row names it, and on
  a row naming something a surface doesn't have. An exemption with an issue is
  a gap that issue closes, and the ratchet counts it as parity-gap drift, so
  closing one deletes its exemption and its entry together. One without an issue
  is a decision. The script's docstring has the rules; option-level parity
  (flags, keyword arguments) isn't checked yet.
- Defaults live at or below core. The CLI and the bindings use the constant
  core re-exports from the layer that owns it, and don't add a duration, size
  or retry literal of their own without a decision in `ratchet/drift.json`.
  The ratchet's `literal-default` rule (`ratchet/rules/literal-default.yml`)
  fails `ratchet:check` on one in adapter source that has no decision there. A
  flag or keyword default isn't held yet: #300 checks those through the
  generated surfaces.

## Maintenance rules

- Use `microvm manifest` for the current command contract. Regenerate
  `docs/manifest.json`, `docs/schema.json`, and Python stubs when affected, and
  `parity/core-api.json` (`mise run core-api`) when core's public surface
  changes.
- Edit `site/authored/` and top-level `docs/*.md`; generated content under
  `site/src/content/docs/` is overwritten. Generated source analyses contain
  line-number citations that can become stale.
- Append dated corrections to platform measurements. Include region, API
  version, and evidence source; never infer runtime guarantees from SDK shapes.
- No internet egress requires a VPC without an IGW or NAT gateway. Omitting
  `--egress` and setting `--deny-egress` do not enforce network isolation.
- Keep secrets out of shared images. The guest can access its execution role
  through metadata, so use least privilege even with VPC isolation.
- Preserve the image bootstrap invariant: `agentd` is `CMD`, and workloads
  start only after readiness. Root workloads are not isolated from the daemon.
- AWS changes need a live exercise and a persistent conformance check, or an
  explicit statement that they remain unverified against AWS. Guards follow
  "Checks that can fail" below.
- Rebuild the release CLI before targeted live checks. Verify cleanup of VMs,
  images, and service-created log groups independently.
- `spec:core` references a local symspec checkout; formal requirements are
  separate from `check`. Portable state checks use `cargo test -p agentd-model`.

Publishing and version changes are documented in CONTRIBUTING.md. Do not
change the stub generator's maturin pin without checking its output-path
behavior. Run Ruff on `.` so the repository selection is respected.

## Code comments

Comments document intent, constraints, invariants, and non-obvious tradeoffs—not syntax.
Add a comment when behavior is surprising, externally constrained, concurrency-sensitive, security-sensitive, performance-motivated, or likely to be "simplified" incorrectly by a future maintainer. Prefer clearer names, types, and structure when they can make the comment unnecessary.

A useful threshold is: would a competent engineer reading this six months from now reasonably ask "why?" If yes, comment it. If they would only ask "what does this syntax do?", prefer clearer code instead.

## Counts in docs

Documentation doesn't state counts of lines, files, modules, tests, checks, or
classes in prose. Those numbers drift with every commit, and a stale count reads
as a fact. A number may appear only when the build generates it, or when it's a
dated measurement that names the commit it was taken at.

## Checks that can fail

A check that passes on broken code gives a false answer. Each rule here names the
check that holds it, or says that review does.

- Every new guard, gate or scanner ships with a seeded fault in
  `guards/faults.toml` that makes it fail. A scanner's floor (an empty input)
  and its sentinel get a fault each. Review holds that a new check has its
  entries; CI holds that every entry fires. The `guards` job seeds the Rust and
  script faults and the `bindings` job seeds the binding ones, as
  `mise run guards:fire` does locally, and each fails when a fault doesn't
  fire. `guards:list` in `check` fails on an entry that no longer applies to
  the tree, and on a new Falsification note that has no entry and no line in
  `guards/unregistered.txt`.
- Tests assert the verdict, not only that something ran or stayed contained.
  CI's `mutants` job fails on a mutant of the changed Rust that no test
  catches, which is what a test that only checks "it returned" leaves behind.
  It isn't a required check yet, so read its result before a merge.
- A requirement is covered by a test that names it, not by a mention.
  `trace:check` counts a key only in a test's name, its own doc comment or
  docstring, a pytest marker, or a Node test's title.
- Each threat in the table in `docs/TRUST.md` names the requirement key that
  states its defense and a test that guards it. `trace:check` fails on a row
  whose key no spec defines, whose guard isn't a running test that names one of
  the row's keys, or whose known gap names no issue.
- Live checks treat an absent value as a failure. `Results.eq` in
  `conformance/run_rs.py` fails on an absent value, and `Results.absent` is the
  one way to assert absence; `conformance:self-test` in `check` runs their
  negative twins.
- These docs name their checks, so `agents:check` in `check` fails when
  AGENTS.md, a crate's AGENTS.md, CONTRIBUTING.md or the pull request template
  names a task, fault id, path, file, CI job, `Results` method or code name
  that doesn't exist, or says a task is in `check` when `check` doesn't run it.
  Its docstring says what it reads as a name. A name outside backticks isn't
  read, so write the ones a rule depends on in backticks.
