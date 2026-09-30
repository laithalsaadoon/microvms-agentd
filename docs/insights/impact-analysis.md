# microvms-agentd · Impact analysis

A **high-impact surface** here is a definition with a high inbound reference count measured across
the whole tree — all the Rust crates, the Python conformance driver, the Python gate scripts,
the CI workflow, and the committed artifacts. Reference count rather than public-export count,
because this workspace's public API is small and its couplings are wide.

Two properties of the tree make reference count the right criterion, and the same two explain why a
`cargo`-only import graph understates the blast radius:

- **Some surfaces reach a consumer through string matching rather than through the type
  system.** `constants::as_json()` is read by a Python script that looks its keys up by name
  (`crates/microvms-domain/src/constants.rs:40`); `pinned_rates()`'s decimal literals are parsed out of the
  Rust source by another Python script (`tools/check-live-rates.py:148`); the conformance oracle
  asserts on `WireKind`'s rendered strings (`conformance/run_rs.py:190`). When one of those couplings
  changes, compilation still succeeds and the corresponding check silently stops comparing.
- **Some surfaces have a generated artifact downstream.** `docs/schema.json` is generated from the
  protocol types and byte-compared (`crates/agentd/tests/schema_artifact.rs:39`), and the CLI manifest is
  generated from the exit table and the clap tree (`crates/microvms-cli/src/manifest.rs:34`).

Each surface below carries a **Gate** line naming the check that catches a breaking change to it,
because the gate is what decides whether a mistake is caught in 45 seconds or in production. A
surface with no gate is called out as such.

The `Touch on change` column answers whether a consumer needs an edit, or at minimum a deliberate
read, in the same commit. `yes` means an edit is required. `likely` means it compiles but a reviewer
has to look. `no` means only a behavioral change reaches it.

Where a surface has more consumers than are useful to list, the top rows are the ones with the
highest reference count and a `(further …)` row summarizes the rest. The per-file reference counts
in the tables count the lines that name the symbol, as `git grep -c` does, and were measured at
`9c462f0` (2026-08-26).

## Protocol wire types

Defined at: `crates/protocol/src/lib.rs:29`-`:32` (the module list), with the shapes in
`crates/protocol/src/exec.rs:24` (`Phase`), `crates/protocol/src/fs.rs:18` (`FsQuery`),
`crates/protocol/src/health.rs:11` (`Health`), `crates/protocol/src/hook.rs:46` (`RunHook`), and the published
constants at `crates/protocol/src/lib.rs:58` (`PROTOCOL_VERSION`) and `:66` (`VERSION_HEADER`).

