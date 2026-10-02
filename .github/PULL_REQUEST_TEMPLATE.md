## What and why

<!-- What changed, and what defect or gap it closes. Same standard as a commit
message here: name what was measured, and admit what is unverified.

One issue per pull request, or one box of a tracker's checklist; a change that
depends on another open one is stacked on it. Past about 400 changed lines of
product code, add a line that opens `Size:` and says why this is one change
(CONTRIBUTING.md, "Pull requests"). -->

## Evidence

<!-- Delete lines that do not apply. Do not check a box you did not run. -->

- [ ] `cargo fmt --all -- --check`
- [ ] `cargo clippy --all-targets -- -D warnings`
- [ ] `cargo test --all`
- [ ] `cargo run -p agentd --bin schema -- --check` (regenerated if the protocol changed)
- [ ] `cargo build --release -p agentd --target aarch64-unknown-linux-musl`
- [ ] `mise run spec` and `mise run spec:core` (if `verify/spec/` changed)
- [ ] `./tools/check-lint-coverage.py && mise exec -- ruff check . && mise exec -- ruff format --check .` (if any Python changed)
- [ ] `mise exec -- cargo deny check` (if a `Cargo.toml`, `Cargo.lock`, or `.cargo/deny.toml` changed)
- [ ] `mise exec -- actionlint` (if a workflow changed)
- [ ] `./conformance/run_rs.py --self-test` (if `conformance/` changed; offline and free)
- [ ] Live conformance run, if this touches the wire protocol or AWS lifecycle.
      Region and pass/fail counts:

## Parity

- [ ] No public name changed on any surface (core, CLI commands, `microvms.pyi`, `index.d.ts`).
- [ ] `verify/parity/capabilities.toml` updated: the other surfaces are implemented, or
      exempted with the reason the surface won't have it (an exemption that names an issue
      is a parity gap, which `parity:check` refuses). `mise run parity:check` passes.

## Guards

<!-- A fix's regression test is proven by FAIL_TO_PASS: run `mise run fail-to-pass -- --emit
<owner>`, commit the entry it writes into `verify/guards/faults/<owner>.toml` with its patch,
and paste the `fired:` lines it printed. A test the merge base can't compile gets a
hand-written fault instead, as below.

A new gate, scanner or script check, or a test FAIL_TO_PASS can't prove: register the
deliberate break that proves it fires. Add its entry to its owner's file in
`verify/guards/faults/` (the schema is in `tools/check-guards-fire.py`) and paste the line
`mise run guards:fire -- --only <id>` printed for it. A test that passes either way gives a
false answer.

For a guard no fault can be seeded for mechanically, such as a live check: what you broke,
that the check failed, and that it passed again after you restored the code.

When it adds no guard, write `None: this adds no guard.` -->

<!-- e.g. "agentd-fs-pop: removed the `?` from `parts.pop()?` in crates/agentd/src/fs.rs so ../x
became x, and `normalize_rejects_escapes_and_absorbs_benign_traversal` failed;
`guards:fire -- --only agentd-fs-pop` printed `fired: agentd-fs-pop`." -->

## Follow-ups

<!-- Each finding this change doesn't need to be correct, one line each, and add each to
the parent tracker's checklist. A finding gets an issue of its own only when it's a
security defect, a panic, data loss, or a design question. Write `None.` when there are
none. -->

## Platform claims

- [ ] Not applicable: this changes no claim about AWS behavior.
- [ ] **A `docs/PLATFORM.md` entry changed, and it carries a date, a region, and
      an API version.** If it contradicts an existing entry, the old one is left
      in place with its date so the drift is visible.

## Scope

- [ ] This is not an orchestrator, a fork implementation, or AgentCore parity work
      (see `docs/STRATEGY.md`).
