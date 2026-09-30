# Contributing

The shared implementation lives in `microvms-core` and the crates it composes
and re-exports: `microvms-domain`, `microvms-app` and `microvms-edges`. The CLI
and bindings adapt that implementation. Read [Protocol](docs/PROTOCOL.md) before changing wire
behavior and [Trust](docs/TRUST.md) before changing authentication or execution.

## Setup and checks

```bash
mise install
mise run install       # install git hooks
mise run check         # code, security, tests, schema, stubs and declarations, surface parity, API drift, packaging, build, traceability, the drift ratchet, seeded-fault registry, doc references and config paths, changelog fragments
mise run ci:local      # CI's Linux jobs, each in a clone shaped like its checkout; before a push
mise tasks             # all available tasks
```

`ci:local` isn't part of `check`: it builds the tree once per job, keeping a target
per job (tens of GB under `$TMPDIR`, or `$CI_LOCAL_DIR`), and fuzzes for fuzz.yml's
time bounds. `mise run ci:<job>` runs one job (`ci:rust`, `ci:security`, `ci:drift`,
`ci:bindings`, `ci:guards`, `ci:build`, `ci:semver`, `ci:fuzz`). It tests your branch, and CI tests
its merge with main, so rebase onto a fresh origin/main before you rely on it. A new
step in ci.yml needs an entry in `ci/local.toml`, or, for a setup action, a reason in
its `[actions]` table; the file also records each step `ci:local` skips and why, and
`ci:parity` in `check` fails without it.

`check` does not create AWS resources. Initial dependency downloads, security
rule loading, and advisory updates can require network access. Documentation,
binding integration tests, and formal requirements have additional setup;
consult their tasks in `.config/mise/tasks/` and the CI workflows.

Useful checks while iterating:

```bash
cargo test -p agentd --lib
cargo test -p agentd-model
cargo test -p model-conformance
cargo test -p microvms-domain
cargo test -p microvms-app
cargo test -p microvms-edges
cargo test -p microvms-core
cargo test -p microvms-cli
cargo test --test proptest_tar
cargo test --test turmoil_transport
cargo test --all
./conformance/run_rs.py --self-test
uvx ruff check .
uvx ruff format --check .
```

Use `-p microvms-protocol` for the protocol package; its Rust import is
`protocol`. The shipping daemon target is `aarch64-unknown-linux-musl`.
`.cargo/config.toml` selects `rust-lld`; no external cross compiler is needed.

Python builds with maturin and tests under `bindings/microvms-py/tests/`. Node builds
with `npm run build` and tests with `npm test` in `bindings/microvms-js/`.

For a new invariant guard, register the fault that proves it catches its
failure in its owner's file in `verify/guards/faults/` (a gate's, a crate's, or one
issue's guards; a new owner starts a file), and show
`mise run guards:fire -- --only <id>` printing `fired` for it. The registry is
split by owner so that pull requests adding guards for different owners don't
edit one file; an id is still unique across every file. `check` runs
`guards:list`, which fails when an entry's anchor or patch no longer matches the
tree, when a test gains a `**Falsification**` note with no entry
(`verify/guards/unregistered.txt` lists the older ones and only shrinks), and when the
single file the registry used to be, verify/guards/faults.toml, comes back: nothing
reads it. CI's `guards` job seeds the Rust, script and binding
faults a pull request affects and, on every push to main, all of them; each of its
workers builds the bindings into its own Python environment (`--venv-per-worker`). A guard no fault can be seeded for, such
as a live check, is still broken by hand, restored, and recorded in the PR.

Locally, `mise run guards:fire -- --jobs 4` seeds faults in four scratch worktrees at
once and reports what a serial run reports, in the same order; add `--venv-per-worker`
to fire the binding entries too. `-- --affected` fires
only the entries whose own files changed against origin/main (`--base <ref>` for
another base) and names the ones it skips. It's what CI's `guards` job runs on a pull
request, and it isn't the full fire: a change that reaches a guard through code the
entry doesn't name, such as the type a clippy ban names, isn't seen. (A change to the
script, a build input such as Cargo.lock, a Cargo.toml or mise.toml, or ci.yml's `guards`
job or top-level `env` selects every entry.) Main's push runs the full fire after the
merge, so a fault only it catches turns main red. Both run as a matrix of shards, each
firing its share of the selection within 60 minutes, and the required
`seeded faults fire` check is their combined result;
`mise run guards:fire -- --affected --shard=1/6` runs one pull request leg's share here.
When your change reaches guards that way, run
`mise run guards:fire -- --jobs 4 --venv-per-worker` before you push. A red `seeded faults fire` on main is
fixed before the next merge, because the next pull request that selects every entry
fails on it as well. If your pull request fails an entry it didn't touch, look at main's
last push run first.

