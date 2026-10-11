# Harness capabilities

Use the Rust client or the Python and Node bindings to add MicroVM execution to
an agent harness. The shared client handles image preparation, launch, execution,
file transfer, and teardown. Start with [Embedding](EMBEDDING.md) for the image
and client contracts, or the [SDK tutorial](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/)
for runnable examples.

## Prepare and launch an environment

`Sandbox::ensure_image` builds or reuses an image from its content-addressed
inputs. `Sandbox::run` launches it and records the accepted VM. Readiness waiting
is controlled by the request; a nonwaiting launch does not establish that the
daemon can accept work yet. The production constructors are available through
`microvms_core::prelude`.

The shared implementation is in `crates/microvms-app/src/sandbox.rs` and
`crates/microvms-app/src/control/ensure.rs`. The Python and Node request types
convert to these same application requests.

For coding-agent workflows, `AgentImageSpec` describes the image and
`AgentVm` composes launch and task execution. These helpers are defined in
`crates/microvms-app/src/agents/mod.rs`. A harness-specific provider belongs in
the consuming project; this repository supplies the reusable client and daemon.

## Run and observe commands

The execution protocol supports shell commands or argv, a working directory,
per-command environment, user and group, stdin, and an optional daemon timeout.
Use a stable execution identifier when retrying the same start request.
`Session::run` returns a handle that can poll, stream, write stdin, kill, wait,
and acknowledge the result. `Session::run_to_completion` combines completion
handling into a result for callers that do not need to manage the handle.

Execution records and buffered output belong to the daemon. A fresh session can
recover a handle by its identifier; rotating the endpoint credential does not
itself discard the record. Streaming uses byte offsets and explicit gap events.
A consumer must handle a gap when requested output is outside the retained
window. Acknowledge completed results when the harness no longer needs them.

See `crates/protocol/src/exec.rs`, `crates/microvms-app/src/session/mod.rs`, and
`crates/microvms-app/src/session/exec.rs`. [Protocol](PROTOCOL.md) defines the
wire contract and [the execution reference](reference/rpc-tools.md) links to
the daemon handlers.

## Transfer inputs and results

Use single-file transfer for individual artifacts and tar transfer for directory
trees. File reads support line ranges; file writes accept a creation mode.
The daemon checks available disk space before writes and reports its disk state
through health. The client also provides workspace synchronization over the
file APIs.

These capabilities are implemented in `crates/microvms-app/src/session/files.rs`,
`crates/microvms-app/src/workspace.rs`, and `crates/agentd/src/fs.rs`.

## Keep a long-running environment available

Endpoint credentials and VM lifetime are separate concerns. Refresh endpoint
credentials when attaching again. For idle prevention, send requests through
the endpoint from outside the VM: a guest request to its own daemon does not
traverse the platform proxy. Health reports `busy` for active execution and
`execs` for registered execution records, including retained results.

Suspend preserves the VM state described in [Suspend and resume](SUSPEND-RESUME.md).
Respect the configured idle and maximum lifetime limits, and save results before
termination. Teardown returns a report containing identifiers that could not be
confirmed deleted; retain that report for cleanup rather than assuming success.

## Choose the integration boundary

The CLI publishes its command, option, response, and exit-code contracts through
`microvm manifest`. Under JSON output, ordinary commands return an envelope;
streaming execution returns event lines followed by its final envelope. Read
the manifest's alternate response when building a command consumer.

Use [Embedding](EMBEDDING.md) to build a custom integration, the SDK for
application code, and the CLI for shell workflows.
Neither the client nor a guest proxy variable establishes internet isolation.
Use the network topology described in [Networking](NETWORKING.md), and review
[Trust](TRUST.md) before selecting workload credentials and permissions.

Snapshot cloning, a replacement platform API, and provider classes tied to
another framework are outside this repository's integration contract. Check
[Platform](PLATFORM.md) for dated service observations. This page adds no new
AWS measurement.
