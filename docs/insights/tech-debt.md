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
| 1 | Formal tasks deliberately omit strict verification; their success means no error-severity findings, not a fully verified specification. Reduce unresolved findings before strengthening the gate. | missing tests | L | `.config/mise/tasks/trace.toml:18-24`, `tools/check-spec.sh:20` |
| 2 | The Maturin version is repeated in stub generation, binding builds, and proof-runner commands. A unit test checks that the binding builds and the release workflow use the stub generator's pin, and the proof runner's copy is outside that test. Add it, or centralize ownership, before the next upgrade. | duplicated logic | S | `tools/generate-py-stubs.py:84-89`, `.config/mise/tasks/test.toml:93-108`, `tools/fail-to-pass.py:113-120`, `tools/test_check_release_builds.py:525-539` |

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
limits in the formal tasks provide a stronger signal for this register
(`.config/mise/tasks/trace.toml:22-23`).

## Pattern-level smells

### Verification tiers make different promises

Formal verification is deliberately separate from `check`, and the formal task
itself stops short of strict verification. Those are distinct boundaries: making
the checker easy to install does not resolve specification findings, and reducing
findings does not make the tier part of the normal local workflow
(`.config/mise/tasks/trace.toml:22-23`, `.config/mise/tasks/check.toml:11-45`).

Shows up in:

- `.config/mise/tasks/trace.toml:22-23`: strict mode and local-gate exclusions.
- `.config/mise/tasks/trace.toml:26-31`: core verification's separate command and reachability budget.
- `tools/check-spec.sh:8-18`: the locked optional verifier that `spec:setup` installs.

Cost: L to strengthen the verification verdict.

### Generator versions have multiple owners

The Python stub generator pins an exact Maturin release, and the binding builds
and the proof runner repeat it. Upgrading Maturin means editing each copy
(`tools/generate-py-stubs.py:84-89`). One unit test checks that the binding builds
and the release workflow use the generator's pin
(`tools/test_check_release_builds.py:525-539`), and the proof runner's copy isn't in
that test's list.

Shows up in:

- `.config/mise/tasks/test.toml:93-108`: repeated Python build-tool version.
- `tools/fail-to-pass.py:113-120`: the binding build command repeated in the proof runner.

Cost: S. Add the proof runner's copy to the agreement test, or derive it from the generator's pin.

## See also

These pages cite many of the same source files as this one.

- [debugging guide](debugging-guide.md)
- [contract map](contract-map.md)
- [impact analysis](impact-analysis.md)
- [module map](../architecture/module-map.md)
- [business logic](business-logic.md)
