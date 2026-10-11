# microvms-protocol

The wire types shared by the `microvms-agentd` daemon and every client that talks to it:
the exec and file-transfer request and response shapes, `Phase`, `Outcome`, the SSE event
payloads, and the bootstrap contract.

Shared types keep the daemon and Rust clients aligned at compile time. They do
not establish compatibility between released versions by themselves; the
repository also checks the generated wire schema against the previous release.

`docs/schema.json` in the repository is derived from these same serde types, so the
published wire schema is a function of this crate rather than a document maintained beside
it.

## Naming

The package is `microvms-protocol` because `protocol` is taken on crates.io. Dependents
rename it back:

```toml
protocol = { package = "microvms-protocol", version = "1.0" }
```

## Reading

The wire contract is documented in [`docs/PROTOCOL.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/PROTOCOL.md),
which is authoritative. It takes precedence over generated documentation.

## License

Apache-2.0
