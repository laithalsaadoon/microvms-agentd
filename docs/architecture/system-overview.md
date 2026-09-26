# microvms-agentd · System overview

AWS Lambda MicroVMs hands you an isolated Firecracker VM and no way to use it: there is no
API to run a command inside one and no API to move a file into or out of one
(`docs/PLATFORM.md:20-23`). Every harness built on the service has to supply both itself.
This repository is that supply — `agentd`, a static daemon baked into the VM image, plus the
`microvm` CLI and the Rust, Python, and Node libraries that talk to it (`README.md:11-15`).
The CLI and libraries are distributed through crates.io, PyPI, and npm; the
daemon is a release binary. Selected crates opt into publishing despite the
workspace default of `publish = false`. The audience
is whoever builds a sandbox product on MicroVMs — an agent harness, a CI runner, a
code-execution service.

The client's real work is absorbing the platform's surprises once. `docs/PLATFORM.md`
records dated findings, many of which are
traps in the specific sense that the platform's answer points away from the cause: an
unsupported region answers `AccessDeniedException` with a null message, a `clientToken`
replay wedges an image in `CREATING` for fifteen hours with no error at all
(`microvms-core/src/lib.rs:7-14`). Each closure is ranked by strength — S1 inexpressible, S2
rejected locally before any call, S3 correct by default and overridable
(`microvms-core/src/lib.rs:23-40`).

The workspace crates carry that (`Cargo.toml:2-10`), and the seams follow defect classes rather than
layers. `protocol` is the wire contract as types: pure data, serde plus schemars, no tokio,
no axum, no base64 (`protocol/src/lib.rs:16-21`). Both the daemon and the client
compile against it, so a renamed field fails a build instead of a consumer's runtime
(`agentd/Cargo.toml:11-15`). `agentd` is the daemon
(`agentd/src/lib.rs:31-46`) — `state` owns the one-shot bootstrap, `auth` decides
before a body byte is read, `exec` and `fs` own idempotent exec and streaming tar. Its
router is assembled by walking the same endpoint list `/v1/schema` publishes, so a
documented route with no handler panics at startup (`agentd/src/routes.rs:29-35`);
the endpoints are split into a Bearer-guarded `control` router and an `open` one
(`agentd/src/routes.rs:51-59`, `agentd/src/routes.rs:110-140`). It runs as the container
`CMD` on a current-thread runtime sized for a 512 MiB guest (`agentd/src/main.rs:4-6`,
`agentd/src/main.rs:24-27`).

`microvms-core` is the client library and the largest crate; its own doc comment sorts most
of its modules into a foundation and a product surface, with
`agents` as the one layer above them and `provision` beside the surface
(`microvms-core/src/lib.rs:59-87`). `control` speaks
hand-signed SigV4 rest-json because `lambda-microvms` has no SDK crate
(`microvms-app/src/control/mod.rs:2-3`); `session` is
the in-VM client, carrying proxy auth and the byte-offset cursor that makes an interrupted
stream resumable (`microvms-app/src/session/mod.rs:4-7`); `sandbox` keeps every lifecycle
field private so the Z3 proofs are proofs about the code
(`microvms-app/src/sandbox.rs:11-17`); `cost` treats unpriced as a distinct variant rather
than zero (`microvms-domain/src/cost.rs:22-27`).

`microvms-cli` ships `microvm` and its subcommands
(`microvms-cli/src/cli.rs:85-362`), each invocation writing exactly one JSON
envelope to stdout and progress to stderr (`microvms-cli/src/envelope.rs:4-11`). It has no
lib target (`microvms-cli/Cargo.toml:21-23`) and no second path to AWS: a denylist
of HTTP clients, signers, and credential chains is asserted against `cargo metadata`
(`microvms-cli/tests/thinness.rs:49-96`). `microvms-py` and `microvms-js`
wrap the same core and never the CLI (`microvms-py/Cargo.toml:22-26`,
`microvms-js/Cargo.toml:20-21`). Verification sits outside the product graph: `model` depends
only on `stateright`, with no workspace edge, modelling the protocol rather than importing it
(`model/Cargo.toml:12-13`), and `conformance/run_rs.py` drives the built CLI through its named
checks against real AWS (`conformance/run_rs.py:9`). Start at
`agentd/src/lib.rs:9-29` for the trust boundary, then `microvms-core/src/lib.rs:21-40` for
the trap ladder.

