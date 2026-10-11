# microvms-agentd · Risk hotspots

Risk here means maintenance attention suggested by recent changes. This is a dated measurement at commit `9729b82`, taken on 2026-10-11, using the UTC interval from 2026-09-11 inclusive to 2026-10-11 exclusive. The cohort is current implementation source under crates, bindings, tools, conformance, and the site's source and scripts; tests, fixtures, examples, generated declarations, and generated site content are excluded. Each changed path contributes its commit count. The median is 2 and the population standard deviation is 3.1185: `↑ rising` means at least 6 commits, `→ flat` means 1–5, and no touched path meets `↓ falling`. A rising path scores 1; ties use descending commit count, then path order.

No current per-file diagnostic dataset supports warning or error totals, so this ranking uses activity alone and omits an open-findings column. The arrows compare paths within this window; they do not measure acceleration over time. Counts use current path spellings, so moves and broad refactors can affect the ranking. “Top owner” names a suggested review role, and the drill-down describes recorded contribution concentration without assigning maintainership to an author. The source citations identify what each path does; they do not establish test coverage or runtime reliability.

| File | Trend | Top owner | Citation |
| --- | --- | --- | --- |
| `conformance/run_rs.py` | ↑ rising | Conformance maintainers | `conformance/run_rs.py:94` |
| `crates/microvms-cli/src/cli.rs` | ↑ rising | CLI interface maintainers | `crates/microvms-cli/src/cli.rs:65` |
| `crates/microvms-cli/src/commands/lifecycle.rs` | ↑ rising | CLI lifecycle maintainers | `crates/microvms-cli/src/commands/lifecycle.rs:475` |
| `bindings/microvms-py/src/session.rs` | ↑ rising | Python binding maintainers | `bindings/microvms-py/src/session.rs:534` |
| `crates/microvms-cli/src/commands/attached.rs` | ↑ rising | CLI session maintainers | `crates/microvms-cli/src/commands/attached.rs:243` |
| `tools/ratchet.py` | ↑ rising | Architecture gate maintainers | `tools/ratchet.py:875` |
| `crates/microvms-cli/src/commands/mod.rs` | ↑ rising | CLI output maintainers | `crates/microvms-cli/src/commands/mod.rs:47` |
| `bindings/microvms-py/src/sandbox.rs` | ↑ rising | Python binding maintainers | `bindings/microvms-py/src/sandbox.rs:512` |
| `conformance/lanes/suite.py` | ↑ rising | Conformance maintainers | `conformance/lanes/suite.py:82` |
| `crates/microvms-app/src/sandbox.rs` | ↑ rising | Sandbox lifecycle maintainers | `crates/microvms-app/src/sandbox.rs:620` |
| `bindings/microvms-js/src/sandbox.rs` | ↑ rising | Node binding maintainers | `bindings/microvms-js/src/sandbox.rs:668` |
| `bindings/microvms-py/src/agents.rs` | ↑ rising | Python binding maintainers | `bindings/microvms-py/src/agents.rs:464` |

## Per-file drill-down

### 1. `conformance/run_rs.py`

**What's there.** The conformance entry point parses command-line options and selects either the offline self-test or the live suite (`conformance/run_rs.py:94-128`). The offline suite constructs a stub CLI and exercises the harness and lane helpers (`conformance/selftest/suite.py:40-71`).

**Recent activity.** 28 commits; `↑ rising`, in the dated window above. Activity in the current entry-point path can include behavior that later moved into the harness and lanes; its current dispatch is small (`conformance/run_rs.py:90-128`).

**Review ownership.** Conformance maintainers should review changes with the affected lane owner. Recorded contributions are concentrated: the leading author's share is 64%, with the remainder at 36% in this window.

**Review focus.** Preserve the explicit choice between the self-test and the live suite when changing argument handling (`conformance/run_rs.py:125-128`). Follow a change into the selected suite rather than treating the entry point's activity as evidence that its present body owns the behavior (`conformance/run_rs.py:90-91`).

### 2. `crates/microvms-cli/src/cli.rs`

**What's there.** `Cli` defines the command selection and global JSON, dense, and quiet output flags (`crates/microvms-cli/src/cli.rs:65-83`). The command enum starts with the run, quickstart, and build workflows (`crates/microvms-cli/src/cli.rs:97-120`).

