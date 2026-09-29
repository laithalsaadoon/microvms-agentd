# microvms-cli

The `microvm` command: parse arguments, call `microvms-core`, render one JSON envelope or
human output. A default, retry, validation rule, wire call, file format or subprocess here
belongs in a lower layer (root `AGENTS.md`, Architecture).

- AWS is reached through `src/seam.rs` and nothing else. `tests/thinness.rs` fails on a crate
  that opens a second path to AWS or HTTP. `clippy.toml` refuses core's transport calls, and
  every core function that builds a signed plane or session, everywhere but `src/seam.rs`, and
  the ratchet's `operation-literal` rule refuses an operation name written as a string.
- With `--json`, stdout carries exactly one envelope and progress goes to stderr (CLI-4, in
  `src/envelope.rs`). A stray `println!` breaks consumers and the guard.
- Exit codes in `src/exit.rs` are append-only: consumers branch on them. Add a row; never
  renumber or reuse one. `tests/exit_codes.rs` checks the codes a spawned process really exits
  with.
- A command or flag change means `mise run manifest` to regenerate `docs/manifest.json`;
  `tests/manifest.rs` fails on a command without a manifest row.
- `clippy.toml` also bans `Command` and direct environment reads under a crate-root `deny`,
  including calling `microvms_core::env::process` by name. `src/main.rs` hands that lookup to
  core's resolvers once; everything else takes the lookup it's given. A reviewed `#[expect]`
  is allowed only when it's listed in `LINT_EXCEPTIONS` in `scripts/test_ratchet.py` with its
  count, and moving the work down is the usual fix.
- A flag that takes seconds parses through core's `duration_of_secs_f64`
  (`cli::parse_seconds`), so a bad value is refused before the handler runs. `clippy.toml`
  bans the panicking float conversions, `Duration::from_secs_f64` and `from_secs_f32`. A new
  seconds flag gets a row in `cli::tests::every_seconds_flag_refuses_what_is_not_a_duration`,
  since the ban doesn't see a silent zero spelled another way.
- Known drift still lives here and each item has an issue: the `aws` upload in `seam.rs`
  (#258), the token minter in `seam.rs` (#270), and directory sync's `tar`, `globset`,
  `sha2` and `const-hex` (#260). Move work down; don't add to that list.

Tests: `cargo test -p microvms-cli`. Live behavior goes through `conformance/run_rs.py`.
