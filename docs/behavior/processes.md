# microvms-agentd · Processes

The daemon dispatches the routes declared in its schema, the app owns the client lifecycle,
and the CLI dispatches user commands into those capabilities. The entry points below trace
those boundaries. `crates/agentd/src/routes.rs:36` `crates/microvms-app/src/sandbox.rs:1112`
`crates/microvms-cli/src/main.rs:216`

## Build or reuse an image

Entry point: `crates/microvms-app/src/sandbox.rs:947`

1. `Sandbox::ensure_image` prepares the request and artifact on a blocking worker before obtaining build services or resolving the caller's account. `crates/microvms-app/src/sandbox.rs:959`
2. The ensure loop describes the content-addressed image and selects reuse, wait, delete, await deletion, or build from its state and the force flag. `crates/microvms-app/src/control/ensure.rs:754`
3. A ready image can return immediately; a running build is awaited, while a failed image or a forced ready image is removed before replacement. `crates/microvms-app/src/control/ensure.rs:760`
4. When the name is available, the artifact is uploaded once and `create_image` sends the request after its local preflight. `crates/microvms-app/src/control/ensure.rs:802` `crates/microvms-app/src/control/image.rs:188`
5. `wait_for_image` polls until ready, failed, or timed out; after the stall grace it probes the build list once for a pending-only build. `crates/microvms-app/src/control/image.rs:405`
6. A refused create can join the image that won a concurrent create; a successful local build retains its logging configuration in the returned image. `crates/microvms-app/src/control/ensure.rs:809`
7. The sandbox retains the resulting image for later launch and teardown; the direct `build_image` path creates and waits without the ensure loop. `crates/microvms-app/src/sandbox.rs:981` `crates/microvms-app/src/sandbox.rs:923`

### Related

- `crates/microvms-app/src/control/ensure.rs:124`
- `crates/microvms-app/src/control/image.rs:273`
- `crates/microvms-app/src/control/artifact.rs:149`
- `crates/microvms-cli/src/commands/lifecycle.rs:1424`

## Launch and wait for readiness

Entry point: `crates/microvms-app/src/sandbox.rs:1112`

1. `Sandbox::run` refuses a detached or previously launched handle, evaluates the requested network posture, and selects an explicit or previously built image. `crates/microvms-app/src/sandbox.rs:1113`
2. It prepares the launch token and optional tunnel identity, builds the run-hook payload, and copies the requested lifecycle settings into the wire request. `crates/microvms-app/src/sandbox.rs:1153`
3. The control plane checks launch connectors, resolves an image name to its ARN when needed, and sends `RunMicrovm`. `crates/microvms-app/src/sandbox.rs:1205`
4. An accepted launch records `Pending`, the image's existence, the launch windows, and the returned VM; a non-waiting call builds a session and returns at this point. `crates/microvms-app/src/sandbox.rs:1219`
5. A waiting call enters `wait_until_running`, using the launch-adoption wait when a caller-supplied client token can refer to an existing VM. `crates/microvms-app/src/sandbox.rs:1284`
6. After the service reports running, the sandbox records bootstrap completion, constructs the session if needed, and polls daemon readiness before returning it. `crates/microvms-app/src/sandbox.rs:1300`

### Related

- `crates/microvms-app/src/control/microvm.rs:433`
- `crates/microvms-app/src/control/microvm.rs:622`
- `crates/microvms-app/src/sandbox.rs:1244`
- `crates/microvms-app/src/session/mod.rs:417`

## Suspend and resume

Entry point: `crates/microvms-app/src/sandbox.rs:1585`

1. `suspend` requires a running, attached VM before sending the suspension request. `crates/microvms-app/src/sandbox.rs:1586`
2. Once the request succeeds, it records `Suspending`, then waits for `SUSPENDED` or `TERMINATED`. `crates/microvms-app/src/sandbox.rs:1605`
3. The returned state updates the local lifecycle, including the terminal marker when the VM terminated during suspension. `crates/microvms-app/src/sandbox.rs:1621`
4. `resume` rejects a terminated VM or a lifecycle other than suspended. It doesn't check the time spent suspended; the service decides. `crates/microvms-app/src/sandbox.rs:1675` `crates/microvms-app/src/sandbox.rs:1682`
5. It sends the resume request and waits for `RUNNING`, treating dead states as failures. `crates/microvms-app/src/sandbox.rs:1694` `crates/microvms-app/src/sandbox.rs:1708`
6. The running response updates the VM record and rebinds the session to its endpoint. A resume the service refuses for a VM that ended records the sandbox `Terminated`. `crates/microvms-app/src/sandbox.rs:1716` `crates/microvms-app/src/sandbox.rs:1697`

