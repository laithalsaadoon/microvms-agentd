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
mise run guards:fire   # re-prove the seeded faults (bindings ones need --venv-per-worker or --venv); CI runs it
mise run live          # billable AWS verification
mise run live:verify-clean
```

`check` does not create AWS resources. Some tools need network access for
installation or advisory/rule updates. It does not run the documentation,
formal requirements, or live AWS tiers, and it doesn't build or test the Python
and Node bindings. When a change reaches behavior a binding exposes, build each
binding and run its suite the way CI's `python and node bindings` job does
(`pytest bindings/microvms-py/tests`, `node --test "bindings/microvms-js/__test__/*.mjs"`), or run
`mise run ci:bindings`, which is that job.

CI's jobs run mise tasks: each job of ci.yml and fuzz.yml installs mise and mise.lock's tools
through `.github/actions/mise`, then runs one `mise run ci:<job>`, whose task holds the job's
steps, so `mise run ci:<job>` runs a job here in the worktree. A check CI should run is a task
the job's `ci:` task calls, and `ci:parity` in `check` fails when a task `check` depends on
isn't reached from one. `mise run ci:local` runs each Linux job in a clone of a snapshot of the
worktree with the job's checkout depth, each job with its own target directory, which catches
what a worktree run can't, such as a command that needs origin/main in a shallow checkout.
`mise run ci:local -- <job>` runs one, and `-- --apply <patch>` runs over the snapshot with a
patch applied. It isn't in `check`, and each job's target takes tens of GB under `$TMPDIR`
(`$CI_LOCAL_DIR` moves it). It tests the branch as it stands, while CI tests a pull request's
merge with main, so rebase onto a fresh origin/main first. `tools/ci-local.py` says which jobs
stay CI-only and why.
See CONTRIBUTING.md for targeted tests and setup; do not infer live verification
from local test results.

## Code map

- `crates/protocol/`: shared types; package name `microvms-protocol`, import `protocol`.
- `crates/agentd/`: lifecycle hooks, authenticated execution, files, tunnels.
- `crates/microvms-domain/`: rules and values with no I/O: sizing, cost, regions,
  names, service constraints, error kinds.
- `crates/microvms-app/`: use cases over ports: the control-plane client, sandboxes,
  sessions, image builds, agent recipes, and the daemon release's verification
  policy.
- `crates/microvms-edges/`: the production port implementations: SigV4 transport,
  sockets, the name registry on disk, the release fetch and Sigstore check,
  clock and entropy.
- `crates/microvms-core/`: the composition root the CLI and bindings depend on; wires
  the edges into the app and re-exports every layer.
- `crates/microvms-cli/`: `microvm` commands and JSON envelopes.
- `bindings/microvms-py/`, `bindings/microvms-js/`: thin PyO3 and napi-rs bindings.
- `crates/model/`, `verify/spec/`, `conformance/`: portable model tests, formal requirements,
  and live AWS checks.
- `crates/model-conformance/`: unpublished, tests only; drives the app's policies and
  `Sandbox` over the models' rows and paths, and the daemon's tunnel route against the
  client's, the one place both halves meet.
- `verify/arch/placement.toml`, `verify/ratchet/`: each crate's allowed dependencies and the
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
  crate-root `forbid`, and its dependency set in `verify/arch/placement.toml` and each
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
(`verify/ratchet/rules/`) refuse an operation name written as a literal and a retyped
default in the CLI's and both bindings' code; they don't read file formats, or
attribute defaults such as clap's `default_value_t` and PyO3's `signature`,
which #300 checks through the generated surfaces.

If an adapter needs something private to a lower crate, make it public there or
move the caller down. Never copy it.

Drift is layering drift, parity gaps, the case corpus's markers and skips, and
untraced requirements. `mise run ratchet:check` collects it from the working
tree and from the merge base's tree, with the same collectors, and fails when
the tree has drift the base doesn't: a PR can't add drift, and a fix removes it
by fixing the code, with no list to edit. Nobody edits
`verify/ratchet/drift.json`: it's a generated snapshot for the docs site's
history chart, which `mise run ratchet:snapshot` rewrites in a change of its
own. A permanent exception is a decision in
`verify/ratchet/decisions.toml`, with its reason, and a decision whose finding is
gone fails the check. An untraced requirement takes no decision: list it in its
group's file under `verify/spec/traced/` (`verify/spec/traced/TRAP.toml` for a
TRAP key) and waive there any layer it can't carry, with its reason. The edges
between the workspace's crates, and each adapter's and layer's direct
dependencies against its set in `verify/arch/placement.toml`, are computed in one
place: `crates/microvms-cli/tests/dependency_direction.rs`. It holds each crate
to exactly its set, its `drift` table there (the CLI's #260 crates) and its
placement decisions, so a new dependency and a fixed one fail there; the
domain's, the app's and core's sets never carry drift. For placement the ratchet
reads no manifest: it reads each tree's drift tables, and refuses drift the base
doesn't have and a crate added to a set the base has.
The ratchet's port-impl collector reads the app and core as
well as the adapters, and the category is enforced, so a port implemented
anywhere but the edges fails `ratchet:check` unless a decision records why. Forbidden calls are refused by each adapter's
`clippy.toml`, and `tools/test_ratchet.py` lists every site that turns those
lints off. The CLI's `clippy.toml` also refuses core's transport calls and its
production constructors outside `crates/microvms-cli/src/seam.rs`, and the bindings refuse the
transport calls. `protocol::exec::StartRequest` is `#[non_exhaustive]`, so every
start request is built from `StartRequest::new`, which holds the wire's defaults.
The Python binding's `run` signatures still restate four of them as keyword
defaults, which #300 checks.

