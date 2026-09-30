# microvms-edges

The MicroVMs client's production port implementations: the SigV4-signed control-plane
transport and build services over reqwest, the daemon's HTTP backend, the port forwarder, the
TCP tunnel and its Noise identity proof, the interactive shell, the CLI's name registry on
disk, the Bedrock token minter, the verified `agentd` fetch, tokio's clock, and the OS random
pool.

Each one implements a port that [`microvms-app`](https://crates.io/crates/microvms-app)
declares. This is the one library crate in the workspace that depends on crates doing network,
AWS, filesystem, subprocess, clock, or entropy I/O.

Most callers want [`microvms-core`](https://crates.io/crates/microvms-core) instead. It
re-exports every item here at its `microvms_core::` path and wires these implementations into
the use cases.

## License

Apache-2.0