### Related

- `crates/microvms-app/src/control/microvm.rs:666`
- `crates/microvms-app/src/control/microvm.rs:794`
- `crates/microvms-app/src/session/mod.rs:375`
- `crates/microvms-app/src/sandbox.rs:1361`

## Tear down resources

Entry point: `crates/microvms-app/src/sandbox.rs:1742`

1. `terminate` returns a report even when the handle is detached; otherwise it marks teardown and releases the session. `crates/microvms-app/src/sandbox.rs:1743`
2. For a known VM, it records termination intent before calling the control plane, then records acceptance or the failure and undeleted identifier. `crates/microvms-app/src/sandbox.rs:1758`
3. If requested and the termination was accepted, it waits for `TERMINATED`; a wait failure remains in the report. `crates/microvms-app/src/sandbox.rs:1773`
4. Optional image deletion retries through the control plane and records whether deletion succeeded. `crates/microvms-app/src/sandbox.rs:1798`
5. Each image deletion attempt lists all version pages before removing extra versions, deleting the image, and checking its response state. `crates/microvms-app/src/control/image.rs:1244`
6. Optional build-log-group cleanup is reported last as undeleted because this path does not remove CloudWatch Logs groups. The returned report includes the final local lifecycle. `crates/microvms-app/src/sandbox.rs:1820`

### Related

- `crates/microvms-app/src/control/microvm.rs:861`
- `crates/microvms-app/src/control/image.rs:1214`
- `crates/microvms-app/src/sandbox.rs:479`
- `crates/microvms-cli/src/commands/lifecycle.rs:2215`

## Handle runtime lifecycle hooks

Entry point: `crates/agentd/src/routes.rs:221`

1. Route assembly maps each platform hook to its handler; the run handler records the invocation before parsing its envelope and nested payload. `crates/agentd/src/routes.rs:151` `crates/agentd/src/routes.rs:229`
2. The run handler prepares optional tunnel identity material and calls `bootstrap_with_identity` to install the launch state. `crates/agentd/src/routes.rs:266`
3. On the first accepted bootstrap, it runs configured per-VM identity repair on a blocking worker and invokes the workload's run handler before returning success. `crates/agentd/src/routes.rs:277`
4. An identical replay succeeds without running initialization again; a conflicting bootstrap returns a conflict. `crates/agentd/src/routes.rs:303`
5. The suspend, resume, and terminate routes record their invocation and await handler processing before acknowledging the hook. `crates/agentd/src/routes.rs:342` `crates/agentd/src/routes.rs:369` `crates/agentd/src/routes.rs:387`
6. For an invocation retained in the hook log, `hook_handlers::run` looks up a configured handler, serializes execution, and records its outcome; handler failure does not fail the platform hook. `crates/agentd/src/hook_handlers.rs:57`

### Related

- `crates/agentd/src/state.rs:335`
- `crates/agentd/src/hook_handlers.rs:97`
- `crates/agentd/src/routes.rs:323`
- `crates/agentd/src/routes.rs:334`

## Start and finish a guest command

Entry point: `crates/agentd/src/exec.rs:341`

1. `start` parses the request, checks its identifier and timeout, and resolves the command's execution plan. `crates/agentd/src/exec.rs:345`
2. `build_command` combines the plan's environment layers, optional working directory and user settings, process group, and requested pipes. `crates/agentd/src/exec.rs:1196`
3. A known execution identifier returns success; a new one enters `spawn`, which starts the child, captures its process group and pipes, and registers the execution. `crates/agentd/src/exec.rs:382` `crates/agentd/src/exec.rs:1291`
4. The detached waiter concurrently observes child exit and drains stdout and stderr; a timeout signals the process group and waits for the child. `crates/agentd/src/exec.rs:1339` `crates/agentd/src/exec.rs:1430`
5. Requested group reaping runs after direct-child exit; the waiter then continues draining inherited output pipes until EOF or the linger deadline. `crates/agentd/src/exec.rs:1457`
6. Completion stores the terminal marker and result, closes the daemon's stdin handle, and broadcasts the finished event. `crates/agentd/src/exec.rs:1361`