## Stack

| Layer | Technology | Source |
| --- | --- | --- |
| Language | Rust, `edition = "2024"`, `resolver = "3"` | `Cargo.toml:23`, `Cargo.toml:11` |
| Toolchain and targets | `channel = "stable"`, `targets = ["aarch64-unknown-linux-musl", "x86_64-unknown-linux-musl"]` | `rust-toolchain.toml:13-16` |
| Shipping artifact | `lto`, `codegen-units = 1`, `panic = "unwind"`, `strip`, `opt-level = "z"` | `Cargo.toml:36-59` |
| Daemon HTTP | `axum = "0.8.9"`; `tower-http` `"0.6"` with `limit` + `catch-panic` | `agentd/Cargo.toml:16`, `agentd/Cargo.toml:25` |
| Async runtime | `tokio = "1.53"`, no `rt-multi-thread` in the daemon or the library | `agentd/Cargo.toml:35-45`, `microvms-app/Cargo.toml:33`, `microvms-edges/Cargo.toml:69` |
| AWS control plane | `reqwest = "0.13"` on `rustls`, `aws-sigv4 = "1.5"`, `aws-config = "1.10"` | `microvms-edges/Cargo.toml:53-58`, `microvms-edges/Cargo.toml:47`, `microvms-edges/Cargo.toml:36-41` |
| Wire schema | `schemars = "1.2.2"`, `default-features = false`, `derive` + `std` only | `protocol/Cargo.toml:16` |
| Money | `rust_decimal = "1.42"` with `serde-with-str` | `microvms-domain/Cargo.toml:38` |
| CLI surface | `clap = "4.6.6"` with `derive`; `ratatui = "0.30.2"` | `microvms-cli/Cargo.toml:55`, `microvms-cli/Cargo.toml:59` |
| Bindings | `pyo3 = "0.29"` with `abi3-py39`; `napi = "3"` with `napi5` + `async` + `web_stream` | `microvms-py/Cargo.toml:38`, `microvms-js/Cargo.toml:44-48` |
| Verification tiers | `stateright = "0.31"`, `turmoil = "0.7.2"`, `proptest = "1.11"` | `model/Cargo.toml:10`, `agentd/Cargo.toml:76`, `agentd/Cargo.toml:73` |
| Live suite | PEP 723 inline script under `uv`, `boto3` + `httpx` | `conformance/run_rs.py:1-5` |
| Build gate | `mise run check` — lint, security, tests, schema, manifest, Python stubs, TypeScript declarations, model drift, publishability, live wiring, build, background example, traceability | `mise.toml:417-433` |

## Module map

```mermaid
flowchart LR
  protocol[protocol wire types]
  agentd[agentd daemon]
  domain[microvms-domain rules]
  app[microvms-app use cases]
  edges[microvms-edges port impls]
  core[microvms-core composition root]
  cli[microvm binary]
  py[microvms-py PyO3]
  js[microvms-js napi]
  model[model stateright]
  conf[conformance suite]

  agentd --> protocol
  domain --> protocol
  app --> domain
  edges --> app
  core --> app
  core --> edges
  core --> domain
  core --> protocol
  cli --> core
  py --> core
  py --> protocol
  js --> core
  js --> protocol
  conf -->|drives| cli
  model -.checks.-> agentd
```

## See also

- [impact analysis](../insights/impact-analysis.md)
- [contract map](../insights/contract-map.md)
- [dependency graph](../diagrams/structural/dependency-graph.md)
- [module map](module-map.md)
- [business logic](../insights/business-logic.md)
