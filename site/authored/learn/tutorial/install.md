---
title: Install the CLI or an SDK
description: Get the prebuilt microvm CLI or install the Python, Node.js, and Rust SDKs.
editUrl: false
sidebar:
  order: 1
---

The `microvm` CLI runs agents and commands in sandboxed AWS Lambda MicroVMs.
Install it first to prepare an image, run a coding agent, or try the SDKs.

## CLI: use a prebuilt binary

Download the archive for your machine from the
[latest release](https://github.com/laithalsaadoon/microvms-agentd/releases/latest),
extract it, and put `microvm` (`microvm.exe` on Windows) on your `PATH`.
Releases cover Linux x64/ARM64, macOS Intel/Apple silicon, and Windows x64.
No Rust compiler is required.

If [cargo-binstall](https://github.com/cargo-bins/cargo-binstall) is already
installed, it selects and installs the release for you:

```bash
cargo binstall microvms-cli --no-confirm
microvm --version
```

With a Rust toolchain, compiling from crates.io is another option:

```bash
cargo install microvms-cli --locked
```

`microvm --version` works without AWS credentials. To see the installed
command surface, use `microvm --help`; `microvm manifest` emits it as JSON.

Next: [configure AWS and start an agent](/learn/tutorial/first-run/).

## SDKs

Choose your language:

| Language | Install | Runtime |
| --- | --- | --- |
| Python | `pip install microvms` | Python 3.9+ |
| Node.js / TypeScript | `npm install @theagenticguy/microvms` | Node.js 22.13+ |
| Rust | `cargo add microvms-core` | Rust toolchain |

[Run sandboxed tools from your application](/learn/tutorial/from-code/)
has complete examples, including cleanup. The SDKs connect to the same
AWS service as the CLI.

## The guest daemon is automatic

`agent-up`, `run`, `build`, and `quickstart` download the matching `agentd`
release binary when needed and cache it locally. Keep `gh` or `curl` on
your `PATH`. A successful `gh` download uses `gh attestation verify` for
provenance; the `curl` fallback verifies the release's SHA256 checksum.
The guest daemon is a static ARM64 Linux binary on every host platform.

For a daemon you build or manage yourself, set `MICROVM_AGENTD` to its
path, or pass the path as the positional argument to `agent-up`, `run`,
or `build`. A `MICROVM_AGENTD` binary that is not an ARM64 ELF is refused
before any build. `microvm doctor --binary ./agentd` checks a positional
binary's architecture.

## Build the CLI from this repository

From a clone with Rust installed:

```bash
cargo install --path microvms-cli --locked
```

This builds the host CLI. It can still download the guest daemon
automatically. Repository contributors can use `mise install` and
`mise run install` to install the pinned tools and Git hooks.
