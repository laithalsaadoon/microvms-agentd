# microvms-agentd · Debugging guide

Start with the reported error class and the command's structured result. The CLI maps
library errors to stable exit codes; daemon status details travel separately as `data.kind`.
This distinction separates a failed workload from a failed platform operation.
`crates/microvms-cli/src/exit.rs:79-102`, `crates/microvms-cli/src/exit.rs:318-327`

## Failure-mode index

| Symptom | Likely surface | First check | Citation |
| --- | --- | --- | --- |
| `ERR_CONFIG` before a run starts | The project configuration could not be loaded or validated | Run `microvm doctor` and read its configuration result before investigating AWS | `crates/microvms-cli/src/exit.rs:114-125`, `crates/microvms-cli/src/commands/doctor.rs:39-49` |
| `doctor` reports `ok: false` inside a success envelope | A prerequisite failed; the diagnostic command itself completed | Read each entry in `data.checks` and the process exit status; failed required checks produce `ERR_PRECONDITION` | `crates/microvms-cli/src/commands/doctor.rs:82-103` |
| Daemon startup rejects a binary, or an image never answers its ready hook | The daemon binary may target the host architecture | Run `microvm doctor --binary <path>` and inspect `daemon-binary`; it checks the ELF machine against AArch64 | `crates/microvms-cli/src/commands/doctor.rs:519-536`, `crates/microvms-app/src/control/image.rs:349-358` |
| `ERR_BUILD_WEDGED` while an image remains in `CREATING` | The stall probe found the client-token replay signature | Inspect the first image version's build states. The diagnosis requires a nonempty list with every build still `PENDING`; an unreadable or empty list does not establish it | `crates/microvms-app/src/control/image.rs:464-500` |
| An image reports a failed state | The failed version or its builds may carry the cause | Read the reported `stateReason` details and the build log group; the client gathers version and build reasons | `crates/microvms-app/src/control/image.rs:600-620`, `crates/microvms-app/src/control/image.rs:637-667` |
| `ERR_LAUNCH_DIED` while waiting for a lifecycle state | The service reported a state in the wait's failure set | Read the state and `stateReason` included in the error before treating it as a connection problem | `crates/microvms-app/src/control/microvm.rs:675-681`, `crates/microvms-app/src/control/microvm.rs:698-714` |
| `ERR_WINDOW_CLOSED` on resume | The service refused the resume, and `GetMicrovm` reports the VM gone: it ended first, and nothing brings it back | Read the state and `stateReason` in the error before you launch a new VM. A VM ended with `microvm terminate` gets the same refusal as one the platform ended, so the error can't say which ended it. The idle policy's `suspendedDurationSeconds` ends a VM it suspended, and `maximumDurationInSeconds` ends any VM; an explicit suspend could still resume past the window (measured 2026-10-12, us-east-1) | `crates/microvms-app/src/control/microvm.rs:819-852`, `crates/microvms-app/src/sandbox.rs:1694-1700` |
| Control requests return 503 before bootstrap | The daemon has no installed run token yet | Read `bootstrapped` from health and distinguish `NotBootstrapped` from a permanent refusal | `crates/agentd/src/auth.rs:68-80`, `crates/protocol/src/health.rs:20`, `crates/microvms-domain/src/error.rs:384-388` |
| A write returns 507 | The destination filesystem is below its configured reserve | Read the available and reserved byte values in the response, then inspect `health.disk`; missing disk data means unmeasurable | `crates/agentd/src/fs.rs:97-123`, `crates/protocol/src/health.rs:21-34` |
| `ERR_EXEC_FAILED` | The workload completed with a nonzero exit status | Inspect the workload's stdout, stderr, exit code, and signal before investigating infrastructure | `crates/microvms-cli/src/exit.rs:97-102`, `crates/protocol/src/exec.rs:56-68` |
| An exec reports `timed_out: true` | The daemon execution deadline expired | Inspect `timeout_sec` and the result note; this deadline signals the process group | `crates/protocol/src/exec.rs:63-66`, `crates/microvms-app/src/session/exec.rs:190-195` |
| A wait reports `ERR_TIMEOUT` | A client-side deadline elapsed | Check whether the error is the client wait deadline before assuming the exec stopped; `ExecTimeout` leaves the VM and exec untouched | `crates/microvms-domain/src/error.rs:397-402` |
| An exec reports `truncated: true` | A captured stream reached the daemon's output cap | Compare expected output volume with `AGENTD_MAX_OUTPUT_BYTES`; the default is 8 MiB per stream | `crates/protocol/src/exec.rs:69-72`, `crates/agentd/src/config.rs:25-27`, `crates/agentd/src/config.rs:107`, `crates/agentd/src/config.rs:148-149` |
| An exec reports `writers_may_be_alive: true` | A descendant kept an output pipe open after the direct child exited | Check background processes and `AGENTD_OUTPUT_LINGER_SECS`; the flag means the linger deadline expired before EOF | `crates/protocol/src/exec.rs:73-77`, `crates/agentd/src/exec.rs:1460-1477`, `crates/agentd/src/config.rs:151-152` |
| The output stream emits `gap` | Replay bytes were evicted or a live subscriber lagged | Record `from` and `to`, then reattach from the last received offset if useful. Output already outside the replay window cannot be recovered through the stream | `crates/protocol/src/exec.rs:339-344`, `crates/agentd/src/exec.rs:182-189`, `crates/agentd/src/exec.rs:641-653` |
| A stdin write returns 409 | The exec was started without a writable stdin pipe | Enable `stdin: true` when starting a new exec | `crates/agentd/src/exec.rs:724-731` |
| A stdin write returns 410 | The pipe closed after EOF or the child exited | Treat that input stream as closed; retrying the same write cannot reopen it | `crates/agentd/src/exec.rs:772-781` |
| A stdin write returns 408 | The child did not drain the pipe within the write timeout | Reconcile possible partial delivery before retrying; the response explicitly warns that some bytes may have been written | `crates/agentd/src/exec.rs:815-825` |
| A Node error's `code` is `GenericFailure` | The asynchronous binding path uses the host runtime's status code | Read `err.cause.message` for `ERR_*` and `err.cause.cause.message` for the wire kind; use `isRetryable` for the retry verdict | `bindings/microvms-js/src/errors.rs:15-54`, `bindings/microvms-js/src/errors.rs:170-179` |
| `microvm ls` still lists outstanding resources after interruption | The local run ledger retains identifiers until deletion is recorded | Inspect `leaked`; plain `ls` reads local state, while `ls --remote` reconciles it with service listings | `crates/microvms-cli/src/ledger.rs:11-22`, `crates/microvms-cli/src/ledger.rs:46-48`, `crates/microvms-cli/src/commands/local.rs:47-68` |

