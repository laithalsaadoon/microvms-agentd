# Repository guide

Rust client stack and guest daemon for AWS Lambda MicroVMs. Start with
[README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md), and
[docs/README.md](docs/README.md). Wire behavior is specified in
[docs/PROTOCOL.md](docs/PROTOCOL.md); security constraints are in
[docs/TRUST.md](docs/TRUST.md).

## Commands

```bash
mise run check         # local code, security, tests, contracts, drift, packaging, build
mise run docs:check    # documentation build and checks
mise run live          # billable AWS verification
mise run live:verify-clean
```

`check` does not create AWS resources. Some tools need network access for
installation or advisory/rule updates. It does not run the documentation,
formal requirements, or live AWS tiers. See CONTRIBUTING.md for targeted tests
and setup; do not infer live verification from local test results.

## Code map

- `protocol/`: shared types; package name `microvms-protocol`, import `protocol`.
- `agentd/`: lifecycle hooks, authenticated execution, files, tunnels.
- `microvms-core/`: AWS control plane, sessions, lifecycle, cost, agent helpers.
- `microvms-cli/`: `microvm` commands and JSON envelopes.
- `microvms-py/`, `microvms-js/`: thin PyO3 and napi-rs bindings.
- `model/`, `spec/`, `conformance/`: portable model tests, formal requirements,
  and live AWS checks.

Dependencies flow from CLI and bindings to core to protocol. The daemon
depends on protocol. Keep shared validation and AWS behavior in core.

When `.codegraph/` exists, use `codegraph explore` before text searches to
locate or understand code. Confirm ambiguous cross-crate symbol matches from
the actual source. The daemon route census is `docs/schema.json` because
routes are generated from a schema.

## Maintenance rules

- Use `microvm manifest` for the current command contract. Regenerate
  `docs/manifest.json`, `docs/schema.json`, and Python stubs when affected.
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
- Demonstrate that new invariant guards catch their intended failure. AWS
  changes need a live exercise and a persistent conformance check, or an
  explicit statement that they remain unverified against AWS.
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
