# microvms-app

The MicroVMs client's use cases: the control-plane client, `Sandbox`, `Session`,
`ensure_image`, and the coding-agent recipes.

Each use case reaches the outside only through a port this crate declares: the control
plane's `Transport`, the session's `HttpBackend`, `TokenMinter`, `NameStore`, `BuildServices`,
`Clock`, `Entropy`, and `Adapters` for the pieces a use case builds partway through. The crate
itself doesn't touch the network, AWS, the filesystem, a subprocess, the wall clock, or the OS
random pool, so a use case can be tested with fakes and a simulated clock instead of the real
thing. The shared fakes are in `microvms_app::testing`, behind the `test-support` feature.

Most callers want [`microvms-core`](https://crates.io/crates/microvms-core) instead. It
re-exports every item here at its `microvms_core::` path and wires in the production
implementations from [`microvms-edges`](https://crates.io/crates/microvms-edges), through
`microvms_core::prelude`.

## Reading

The platform behaviors the use cases guard against are recorded in
[`docs/PLATFORM.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/PLATFORM.md).

## License

Apache-2.0
