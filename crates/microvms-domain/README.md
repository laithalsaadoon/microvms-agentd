# microvms-domain

The rules every MicroVMs surface has to agree on: size classes, the cost engine and its
rate table, region parsing, VM name validation, the service constraints, the error kinds,
and the tunnel identity's derivation and pin.

This crate performs no I/O. It doesn't read the network, the filesystem, the environment,
the clock, or the OS random pool, and it doesn't start a subprocess. A rule that needs one
of those takes it as a parameter: `Region::from_env` takes a lookup function,
`CalendarDate::from_unix_secs` takes the time, and `LaunchIdentity::from_seeds` takes the
seeds. So a rule gives the same answer on every machine, and a test passes its inputs in
rather than faking global state.

Most callers want [`microvms-core`](https://crates.io/crates/microvms-core) instead. It
re-exports every item here at its `microvms_core::` path and supplies the I/O this crate
leaves out, through `microvms_core::prelude`.

## Reading

The constraints and their measurements are recorded in
[`docs/PLATFORM.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/PLATFORM.md).

## License

Apache-2.0