**Recent activity.** 21 commits; `↑ rising`, in the dated window above.

**Review ownership.** CLI interface maintainers should coordinate with the handler's review area. Recorded contributions are concentrated: the leading author's share is 90%, with the remainder at 10%.

**Review focus.** Treat changes to accepted arguments as interface changes. The parser's size-request test checks both valid combinations and conflicts with the older memory option (`crates/microvms-cli/src/cli.rs:3843-3868`); the duration test separately checks the accepted domain for seconds-valued flags (`crates/microvms-cli/src/cli.rs:3872-3887`). These are source-level review anchors, not a claim that either test ran for this measurement.

### 3. `crates/microvms-cli/src/commands/lifecycle.rs`

**What's there.** The lifecycle adapter merges run configuration into effective arguments and a report of their sources (`crates/microvms-cli/src/commands/lifecycle.rs:150-180`). Its run path owns a sandbox across the launch/interrupt race, then reads identifiers and performs teardown (`crates/microvms-cli/src/commands/lifecycle.rs:665-731`, `crates/microvms-cli/src/commands/lifecycle.rs:774-786`).

**Recent activity.** 15 commits; `↑ rising`, in the dated window above.

**Review ownership.** CLI lifecycle maintainers should review changes alongside the sandbox lifecycle implementation (`crates/microvms-app/src/sandbox.rs:1105`, `crates/microvms-app/src/sandbox.rs:1772`). The recorded activity is concentrated in one author during this window.

**Review focus.** Preserve the sandbox's ownership outside the cancellable launch future so the following code can read its recorded identifiers (`crates/microvms-cli/src/commands/lifecycle.rs:670-731`). Review result reporting and ledger updates together with teardown, because this adapter reads all of them after the race (`crates/microvms-cli/src/commands/lifecycle.rs:731-786`).

### 4. `bindings/microvms-py/src/session.rs`

**What's there.** `PySession` wraps either an owned session or a session reached through a shared sandbox, and a lease holds the latter's mutex during an operation (`bindings/microvms-py/src/session.rs:451-489`, `bindings/microvms-py/src/session.rs:532-548`). The blocking and awaitable health methods drive the same operation future through different runtime entry points (`bindings/microvms-py/src/session.rs:568-572`, `bindings/microvms-py/src/session.rs:1001-1007`).

**Recent activity.** 12 commits; `↑ rising`, in the dated window above.

**Review ownership.** Python binding maintainers should coordinate with session and runtime maintainers. Recorded contributions are concentrated: the leading author's share is 92%, with the remainder at 8%.

**Review focus.** Keep operation construction shared between blocking and awaitable methods, and preserve the lease's scope across awaited work (`bindings/microvms-py/src/session.rs:513-528`). The binding's async suite documents separate expectations for signature parity, wire behavior, event-loop progress, and cancellation (`bindings/microvms-py/tests/test_async.py:2-18`); use those categories when reviewing a new method.

### 5. `crates/microvms-cli/src/commands/attached.rs`

**What's there.** The attached exec handler obtains a session, branches to polling when requested, and builds new exec requests through the lifecycle adapter's request constructor (`crates/microvms-cli/src/commands/attached.rs:243-279`). Streaming execution translates events for output and records terminal information while consuming the stream (`crates/microvms-cli/src/commands/attached.rs:479-517`).

**Recent activity.** 12 commits; `↑ rising`, in the dated window above. Its tie with the Python session adapter is resolved by path order.

**Review ownership.** CLI session maintainers should coordinate with output maintainers for streaming changes. The recorded activity is concentrated in one author during this window.

**Review focus.** Review polling, new execution, and streaming as distinct paths: polling returns before request construction (`crates/microvms-cli/src/commands/attached.rs:249-275`). Streaming emits events while it runs, so changes to completion and error handling also need to preserve the already-emitted output contract (`crates/microvms-cli/src/commands/attached.rs:497-517`).

## See also

- [module map](../architecture/module-map.md) — 12 shared source citations
- [contract map](../insights/contract-map.md) — 8 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 6 shared source citations
- [dead code](dead-code.md) — 4 shared source citations
- [data flow](../architecture/data-flow.md) — 3 shared source citations
