# Contributing

The shared implementation lives in `microvms-core`; the CLI and bindings adapt
that implementation. Read [Protocol](docs/PROTOCOL.md) before changing wire
behavior and [Trust](docs/TRUST.md) before changing authentication or execution.

## Setup and checks

```bash
mise install
mise run install       # install git hooks
mise run check         # code, security, tests, schema, stubs and declarations, API drift, packaging, build, traceability, layering drift
mise tasks             # all available tasks
```

`check` does not create AWS resources. Initial dependency downloads, security
rule loading, and advisory updates can require network access. Documentation,
binding integration tests, and formal requirements have additional setup;
consult their tasks in `mise.toml` and the CI workflows.

Useful checks while iterating:

```bash
cargo test -p agentd --lib
cargo test -p agentd-model
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

Python builds with maturin and tests under `microvms-py/tests/`. Node builds
with `npm run build` and tests with `npm test` in `microvms-js/`.

For a new invariant guard, demonstrate that the test catches the intended
failure, restore the implementation, and record the result in the PR. In
network simulation tests, coordinate child processes through stdin rather than
wall-clock sleeps: child processes and the simulator use different clocks.

`mise run ratchet:check` holds `ratchet/drift.json` equal to the layering drift
its collectors find, such as an adapter dependency outside `arch/placement.toml`
or a subprocess in a shipping crate. A new finding fails, and so does a fix the
file still lists: run `mise run ratchet:update` and commit the file. The check
refuses an entry the base branch doesn't have, and a crate added to a set the
base already has, so new drift moves below the adapter or goes under
`decisions` with its reason. Moving recorded drift to another file or crate
isn't a fix: re-key its entry in the same change. A re-keyed entry keeps its
issue and changes its path or its text, not both, so a move and a rename land
in separate PRs.

## Generated contracts and API changes

Regenerate affected contracts and include their diffs:

```bash
mise run schema        # docs/schema.json
mise run manifest      # docs/manifest.json
mise run stubs         # Python declarations
mise run model:check   # implemented constraints versus the installed boto3 model
```

Use current boto3 models and AWS documentation to identify capabilities the
package should expose, verify request serialization, and check CLI/SDK parity.
The [MicroVM API](https://docs.aws.amazon.com/lambda/latest/microvm-api/Welcome.html)
manages images and VMs; [Lambda core](https://docs.aws.amazon.com/lambda/latest/lambda-core/Welcome.html)
manages VPC connectors. Document the package's supported workflows and limits.

To check implemented constraints against the latest SDK without creating AWS
resources, run `uv run --upgrade --script scripts/check-model-drift.py`.

The `spec` and `spec:core` tasks are separate from `check`. They require
compatible symspec tooling; `spec:core` currently names a local checkout and
is not portable. A passing `check` does not verify those documents.
`cargo test -p agentd-model` runs the portable state-machine checks.

## Documentation

Edit `site/authored/` for tutorials and landing pages, and top-level `docs/*.md`
for contracts and measured findings. `site/src/content/docs/` is generated and
ignored. The CLI reference comes from `docs/manifest.json`; historical source
analyses under `docs/architecture/`, `reference/`, `behavior/`, `analysis/`,
`diagrams/`, and `insights/` may need regeneration after a refactor.

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

Changes to AWS behavior need a live exercise of the changed path and a named
regression check in `conformance/run_rs.py`. State explicitly when live
verification was not performed. Documentation and other local-only changes
do not need a billable run.

```bash
mise run live                # builds binaries, provisions infrastructure, tests AWS
mise run live:verify-clean   # independently check for remaining resources
mise run live:destroy        # remove the Terraform stack when finished
```

For a targeted run, rebuild `target/release/microvm` first: `check` does not
build that release CLI. `--keep` retains resources and their charges. Inspect
MicroVMs, images, and service-created `/aws/lambda-microvms/` log groups after
cleanup; Terraform does not own every resource the service creates.

## Releases and reviews

The release workflow publishes `microvms-protocol`, `microvms-core`, and
`microvms-cli` to crates.io, `microvms` to PyPI, and
`@theagenticguy/microvms` to npm. `agentd` ships as a GitHub release binary.

```bash
mise run publish:check
mise run publish:dry-run     # registry access and a committed tree required
mise run release:tag vX.Y.Z  # creates/pushes a release tag; publishes artifacts
```

Before a release, synchronize the Cargo, Python, and Node versions, regenerate
the Python stub, and check the tag with
`./scripts/check-publishable.py --tag=vX.Y.Z`. Use the release task rather
than manually pushing an old tag. Registry versions are immutable.

PRs should explain the problem, the resulting behavior, validation, and any
remaining uncertainty. Keep scheduling and pooling in consumer applications;
see [Strategy](docs/STRATEGY.md) for scope. Report suspected vulnerabilities
through [Security](SECURITY.md).
