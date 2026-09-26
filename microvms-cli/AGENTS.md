# microvms-cli

The `microvm` command: parse arguments, call `microvms-core`, render one JSON envelope or
human output. A default, retry, validation rule, wire call, file format or subprocess here
belongs in a lower layer (root `AGENTS.md`, Architecture).

- AWS is reached through `src/seam.rs` and nothing else. `tests/thinness.rs` fails on a crate
  that opens a second path to AWS or HTTP, and on a handler that calls a control-plane
  operation past the seam.
- With `--json`, stdout carries exactly one envelope and progress goes to stderr (CLI-4, in
  `src/envelope.rs`). A stray `println!` breaks consumers and the guard.
- Exit codes in `src/exit.rs` are append-only: consumers branch on them. Add a row; never
  renumber or reuse one. `tests/exit_codes.rs` checks the codes a spawned process really exits
  with.
- A command or flag change means `mise run manifest` to regenerate `docs/manifest.json`;
  `tests/manifest.rs` fails on a command without a manifest row.
- `clippy.toml` bans `Command` and direct environment reads under a crate-root `deny`,
  including calling `microvms_core::env::process` by name. `src/main.rs` hands that lookup to
  core's resolvers once; everything else takes the lookup it's given. A reviewed `#[expect]`
  is allowed only when it's listed in `LINT_EXCEPTIONS` in `scripts/test_ratchet.py` with its
  count, and moving the work down is the usual fix.
- Known drift still lives here and each item has an issue: the `aws` upload in `seam.rs`
  (#258), the token minter in `seam.rs` (#270), and directory sync's `tar`, `globset`,
  `sha2` and `const-hex` (#260). Move work down; don't add to that list.

Tests: `cargo test -p microvms-cli`. Live behavior goes through `conformance/run_rs.py`.