### Related

- `crates/agentd/src/exec_start.rs:390`
- `crates/agentd/src/exec.rs:1404`
- `crates/agentd/src/exec.rs:1533`
- `crates/microvms-app/src/session/mod.rs:455`

## Stream output and reconnect

Entry point: `crates/agentd/src/exec.rs:477`

1. `stream` parses the requested offset, finds the execution, and attaches to its shared output. `crates/agentd/src/exec.rs:482`
2. `Shared::attach` subscribes to live frames and snapshots retained output under the output-log lock, returning any gap and the replay cursor. `crates/agentd/src/exec.rs:303`
3. The handler reads the terminal marker after the snapshot and constructs an SSE response with keepalives. `crates/agentd/src/exec.rs:495`
4. The stream trims output already delivered and emits gap events for missing byte ranges; its terminal event reads the retained completion marker. `crates/agentd/src/exec.rs:537` `crates/agentd/src/exec.rs:557`
5. The client's stream state machine advances its cursor across delivered output and gaps, and stops after an exit event. `crates/microvms-app/src/session/exec.rs:650`
6. A retryable failure or body end without an exit event can reconnect with bounded backoff from that cursor; each attach builds fresh request headers. `crates/microvms-app/src/session/exec.rs:605` `crates/microvms-app/src/session/exec.rs:704` `crates/microvms-app/src/session/exec.rs:726`

### Related

- `crates/agentd/src/exec.rs:188`
- `crates/agentd/src/exec.rs:584`
- `crates/microvms-app/src/session/exec.rs:431`
- `crates/microvms-cli/src/commands/attached.rs:479`

## Upload and extract a directory

Entry point: `crates/agentd/src/fs.rs:1450`

1. `write_tar` parses the target path, requires an absolute extraction root, and reads the configured archive limits. `crates/agentd/src/fs.rs:1451`
2. It checks the root filesystem's available capacity before spooling the request body through the disk guard. `crates/agentd/src/fs.rs:1471`
3. A successfully spooled archive is handed to a blocking worker for extraction. `crates/agentd/src/fs.rs:1496`
4. The extractor opens the confined root and disables ownership, permission, and extended-attribute restoration from the archive. `crates/agentd/src/fs.rs:666`
5. Each member is checked against the archive limits and destination rules before its data is written through the confined filesystem access. `crates/agentd/src/fs.rs:694` `crates/agentd/src/fs.rs:821`
6. The extractor checks capacity as data lands, applies deferred modes after extraction, and returns a success or refusal through the route. `crates/agentd/src/fs.rs:841` `crates/agentd/src/fs.rs:1501`

### Related

- `crates/microvms-app/src/session/files.rs:133`
- `crates/agentd/src/fs.rs:863`
- `crates/microvms-app/src/workspace.rs:324`
- `crates/microvms-cli/src/commands/attached.rs:1470`

## Minor flows

