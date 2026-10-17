# microvms-py

PyO3 bindings over `microvms-core`: one binding constructor per core constructor, one getter
per accessor, and no arithmetic or coercion the core doesn't have. The crate docs in
`src/lib.rs` explain why that line matters.

- Behavior belongs in the Rust layers below. A binding converts types and maps errors; it
  doesn't add defaults, retries or validation.
- Every method or function that calls AWS or the daemon has an awaitable twin named with an
  `_async` suffix (`create_async` for a constructor), with the same signature. Both spellings
  drive one future, built by the method's `*_op` helper: the blocking one through
  `runtime::block_on`, the twin through `runtime::spawn`, or `runtime::spawn_shielded` for a
  lifecycle transition, which runs to completion when its awaitable is cancelled
  (`src/runtime.rs` has the rules). A new twin goes in `TWINS` in `tests/test_async.py`, whose
  census fails otherwise, and in its row's `py` cell in `verify/parity/capabilities.toml`.
- A change to the exposed surface means `mise run stubs` to regenerate `microvms.pyi`;
  `stubs:check` in `mise run check` fails on a stale stub. Don't change the stub generator's
  maturin pin without checking its output path. A new public function, or a new method
  of a class `verify/parity/capabilities.toml` names, needs its row there, or `parity:check` fails.
- `mise run check` doesn't run this crate's Python tests. Build and run the whole suite
  the way CI's `python and node bindings` job does:

  ```bash
  mise run bindings:py
  ```

  The task creates and activates its own `.venv-bindings` environment, then runs this
  pinned builder from `.config/mise/tasks/test.toml`:

  ```bash
  uvx maturin@1.14.1 develop -m bindings/microvms-py/Cargo.toml --features parity-fixtures
  ```

  The whole suite needs those test helpers. Ordinary wheel builds omit that feature.
  `mise run ci:bindings` also checks the declarations, typed consumers, and npm package.

- Tests stay offline. To make the daemon fetch fail in a test, point `HTTPS_PROXY` at a closed
  local port and clear `NO_PROXY` (see `tests/test_provision.py`); emptying `PATH` no longer
  stops it.
- `clippy.toml` bans `Command` and direct environment reads, the same as the CLI's, calling
  `microvms_core::env::process` by name included.
