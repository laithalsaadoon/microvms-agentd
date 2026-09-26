# microvms-py

PyO3 bindings over `microvms-core`: one binding constructor per core constructor, one getter
per accessor, and no arithmetic or coercion the core doesn't have. The crate docs in
`src/lib.rs` explain why that line matters.

- Behavior belongs in the Rust layers below. A binding converts types and maps errors; it
  doesn't add defaults, retries or validation.
- A change to the exposed surface means `mise run stubs` to regenerate `microvms.pyi`;
  `stubs:check` in `mise run check` fails on a stale stub. Don't change the stub generator's
  maturin pin without checking its output path.
- `mise run check` doesn't build this crate as a Python module or run its tests. Build and
  run them the way CI's `python and node bindings` job does:

  ```bash
  uv venv .venv-bindings && . .venv-bindings/bin/activate
  uvx maturin@1.14 develop -m microvms-py/Cargo.toml
  uv pip install pytest && pytest microvms-py/tests -q
  ```

- Tests stay offline. To make the daemon fetch fail in a test, point `HTTPS_PROXY` at a closed
  local port and clear `NO_PROXY` (see `tests/test_provision.py`); emptying `PATH` no longer
  stops it.
- `clippy.toml` bans `Command` and direct environment reads, the same as the CLI's, calling
  `microvms_core::env::process` by name included.
