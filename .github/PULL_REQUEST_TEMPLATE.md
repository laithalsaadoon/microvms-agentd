## What and why

<!-- What changed, and what defect or gap it closes. Same standard as a commit
message here: name what was measured, and admit what is unverified. -->

## Evidence

<!-- Delete lines that do not apply. Do not check a box you did not run. -->

- [ ] `cargo fmt --all -- --check`
- [ ] `cargo clippy --all-targets -- -D warnings`
- [ ] `cargo test --all`
- [ ] `cargo run -p agentd --bin schema -- --check` (regenerated if the protocol changed)
- [ ] `cargo build --release -p agentd --target aarch64-unknown-linux-musl`
- [ ] `mise run spec` and `mise run spec:core` (if `spec/` changed)
- [ ] `./scripts/check-lint-coverage.py && uvx ruff check . && uvx ruff format --check .` (if any Python changed)
- [ ] `mise exec -- cargo deny check` (if a `Cargo.toml`, `Cargo.lock`, or `deny.toml` changed)
- [ ] `mise exec -- actionlint` (if a workflow changed)
- [ ] `./conformance/run_rs.py --self-test` (if `conformance/` changed — offline and free)
- [ ] Live conformance run, if this touches the wire protocol or AWS lifecycle.
      Region and pass/fail counts:

## Guards

**If this adds a guard, register the deliberate break that proves it fires.** Add its
entry to `guards/faults.toml` (the schema is in `scripts/check-guards-fire.py`) and paste
the line `mise run guards:fire -- --only <id>` printed for it. A test that passes either
way is a false answer, not a passing test; see `CONTRIBUTING.md` for the two that did
exactly that here.

For a guard no fault can be seeded for mechanically, such as a live check: what you broke,
that the check failed, and that it passed again after you restored the code.

<!-- e.g. "agentd-fs-pop: removed the `?` from `parts.pop()?` in agentd/src/fs.rs so ../x
became x; `guards:fire -- --only agentd-fs-pop` printed `fired: agentd-fs-pop`." -->

## Platform claims

- [ ] Not applicable — this changes no claim about AWS behavior.
- [ ] **A `docs/PLATFORM.md` entry changed, and it carries a date, a region, and
      an API version.** If it contradicts an existing entry, the old one is left
      in place with its date so the drift is visible.

## Scope

- [ ] This is not an orchestrator, a fork implementation, or AgentCore parity work
      (see `docs/STRATEGY.md`).
