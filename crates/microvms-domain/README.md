# microvms-domain

Shared rules for VM sizes, cost, regions, names, service limits, errors, and
tunnel identity. The CLI and SDKs use these rules through `microvms-core`.

This crate does no I/O. It does not read files, the network, the clock, or
process state. It does not start a process or ask the OS for random bytes.
A rule that needs such a value takes it as an input. For example,
`Region::from_env` takes a lookup function, and `LaunchIdentity::from_seeds`
takes seeds. Tests pass values in and check the result.

## Choose a crate

Use this crate to check inputs or compute costs with no I/O. Most apps should
use [`microvms-core`](https://crates.io/crates/microvms-core). It re-exports
these types and supplies I/O through `microvms_core::prelude`.

The service limits and dated AWS measurements are in
[`docs/PLATFORM.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/PLATFORM.md).

## License

Apache-2.0