## Log and error surfaces

| Surface | Where it emits | What to grep for | Citation |
| --- | --- | --- | --- |
| Daemon logging | JSON on stdout, with targets; `AGENTD_LOG` selects the filter and defaults to `info` | `agentd listening`, `addr`, `version` | `crates/agentd/src/main.rs:83-85`, `crates/agentd/src/main.rs:90-102` |
| Configuration warnings | The daemon log | `ignoring unparseable configuration value`, with `key` and `raw`; the daemon retains the default | `crates/agentd/src/config.rs:135-139`, `crates/agentd/src/config.rs:199-206` |
| Exec diagnostics | The daemon log | `exec stream subscriber lagged`, `wait on exec child failed`, `exec exceeded its timeout and its process group was signalled` | `crates/agentd/src/exec.rs:648`, `crates/agentd/src/exec.rs:1485-1493` |
| Disk-pressure diagnostics | The daemon log and the 507 response body | `refusing a write`, `path`, `available`, `reserve` | `crates/agentd/src/fs.rs:106-121` |
| Shutdown diagnostics | The daemon log | `SIGTERM received; draining`, `interrupt received; draining`, `cannot listen for SIGTERM` | `crates/agentd/src/serve.rs:127-139` |
| Build log locator | `microvm logs <image-name>` returns the group and an AWS CLI tail command; it does not fetch log events | `data.logGroup`, `data.tailCommand`, `data.tailRequires`; `data.lines` is null | `crates/microvms-cli/src/commands/local.rs:778-804` |
| Daemon health | The health response | `version`, `bootstrapped`, `disk`, `busy`, `execs`; `busy` tracks unfinished execs rather than results awaiting acknowledgment | `crates/protocol/src/health.rs:19-34`, `crates/protocol/src/health.rs:63-88` |
| CLI failure envelope | Structured command output; normal progress is separate from stdout | `code`, `error`, `finding`, `suggestions`, `data.kind` | `crates/microvms-cli/src/envelope.rs:20-31`, `crates/microvms-cli/src/main.rs:22-24` |
| CLI streamed exec | NDJSON on stdout with a compact terminal envelope | Event records followed by the `microvm.exec.stream` envelope; do not parse it as one JSON document | `crates/microvms-cli/src/envelope.rs:33-49` |
| Python exception | The raised exception instance | `code`, `wire_kind`, `retryable` | `bindings/microvms-py/src/errors.rs:151-166` |
| Node exception | The error cause chain | `cause.message` and `cause.cause.message` | `bindings/microvms-js/src/errors.rs:35-44` |
| Local run ledger | A JSON record read by `microvm ls` | `runId`, `region`, `imageIdentifier`, `microvmId`, `leaked` | `crates/microvms-cli/src/ledger.rs:35-48`, `crates/microvms-cli/src/commands/local.rs:52-58` |