Core is the one implementation. The CLI, Python and TypeScript expose the same
capabilities, or `verify/parity/capabilities.toml` says why one doesn't.

- A capability lands in core first. The change that adds a public name to any
  surface adds its row to the table, with the other surfaces implemented, or
  exempted with a reason. `mise run parity:check` fails on a public function, a
  method of a class the table names, or a command, when no row names it, and on
  a row naming something a surface doesn't have. An exemption with an issue is
  a gap that issue closes, and the ratchet counts it as parity-gap drift, so
  closing one deletes its exemption and nothing else. One without an issue is a
  decision. The script's docstring has the rules; option-level parity
  (flags, keyword arguments) isn't checked yet.
- Defaults live at or below core. The CLI and the bindings use the constant
  core re-exports from the layer that owns it, and don't add a duration, size
  or retry literal of their own without a decision in `verify/ratchet/decisions.toml`.
  The ratchet's `literal-default` rule (`verify/ratchet/rules/literal-default.yml`)
  fails `ratchet:check` on one in adapter source that has no decision there. A
  flag or keyword default isn't held yet: #300 checks those through the
  generated surfaces.
- A bug in behavior the surfaces share is a case in `verify/parity/cases/`,
  which every surface's runner checks against one `expect`, not a new test and
  seeded fault on each surface. Where main marks the wrong surface with
  `known_drift`, the marker is the failing-first proof, since every runner fails
  a marked path that starts agreeing, and the fix deletes it. A case the corpus
  doesn't have yet lands with the fix, and the pull request shows it failing on
  the merge base (review holds that). The ratchet counts markers as parity-drift,
  so a pull request can't add one, and the runners' own seeded faults prove each
  runner can fail. CONTRIBUTING.md has the rest.

## Maintenance rules

- Use `microvm manifest` for the current command contract. Regenerate
  `docs/manifest.json`, `docs/schema.json`, and Python stubs when affected, and
  `verify/parity/core-api.json` (`mise run core-api`) when core's public surface
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
- AWS changes need a persistent conformance check in `conformance/`, or an
  explicit statement that they remain unverified against AWS. Their live
  exercise happens once per wave on main, in one live run at a time, since the
  Terraform state is single; until that run, the pull request says it's verified
  offline only. Guards follow "Checks that can fail" below.
- A release needs a green live run on its tag. `release.yml` drafts the GitHub
  release, and its gate, `live-gate`, opens only for a `live-conformance.yml`
  run dispatched on the tag that passed on that draft's assets. Every publishing
  job needs the gate, which `release:check` in `check` holds.
- Rebuild the release CLI before targeted live checks. Verify cleanup of VMs,
  images, and service-created log groups independently.
- `spec:core` references a local symspec checkout; formal requirements are
  separate from `check`. Portable state checks use `cargo test -p agentd-model`;
  `cargo test -p model-conformance` ties those models to the app.

Publishing and version changes are documented in CONTRIBUTING.md. Do not
change the stub generator's maturin pin without checking its output-path
behavior. Run Ruff on `.` so the repository selection is respected.

## Pull requests

CONTRIBUTING.md's "Pull requests" has the rules and the review budget.

- One issue per pull request, or one box of a tracker's checklist, with a
  change that depends on another open one stacked on it. Past the soft cap on
  changed lines of product code, the body has a `Size:` line saying why it's
  one change.
- A finding the change doesn't need to be correct goes in the body's Follow-ups
  section and onto the parent tracker's checklist. It gets an issue only when
  it's a security defect, a panic, data loss, or a design question.
- Review scales with risk and size, each reviewer reports at most five
  findings, and a reviewer who asks for another seeded fault names the mutant
  or input the existing guards miss.

`pr-body:check` in `check` holds the template to the sections every body needs,
and CI's `security` job runs it on each pull request's body: What and why,
Evidence, Guards and Follow-ups each there and filled in, and the `Size:` line
past the cap. Dependabot's pull requests are skipped. The rest is review's.

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

- Every new guard, gate or scanner ships with a seeded fault that makes it
  fail, in its owner's file in `verify/guards/faults/` (a new owner starts a file).
  A scanner's floor (an empty input) and its sentinel get a fault each. Review
  holds that a new check has its entries; CI holds that every entry fires.
  The `guards` job seeds the Rust, script and binding faults (each worker with
  its own Python environment, `--venv-per-worker`), as `mise run guards:fire`
  does locally, and it fails when a fault doesn't fire. `guards:list` in `check` fails on an entry
  that no longer applies to the tree, and on a new Falsification note that has
  no entry and no line in `verify/guards/unregistered.txt`.
- Tests assert the verdict, not only that something ran or stayed contained.
  CI's `mutants` job fails on a mutant of the changed Rust that no test
  catches, which is what a test that only checks "it returned" leaves behind,
  and its `mutmut` job fails when a function a change touches in `tools/*.py`
  has more surviving mutants than it had on the base. Neither is a required
  check yet, so read their results before a merge.
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
- A stale path in a file that runs things turns a gate off without failing, so
  `agents:check` also fails on a path or glob that matches nothing in the
  tree when `.config/lefthook.yml` names it, a mise task names it (its `run`, `dir`,
  `sources` or `outputs`), a workflow names it (its `paths` filters,
  `working-directory` or steps), `.github/dependabot.yml` names it, or a gate
  script binds it to a module-level constant. A path that names nothing here
  on purpose, such as a directory a job creates, goes in the script's
  `CENSUS_NOT_PATHS` with what it is. A decision id cited in a comment, such
  as `D14`, needs its table in `docs/decisions.toml`.
