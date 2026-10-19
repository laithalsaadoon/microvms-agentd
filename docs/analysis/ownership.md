# microvms-agentd · Ownership

Use subsystem boundaries to choose a review, then use history to prioritize the reading.
Shared rules belong in the domain, use cases in the app, production I/O in the edges,
and composition in core. The dependency tests hold these boundaries to explicit sets;
the CLI and bindings adapt the resulting client surface.
`crates/microvms-cli/tests/dependency_direction.rs:23-45`

This page describes stewardship responsibilities rather than assigning people to folders.
Commit attribution cannot establish who reviewed a change, who can maintain it today,
or whether knowledge is shared. No tracked ownership declaration was present when this
snapshot was measured; establish review responsibility for a change explicitly.

## History and its limits

The table is a dated measurement at `9729b82` on 2026-10-11. The available repository
history is not shallow; its non-merge author dates run from 2026-08-05 through 2026-10-11.
The inventory comes from `git ls-tree -rz --name-only 9729b82`; touches come from
`git log --no-merges --format= --name-only 9729b82`, filtered to those current paths.
Each appearance of a path is a touch. Deleted paths are excluded and earlier names are
not followed across renames.

Paths are grouped by their top-level folder, with separate entries for each crate,
binding, verification area and site area. The selection starts with the largest current
folder groups and includes the shared domain, protocol, model and requirements
areas. Rows are ranked by touch frequency. Share is a percentage of all matched touches,
rounded to a whole percent; zero percent can represent a small nonzero share. Omitted
folders and rounding mean the displayed shares need not sum to 100%.

Churn measures repeated editing, not complexity, defects or reviewer coverage. In particular,
a file moved out of core starts appearing in its destination's history at that path;
use the current composition boundary when planning changes.
`Cargo.toml:21-27`, `crates/microvms-core/src/lib.rs:57-76`

## Subsystem review map

The review boundaries below describe what to examine, not exclusive ownership.

| Folder | Churn share | Review boundary | Evidence |
| --- | --- | --- | --- |
| `docs/` | 18% | Behavioral explanations and published references; review alongside the contract they describe. | `site/scripts/sync-docs.mjs:21-30` |
| `verify/guards/` | 9% | Seeded faults and their expected verdicts; review a guard and its fault together. | `tools/check-guards-fire.py:7-27` |
| `crates/microvms-cli/` | 7% | Command parsing, output and exit behavior; keep shared behavior below the adapter. | `crates/microvms-cli/src/main.rs:2-16` |
| `conformance/` | 5% | Live evidence and offline twins; distinguish suite self-tests from AWS verification. | `conformance/AGENTS.md:3-17` |
| `bindings/microvms-py/` | 5% | Python conversions and runtime integration; coordinate exposed behavior with core and parity cases. | `bindings/microvms-py/src/lib.rs:2-20` |
| `tools/` | 5% | Executable repository rules; assess both the rule and its failure evidence. | `tools/check-guards-fire.py:7-27` |
| `bindings/microvms-js/` | 4% | JavaScript conversions and runtime integration; preserve the shared service rules. | `bindings/microvms-js/src/lib.rs:2-15` |
| `verify/parity/` | 3% | Capability mappings and shared expected answers across language surfaces. | `verify/parity/cases/README.md:3-18` |
| `crates/microvms-app/` | 3% | Use cases and port contracts; review behavior independently of production I/O. | `crates/microvms-app/src/lib.rs:2-12` |
| `site/authored/` | 3% | Reader-oriented pages and navigation content; review the published route and audience. | `site/scripts/sync-docs.mjs:21-33` |
| `examples/` | 2% | Runnable workflows; keep instructions consistent with their scripts and supported API. | `examples/s3-prefetch-at-build/README.md:20-24` |
| `crates/microvms-core/` | 2% | Composition and public re-exports; review compatibility when a lower layer changes. | `crates/microvms-core/src/lib.rs:57-76` |
| `crates/microvms-edges/` | 2% | Production implementations of app ports; review effects and adapters with the port contract. | `crates/microvms-edges/src/lib.rs:2-22` |
| `crates/agentd/` | 2% | Guest behavior and the shared wire contract; coordinate protocol changes with clients. | `crates/agentd/Cargo.toml:15-20` |
| `verify/ratchet/` | 2% | Declared decisions and drift policy; distinguish accepted exceptions from new drift. | `tools/ratchet.py:7-16` |
| `verify/spec/` | 1% | Formal requirements and their trace evidence; review requirements with the code and tests they name. | `.config/mise/tasks/trace.toml:1-43` |
| `crates/microvms-domain/` | 1% | Shared rules and values; supply external inputs as parameters. | `crates/microvms-domain/src/lib.rs:2-17` |
| `crates/model/` | 0% | Executable lifecycle models; review their assumptions as well as their properties. | `crates/model/src/lib.rs:2-6` |
| `crates/protocol/` | 0% | Shared wire data; assess serialization and client/daemon compatibility together. | `crates/protocol/src/lib.rs:2-27` |
| `crates/model-conformance/` | 0% | Evidence that app behavior matches model rows, and client/daemon integration. | `crates/model-conformance/src/lib.rs:2-14` |

