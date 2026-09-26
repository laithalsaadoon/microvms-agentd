# microvms-app

The use cases, written only against ports this crate declares (ARCH-7): the control-plane
client, `Sandbox`, `Session`, `ensure_image`, the agent recipes and the daemon release's
verification policy. The root `AGENTS.md` has the layer order and the port list; the crate
docs in `src/lib.rs` say what each port is for.

- Something a use case needs from outside the process goes through a port. If no port covers
  it, add a trait here and implement it in `microvms-edges`; don't reach for the std or tokio
  item. Time goes through `Clock` and randomness through `Entropy`, so tests stay
  deterministic.
- `clippy.toml` bans the std and tokio file, network, process, environment, stdio, signal and
  clock items under a crate-root `forbid`, and the crate has no exception list.
  `std::time::SystemTime` stays usable as a data type; only reading the clock is banned.
  `AppLintTests` in `scripts/test_ratchet.py` pins the ban list.
- The dependency set in `arch/placement.toml` and each dependency's features (tokio's
  especially) are asserted exactly by `microvms-cli/tests/dependency_direction.rs`, and the
  set can't grow in a PR (the domain's `AGENTS.md` has the procedure). reqwest, the AWS
  crates, getrandom, the socket crates and `sigstore-*` belong in the edges.
- The shared test doubles are in `src/testing.rs` behind the `test-support` feature. Reuse and
  extend them rather than writing a local fake in a test.
- CodeQL's `rust/hard-coded-cryptographic-value` flags a zeroed buffer that `dyn Entropy`
  fills and that ends up in a nonce. It can't follow the trait call. That's a false positive:
  dismiss it with the reason (alert 121 is the precedent) rather than working around it.
- A behavior change here usually shows in the bindings; build and run their suites as the root
  `AGENTS.md` describes.

Tests: `cargo test -p microvms-app` and `cargo clippy -p microvms-app --all-targets -- -D
warnings` (clippy is what enforces the ban; `mise run lint` runs it for the workspace). After
a `clippy.toml` change, also run `mise run ratchet:check`.
