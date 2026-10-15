# microvms-agentd · Tech debt

This register was rebuilt from tracked source and configuration at `9729b82` on
2026-10-11. The review combined explicit comment-marker searches, declared
verification limits, dependency and generator pins, and comparisons between
implementation and explanatory prose. A version pin is not evidence that a
dependency is obsolete; entries below identify a specific maintenance cost.

Rank reflects the expected consequence of leaving an item and the effort needed
to address it. Costs are S for a local change, M for a coordinated change, and L
for a change spanning contracts or verification layers. Categories are limited to
`marker`, `wrong abstraction`, `error handling`, `dead code adjacent`,
`deprecated pattern`, `version pin`, `duplicated logic`, and `missing tests`.
Entries labeled *judgment call* describe intentional tradeoffs, not confirmed
implementation defects.

## Ranked register

| Rank | Debt item | Category | Cost to fix | Citation |
| --- | --- | --- | --- | --- |
| 1 | Parity enrollment excludes flags, positionals, global flags, and classes that no table row names. Extend enrollment before treating name parity as a complete API comparison. | missing tests | M | `tools/check-parity.py:34-43` |
| 2 | Shared parity cases need a review-supplied failing-first proof: the automated proof finder recognizes language test functions rather than corpus entries. Add corpus support without duplicating each case across language suites. | missing tests | M | `CONTRIBUTING.md:210-216`, `tools/fail-to-pass.py:29-36` |
| 3 | The CLI owns persistent ledger and history formats. Moving their reusable behavior below the CLI would let other adapters share it; preserve their different retention semantics. | wrong abstraction | L | `AGENTS.md:110-116`, `crates/microvms-cli/src/ledger.rs:28-48`, `crates/microvms-cli/src/history.rs:5-14` |
| 4 | Default parity intentionally omits string, zero, and `None` defaults, while TypeScript defaults appear only in prose. Expand the contract representation before asserting option-level consistency. | missing tests | M | `tools/check-parity.py:72-86` |
| 5 | Formal tasks deliberately omit strict verification; their success means no error-severity findings, not a fully verified specification. Reduce unresolved findings before strengthening the gate. | missing tests | L | `.config/mise/tasks/trace.toml:3-13`, `tools/check-spec.sh:4-9` |
| 6 | *Judgment call:* formal requirements remain outside the local `check` task and need a separately installed checker. A reproducible optional verification environment would make this tier easier to run without changing the ordinary local gate. | missing tests | M | `.config/mise/tasks/trace.toml:11-28`, `.config/mise/tasks/check.toml:11-44`, `tools/check-spec.sh:15-24` |
| 7 | The Maturin version is repeated in stub generation, binding builds, and proof-runner commands. A unit test checks that the binding builds and the release workflow use the stub generator's pin, and the proof runner's copy is outside that test. Add it, or centralize ownership, before the next upgrade. | duplicated logic | S | `tools/generate-py-stubs.py:84-89`, `.config/mise/tasks/test.toml:93-108`, `tools/fail-to-pass.py:101-108`, `tools/test_check_release_builds.py:95-109` |
| 8 | The daemon port default is independently declared by control, session, and daemon configuration. Give the shared default an owner while preserving the existing public paths. | duplicated logic | S | `crates/microvms-app/src/control/mod.rs:118-119`, `crates/microvms-app/src/session/proxy.rs:84-85`, `crates/agentd/src/config.rs:118-121` |

## Explicit markers

No explicit marker comments matched the source-and-configuration scan. The
alternatives were `TODO`, `FIXME`, `HACK`, `XXX`, `// REFACTOR`, and
`# DEPRECATED`. Historical analysis prose describing these searches was excluded.
The scan can be repeated with:

```bash
git ls-files -z -- '*.rs' '*.py' '*.ts' '*.mts' '*.js' '*.mjs' '*.sh' \
  '*.toml' '*.yml' '*.yaml' | xargs -0 rg -n \
  '\b(TODO|FIXME|HACK|XXX)\b|//[[:space:]]*REFACTOR\b|#[[:space:]]*DEPRECATED\b'
```