CI's `mutants` job runs cargo-mutants over the Rust a pull request changes, and
`mise run mutants` runs it over your branch against origin/main. It isn't in
`check`, because each mutant is a build. A mutant is one small change to the
code, such as a return value replaced or a `>` turned into `>=`. Only the
mutated package's own tests run against it, so a test in `crates/microvms-core/tests`
doesn't catch a mutant in the app. A change with no Rust in it passes at once.

Reading `missed.txt`: each shard uploads its `mutants.out` as an artifact, and
the job's log prints the same list. Each line names a mutant by file, line,
column and change, such as
`crates/agentd/src/fs.rs:632:5: replace fuzz_extract -> Result<u64, String> with Ok(0)`,
and `mutants.out/diff/` and `mutants.out/log/` hold its diff and its test
output. A missed mutant means the tests ran that code and nothing checked what
it did. Write the assertion that fails with the mutant in, and rerun. When no
offline test can reach the code (a network call only a live test makes, or code
behind a feature `cargo test` doesn't enable), add an `exclude_re` entry to
`.cargo/mutants.toml` with the measured miss and the check that covers the code
instead. An entry drops only a function's `replace <function> -> ` mutants, so
move any logic worth testing out of that function first. A mutant in
`timeout.txt` made the tests hang, and it's handled like a missed one.

Two kinds of code take `#[cfg_attr(test, mutants::skip)]` rather than an
exclusion: a new test double behind `cfg(any(test, feature = "test-support"))`
(on its module, or a glob in `.cargo/mutants.toml` for a file of its own), and
code only a non-Unix build compiles, such as a `cfg(not(unix))` twin, which the
Linux job always reports missed. A name regex would also drop the tested Unix
twin's mutants, since the two share a name. Each skip is listed in `SKIPS` in
`tools/test_check_mutants.py`, and a crate with one needs the `mutants` crate
as a dev-dependency. A new workspace crate goes in the wrapper's `PACKAGES` or,
with a reason, `LEFT_OUT`; the job fails until it's in one of them.

In network simulation tests, coordinate child processes through stdin rather
than wall-clock sleeps: child processes and the simulator use different clocks.

`mise run ratchet:check` collects the drift, such as a subprocess in a shipping crate
or a spec requirement no file in `verify/spec/traced/` lists, from your working tree and
from the tree of its merge base with origin/main (CI uses the pull request's base
branch), with the same collectors, and fails on drift the base doesn't have. A fix
removes drift by fixing the code and needs no other edit. Don't edit
`verify/ratchet/drift.json`: it's a generated snapshot the docs site charts, and
`mise run ratchet:snapshot` rewrites it in a change of its own. New drift moves to the
layer whose job it is, or gets a decision with its reason in
`verify/ratchet/decisions.toml`; a decision whose finding is gone fails too. Placement,
a direct dependency outside its crate's set in `verify/arch/placement.toml`, is computed
by `crates/microvms-cli/tests/dependency_direction.rs` instead, which holds each crate to
its set, its `drift` table there and its placement decisions: a new dependency fails
there, and so does a fixed one still in the drift table. The ratchet refuses drift the
base doesn't have there too, and a crate added to a set the base already has. An
untraced requirement can't be a decision: it gets an entry in its group's file
(`verify/spec/traced/TRAP.toml` for a TRAP key), with a waiver for any layer it can't
carry. Each group has its own file, so the changes that trace different groups don't
edit one table; `tools/check-trace.py` loads them all and refuses a key in the wrong
group's file or listed twice. Moving drift to another file or crate is a move, not new
drift, when its key keeps its path or its text; a move and a rename at once read as new
drift, so they land in separate PRs.

`mise run parity:check` holds `verify/parity/capabilities.toml` to the four surfaces
(core through `verify/parity/core-api.json`, the CLI through `docs/manifest.json`, and
the two bindings through their stub and declarations). A public function, a
method of a class the table names, or a command that no row names fails, and so
does a row naming something a surface doesn't have. A new class and its methods
aren't held yet; that's the option-level follow-up. Name
the capability on each surface, or exempt the surface with a reason, and an
`issue` if a later change closes the gap. The ratchet counts an exemption with
an issue as parity-gap drift, and a PR can't add drift, so a new function or
method lands on every surface or is exempted without an issue, as a decision.
`mise run core-api` regenerates the core snapshot, and `core-api:check` fails
when it's stale.

Each driving adapter's `clippy.toml` bans a subprocess (`std::process::Command`,
`tokio::process::Command`) and a direct environment read (`std::env::var`,
`var_os`, `vars`, `vars_os`, and calling `microvms_core::env::process` by
name), and its crate root denies both lints. The adapter hands
`microvms_core::env::process` to core's resolvers once, where it composes them,
and everything else takes the lookup it's given. An exception is an
`#[expect(..., reason = "...")]` at the call site plus its line in
`LINT_EXCEPTIONS` in `tools/test_ratchet.py`, which fails `ratchet:check` on
any other `allow`, `warn` or `expect` of those lints in an adapter's source. A
subprocess exception is also drift the ratchet counts, or a decision in
`verify/ratchet/decisions.toml`.
An environment read has no drift category, so its `reason` and its line in
that list are the whole record, and review is the check.

