# microvms-js

napi-rs bindings over `microvms-core`, published to npm. Like the Python binding, it
converts types and maps errors; behavior belongs in the Rust layers below.

- A change to the exposed surface means `mise run dts` to regenerate `index.d.ts`;
  `dts:check` in `mise run check` fails on stale declarations, and the docs site's TypeScript
  reference is generated from that file.
- `mise run check` doesn't build the addon or run its tests. Build and run them the way CI's
  `python and node bindings` job does:

  ```bash
  npx -y -p @napi-rs/cli@3 napi build --manifest-path Cargo.toml --package microvms-js \
    --platform --output-dir . --cwd microvms-js
  node --test "microvms-js/__test__/*.mjs"
  ```

- Tests stay offline. To make the daemon fetch fail in a test, point `HTTPS_PROXY` at a closed
  local port and clear `NO_PROXY` (see `__test__/provision.mjs`).
- `package.json`'s `napi.targets` is the source of truth for the platforms that get an npm
  package; `scripts/check-publishable.py` fails a workflow matrix that doesn't match it.
- This is the one crate where `unsafe_code` is `deny` rather than `forbid`, because
  napi-derive's expansion carries its own `allow`. Hand-written `unsafe` needs a scoped
  `#[allow(unsafe_code)]` and a SAFETY note.