The absence of marker comments does not establish absence of debt. Explicit
limits in the parity checker and the formal tasks provide stronger signals for
this register (`tools/check-parity.py:34-43`, `.config/mise/tasks/trace.toml:6-12`).

## Pattern-level smells

### Parity covers only the surface represented in its table

The checker holds named capabilities and selected defaults to shared contracts,
but enrollment deliberately leaves out some API shapes. This is a boundary on
what a green result proves; expanding the table and readers is more useful than
adding independent assertions in each adapter (`tools/check-parity.py:34-43`,
`tools/check-parity.py:72-86`).

Shows up in:

- `tools/check-parity.py:34-43`: classes and command options outside enrollment.
- `tools/check-parity.py:72-86`: defaults that the comparison does not hold.
- `CONTRIBUTING.md:210-216`: shared cases whose failing-first proof is still a review obligation.

Cost: M. Extend the shared contract and prove the new comparison can fail.

### Verification tiers make different promises

Formal verification is deliberately separate from `check`, and the formal task
itself stops short of strict verification. Those are distinct boundaries: making
the checker easy to install does not resolve specification findings, and reducing
findings does not make the tier part of the normal local workflow
(`.config/mise/tasks/trace.toml:6-12`, `.config/mise/tasks/check.toml:11-44`).

Shows up in:

- `.config/mise/tasks/trace.toml:6-12`: strict mode and local-gate exclusions.
- `.config/mise/tasks/trace.toml:15-28`: core verification's separate command and reachability budget.
- `tools/check-spec.sh:15-24`: the external checker prerequisite.

Cost: M for a reproducible optional environment; L to strengthen the verification verdict.

### Generator versions have multiple owners

The Python stub generator pins an exact Maturin release, and the binding builds
and the proof runner repeat it. Upgrading Maturin means editing each copy
(`tools/generate-py-stubs.py:84-89`). One unit test checks that the binding builds
and the release workflow use the generator's pin
(`tools/test_check_release_builds.py:95-109`), and the proof runner's copy isn't in
that test's list.

Shows up in:

- `.config/mise/tasks/test.toml:93-108`: repeated Python build-tool version.
- `tools/fail-to-pass.py:101-108`: the binding build command repeated in the proof runner.

Cost: S. Add the proof runner's copy to the agreement test, or derive it from the generator's pin.

### Persistent formats live above the shared implementation

The ledger and history serve different purposes and must retain different
lifetimes. Their placement in the CLI nevertheless makes reuse by other adapters
a separate design effort. Moving ownership should preserve those semantics,
without merging the formats merely because they share a state directory
(`crates/microvms-cli/src/ledger.rs:18-22`,
`crates/microvms-cli/src/history.rs:5-14`).

Shows up in:

- `AGENTS.md:110-116`: the repository identifies CLI file formats as remaining layering work.
- `crates/microvms-cli/src/ledger.rs:28-48`: the persistent invocation record.
- `crates/microvms-cli/src/history.rs:5-14`: the reason history cannot share ledger retention.

Cost: L. Move reusable behavior with compatibility coverage for existing records.

### The daemon port default needs an owner

The daemon port default appears in control, session, and daemon configuration.
Changing it requires finding copies that the type system does not connect
(`crates/microvms-app/src/control/mod.rs:118-119`).

Shows up in:

- `crates/microvms-app/src/control/mod.rs:118-119`: control's port default.
- `crates/microvms-app/src/session/proxy.rs:84-85`: session's port default.
- `crates/agentd/src/config.rs:118-121`: the daemon's port default.

Cost: S for a shared constant; preserve compatibility re-exports.

## See also

These pages cite many of the same source files as this one.

- [debugging guide](debugging-guide.md)
- [contract map](contract-map.md)
- [impact analysis](impact-analysis.md)
- [module map](../architecture/module-map.md)
- [business logic](business-logic.md)