Gate: a shape change is a **compile error in every crate that declares the dependency, by design** (`crates/microvms-core/Cargo.toml:29`
states that a field renamed in `crates/protocol/` must break core's build), and the generated document is
byte-compared by `crates/agentd/tests/schema_artifact.rs:39`, wired into the unconditional local gate as
`.config/mise/tasks/contracts.toml:51 ["schema:check"]`.

The crates that declare the dependency:
`crates/agentd/Cargo.toml:15`, `crates/microvms-core/Cargo.toml:29`, `bindings/microvms-js/Cargo.toml:25`,
`bindings/microvms-py/Cargo.toml:32`. `microvms-cli` deliberately declares none — it names the wire types
through core's re-export (`crates/microvms-cli/Cargo.toml:48`-`:49`, `crates/microvms-core/src/lib.rs:77`), which
is what keeps `protocol` out of the CLI's direct dependency set.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `docs/schema.json` (committed artifact) | config | yes | `crates/agentd/src/bin/schema.rs:44` writes it, resolving the path at `:99`; `crates/agentd/tests/schema_artifact.rs:39` byte-compares it |
| `crates/agentd/src/schema.rs` | direct import | yes | `:51` re-exports `PROTOCOL_VERSION`; `:286` merges the `$defs` rendered from these types |
| `crates/agentd/src/exec.rs` | direct import | yes | `:73` imports the `ERROR_*` and `EVENT_*` names; `:87` re-exports the exec types |
| `crates/microvms-cli/src/commands/attached.rs` | indirect | yes | 22 `protocol::` references through core's re-export, e.g. `:178` `Phase::Running`, `:253` `ExitEvent`, `:442` `phase_name` |
| `crates/microvms-app/src/session/exec.rs` | direct import | yes | 16 references; `:69` and `:71` are public struct fields of `protocol::exec::Phase` / `Outcome`; `:118` aliases `StdinResponse` |
| `crates/microvms-app/src/session/sse.rs` | direct import | yes | `:272`, `:295`, `:304` dispatch on `EVENT_OUTPUT` / `EVENT_GAP` / `EVENT_EXIT`; `:255` wraps `ExitEvent` |
| `crates/microvms-app/src/session/mod.rs` | direct import | yes | `:270` and `:286` return `protocol::health::Health`; `:321` takes `protocol::exec::StartRequest` |
| `crates/agentd/src/routes.rs` | direct import | yes | `:18`-`:20` re-export `VERSION_HEADER`, `Health`/`DiskHealth`, and `HOOK_PREFIX`/`RunHook`/`RunHookEnvelope`/`RunHookError` |
| `bindings/microvms-js/src/session.rs` | direct import | yes | `:142` builds a `protocol::exec::StartRequest`; `:577` enumerates `Phase::ALL` so a new phase appears without an edit |
| `bindings/microvms-py/src/session.rs` | direct import | yes | `:338` and `:400` build `StartRequest`; `:598` maps `Phase::ALL` through `Phase::as_str` |
| `crates/agentd/src/fs.rs` | direct import | likely | `:95` re-exports `FileReadQuery` and `FsQuery`; touched when the fs query shape moves |
| `bindings/microvms-js/index.d.ts` | config | likely | a build product, **gitignored** at `.gitignore:29` and regenerated by the `napi build --platform` step at `.github/workflows/ci.yml:341`; nothing compares it against the crate |
| `crates/microvms-core/tests/turmoil_client.rs` | test | yes | 10 references; `:848` and `:861` synthesize real `OutputEvent` / `ExitEvent` frames against `EVENT_OUTPUT` (`:855`) and `EVENT_EXIT` (`:870`) |
| `crates/agentd/tests/schema_artifact.rs` | test | yes | `:58` is the staleness check; `:71` renames `"exec_id"` to `"execId"` to prove it can fail; `:431` pins the event names to `["output", "gap", "exit"]` |
| `conformance/run_rs.py` | test | likely | `:1692` embeds the exact `exit` event object as a fixture; `:1990` asserts the last NDJSON record is the `exit` event |
| `crates/model/src/lib.rs` | indirect | likely | `:74 ExecPhase` is a deliberate independent mirror of `Phase`, declared as such at `crates/protocol/src/exec.rs:16`; there is no compile edge, so it drifts silently |

### Blast-radius notes

- **Every type derives both halves of serde, and the pairing is a requirement rather than a
  convenience.** `crates/protocol/src/lib.rs:23` states the rule: a type carrying one half is a type the
  other side has to hand-write. Adding a `Serialize`-only type reopens the drift class the crate was
  extracted to close.
- **The schema is generated under two serde contracts and their `$defs` are merged, so a serde
  attribute can fail the build on a name collision rather than on the field.**
  `crates/agentd/src/schema.rs:242` builds a `for_serialize` generator and `:245` a `for_deserialize` one;
  `:286 merge_definitions` reports a name whose content differs instead of resolving it, and `:321`
  publishes `definition_collisions` into the document, asserted empty at `:764`.
- **A doc comment on a wire field is a schema change.** `crates/protocol/src/health.rs:17` records that the
  `version` field's comment is deliberately a `//` and not a `///`, because schemars publishes doc
  comments as `description` and the artifact is compared byte for byte.

## The Error kind / wire_kind taxonomy

Defined at: `crates/microvms-domain/src/error.rs:43` (`Error`), `:127` (`ErrorKind`, variants enumerated at
`:166`), `:219` (`WireKind`, variants enumerated at `:272`).

Gate: `crates/microvms-domain/src/error.rs:433 every_kind_carries_its_python_err_code` — every kind must carry
an `ERR_*` code — plus `:459 no_two_kinds_share_a_code` and, across the crate boundary,
`crates/microvms-cli/src/exit.rs:486 the_exit_table_and_cores_error_kinds_are_the_same_thirteen_classes`.
The Python side is gated by `.config/mise/tasks/contracts.toml:188 ["stubs:check"]` and the Node side by
`.config/mise/tasks/contracts.toml:248 ["dts:check"]`, which diffs the committed `bindings/microvms-js/index.d.ts` against a
fresh napi build.

The two types are two contracts serving two different consumers: `ErrorKind` answers which exit code
applies, and `WireKind` answers which status the daemon chose
(`crates/microvms-domain/src/error.rs:18`). `crates/microvms-domain/src/error.rs` itself holds 140 references; the heaviest consumers
follow.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `crates/microvms-cli/src/exit.rs` | direct import | yes | 37 references; `:140 Exit::for_kind` is an exhaustive match over `ErrorKind`, so a new kind is a compile error here |
| `bindings/microvms-py/src/errors.rs` | direct import | yes | 18 references; `:129 exception_for` matches every kind onto one of the `create_exception!` classes declared at `:33`-`:117` (one base plus one per kind) |
| `crates/microvms-cli/src/envelope.rs` | direct import | yes | 9 references; `:321 error()` emits the failure envelope and `:327` writes `data.kind` from the wire kind |
| `bindings/microvms-js/src/errors.rs` | direct import | yes | `:70 code_chain` is the single conversion out to JS; the module docs at `:35` and `:43` fix the contract as `err.cause.message` for the code and `err.cause.cause.message` for the wire kind |
| `crates/microvms-app/src/session/http.rs` | direct import | yes | `:127` is the sole non-test caller of `WireKind::from_status`, so the status table's shape is this file's contract |
| `crates/microvms-app/src/control/transport.rs` | direct import | likely | 29 references; `:36` imports `ErrorKind` and `:128`, `:154`, `:220` are control-plane raise sites; `:304` records that `WireKind` is the daemon's discipline and has no role here |
| `crates/microvms-app/src/control/image.rs` | direct import | likely | 29 references, all classifying at the point of raise; `:289`-`:290` document `BuildWedged`, `Platform`, and `Timeout` as three distinct build outcomes |
| `crates/microvms-app/src/control/microvm.rs` | direct import | likely | 23 references; `:303`-`:304` record that a missing proxy-auth key is `ErrorKind::Retryable` via `WireKind::AuthTokenMint` because minting sits inside the retry path |
| `crates/microvms-app/src/sandbox.rs`, `session/mod.rs`, `control/mod.rs`, `session/proxy.rs`, `control/artifact.rs`, `session/exec.rs`, `cost.rs`, `session/sse.rs`, `session/files.rs` | direct import | likely | (further direct imports, 4-17 references each, all raise sites under `crates/microvms-core/src/`) |
| `crates/microvms-cli/src/guards/` | test | yes | 23 references; the classification half of the exit catalogue, inducing each failure at the seam (`crates/microvms-cli/src/guards/support.rs:62`, `:99`, `:421`) |
| `conformance/run_rs.py` | test | yes | `:191` documents `data.kind` as a `microvms_core::WireKind` and `:226` asserts `Conflict` and `NotFound` are distinguishable by exception type |
| `crates/microvms-core/tests/turmoil_client.rs` | test | yes | 7 references; `:452` and `:726` assert `WireKind::Transport`, `:781` and `:1383` assert `WireKind::AuthTokenMint` |
| `bindings/microvms-py/tests/test_smoke.py` | test | yes | `:412` asserts one exception per kind under one shared base; `:275` asserts `wire_kind is None` for a local reject |
| `bindings/microvms-js/__test__/smoke.mjs` | test | yes | `:345` asserts the enumerable `ERR_*` codes are exactly one per `ErrorKind` (`:347`) |
| `crates/microvms-cli/src/seam.rs`, `commands/lifecycle.rs` | direct import | likely | 8 and 5 references on the classify-and-report path — `crates/microvms-cli/src/seam.rs:315`, `:330`, `:340`; `crates/microvms-cli/src/commands/lifecycle.rs:203`, `:745`, `:757` |
| `crates/agentd/src/fs.rs`, `crates/agentd/src/exec.rs`, `crates/agentd/src/disk.rs`, `crates/agentd/src/identity.rs`, `crates/agentd/tests/turmoil_transport.rs` | indirect | no | these are `std::io::ErrorKind`, not core's — the name collides but the type does not |

### Blast-radius notes

- **`ErrorKind` is derived from `WireKind` and must never be passed in beside it.**
  `crates/microvms-domain/src/error.rs:77 Error::wire` computes the kind from the wire kind precisely so the
  two cannot disagree, and `:366 WireKind::error_kind` is the mapping. An `Error::wire_with_kind`
  escape hatch would let a 401 be classified as retryable and retried forever.
- **`from_status` has no generic 4xx fallback, and that absence is a tested invariant.**
  `crates/microvms-domain/src/error.rs:343` maps the statuses explicitly, and
  `:519 no_generic_four_hundred_fallback_can_produce_a_protocol_error` asserts the unmapped ones
  resolve to `None`. A generic fallback would make a protocol typo look like a missing file, which is
  the defect named in `docs/PROTOCOL.md:49`. 5xx statuses do fall back, to `ServerError`, with 503
  excepted as `NotBootstrapped` (`crates/microvms-domain/src/error.rs:539`).
- **The retryable set is derived from the kind, never stored, and the test compares it against an
  independently restated table.** `crates/microvms-domain/src/error.rs:116 Error::retryable` reads
  `ErrorKind::Retryable`; `:471 retryable_agrees_with_the_python_exception_contract` compares that
  against a `#[cfg(test)]` restatement, and `:485 exactly_five_wire_kinds_are_retryable` pins the
  cardinality. A sixth added by mistake fails there rather than in a non-terminating retry loop.

## The exit table

Defined at: `crates/microvms-cli/src/exit.rs:78` (`Exit`, `#[repr(u8)]` with explicit discriminants) and
`:173` (`EXIT_TABLE: [ExitRow; 14]`).

Gate: `crates/microvms-cli/tests/exit_codes.rs:29 every_locally_reachable_row_exits_with_its_own_integer_and_code`
drives real spawned binaries, and `crates/microvms-cli/tests/manifest.rs:161` cross-checks the published
table against what the binary actually exits. Inside the crate,
`crates/microvms-cli/src/exit.rs:486` asserts the table and core's `ErrorKind` describe the same
classes, and `:512` asserts the mapping is injective.

The table is append-only. Its rows are the contract three consumers read: a shell reading `$?`, an
agent reading the `--json` envelope's `code`, and the conformance oracle reading `exitCode`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `crates/microvms-cli/src/manifest.rs` | direct import | yes | `:85` publishes every row as `exitCodes`, asserted at `:336 the_manifest_carries_all_fourteen_exit_rows` |
| `crates/microvms-cli/src/main.rs` | direct import | yes | the process exit path; `Exit::as_u8` (`crates/microvms-cli/src/exit.rs:109`) is what reaches `$?` |
| `crates/microvms-cli/src/envelope.rs` | direct import | yes | the failure envelope carries `exitCode`, `code`, and `finding` off the row |
| `crates/microvms-domain/src/error.rs` | indirect | yes | `ErrorKind::ALL` (`:166`) and `ErrorKind::code` (`:187`) are the other half; `crates/microvms-cli/src/exit.rs:486` asserts the two describe the same classes |
| `crates/microvms-cli/src/commands/lifecycle.rs`, `attached.rs`, `local.rs`, `cost.rs`, `doctor.rs`, `mod.rs` | direct import | likely | (further direct imports under `crates/microvms-cli/src/commands/`; every command constructs a `CliError` carrying an `Exit`, the shape declared at `crates/microvms-cli/src/exit.rs:78`) |
| `crates/microvms-cli/tests/exit_codes.rs` | test | yes | `:29` asserts integer, code, and finding together over every locally reachable row; `:122` pins the shared argument-error code; `:286` pins the streaming exception |
| `crates/microvms-cli/tests/manifest.rs` | test | yes | `:161 the_published_exit_table_agrees_with_what_the_binary_exits` |
| `crates/microvms-cli/src/guards/` | test | yes | the classification half — induces the rows an invocation cannot reach without an account, asserting the row directly (`crates/microvms-cli/src/guards/interrupt.rs:162` `Exit::Interrupted`, `crates/microvms-cli/src/guards/run.rs:254` `Exit::Precondition`) |
| `conformance/run_rs.py` | test | yes | `:287` cross-checks the process exit code against the envelope's own `exitCode`, and `:302` names CLI-3 as the claim that they agree; `:384` repeats it on the streaming path |
| `docs/PLATFORM.md` | config | likely | rows carry a `finding` naming a section of it (`crates/microvms-cli/src/exit.rs:62`, `:127`, `:312`; the module docs at `:10` state each platform code names a different finding) |

### Blast-radius notes

- **The table is indexed by the discriminant, so `Exit::row` is infallible — and the same indexing
  means a reordered row silently returns a neighbour's data.** `crates/microvms-cli/src/exit.rs:118`
  returns `&EXIT_TABLE[self.as_u8() as usize]` (`:119`), and
  `:452 the_table_is_indexed_by_the_exit_integer` pins the correspondence. The explicit `#[repr(u8)]`
  discriminants exist because a variant inserted mid-enum renumbers everything after it, which for
  this type means silently rewriting the published contract.
- **The rows are spelled out rather than generated from `ErrorKind::code`, on purpose.**
  `crates/microvms-cli/src/exit.rs:170` records the reason: a generated table would agree with a typo. The
  cross-check at `:486` is what makes the duplication safe, and
  `:512 the_kind_to_exit_mapping_is_injective` keeps two kinds from collapsing onto one row — the
  named temptation being `Precondition` and `InvalidArg`, which need separate rows because one is
  fixed by editing a flag and the other by applying a Terraform stack.
- **The protocol `WireKind`s collapse onto one exit row on purpose, and `data.kind` preserves the
  distinction.** `crates/microvms-cli/src/exit.rs:534 the_five_protocol_wire_kinds_collapse_and_the_others_do_not`
  pins the collapsing set. Widening the exit table to split them would break the append-only rule;
  narrowing `data.kind` would leave the conformance oracle unable to tell them apart
  (`conformance/run_rs.py:190`).

## constants.rs and its JSON emission

Defined at: `crates/microvms-domain/src/constants.rs:57`-`:455` (the constants, from `MODEL_API_VERSION` to
`DEAD_STATES`) and `:589` (`as_json`).

Gate: `.config/mise/tasks/contracts.toml:286 ["model:check"]`, which runs `./tools/check-model-drift.py` (`:257`) and
compares every emitted key against the pinned botocore service model. It sits in `check` rather than
`live` because the model is a file inside botocore — no network, no credentials. Inside the crate,
`:693 as_json_carries_every_key_the_drift_gate_reads` and
`:752 the_emitted_values_are_the_measured_ones` hold the key set and the literals.

Every value here is transcribed from the botocore service model for `lambda-microvms`, API version
`2025-09-09` (`:57`). The JSON key names are a contract with a Python script, so renaming a key is a
breaking change the compiler accepts — the module states the coupling at `:40`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `tools/check-model-drift.py` | config | yes | `:95 RUST_SOURCE_ARGV` reads the object through `microvm constants --emit-json` (`:103`), which `:45` names as the only client; `:149` is the key list, spelled as `as_json()`'s keys |
| `crates/microvms-cli/src/commands/local.rs` | direct import | yes | `:219` calls `as_json()`; `:223` prints the bare object as the one non-envelope stdout write in the binary, and `:206` records that the keys are the gate's contract |
| `crates/microvms-app/src/control/mod.rs` | direct import | yes | `:455` checks `MAX_DURATION_SEC`; `:463` and `:490` name `MODEL_API_VERSION` in the refusal text |
| `crates/microvms-app/src/control/token.rs` | direct import | yes | `:49` imports `MAX_CLIENT_TOKEN_LEN` and `:144` enforces it; `:71` records that the ceiling is measured against the worst legal scope because the run token folds a full ARN in |
| `crates/microvms-app/src/control/image.rs` | direct import | yes | `:91`-`:92` read both ready-state sets; `:171` and `:185` read `ARCHITECTURES[0]` and `CAPABILITIES[0]` |
| `crates/microvms-domain/src/hooks.rs` | direct import | yes | `:40` imports both hook-timeout ceilings; `:58` and `:86` are the two newtypes' `MAX_SECS` |
| `crates/microvms-app/src/control/transport.rs` | direct import | yes | `:45 const API_PATH_VERSION = crate::constants::MODEL_API_VERSION` — the request path is built from it, and `:41` says it is read rather than written again |
| `crates/microvms-domain/src/sizing.rs`, `crates/microvms-domain/src/region.rs` | indirect | yes | `as_json` reaches into `crate::sizing::SIZE_CLASSES` at `crates/microvms-domain/src/constants.rs:660` and `MICROVM_REGIONS` at `crates/microvms-domain/src/constants.rs:651`, so editing either table changes the gate's payload |
| `crates/microvms-app/src/sandbox.rs` | direct import | likely | `:884` reads `DEAD_STATES` on the launch guard |
| `crates/microvms-cli/src/commands/lifecycle.rs` | direct import | likely | `:988` reads `DEAD_STATES`; `:974` records that failing fast on it beats burning the poll budget |
| `crates/microvms-cli/tests/manifest.rs` | test | yes | `:229 constants_emit_json_writes_the_bare_object_the_drift_gate_reads`; `:297` asserts the command is listed rather than hidden |
| `crates/microvms-cli/src/commands/local.rs` (own tests) | test | yes | `:399` asserts the parsed output equals `microvms_core::constants::as_json()` |
| `docs/PLATFORM.md`, `docs/TRUST.md` | config | likely | `docs/PLATFORM.md:43-48` documents the 4096-byte ceiling and notes that the service model's member documentation still says 16,384; `docs/TRUST.md:240-241` restates it |

### Blast-radius notes

- **These guards exist because botocore does not enforce the limits itself.**
  `crates/microvms-domain/src/constants.rs:14` records the measurement: `VALIDATED_METADATA_ATTRS` is
  `{'required', 'min', 'document', 'union'}`, so `max`, `pattern`, and `enum` violations reach the
  wire. Deleting a guard on the assumption that the SDK validates the model reopens all of them.
- **`DEAD_STATES` is a strict subset of `TERMINAL_STATES`, and `SUSPENDED` must stay out of it.**
  `crates/microvms-domain/src/constants.rs:448` lists the terminal states `TERMINATED`, `TERMINATING`,
  `SUSPENDED`, and `SUSPENDING`, `:455` lists the dead ones, `TERMINATED` and `TERMINATING`, and
  `:878 every_dead_state_is_also_a_terminal_state` asserts the containment. `:452`
  gives the reason: `SUSPENDED` means death when it occurs before `RUNNING` and is also an ordinary
  waypoint on the resume path, so a resume that failed fast on it would fail on every resume.
- **The two image-ready sets must stay disjoint, or the gate reports a tolerated spelling as
  model-backed.** `crates/microvms-domain/src/constants.rs:431` is checked against the model exactly and
  `:441` exists because the service has answered differently across API versions;
  `:941 the_model_and_tolerated_ready_states_do_not_overlap` keeps them apart.

## The size-class table

Defined at: `crates/microvms-domain/src/sizing.rs:68` (`SIZE_CLASSES`, 5 rows / 20 numbers) and `:113`
(`SizeClass`).

Gate: `tools/check-model-drift.py:279 PINNED_SIZE_CLASSES` is a deliberate literal twin compared
against the emitted table, reached through `.config/mise/tasks/contracts.toml:286 ["model:check"]`. `.config/mise/tasks/contracts.toml:304`
records why a twin is the only possible check here: the sizing table is measurement-backed, so the
service model can say nothing about it and client-versus-client is the only comparison available.
In-crate, `crates/microvms-domain/src/sizing.rs:273 the_documented_table_carries_the_measured_rows` pins the
rows.

`minimumMemoryInMiB` selects a class whose two numbers differ by 4x; it does not size a VM directly
(`docs/PLATFORM.md:139`). The table is the only place any of the twenty numbers appears
(`crates/microvms-domain/src/sizing.rs:20`).

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `crates/microvms-domain/src/cost.rs` | direct import | yes | the rate arithmetic multiplies `baseline_gb()` (`crates/microvms-domain/src/sizing.rs:195`) and never the peak |
| `crates/microvms-cli/src/cli.rs` | direct import | yes | `:240 MemoryMib` is the clap `ValueEnum` mirror and `:255 size_class()` the exhaustive mapping; `:1034` asserts the flag domain is exactly the documented table |
| `bindings/microvms-py/src/cost.rs`, `bindings/microvms-js/src/cost.rs` | direct import | yes | `bindings/microvms-py/src/cost.rs:488 PySizeClass` and `bindings/microvms-js/src/cost.rs:422 SizeClass` each wrap the core type (`bindings/microvms-js/src/cost.rs:41`) over the same five rows |
| `crates/microvms-app/src/control/image.rs` | direct import | yes | the `resources` list on the build request |
| `crates/microvms-domain/src/constants.rs` | direct import | yes | `:660` flattens every row into the drift gate's JSON payload |
| `tools/check-model-drift.py` | config | yes | `:266 PINNED_SIZE_CLASSES`; `:247` records that a value compared only against itself passes by construction |
| `crates/microvms-app/src/control/mod.rs` | direct import | likely | the request builders take a `SizeClass` rather than an integer |
| `crates/microvms-cli/src/commands/lifecycle.rs`, `crates/microvms-cli/src/commands/cost.rs` | direct import | likely | each converts the `--memory` flag to a class and keeps it a class all the way down |
| `bindings/microvms-js/src/sandbox.rs`, `bindings/microvms-py/src/sandbox.rs` | direct import | likely | the build entry points take an `Option` size (`bindings/microvms-js/src/sandbox.rs:244`, `bindings/microvms-py/src/sandbox.rs:464`); `bindings/microvms-js/src/sandbox.rs:233` records that an off-table baseline stays refused because the only way to hold a `SizeClass` is to have parsed one |
| `crates/microvms-cli/src/render.rs` | direct import | no | the rendering takes a report, not a class; the references are under `#[cfg(test)]` from `:394` |
| `crates/microvms-cli/tests/manifest.rs` | test | yes | `:90 every_published_domain_is_the_domain_the_parser_enforces` feeds the published `--memory` domain back to the parser |
| `bindings/microvms-py/tests/test_smoke.py`, `bindings/microvms-js/__test__/smoke.mjs` | test | yes | `bindings/microvms-js/__test__/smoke.mjs:302`-`:304` asserts 1500 is refused and names TRAP-10; `bindings/microvms-py/tests/test_smoke.py:153` asserts the refusal surfaces as `ERR_INVALID_ARG` |
| `docs/PLATFORM.md` | config | likely | `:236` is the finding the table transcribes; `:244` states the four-times pairing and `:259` that billing follows the baseline |

### Blast-radius notes

- **Every shipped peak is exactly 4x its baseline, and nothing may compute it.**
  `crates/microvms-domain/src/sizing.rs:15` names computing the peak as the one thing the module must not do.
  The regularity comes from AWS, not from this codebase, so a sixth row breaking the pattern would
  silently get the pattern applied. The guard is testable because the lookups are table-parameterized
  (`:247 row_in`, `:255 class_for_baseline_in`), and
  `:298 a_peak_that_is_not_four_times_its_baseline_is_read_not_computed` drives them over a table a
  computed implementation cannot answer.
- **An off-table baseline is refused rather than snapped, and the refusal is a billing decision.**
  `crates/microvms-domain/src/sizing.rs:146 from_baseline_mib` rejects anything not in the table, asserted at
  `:360 an_off_table_baseline_is_refused_naming_the_finding`. A request of 1500 has two plausible
  readings — round up, or take it literally — and they differ in both the memory the guest gets and
  the rate it is billed at. The proptest sampler at `:427 plausible_baseline_mib` exists because a
  uniform `u32` draw almost never lands where snapping is even possible.
- **Two of the numbers are each both a baseline and a peak, so a caller echoing back a `MemTotal`
  gets a different class silently.** `crates/microvms-domain/src/sizing.rs:384 a_peak_that_is_not_also_a_baseline_is_refused`
  records the overlap, and it is why `Display` (`:226`) always names both numbers — asserted at
  `:410 display_names_the_baseline_and_the_peak_together`. A `Display` that named one would let
  someone budget for memory they are not billed for.

## The region list

Defined at: `crates/microvms-domain/src/region.rs:45` (`Region`) and `:73` (`MICROVM_REGIONS: [Region; 5]`).

Gate: `tools/check-model-drift.py:267 PINNED_REGIONS` is the literal twin, compared through
`.config/mise/tasks/contracts.toml:286 ["model:check"]`; in-crate,
`crates/microvms-domain/src/region.rs:176 the_five_supported_regions_are_the_measured_ones` and
`crates/microvms-cli/src/cli.rs:1061 the_region_domain_is_exactly_the_five_measured_regions_and_excludes_eu_central_one`
hold both ends. No service model states the set — this list is maintained by hand, and the two
botocore calls that look like substitutes disagree with each other (`crates/microvms-domain/src/region.rs:21`).

`codegraph callers Region` reports more inbound callers than for any other surface here.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `bindings/microvms-py/src/region.rs` | direct import | yes | `:32 PyRegion` is a hand-ported closed class with one static constructor per region |
| `bindings/microvms-js/src/region.rs` | direct import | yes | `:35 Region` with a factory per region and deliberately no constructor, asserted by `bindings/microvms-js/__test__/smoke.mjs:267` |
| `crates/microvms-cli/src/cli.rs` | direct import | yes | `:273 RegionArg` is the clap mirror and `:287 region()` the exhaustive mapping |
| `crates/microvms-domain/src/cost.rs` | direct import | yes | a `RateTable` is region-scoped (`:849`), and the region is what a caller reads back (`bindings/microvms-js/__test__/smoke.mjs:331`) |
| `crates/microvms-app/src/control/transport.rs` | direct import | yes | `:430` and `crates/microvms-edges/src/control/transport.rs:47` build the AWS config and the endpoint host from `region.as_str()` (`crates/microvms-domain/src/region.rs:83`) |
| `crates/microvms-domain/src/constants.rs` | direct import | yes | `:50` imports `MICROVM_REGIONS`; `:651` publishes it in the gate's payload; `:575` records that it is explicitly not model-backed |
| `tools/check-model-drift.py` | config | yes | `:254 PINNED_REGIONS`; `:57` explains why the two measurement-backed values each need a second reader |
| `crates/microvms-cli/src/seam.rs` | direct import | likely | `:341 resolve_region` and the `CoreSeam` methods are region-parameterized |
| `crates/microvms-app/src/control/mod.rs` | direct import | likely | `:132 ControlPlane::new` takes a `Region` rather than a string |
| `crates/microvms-app/src/control/connector.rs`, `control/microvm.rs`, `control/artifact.rs`, `control/image.rs`, `sandbox.rs` | direct import | likely | (further direct imports under `crates/microvms-core/src/`) |
| `bindings/microvms-js/src/sandbox.rs`, `bindings/microvms-py/src/sandbox.rs` | direct import | likely | `create`/`new` takes a `Region` object rather than a string, which is what keeps the closure |
| `crates/microvms-cli/src/commands/doctor.rs` | direct import | yes | lists the supported names and falls back to `Region::UsEast1` |
| `crates/microvms-cli/src/guards/` | test | likely | the injected seams are region-parameterized (`crates/microvms-cli/src/guards/support.rs:73`, `:80`, `:89`) |
| `bindings/microvms-py/tests/test_smoke.py`, `bindings/microvms-js/__test__/smoke.mjs` | test | yes | `bindings/microvms-js/__test__/smoke.mjs:251` asserts the five names; `bindings/microvms-js/__test__/smoke.mjs:232` and `bindings/microvms-py/tests/test_smoke.py:270` each assert `eu-central-1` is refused, `bindings/microvms-py/tests/test_smoke.py:264` naming the 2026-08-07 removal |
| `crates/microvms-cli/src/cli.rs` (tests) | test | yes | `:1061` asserts the flag domain equals the measured five; `:1102` asserts the unlisted escape hatch conflicts with the closed set |

### Blast-radius notes

- **The correctness condition runs in both directions, and an extra entry causes more damage than a
  missing one.** `crates/microvms-domain/src/region.rs:24` states both cases. A missing region refuses a launch
  AWS would have accepted, which is recoverable — `Region::unlisted` (`:107`) exists for that case. An
  extra region reopens the null-message trap for a name nothing will reject
  (`docs/PLATFORM.md:59`), which is why
  `crates/microvms-domain/src/region.rs:196 eu_central_one_is_refused_naming_the_null_message_trap`
  names that specific value and why tests in Rust, Python, and Node each repeat it.
- **`unlisted()` normalizes a supported name back to its variant, so there is never a second spelling
  of one region.** `crates/microvms-domain/src/region.rs:107`, asserted at
  `:243 the_escape_hatch_normalises_a_supported_name_to_its_variant` and mirrored in the Node binding
  at `bindings/microvms-js/__test__/smoke.mjs:262`. Removing the normalization would make
  `unlisted("us-east-1")` an unequal value that every downstream `match` has to handle twice.
- **`supported()` is the single reader of the five spellings, and both `FromStr` and `unlisted` go
  through it.** `crates/microvms-domain/src/region.rs:119`. A second lookup table added anywhere, including in
  a binding, could drift from this one; `:219 each_supported_region_round_trips_through_its_wire_name`
  keeps `as_str` and the parse path on one table.

## The pinned cost rate table

Defined at: `crates/microvms-domain/src/cost.rs:1018` (`pinned_rates`), returning the `RateTable` declared at
`:849`, with the decimal literals at `:1016`-`:1023`.

Gate: an offline check and a live one, running at different times.
`crates/microvms-domain/src/cost.rs:2227 every_rate_byte_matches_the_python_literal` compares each field
against a literal in the offline tier, and `./tools/check-live-rates.py --twin-only` cross-checks
the script's own pinned copy against the Rust source — offline and free, per `.config/mise/tasks/live.toml:137`. The
billable half, `.config/mise/tasks/live.toml:120 ["live:rates"]`, compares both against the live AWS Pricing API;
it sits in `live` rather than `check` because it needs network and credentials (`:402`).

The figures were read from the Lambda pricing page on 2026-08-07 in us-east-1
(`crates/microvms-domain/src/cost.rs:999`). One of them, `storage_gb_month`, is derived rather than read.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `tools/check-live-rates.py` | config | yes | `:121 PINNED` restates every figure; `:133 TWIN_PATH` and `:134 TWIN_FN` point at `pinned_rates`, and `:148 verify_twin` parses the `dec!()` literals out of the Rust source |
| `crates/microvms-cli/src/commands/cost.rs` | direct import | yes | the `cost` command's table |
| `bindings/microvms-py/src/cost.rs` | direct import | yes | `:576 PyRateTable` and `:590 pinned()` — the only pinned door, with deliberately no rates-taking constructor |
| `bindings/microvms-js/src/cost.rs` | direct import | yes | `:501 RateTable`, `:514 pinned()`; `:908`, `:960`, `:982` default to `cost::pinned_rates` when no table is passed |
| `crates/microvms-cli/src/commands/lifecycle.rs` | direct import | likely | `:449` imports `pinned_rates` and `run_report`; `:470`-`:473` price a completed run |
| `crates/microvms-cli/src/render.rs` | direct import | likely | `:399` reads `retrieved()` (`crates/microvms-domain/src/cost.rs:885`) for the report header; the remaining uses are under `#[cfg(test)]` from `:404` |
| `crates/microvms-domain/src/cost.rs` (own tests) | test | yes | `:2180` pins every figure as a literal; `:2205` asserts the GB-month derivation as `dec!(0.0001111111) * dec!(730)` |
| `conformance/run_rs.py` | test | likely | asserts the `cost` command and the run envelope each report a labelled estimate |
| `docs/PLATFORM.md` | config | yes | `:293`, `:295`, and `:299` carry the same figures; `:304`-`:306` carry the GB-hour → GB-month derivation. `crates/microvms-domain/src/cost.rs:57` and `:1002` both point here, so the two change in one commit |
| `mise.toml` | config | no | `:395` wires `live:rates` to the script; `:411` records that `--twin-only` runs first on that path |

### Blast-radius notes

- **Renaming `pinned_rates` breaks the twin check by name, not by compilation.**
  `tools/check-live-rates.py:134` finds the function by the literal string
  `"pub fn pinned_rates()"`, and `:180` is the error raised when it cannot — an error that explicitly
  instructs the reader to repoint `TWIN_FN` rather than delete the check. The script's pinned figures
  are a deliberate second copy (`.config/mise/tasks/live.toml:135`), because a drift check that imported the values it
  checks would compare a table against itself.
- **Money is always a `Decimal`, and the pinned figures carry ten significant digits.**
  The literals at `crates/microvms-domain/src/cost.rs:1023`-`:1033` are `dec!()` values, not floats. Summing a
  few thousand per-second ARM rates in binary floating point drifts toward a bill nobody can
  reproduce, and `docs/PLATFORM.md:164-170` lists the dated Pricing API rates to ten decimal places.
- **`storage_gb_month` is derived, and the code and the platform doc both record the earlier wrong
  value.** `crates/microvms-domain/src/cost.rs:2255` holds `dec!(0.08)` in the test that proves the current
  figure is not it, and `docs/PLATFORM.md:180`-`:181` records that $0.08 per GB-month was rounded
  low against the API rate, which gives $0.081111103 at a 730-hour month.
  `CatalogLine` (`:1038`) checks the unit the API reports for exactly this reason: if AWS restated
  storage per GB-month, the number would change by 730x and every downstream arithmetic check would
  still pass, because they all read the same table.

## The CLI manifest

Defined at: `crates/microvms-cli/src/manifest.rs:34` (`build`), reading `Cli::command()`,
`crates/microvms-cli/src/exit.rs:173 EXIT_TABLE`, `crates/microvms-cli/src/commands/mod.rs:104 RESPONSE_TYPES`, and
`crates/microvms-cli/src/envelope.rs:66 API_VERSION`.

Gate: one file checks it from independent directions —
`crates/microvms-cli/tests/manifest.rs:46 every_command_the_manifest_lists_is_one_the_binary_routes`,
`:90 every_published_domain_is_the_domain_the_parser_enforces`, and
`:161 the_published_exit_table_agrees_with_what_the_binary_exits` — plus
`conformance/run_rs.py:832`, which reads the manifest at runtime and drives every command it lists.

The manifest is generated and never hand-maintained (`crates/microvms-cli/src/manifest.rs:4`). Because it is
built from the same tables the binary runs on, it cannot drift from the binary's actual behavior, and
that guarantee is what makes it useful to an agent.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| `crates/microvms-cli/src/commands/local.rs` | direct import | yes | `:193` is the `manifest` command handler, calling `crate::manifest::build()` at `:196`; `:186` records that a command added without a `RESPONSE_TYPES` row fails `crates/microvms-cli/tests/manifest.rs` rather than shipping undescribed |
| `crates/microvms-cli/src/commands/mod.rs` | indirect | yes | `:104 RESPONSE_TYPES` is the one table the manifest reads rather than introspects; `:263 response_type` is called from every command module |
| `crates/microvms-cli/src/cli.rs` | runtime dispatch | yes | the whole clap tree is the input, so a flag added to any command appears in the manifest without an edit here, and a `--stream` added elsewhere changes `alternateResponse` (`crates/microvms-cli/src/manifest.rs:75`) |
| `crates/microvms-cli/src/exit.rs` | direct import | yes | `:85` publishes every row as `exitCodes` |
| `crates/microvms-cli/src/envelope.rs` | direct import | likely | `:30` imports `API_VERSION`, published as the manifest's `apiVersion` at `:81` and emitted on every envelope (`crates/microvms-cli/src/envelope.rs:314`, `:331`) |
| `conformance/run_rs.py` | test | yes | `:816` calls `microvm manifest` and `:817`-`:819` assert the suite drives every command it lists; `:751` takes a fixture value out of the manifest rather than writing it down; `:205` reads `apiVersion` back |
| `crates/microvms-cli/tests/manifest.rs` | test | yes | `:46`, `:90`, `:161` as above; `:195` asserts a bare invocation emits JSON |
| `crates/microvms-cli/src/manifest.rs` (own tests) | test | yes | `:265` asserts the command list equals the clap tree exactly; `:426` asserts every command declares a response type and its keys; `:450` asserts every command publishes a summary from its doc comment |
| `docs/reference/cli.md` | config | likely | `:3` cites the clap enum in `crates/microvms-cli/src/cli.rs` for the subcommand list, so it restates by hand what the manifest generates |

### Blast-radius notes

- **The command count is asserted in three places, so adding a command is a three-file change.**
  `crates/microvms-cli/src/commands/mod.rs:104` declares `RESPONSE_TYPES: [(&str, &str, &[&str]); 17]`,
  `crates/microvms-cli/src/manifest.rs:280` asserts it, and `crates/microvms-cli/tests/manifest.rs:51` asserts it
  again with the breakdown — "the lifecycle six, the attached five, and the local six". The triple
  assertion is deliberate: it is what keeps `RESPONSE_TYPES` from becoming the hand-maintained
  artifact generation forbids (`crates/microvms-cli/src/manifest.rs:13`).
- **`choices: null` and `choices: []` mean different things, and a boolean flag must publish
  neither.** `crates/microvms-cli/src/manifest.rs:401 a_free_text_parameter_reports_a_null_domain` asserts
  free text reports `null` (`:417`), and `:464` records that publishing clap's `["true", "false"]`
  for a `SetTrue` flag would put a `choices` array on every boolean flag — making `choices` useless
  as the field a reviewer scans to find the genuinely closed sets (`:137`).
- **`exec --stream` is the one documented exception to the one-envelope-per-invocation rule, and it
  is published as a machine-readable fact rather than as prose.** `crates/microvms-cli/src/manifest.rs:75`
  emits `alternateResponse` keyed off the flag's presence, and
  `:292 only_exec_publishes_an_alternate_streaming_response` asserts no other command claims one and
  that the streaming discriminant differs from the normal one — because a consumer branching on
  `type` cannot otherwise tell which parse applies. `crates/microvms-cli/tests/exit_codes.rs:286` asserts the
  binary really publishes it.

## Other notable surfaces

- `crates/agentd/src/routes.rs:371 surface_docs()` — the single route list the router (`:31`), the
  `/v1/schema` handler (`:346`), the schema binary, and the assertions in
  `crates/agentd/tests/schema_artifact.rs` all walk
  (`crates/agentd/tests/schema_artifact.rs:149 every_documented_route_is_served_by_the_router`,
  `:207 every_bearer_route_answers_503_before_bootstrap`, `:249`, `:291`, `:349`). A route absent from
  it does not exist.
- `crates/microvms-domain/src/hooks.rs:48 RunHookTimeout` / `:54 BuildHookTimeout` — two newtypes with no
  conversion between them and separate `MAX_SECS` (`:58` = 60, `:86` = 3600), so a 3600-second build
  timeout cannot reach a field capped at 60. Mirrored in `bindings/microvms-py/src/hooks.rs` and
  `bindings/microvms-js/src/hooks.rs`.
- `crates/microvms-cli/src/seam.rs:138 CoreSeam` — the trait every CLI command reaches AWS through, and the
  injection point the test suite substitutes at.
  The CLI's `clippy.toml` refuses core's constructors and transport calls everywhere but
  `src/seam.rs`, so no shipping source line reaches past it.
- `crates/microvms-app/src/control/transport.rs:243 Transport` and `crates/microvms-app/src/clock.rs:42
  Clock` — the `Send + Sync` trait seams `ControlPlane` is constructed over
  (`crates/microvms-app/src/control/mod.rs:132`), with `crates/microvms-app/src/control/fake.rs` as the recording implementation.
- `crates/microvms-cli/Cargo.toml`'s direct dependency set: checked by
  `crates/microvms-cli/tests/thinness.rs:107 no_direct_dependency_is_a_second_path_to_aws` against
  the `FORBIDDEN` denylist at `:54`, and the absence of a `lib` target asserted by
  `crates/microvms-cli/tests/dependency_direction.rs:127 the_cli_exports_no_library_target_at_all`. Both are
  manifest-shaped invariants a dependency addition trips.
- **The Node binding's typed surface has no drift gate.** `bindings/microvms-js/index.d.ts` is gitignored
  (`.gitignore:29`) and untracked, and the `bindings` CI job builds the addon and runs
  `node --test` (`.github/workflows/ci.yml:341`) without comparing the generated declarations against
  the crate. The Python side is gated — `.github/workflows/ci.yml:319` runs
  `./tools/generate-py-stubs.py --check`, and `:314`-`:317` records why a stale stub is worse than
  a stale schema: `py.typed` ships in the wheel beside it, so a stale stub leaves a caller
  confidently wrong rather than unchecked. The failure mode on the Node side is the same class with
  no detector: a renamed or removed method changes `index.d.ts` on the next build, and nothing fails
  until a TypeScript consumer's call reaches a method the addon no longer exports.

## See also

- [contract map](contract-map.md)
- [business logic](business-logic.md)
- [public api](../reference/public-api.md)
- [debugging guide](debugging-guide.md)
- [system overview](../architecture/system-overview.md)
