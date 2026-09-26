# microvms-domain

Rules and values with no I/O: sizing, cost, regions, names, service constraints and error
kinds (ARCH-6). The root `AGENTS.md` has the layer order; this file covers working here.

- A rule that needs the clock, the environment, the filesystem or randomness takes it as a
  parameter. `Region::from_env` takes a lookup and `CalendarDate::from_unix_secs` takes
  seconds; follow that shape instead of reading the input here.
- `clippy.toml` bans the std file, network, process, environment, stdio and clock calls, plus
  `jiff`'s clock and system time zone and `x25519_dalek`'s `random` constructors. The crate
  root forbids both lints, so an `#[allow]` or `#[expect]` is itself an error. Don't look for
  a way around the ban; move the I/O to `microvms-edges` and pass its result in.
- The dependency set can't grow in a PR: `ratchet:check` refuses a crate added to
  `[microvms-domain]` in `arch/placement.toml`, and a decision can't clear it. A new
  dependency is a maintainer's call, so raise it first. Once it's approved, it goes in
  `placement.toml`, in `DOMAIN_FEATURES` in `microvms-cli/tests/dependency_direction.rs`
  (which asserts the set and each dependency's features exactly), and, if it brings a clock
  or entropy call, in `clippy.toml`.
- A change to `clippy.toml` must keep `DomainLintTests` in `scripts/test_ratchet.py` green:
  it pins the ban list and runs real clippy over each group.

Tests: `cargo test -p microvms-domain` and `cargo clippy -p microvms-domain --all-targets --
-D warnings` (clippy is what enforces the ban; `mise run lint` runs it for the workspace).
After a `clippy.toml` change, also run `mise run ratchet:check`.