`microvms-domain` holds the rules and does no I/O (ARCH-6). Its `clippy.toml`
bans the std file, process, network, environment and clock calls and the clock
and entropy calls of the crates it uses, and its crate root forbids both lints,
so an inner `#[allow]` or `#[expect]` doesn't compile. It has no exception list:
a rule that needs the clock, a file or a variable takes it as a parameter, and
core supplies it. `dependency_direction.rs` pins each dependency's declared
features as well as its name. The I/O
methods its types had in 0.10 (`CalendarDate::today_utc`, `NameRecord::new`,
`LaunchIdentity::generate`, `TunnelIdentity::initiator`) are extension traits in
`microvms_core::prelude`, so in-repo callers import `microvms_core::prelude::*`.
`crates/microvms-core/tests/public_paths.rs` names every public path core had at
v0.10.0; regenerate it with `tools/generate-public-paths.py` only when a
release changes the API on purpose.

`microvms-app` holds the use cases and reaches the outside only through the
ports it declares (ARCH-7). Its `clippy.toml` bans the std file, process,
network, environment and clock calls and tokio's `net`, `fs` and `process`
items, under a crate-root `forbid`, and `dependency_direction.rs` pins its
dependency set and each dependency's features, tokio's among them. A use case
that needs I/O gets a port in the app and an implementation in `microvms-edges`,
the one library crate allowed the I/O crates. `microvms-core` is the
composition root (ARCH-8): the prelude's constructors wire the edges into the
app's port-taking ones (`ControlPlane::from_ports`, `SessionBuilder::try_build`),
and every item below it is re-exported at its 0.10 path. The shared test
doubles are `microvms_core::testing`, behind the `test-support` feature; a
crate's `[dev-dependencies]` turns it on. The ratchet's port-impl collector reads
the app and core as well as the adapters, so a port implementation there needs a
decision in `verify/ratchet/decisions.toml`.

## Generated contracts and API changes

Regenerate affected contracts and include their diffs:

```bash
mise run schema        # docs/schema.json
mise run manifest      # docs/manifest.json
mise run stubs         # Python declarations
mise run core-api      # verify/parity/core-api.json, core's public paths
mise run model:check   # implemented constraints versus the installed boto3 model
```