## Knowledge recorded outside history

Architecture is executable as well as documented: dependency assertions check the workspace
edges and allowed dependency sets. Review the placement policy with an architectural change,
then check the affected implementation in its current crate.
`crates/microvms-cli/tests/dependency_direction.rs:23-45`

Shared capabilities have a contract in the parity table and expected behavior in the case
corpus. Each surface runs the case through its own entry point. A cross-language behavior
change therefore needs agreement on the expected result, not independent adapter rules.
`verify/parity/cases/README.md:3-18`, `.config/mise/tasks/parity.toml:4-15`

Model-conformance tests compare the app's answers with model rows and exercise the daemon
and client together. Formal requirement checks are separate from the local `check` task;
traceability checks are included in it. Passing the local task does not establish that the
formal verification tasks ran.
`crates/model-conformance/src/lib.rs:2-14`, `.config/mise/tasks/trace.toml:18-43`,
`.config/mise/tasks/check.toml:13-45`

The fault registry records ways to make guards fail. Listing the registry checks that its
entries still apply; firing the faults is a separate task. Review evidence should identify
which of those ran.
`.config/mise/tasks/guards.toml:33-56`

Documentation also has separate verification. The fast documentation task builds and checks
the site; the broader documentation gate adds browser accessibility and budget checks.
A successful code check does not prove either documentation tier passed.
`.config/mise/tasks/docs.toml:136-153`, `.config/mise/tasks/docs.toml:16-20`

## Single points of failure

A reliable personnel bus factor cannot be derived from commit attribution alone. The useful
question for a proposed change is whether another reader can reconstruct its requirements,
implementation boundary and evidence. Apply these mitigations during review:

- **Changes across layers:** trace the behavior from its owning layer through core and each
  exposed adapter, and review the dependency assertions with any new edge.
  `crates/microvms-cli/tests/dependency_direction.rs:23-45`
- **Changes across language surfaces:** agree on a shared parity case and examine each runner's
  result before accepting that the surfaces match.
  `verify/parity/cases/README.md:3-18`
- **Model or requirement changes:** review the model's assumptions, the app comparison, and
  the relevant formal and traceability evidence together.
  `crates/model-conformance/src/lib.rs:2-14`, `.config/mise/tasks/trace.toml:18-43`
- **Platform-dependent changes:** retain live evidence separately from offline evidence;
  the conformance self-test exercises the suite's helpers, not the AWS service.
  `conformance/AGENTS.md:3-17`, `conformance/AGENTS.md:29-32`
- **Guard changes:** keep a reproducible failing case with the guard and report the seeded
  fault's verdict, so future maintenance can test the same assumption.
  `tools/check-guards-fire.py:7-27`, `.config/mise/tasks/guards.toml:49-56`

## See also

These pages cite many of the same source files as this one.

- [module map](../architecture/module-map.md)
- [system overview](../architecture/system-overview.md)
- [dependency graph](../diagrams/structural/dependency-graph.md)
- [dead code](dead-code.md)
- [impact analysis](../insights/impact-analysis.md)