## First-checks ladder

1. **Preserve the structured error and exit status.** Read the coarse `ERR_*` class, then
   `data.kind`, `finding`, and `suggestions`. Retryability comes from the error class, not a
   search for words in its message. `crates/microvms-cli/src/envelope.rs:20-31`, `crates/microvms-domain/src/error.rs:174-179`
2. **Separate workload failure from infrastructure failure.** An `ERR_EXEC_FAILED` means
   the workload exited nonzero. Inspect its exit code, signal, stdout, and stderr before
   changing deployment settings. `crates/microvms-cli/src/exit.rs:97-102`, `crates/protocol/src/exec.rs:56-68`
3. **Inspect local leftovers with `microvm ls`.** Preserve the run identifiers and `leaked`
   entries. Plain `ls` reads the local ledger; adding `--remote` performs service listings.
   `crates/microvms-cli/src/commands/local.rs:47-68`, `crates/microvms-cli/src/ledger.rs:46-48`
4. **Run `microvm doctor --binary <path>` for setup problems.** Its diagnosis starts with
   project configuration, then resolves the region and credentials, checks infrastructure,
   reads managed bases, and inspects the binary. It includes service reads, so it is not a
   purely local command. `crates/microvms-cli/src/commands/doctor.rs:39-80`
5. **Read health and the daemon log for guest problems.** Check `bootstrapped`, disk data,
   and whether execs are still busy, then correlate the JSON log with the error.
   `crates/protocol/src/health.rs:20-34`, `crates/protocol/src/health.rs:63-88`, `crates/agentd/src/main.rs:90-102`
6. **Use the build's own evidence for image failures.** Read failed-version and build
   reasons; use `microvm logs <image-name>` to obtain the tail command. An unreadable list
   cannot establish the stalled-build diagnosis. `crates/microvms-app/src/control/image.rs:464-479`, `crates/microvms-app/src/control/image.rs:637-667`, `crates/microvms-cli/src/commands/local.rs:778-804`
7. **Reproduce suspected repository regressions with the relevant local tests.** The broad
   `mise run check` task creates no AWS resources; documentation, formal verification,
   bindings test suites, and live verification have separate commands. A green local run
   does not establish current AWS behavior. `AGENTS.md:19-27`, `AGENTS.md:41-42`, `.config/mise/tasks/check.toml:1-2`

## Known incident patterns

No tagged incident history found in source. The following patterns are recorded in source
comments beside the behavior they explain.

- **A stall diagnosis without evidence:** a failed build listing or an empty result is not
  proof of a replayed create. Signal: a slow build without a complete nonempty list of
  pending builds. Mitigation: keep the distinction between an unknown state and the
  specific wedge signature. `crates/microvms-app/src/control/image.rs:451-479`
- **A configuration override that never applied:** an unparseable environment value leaves
  the default in place. Signal: an `ignoring unparseable configuration value` warning.
  Mitigation: inspect the logged key and value before tuning timeouts or limits again.
  `crates/agentd/src/config.rs:135-139`, `crates/agentd/src/config.rs:199-206`
- **A simulator deadline racing a real child:** simulated time advances independently of a
  subprocess's wall clock. Signal: test-only missing output and `writers_may_be_alive`.
  Mitigation: synchronize the child with stdin reads, or give its real pipe drain an
  appropriate test deadline. `crates/agentd/tests/turmoil_transport.rs:52-76`
- **A malformed stream consumer:** treating streamed exec output as a single document
  fails even when the command succeeds. Signal: a parser error after valid event records.
  Mitigation: consume NDJSON events and the compact terminal envelope separately.
  `crates/microvms-cli/src/envelope.rs:33-49`
- **A misleading server compiler error:** a generic listener without `L::Addr: Debug` can
  make `Serve` appear not to implement `Future`. Signal: that error while modifying the
  generic serving path. Mitigation: preserve the listener address bound.
  `crates/agentd/src/serve.rs:14-26`

## See also

- [contract map](contract-map.md): 13 shared source citations
- [impact analysis](impact-analysis.md): 13 shared source citations
- [processes](../behavior/processes.md): 11 shared source citations
- [module map](../architecture/module-map.md): 10 shared source citations
- [business logic](business-logic.md): 7 shared source citations