The published crates' Rust API is compared with their last release on crates.io:
`mise run semver:check` runs cargo-semver-checks, and CI's `semver` job runs the same
commands. `microvms-protocol` is gated: a change its last release's users couldn't compile
against fails, unless the version bumps with it or its `Cargo.toml` allows that lint with the
reason (the one it allows now is `StartRequest`'s `#[non_exhaustive]`, which 0.11.0 ships).
`microvms-core`'s comparison is printed and never fails until 0.11.0 is its baseline, since
against 0.10.0 every item the layer split moved and re-exported reads as removed. The domain,
the app and the edges join once their first release is on crates.io. It isn't in `check`: it
fetches each baseline and builds two rustdocs per crate.

A wire change also has to work with the previous release in both directions.
`schema:compat` in `check` compares `docs/schema.json` with the copy at the
highest `v*` tag reachable from HEAD, route by route, and fails on a removed
route, status or field, or a field an older client or daemon doesn't send
becoming required; [Protocol](docs/PROTOCOL.md) has the rule. An intended break
bumps `PROTOCOL_VERSION` and lists each break in `docs/schema-breaks.toml`. It
refuses a clone with no release tag, so run `git fetch --unshallow --tags` in a
shallow one.

Use current boto3 models and AWS documentation to identify capabilities the
package should expose, verify request serialization, and check CLI/SDK parity
(`mise run parity:check` covers names, not options).
The [MicroVM API](https://docs.aws.amazon.com/lambda/latest/microvm-api/Welcome.html)
manages images and VMs; [Lambda core](https://docs.aws.amazon.com/lambda/latest/lambda-core/Welcome.html)
manages VPC connectors. Document the package's supported workflows and limits.

To check implemented constraints against the latest SDK without creating AWS
resources, run `uv run --upgrade --script tools/check-model-drift.py`.

The `spec` and `spec:core` tasks are separate from `check`. They require
compatible symspec tooling; `spec:core` currently names a local checkout and
is not portable. A passing `check` does not verify those documents.
`cargo test -p agentd-model` runs the portable state-machine checks, and
`cargo test -p model-conformance` checks the app against them.

## Documentation

Edit `site/authored/` for tutorials and landing pages, and top-level `docs/*.md`
for contracts and measured findings. `site/src/content/docs/` is generated and
ignored. The CLI reference comes from `docs/manifest.json`; historical source
analyses under `docs/architecture/`, `docs/reference/`, `docs/behavior/`,
`docs/analysis/`, `docs/diagrams/`, and `docs/insights/` may need regeneration
after a refactor.

```bash
mise run docs:check     # lint, spelling, build, typecheck, output checks
mise run docs:browsers  # browser setup
mise run docs:gate      # also accessibility and browser performance checks
```

For platform observations, record the date, region, API version, and whether
the evidence comes from AWS documentation, a model, or a live request. Keep
previous measurements when behavior changes and append a correction. Model
fields do not establish runtime enforcement: an omitted internet connector,
for example, does not prove no egress. Internet isolation requires a VPC
without an IGW or NAT gateway and with no alternative internet route.

## Live verification

Changes to AWS behavior need a named regression check in the live suite
(`conformance/lanes/`, driven by `conformance/run_rs.py`). The live exercise of
the changed path happens once per wave on main, one live run at a time because
the Terraform state is single, rather than on each pull request; until that run,
the pull request states that the change is verified offline only. Documentation
and other local-only changes do not need a billable run.

```bash
mise run live                # builds binaries, provisions infrastructure, tests AWS
mise run live:verify-clean   # independently check for remaining resources
mise run live:destroy        # remove the Terraform stack when finished
```

For a targeted run, rebuild `target/release/microvm` first: `check` does not
build that release CLI. `--keep` retains resources and their charges. Inspect
MicroVMs, images, and service-created `/aws/lambda-microvms/` log groups after
cleanup; Terraform does not own every resource the service creates.

## Changelog

A change a user of the crates, the CLI or the bindings can observe gets a changelog entry. The
next release's entries are fragments in `changelog.d/`, a file each, named for the issue and a
Keep a Changelog type: `changelog.d/<issue>.<type>.md`, where the type is `added`, `changed`,
`deprecated`, `removed`, `fixed` or `security`, and the issue is the pull request's number when
there's no issue. Another fragment of the same issue and type is `<issue>.<type>.1.md`, which is
what `towncrier create` names it. Write each entry as it will read in CHANGELOG.md: a Markdown
list item with a bold lead that names the issue, its later lines indented two spaces.

```markdown
- **`run` and `build` no longer upload over a caller's `--artifact-uri` (#249).** With a bucket
  also set, both commands uploaded their own artifact to the caller's URI.
```

A fragment can hold several entries of its type, in the order they should read, and a release
lists each type's fragments in issue order. `mise run changelog:draft` prints the next release's
section as it will read. Two pull requests can't conflict over their entries, because each adds
its own file; that's why CHANGELOG.md isn't edited for a new entry, and the check doesn't count
an edit there as one.

`changelog:check` in `check`, and CI's `security` job on a pull request, fail a branch that
changes shipped code and adds no fragment. Shipped code is the source directory of each
published crate, the daemon and both bindings, plus `microvms.pyi` and `index.d.ts`; the fuzz
harnesses and the CLI's guards in those directories compile only into tests and don't count.
`tools/changelog.py` holds the set and says why each part is in it. A change there that no
user can observe, such as a unit test beside the code or a lint attribute, takes
`changelog.d/<issue>.internal.md` instead: a line saying why, which no release renders and the
release build removes with the rest. A change to scripts, CI, guards, docs or manifests needs no
fragment, and a Dependabot pull request needs none either: the check skips the `dependabot[bot]`
author and no other. It also fails a file in `changelog.d/` that isn't a fragment, a type
`towncrier.toml` doesn't define, an entry that isn't a bold-lead list item, and fragments that
don't build.

## Releases and reviews

The release workflow publishes `microvms-protocol`, `microvms-domain`,
`microvms-app`, `microvms-edges`, `microvms-core`, and `microvms-cli` to crates.io, `microvms` to PyPI, and
`@theagenticguy/microvms` to npm. `agentd` ships as a GitHub release binary.

crates.io trusted publishing can't create a crate, so a crate that's new to the
publish set needs one manual publish before the first release tag that includes
it. `microvms-domain`, `microvms-app` and `microvms-edges` are each one until
that's done. From a clean checkout of main, before the release's version bump
lands, run `cargo publish -p <crate> --locked` for each, in dependency order
(the domain, then the app, then the edges), with a crates.io API token scoped to
publishing new crates, then add each trusted publisher on crates.io (this repository, workflow `release.yml`, environment
`release`). Without it, the release publishes the crates below it and fails at
the new one. Publishing after the bump is the same failure the other way round:
the release's version already exists, and `cargo publish --workspace` has no
`--skip-existing`.
`./tools/check-publishable.py --dry-run` warns about a published crate the
registry doesn't have, and fails with `--tag`, which is how the release guard
runs it.

```bash
mise run publish:check
mise run release:check          # every publishing job in release.yml waits on the live gate
mise run publish:dry-run        # registry access and a committed tree required
mise run release:prepare X.Y.Z  # writes changelog.d/ into CHANGELOG.md as X.Y.Z; in the release PR
mise run release:tag vX.Y.Z     # creates/pushes a release tag; builds and drafts the release
```

A release starts with a pull request that synchronizes the Cargo, Python, and
Node versions, regenerates the Python stub, and runs
`mise run release:prepare X.Y.Z`. That task runs
`towncrier build --version X.Y.Z --yes`, which writes the fragments into
CHANGELOG.md as that version, below `## Unreleased`, removes them, and stages
both. It refuses a malformed fragment and a build with nothing to render, which
is what a second run finds. Read the section it wrote, commit it with the bump,
and check the tag with `./tools/check-publishable.py --tag=vX.Y.Z`. Once that
pull request merges, tag main's tip with `mise run release:tag vX.Y.Z`. Use the
release task rather than manually pushing an old tag. Registry versions are
immutable.

A release needs a green live run on its tag, so the tag publishes nothing by
itself. `release.yml` builds and attests every artifact, creates the GitHub
release as a draft, and stops at its gate, `live-gate`, which waits for a
reviewer in the `release` environment. Then:

1. Dispatch the live suite on the tag: `gh workflow run live-conformance.yml
   --ref vX.Y.Z`, and approve it in the `live-aws` environment. On a tag it
   tests the draft's own `agentd` and Linux CLI rather than a build, and its
   quickstart section reads the draft's assets through `$MICROVM_RELEASE_DIR`,
   verifying their attestation the way a client does. When every step passes,
   it uploads a `live-verified` marker carrying the draft's `SHA256SUMS`.
2. Once that run is green, approve `live-gate`. It checks the run for itself
   (`tools/release-gate.py verify`): live-conformance.yml, dispatched on the
   tag, at the tagged commit, a success, and a marker naming this draft. An
   approval given before that fails the job, and re-running the job once the
   live run has passed is the fix.
3. Approve the four publishing jobs, which wait in the same environment:
   `github-release` checks that the draft still holds what the live run tested
   and publishes it, and `crates-io`, `pypi` and `npm` publish the packages.

The live workflow needs the `live-aws` environment, with a required reviewer and
`v*` tags allowed, and the `LIVE_CONFORMANCE_ROLE_ARN` secret its OIDC role
comes from. A red live run publishes nothing, so the version isn't spent:
delete the draft and its tag (`gh release delete vX.Y.Z --cleanup-tag`, then
`git tag -d vX.Y.Z`), land the fix, and tag again. The draft job refuses a tag
that already has a release, draft or published, so rerunning the whole workflow
after a failed draft needs the stale draft deleted first.

PRs should explain the problem, the resulting behavior, validation, and any
remaining uncertainty. Keep scheduling and pooling in consumer applications;
see [Strategy](docs/STRATEGY.md) for scope. Report suspected vulnerabilities
through [Security](SECURITY.md).
