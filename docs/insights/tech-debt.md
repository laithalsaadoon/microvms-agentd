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
| 5 | Declaration generation resolves `@napi-rs/cli@3` afresh, so an upstream release can change the expected declarations without a repository change. Pin the generator used by the task and package build together. | version pin | S | `.config/mise/tasks/contracts.toml:270-295`, `bindings/microvms-js/package.json:51-54` |
| 6 | Formal tasks deliberately omit strict verification; their success means no error-severity findings, not a fully verified specification. Reduce unresolved findings before strengthening the gate. | missing tests | L | `.config/mise/tasks/trace.toml:3-13`, `tools/check-spec.sh:4-9` |
| 7 | *Judgment call:* formal requirements remain outside the local `check` task and need a separately installed checker. A reproducible optional verification environment would make this tier easier to run without changing the ordinary local gate. | missing tests | M | `.config/mise/tasks/trace.toml:11-28`, `.config/mise/tasks/check.toml:11-43`, `tools/check-spec.sh:15-24` |
| 8 | The Maturin version is repeated in stub generation, binding builds, and proof-runner commands. Centralize its ownership or assert agreement to keep coordinated upgrades from drifting. | duplicated logic | S | `tools/generate-py-stubs.py:84-89`, `.config/mise/tasks/test.toml:89-104`, `tools/fail-to-pass.py:101-107` |
| 9 | The daemon port default is independently declared by control, session, and daemon configuration. Give the shared default an owner while preserving the existing public paths. | duplicated logic | S | `crates/microvms-app/src/control/mod.rs:118-119`, `crates/microvms-app/src/session/proxy.rs:84-85`, `crates/agentd/src/config.rs:101-104` |
| 10 | Lifecycle commentary says `Sandbox` cannot address a VM launched elsewhere, while `Sandbox::adopt` now supports that workflow. Rewrite the rationale around the current ownership model before using it to guide a refactor. | dead code adjacent | S | `crates/microvms-cli/src/commands/lifecycle.rs:10-18`, `crates/microvms-app/src/sandbox.rs:1338-1345` |

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
(`.config/mise/tasks/trace.toml:6-12`, `.config/mise/tasks/check.toml:11-43`).

Shows up in:

- `.config/mise/tasks/trace.toml:6-12`: strict mode and local-gate exclusions.
- `.config/mise/tasks/trace.toml:15-28`: core verification's separate command and reachability budget.
- `tools/check-spec.sh:15-24`: the external checker prerequisite.

Cost: M for a reproducible optional environment; L to strengthen the verification verdict.

### Generator versions have multiple owners

The declaration task deliberately resolves a moving major version, while the
Python stub generator uses an exact version repeated in other build commands.
Both choices create coordination work: one admits upstream changes between runs,
and the other requires synchronized edits during upgrades
(`.config/mise/tasks/contracts.toml:293-306`, `tools/generate-py-stubs.py:84-89`).

Shows up in:

- `.config/mise/tasks/contracts.toml:293-306`: a declaration check that anticipates generator changes.
- `bindings/microvms-js/package.json:51-54`: the package's own generator constraint.
- `.config/mise/tasks/test.toml:89-104`: repeated Python build-tool version.
- `tools/fail-to-pass.py:101-113`: binding build commands repeated in the proof runner.

Cost: S. Establish a shared version source or an agreement check before the next upgrade.

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

### Explanations and repeated constants need an owner

The lifecycle module's launch-only explanation predates the supported adoption
workflow. Independently, the daemon port default appears in control, session, and
daemon configuration. These are different forms of the same maintenance cost:
changing a contract requires finding copies that the type system does not connect
(`crates/microvms-cli/src/commands/lifecycle.rs:10-18`,
`crates/microvms-app/src/sandbox.rs:1338-1345`,
`crates/microvms-app/src/control/mod.rs:118-119`).

Shows up in:

- `crates/microvms-cli/src/commands/lifecycle.rs:10-18`: the old ownership rationale.
- `crates/microvms-app/src/sandbox.rs:1338-1345`: the adoption workflow that requires a revised explanation.
- `crates/microvms-app/src/control/mod.rs:118-119`: control's port default.
- `crates/microvms-app/src/session/proxy.rs:84-85`: session's port default.
- `crates/agentd/src/config.rs:101-104`: the daemon's port default.

Cost: S for the explanation and shared constant; preserve compatibility re-exports.

## See also

- [debugging guide](debugging-guide.md): 5 shared source citations
- [contract map](contract-map.md): 4 shared source citations
- [impact analysis](impact-analysis.md): 4 shared source citations
- [module map](../architecture/module-map.md): 3 shared source citations
- [business logic](business-logic.md): 3 shared source citations