- Daemon startup, entry at `crates/agentd/src/main.rs:14`. Initializes the runtime and image environment, starts periodic execution collection, assembles routes, and serves the listener; identity repair waits for the run hook.
- Graceful shutdown, entry at `crates/agentd/src/serve.rs:23`. Serves with shutdown handling for termination or interrupt signals.
- Build readiness and validation, entry at `crates/agentd/src/routes.rs:323`. Records build-hook invocations and acknowledges them without waiting for instance bootstrap.
- Execution poll, entry at `crates/agentd/src/exec.rs:429`. Returns the execution phase and retained outcome without changing the entry.
- Execution stdin, entry at `crates/agentd/src/exec.rs:706`. Writes requested bytes and optional EOF to an execution's stdin.
- Execution acknowledgment, entry at `crates/agentd/src/exec.rs:855`. Takes completed output and records the acknowledgment time for later collection.
- Execution kill, entry at `crates/agentd/src/exec.rs:929`. Signals the recorded process group and reports whether a signal reached it.
- Expired-execution collection, entry at `crates/agentd/src/exec.rs:975`. Removes acknowledged entries after the configured TTL.
- Process-group inspection, entry at `crates/agentd/src/exec.rs:1056`. Lists tracked executions and their process-group liveness.
- Health, entry at `crates/agentd/src/routes.rs:414`. Reports daemon, disk, bootstrap, lifecycle-hook, identity, and execution-activity state.
- Schema publication, entry at `crates/agentd/src/routes.rs:471`. Returns the schema used to assemble daemon routes.
- Single-file read, entry at `crates/agentd/src/fs.rs:1109`. Reads a file or its requested inclusive line range.
- Single-file write, entry at `crates/agentd/src/fs.rs:1199`. Writes a file with an optional parsed mode and disk-capacity checks.
- Directory download, entry at `crates/agentd/src/fs.rs:1387`. Estimates and packs the requested directory into a tar response.
- Guest TCP tunnel, entry at `crates/agentd/src/tunnel.rs:126`. Upgrades the connection and selects the plain or identity-verified relay to the requested guest port.
- Client TCP relay, entry at `crates/microvms-edges/src/session/tunnel.rs:175`. Connects a local byte stream to the guest tunnel; the verified variant at `crates/microvms-edges/src/session/tunnel.rs:196` performs the identity handshake first.
- Client HTTP forwarding, entry at `crates/microvms-edges/src/session/forward.rs:396`. Parses and forwards a local HTTP connection through the configured guest endpoint.
- Client shell session, entry at `crates/microvms-edges/src/session/shell.rs:228`. Connects the platform shell WebSocket and drives input, output, and resize events.
- Control-plane retry, entry at `crates/microvms-app/src/control/transport.rs:290`. Retries failures classified as retryable with jittered exponential backoff.
- Session request, entry at `crates/microvms-app/src/session/mod.rs:136`. Prepares request headers, applies the request timeout, and calls the HTTP backend.
- Readiness polling, entry at `crates/microvms-app/src/session/mod.rs:417`. Polls daemon health until bootstrap is reported, a fatal failure occurs, or the deadline expires.
- Split output readers, entry at `crates/microvms-app/src/session/split.rs:157`. Creates stdout and stderr channels driven by a shared stream task with the selected gap policy.
- Wait and acknowledge, entry at `crates/microvms-app/src/session/exec.rs:823`. Waits for completion and returns the acknowledgment result that owns the output.
- Run to completion, entry at `crates/microvms-app/src/session/complete.rs:263`. Coordinates command execution, output delivery, completion, and cancellation policy.
- Keep awake, entry at `crates/microvms-app/src/session/keepalive.rs:188`. Runs periodic probes until stopped or its failure policy ends the loop.
- Adopt a VM, entry at `crates/microvms-app/src/sandbox.rs:1361`. Reads the service's VM state and rebuilds a lifecycle handle for an existing launch.
- Detach for handoff, entry at `crates/microvms-app/src/sandbox.rs:1500`. Returns the existing VM connection record and makes the current sandbox inert without sending a lifecycle request.
- Workspace synchronization, entry at `crates/microvms-app/src/workspace.rs:301`. Compares manifests, uploads changed paths, removes eligible deleted paths, and writes the new manifest.
- Download selected artifacts, entry at `crates/microvms-app/src/workspace.rs:425`. Downloads the remote tar and asks the local-tree adapter to extract selected files.
- Agent launch and prompt, entry at `crates/microvms-app/src/agents/mod.rs:901`. Uses the sandbox lifecycle, then exposes provisioning and command execution through the agent recipe.
- Daemon release fetch, entry at `crates/microvms-app/src/provision.rs:158`. Fetches the release artifact and applies the release-verification policy before returning its bytes.
- CLI dispatch, entry at `crates/microvms-cli/src/main.rs:81`. Parses arguments, dispatches the command, and renders the result with its exit status.
- CLI run, entry at `crates/microvms-cli/src/commands/lifecycle.rs:435`. Composes build or reuse, launch, optional workspace transfer, execution, and cleanup.
- CLI quickstart, entry at `crates/microvms-cli/src/commands/lifecycle.rs:950`. Resolves quickstart settings and delegates to the run workflow.
- CLI build, entry at `crates/microvms-cli/src/commands/lifecycle.rs:1424`. Prepares an image without launching a VM.
- CLI attached lifecycle, entry at `crates/microvms-cli/src/commands/lifecycle.rs:2013`. Adapts suspension for an existing VM; resume, readiness wait, and termination are sibling handlers at `crates/microvms-cli/src/commands/lifecycle.rs:2094`, `crates/microvms-cli/src/commands/lifecycle.rs:2170`, and `crates/microvms-cli/src/commands/lifecycle.rs:2215`.
- CLI execution, entry at `crates/microvms-cli/src/commands/attached.rs:243`. Starts, polls, or streams a command through a session; sibling handlers provide acknowledgment at `crates/microvms-cli/src/commands/attached.rs:1120`, kill at `crates/microvms-cli/src/commands/attached.rs:1161`, process inspection at `crates/microvms-cli/src/commands/attached.rs:1213`, and stdin at `crates/microvms-cli/src/commands/attached.rs:1304`.
- CLI health and keepalive, entry at `crates/microvms-cli/src/commands/attached.rs:915`. Renders daemon health; the keepalive handler at `crates/microvms-cli/src/commands/attached.rs:806` runs repeated probes through the shared policy.
- CLI files and synchronization, entry at `crates/microvms-cli/src/commands/attached.rs:1470`. Resolves copy direction and uses session file operations; sibling handlers expose existence at `crates/microvms-cli/src/commands/attached.rs:1622` and incremental synchronization at `crates/microvms-cli/src/commands/attached.rs:1781`.
- CLI attachment, entry at `crates/microvms-cli/src/commands/attached.rs:1970`. Imports or resolves a name record and attaches it to a live VM.
- CLI durable adoption, entry at `crates/microvms-cli/src/commands/attached.rs:124`. Reconstructs an existing VM's lifecycle handle from its record.
- CLI HTTP forwarding, entry at `crates/microvms-cli/src/commands/attached.rs:2316`. Binds a local listener and forwards connections to a guest port.
- CLI TCP tunnel, entry at `crates/microvms-cli/src/commands/attached.rs:2462`. Binds a local listener and relays connections through the guest TCP tunnel.
- CLI shell, entry at `crates/microvms-cli/src/commands/attached.rs:2750`. Resolves the VM and opens its platform shell session.
- CLI agent workflows, entry at `crates/microvms-cli/src/commands/agent.rs:235`. Creates or refreshes a named agent VM; the prompt handler at `crates/microvms-cli/src/commands/agent.rs:764` submits a task to an installed agent.
- CLI local records, entry at `crates/microvms-cli/src/commands/local.rs:52`. Lists outstanding runs; sibling handlers render lifecycle history at `crates/microvms-cli/src/commands/local.rs:547` and manage names at `crates/microvms-cli/src/commands/local.rs:633`.
- CLI build logs, entry at `crates/microvms-cli/src/commands/local.rs:778`. Reports the log group and a command for reading it.
- CLI image operations, entry at `crates/microvms-cli/src/commands/image.rs:18`. Lists image versions; sibling handlers change version availability at `crates/microvms-cli/src/commands/image.rs:50` and list builds at `crates/microvms-cli/src/commands/image.rs:83`.
- CLI diagnostics and estimates, entry at `crates/microvms-cli/src/commands/doctor.rs:33`. Checks local and service prerequisites; the cost handler at `crates/microvms-cli/src/commands/cost.rs:29` and egress-posture handler at `crates/microvms-cli/src/commands/local.rs:862` render their reports.
- CLI contract output, entry at `crates/microvms-cli/src/commands/local.rs:931`. Emits the command manifest; sibling handlers emit constants at `crates/microvms-cli/src/commands/local.rs:954` and a Dockerfile at `crates/microvms-cli/src/commands/local.rs:992`.

## See also

These pages cite many of the same source files as this one.

- [contract map](../insights/contract-map.md)
- [module map](../architecture/module-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [business logic](../insights/business-logic.md)
- [debugging guide](../insights/debugging-guide.md)
