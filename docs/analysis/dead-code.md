# microvms-agentd · Dead code

Reviewed at `9729b82` on 2026-10-11. These source-level findings require further reachability analysis. An empty section means no candidate was confirmed
in this review. Public library APIs may have downstream callers, and language
bindings register exports that need no Rust caller: the Node binding describes its
registration mechanism at `bindings/microvms-js/src/lib.rs:86-90`.

The repository's lint task runs `cargo clippy --all-targets -- -D warnings` and
`ruff check .`; its configured Rust invocation does not enable every feature.
Python's selected rules include unused-import diagnostics through the `F` family.
These checks complement source review but do not establish reachability across
all features, platforms, or downstream applications.
`.config/mise/tasks/lint.toml:48-53`
`pyproject.toml:130-158`

## Unreferenced exports

_none_

`SessionBuilder::with_timeout` is used by the production session constructor when
an attachment requests a timeout. Its declaration and caller are
`crates/microvms-app/src/session/mod.rs:638-643` and
`crates/microvms-core/src/prelude.rs:350-368`.

Runtime-exported functions remain part of their binding's public surface even
without direct Rust calls. Examples include the Node binding's error-code
enumeration and base-image helpers, which carry export attributes at
`bindings/microvms-js/src/errors.rs:147-165` and
`bindings/microvms-js/src/sandbox.rs:256-274`.

## Unreferenced files

_none_

Cargo workspace roots and module declarations are entry points rather than
ordinary imports. Test-only source also remains reachable through conditional
module declarations: the app's control tests include `posture_fuzz` under
`cfg(test)`. The ratchet's sentinel source is an intentional scanner input, not an
application module awaiting a caller.
`Cargo.toml:1-19`
`crates/microvms-app/src/control/mod.rs:1090-1097`
`tools/ratchet.py:265-270`
`tools/ratchet.py:1454-1461`

## Dead imports

| Path | Symbol | Imported from |
| --- | --- | --- |
| `crates/microvms-cli/src/commands/lifecycle.rs:2539` | `ControlPlane as _DocsOnly` | `crates/microvms-cli/src/commands/lifecycle.rs:30` |
| `crates/microvms-cli/src/commands/attached.rs:2881` | `ErrorKind as _DocsOnly` | `crates/microvms-cli/src/commands/attached.rs:41` |

These aliases have no subsequent references. Both explicitly allow
`unused_imports`, with a documentation-related reason, so they are intentional
exceptions to the lint rather than evidence that the source types are unused.
`crates/microvms-cli/src/commands/lifecycle.rs:2537-2539`
`crates/microvms-cli/src/commands/attached.rs:2879-2881`

The lifecycle module links `ControlPlane` in its documentation. The attached
commands also use `ErrorKind` directly when handling a timeout. Removing either
underlying import solely because its `_DocsOnly` alias is unused would ignore
these uses.
`crates/microvms-cli/src/commands/lifecycle.rs:8`
`crates/microvms-cli/src/commands/attached.rs:392`

## See also

These pages cite many of the same source files as this one.

- [module map](../architecture/module-map.md)
- [contract map](../insights/contract-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [risk hotspots](risk-hotspots.md)
- [system overview](../architecture/system-overview.md)
