# microvms-agentd · Impact analysis

A **high-impact surface** here is a contract whose change crosses a crate boundary,
a language binding, a serialized representation, or an independently checked table.
The selection follows those boundaries: shared wire types, failure classification,
CLI results, service constraints, sizing, regions, pricing, and command discovery.
Core re-exports the domain and app layers; the CLI and bindings therefore often
consume definitions through core without owning their behavior.
`crates/microvms-core/src/lib.rs:85-100`

Use each table as a review map. `yes` means update a mirrored contract or generated
artifact when its represented value changes; `likely` means inspect a consumer
whose conversion or interpretation may need to change; `no` means a compatible
implementation change needs behavioral validation rather than a consumer edit.
Rows identify concrete consumers and their checks, not a guarantee of runtime
coverage.

## Protocol wire types

Defined at: `crates/protocol/src/lib.rs:30-35`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| Daemon exec request and event handling | direct import | likely | `crates/agentd/src/exec.rs:73-91` |
| Daemon route metadata and tunnel close codes | direct import | likely | `crates/agentd/src/routes.rs:18-20`, `crates/agentd/src/tunnel.rs:104` |
| Client tunnel close-code interpretation | direct import | likely | `crates/microvms-edges/src/session/tunnel.rs:149-155` |
| Daemon filesystem query handling | direct import | likely | `crates/agentd/src/fs.rs:95` |
| Session request construction and response decoding | direct import | likely | `crates/microvms-app/src/session/mod.rs:520-545` |
| File-read query encoding | direct import | likely | `crates/microvms-app/src/session/files.rs:46-53` |
| Exec results and terminal-phase interpretation | direct import | likely | `crates/microvms-app/src/session/exec.rs:69-103` |
| SSE event dispatch | runtime dispatch | likely | `crates/microvms-app/src/session/sse.rs:271-310` |
| Python request builder and exported phase names | direct import | likely | `bindings/microvms-py/src/session.rs:820-821`, `bindings/microvms-py/src/session.rs:2241-2248` |
| Node request builder and exported phase names | direct import | likely | `bindings/microvms-js/src/session.rs:303-319`, `bindings/microvms-js/src/session.rs:1275-1281` |
| Client-default publication | indirect | likely | `crates/microvms-app/src/defaults.rs:151-186` |
| Model-to-client outcome conversion | test | likely | `crates/model-conformance/tests/tables.rs:380-401` |
| Core's compatibility re-export | indirect | no | `crates/microvms-core/src/lib.rs:93-95` |
| Committed schema artifact | config | yes | `crates/agentd/src/schema.rs:240-286`, `crates/agentd/tests/schema_artifact.rs:39-50` |
| Transport simulation fixtures | test | likely | `crates/microvms-core/tests/turmoil_client.rs:853-876` |
| Python and TypeScript declaration checks | config | likely | `.config/mise/tasks/contracts.toml:220-232`, `.config/mise/tasks/contracts.toml:279-314` |

### Blast-radius notes

- Wire types support both serialization and deserialization. The schema generator
  uses separate read and write contracts, then reports conflicting definitions;
  inspect serde attributes as well as Rust field names.
  `crates/protocol/src/lib.rs:23-27`, `crates/agentd/src/schema.rs:240-286`,
  `crates/agentd/src/schema.rs:327-340`
- Build exec requests through `StartRequest::new`. The type is non-exhaustive,
  and the constructor is the common source of request defaults.
  `crates/protocol/src/exec.rs:218-219`, `crates/protocol/src/exec.rs:505-535`
- Schema text is part of the checked artifact: the comparison is byte-for-byte,
  including whitespace. Regenerate it after a wire documentation change.
  `crates/agentd/tests/schema_artifact.rs:39-74`

## Error classification

Defined at: `crates/microvms-domain/src/error.rs:34`,
`crates/microvms-domain/src/error.rs:167`, and
`crates/microvms-domain/src/error.rs:270`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| CLI kind-to-exit mapping | direct import | yes | `crates/microvms-cli/src/exit.rs:172-187` |
| Python exception classes | direct import | yes | `bindings/microvms-py/src/errors.rs:132-147` |
| Node error codes, causes, and retry helper | direct import | likely | `bindings/microvms-js/src/errors.rs:92-105`, `bindings/microvms-js/src/errors.rs:195-226` |
| Control-plane and sandbox error producers | direct import | likely | `crates/microvms-app/src/control/transport.rs:36`, `crates/microvms-app/src/sandbox.rs:72` |
| Session HTTP status conversion | direct import | likely | `crates/microvms-app/src/session/http.rs:108-134` |
| Failure envelope's detailed kind | indirect | likely | `crates/microvms-cli/src/envelope.rs:367-383` |
| Conformance envelope interpretation | indirect | likely | `conformance/harness/envelope.py:35-44`, `conformance/harness/envelope.py:70-89` |
| Mapping, retryability, and status round-trip tests | test | yes | `crates/microvms-domain/src/error.rs:605-655`, `crates/microvms-domain/src/error.rs:698-730` |
| Cross-surface exception and code tests | test | yes | `bindings/microvms-py/tests/test_smoke.py:458-491`, `bindings/microvms-js/__test__/smoke.mjs:359-391` |
| Exit-table agreement tests | test | yes | `crates/microvms-cli/src/exit.rs:588-627` |

### Blast-radius notes

- `Error::wire` derives the coarse kind from the detailed wire kind. Preserve that
  relationship when adding a status; callers should not choose conflicting
  classifications. `crates/microvms-domain/src/error.rs:74-84`,
  `crates/microvms-domain/src/error.rs:428-457`
- Retryability is a property of `ErrorKind`, and the Node helper resolves a code
  back through that enum. Changing the retry policy affects callers without
  changing an error's text. `crates/microvms-domain/src/error.rs:223-231`,
  `bindings/microvms-js/src/errors.rs:215-226`
- The CLI exit code groups some daemon failures together, while `data.kind`
  preserves their distinction. A change to either representation needs both
  envelope and exception review. `crates/microvms-cli/src/exit.rs:641-656`,
  `crates/microvms-cli/src/envelope.rs:367-383`

## CLI exit table

Defined at: `crates/microvms-cli/src/exit.rs:79-134` and
`crates/microvms-cli/src/exit.rs:205-311`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| Process completion and parse-failure reporting | direct import | likely | `crates/microvms-cli/src/main.rs:119-127`, `crates/microvms-cli/src/main.rs:236-244` |
| Failure classification | direct import | likely | `crates/microvms-cli/src/exit.rs:371-466` |
| JSON failure envelopes | direct import | likely | `crates/microvms-cli/src/envelope.rs:367-383` |
| Published manifest exit rows | direct import | yes | `crates/microvms-cli/src/manifest.rs:112-117` |
| Conformance subprocess status interpretation | indirect | likely | `conformance/harness/envelope.py:29-33`, `conformance/harness/envelope.py:57-66` |
| Table-index and core-class agreement tests | test | yes | `crates/microvms-cli/src/exit.rs:551-566`, `crates/microvms-cli/src/exit.rs:588-627` |
| CLI command-local failures | direct import | likely | `crates/microvms-cli/src/commands/attached.rs:169-186`, `crates/microvms-cli/src/commands/mod.rs:630-651` |
| Induced failure-class guards | test | yes | `crates/microvms-cli/src/guards/exit_codes.rs:25-37` |
| Spawned CLI exit-code tests | test | yes | `crates/microvms-cli/tests/exit_codes.rs:29-49` |
| Manifest-to-process exit comparison | test | yes | `crates/microvms-cli/tests/manifest.rs:195-238` |

### Blast-radius notes

- Exit integers are append-only. The enum uses explicit discriminants, and
  `Exit::row` indexes the table by that integer; changing row order can change
  metadata without changing the selected variant.
  `crates/microvms-cli/src/exit.rs:70-79`, `crates/microvms-cli/src/exit.rs:145-151`
- Some failures are owned by the CLI rather than by `ErrorKind`: name conflicts,
  configuration, and local synchronization have dedicated rows. Adding a core
  kind does not replace those local classifications.
  `crates/microvms-cli/src/exit.rs:103-133`
- The table deliberately spells out error codes separately from core. Preserve
  the agreement test instead of deriving both sides from the same table.
  `crates/microvms-cli/src/exit.rs:197-205`, `crates/microvms-cli/src/exit.rs:581-607`

## Service constants and JSON emission

Defined at: `crates/microvms-domain/src/constants.rs:54` and
`crates/microvms-domain/src/constants.rs:647-739`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| CLI constants command | indirect | likely | `crates/microvms-cli/src/commands/local.rs:946-967` |
| Model comparison keys and source loader | runtime dispatch | yes | `tools/check-model-drift.py:54-60`, `tools/check-model-drift.py:90-151`, `tools/check-model-drift.py:288-307` |
| Control-plane versioned request paths | direct import | likely | `crates/microvms-app/src/control/transport.rs:179-183` |
| Control request duration policy | direct import | likely | `crates/microvms-app/src/control/mod.rs:611-621` |
| Image state and build parameters | direct import | likely | `crates/microvms-app/src/control/image.rs:112-113`, `crates/microvms-app/src/control/image.rs:219-233` |
| Hook-timeout types | direct import | likely | `crates/microvms-domain/src/hooks.rs:37-40` |
| Session keepalive and no-timeout ceiling | direct import | likely | `crates/microvms-app/src/session/keepalive.rs:32-37`, `crates/microvms-app/src/session/complete.rs:27-30` |
| Region and size table emission | indirect | likely | `crates/microvms-domain/src/constants.rs:718-739` |
| JSON key and value assertions | test | yes | `crates/microvms-domain/src/constants.rs:760-826` |
| Bare-object command contract test | test | yes | `crates/microvms-cli/tests/manifest.rs:277-314` |
| Model comparison task | config | no | `.config/mise/tasks/contracts.toml:316-341` |

### Blast-radius notes

- The JSON keys are a script interface. Renaming a constant in Rust is incomplete
  until its emitted key and the script's expected-key set agree.
  `crates/microvms-domain/src/constants.rs:647-652`, `tools/check-model-drift.py:90-151`
- `constants --emit-json` exposes the bare object; the normal JSON command wraps
  it in `data.constants`. Preserve both views when changing command rendering.
  `crates/microvms-cli/src/commands/local.rs:946-967`
- Region and size tables are compared with independent literals because the
  service model does not define them. A model comparison is not evidence that
  their measured platform behavior remains current.
  `tools/check-model-drift.py:949-975`

## Size classes

Defined at: `crates/microvms-domain/src/sizing.rs:74-125`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| CLI memory variants and conversions | direct import | yes | `crates/microvms-cli/src/cli.rs:448-498` |
| CLI resource-request resolution | direct import | likely | `crates/microvms-cli/src/cli.rs:503-534` |
| Control-plane run and image data | direct import | likely | `crates/microvms-app/src/control/mod.rs:368`, `crates/microvms-app/src/control/image.rs:72` |
| Python size factories | direct import | likely | `bindings/microvms-py/src/cost.rs:463-491` |
| Node size factories | direct import | likely | `bindings/microvms-js/src/cost.rs:363-401` |
| Published client defaults | indirect | likely | `crates/microvms-app/src/defaults.rs:51-52`, `crates/microvms-app/src/defaults.rs:159` |
| Agent recipe sizes | direct import | likely | `crates/microvms-app/src/agents/mod.rs:44-52` |
| CLI cost report | indirect | likely | `crates/microvms-cli/src/commands/cost.rs:33-34` |
| Constants emission and independent size table | config | yes | `crates/microvms-domain/src/constants.rs:727-739`, `tools/check-model-drift.py:213-219` |
| CLI domain and resource-request tests | test | yes | `crates/microvms-cli/src/cli.rs:2793-2810`, `crates/microvms-cli/src/cli.rs:3962-3980` |
| Binding sizing and billing tests | test | likely | `bindings/microvms-py/tests/test_smoke.py:406-430`, `bindings/microvms-js/__test__/smoke.mjs:315-340` |
| Shared size and cost case runner | test | likely | `crates/microvms-core/tests/parity_cases.rs:35-73` |
| Public capability mapping | config | likely | `verify/parity/capabilities.toml:759-790` |

### Blast-radius notes

- Read baseline and peak from the selected row. The accessors use a table lookup;
  a peak-to-baseline formula would discard the table's independent values.
  `crates/microvms-domain/src/sizing.rs:214-244`
- Resource requests choose the smallest class whose baseline covers both axes.
  Missing or zero requirements select the default when neither axis remains.
  `crates/microvms-domain/src/sizing.rs:179-196`
- `SizeClass::ALL`, the row order, and the private index mapping must agree. A new
  class requires reviewing these together with the CLI's exhaustive conversion.
  `crates/microvms-domain/src/sizing.rs:138-145`,
  `crates/microvms-domain/src/sizing.rs:273-280`,
  `crates/microvms-cli/src/cli.rs:463-481`

## Region values

Defined at: `crates/microvms-domain/src/region.rs:46-79`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| CLI region variants and conversion | direct import | yes | `crates/microvms-cli/src/cli.rs:545-583` |
| Python named factories and parser | direct import | yes | `bindings/microvms-py/src/region.rs:32-82` |
| Node named factories and parser | direct import | yes | `bindings/microvms-js/src/region.rs:35-84` |
| Signed transport configuration | direct import | likely | `crates/microvms-edges/src/control/transport.rs:48-81` |
| Control-plane identity | direct import | no | `crates/microvms-app/src/control/mod.rs:154`, `crates/microvms-app/src/control/mod.rs:268-270` |
| Constant emission and independent region list | config | yes | `crates/microvms-domain/src/constants.rs:718-725`, `tools/check-model-drift.py:201-207` |
| Preflight and registered-name resolution | direct import | likely | `crates/microvms-app/src/preflight.rs:40-47`, `crates/microvms-app/src/names.rs:55-63` |
| Agent recipe region | direct import | likely | `crates/microvms-app/src/agents/mod.rs:49` |
| Pinned rate-table region | indirect | likely | `crates/microvms-domain/src/cost.rs:1007-1011` |
| Region parsing and round-trip tests | test | yes | `crates/microvms-domain/src/region.rs:197-207`, `crates/microvms-domain/src/region.rs:240-254` |
| Binding region-list tests | test | yes | `bindings/microvms-py/tests/test_smoke.py:296-316`, `bindings/microvms-js/__test__/smoke.mjs:262-277` |

### Blast-radius notes

- The supported region list is a measured client policy, separate from the service-model
  enum. Updating it needs new platform evidence as well as a matching independent
  literal in the comparison script.
  `crates/microvms-domain/src/region.rs:65-73`, `tools/check-model-drift.py:949-965`
- `Region::unlisted` normalizes a supported name to its named variant. Preserve
  that equality when changing factories or parsing.
  `crates/microvms-domain/src/region.rs:104-122`
- Environment resolution tries `AWS_REGION`, then `AWS_DEFAULT_REGION`, and
  applies the same parser. A change to parsing reaches default construction as
  well as explicit CLI and binding arguments.
  `crates/microvms-domain/src/region.rs:130-146`

## Pinned cost rates

Defined at: `crates/microvms-domain/src/cost.rs:1007-1021`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| Independent rate literals and source parser | runtime dispatch | yes | `tools/check-live-rates.py:119-142`, `tools/check-live-rates.py:169-210` |
| CLI cost command | direct import | likely | `crates/microvms-cli/src/commands/cost.rs:18`, `crates/microvms-cli/src/commands/cost.rs:33-34` |
| Python rate factory and report defaults | direct import | likely | `bindings/microvms-py/src/cost.rs:583`, `bindings/microvms-py/src/cost.rs:954` |
| Node rate factory and report defaults | direct import | likely | `bindings/microvms-js/src/cost.rs:482`, `bindings/microvms-js/src/cost.rs:837` |
| Completed-run cost attachment | direct import | likely | `crates/microvms-cli/src/commands/lifecycle.rs:1363-1396` |
| Shared cost case runner | test | likely | `crates/microvms-core/tests/parity_cases.rs:224-233` |
| Report's preserved rate table | indirect | no | `crates/microvms-domain/src/cost.rs:1471-1496` |
| Independent rate and storage-unit assertions | test | yes | `crates/microvms-domain/src/cost.rs:2648-2677` |
| Binding cost-contract tests | test | likely | `bindings/microvms-py/tests/test_smoke.py:531-546`, `bindings/microvms-js/__test__/smoke.mjs:428-442` |
| `docs/PLATFORM.md` rate evidence | config | yes | `crates/microvms-domain/src/cost.rs:988-998` |

### Blast-radius notes

- The comparison script locates `pinned_rates` and its decimal literals by source
  text. A function rename or different literal representation needs a matching
  parser update even when Rust callers compile.
  `tools/check-live-rates.py:133-142`, `tools/check-live-rates.py:177-199`
- Snapshot storage is expressed per GB-month in the table but per GB-hour in
  the catalog. Preserve the unit conversion and its independent assertion.
  `crates/microvms-domain/src/cost.rs:988-994`,
  `crates/microvms-domain/src/cost.rs:2670-2677`
- Reports retain their own rate table. A new pinned table changes future reports;
  it should not make an existing report depend on whatever rates are current
  when rendered. `crates/microvms-domain/src/cost.rs:1471-1496`

## CLI manifest

Defined at: `crates/microvms-cli/src/manifest.rs:51-117`.

| Downstream | Type | Touch on change | Citation |
| --- | --- | --- | --- |
| Manifest command output | direct import | likely | `crates/microvms-cli/src/commands/local.rs:933-941` |
| Clap command and flag tree | runtime dispatch | likely | `crates/microvms-cli/src/manifest.rs:51-85` |
| Command response type and key registry | indirect | yes | `crates/microvms-cli/src/commands/mod.rs:110-119`, `crates/microvms-cli/src/commands/mod.rs:674-684` |
| Global-flag and client-default publication | indirect | likely | `crates/microvms-cli/src/manifest.rs:97-111` |
| Committed manifest artifact | config | yes | `.config/mise/tasks/contracts.toml:123-164` |
| CLI command, flag-domain, and exit-code tests | test | yes | `crates/microvms-cli/tests/manifest.rs:46-80`, `crates/microvms-cli/tests/manifest.rs:90-112`, `crates/microvms-cli/tests/manifest.rs:195-238` |
| Streaming response contract test | test | likely | `crates/microvms-cli/src/manifest.rs:369-402` |
| Conformance command discovery | indirect | likely | `conformance/lanes/local.py:82-89` |
| Binding default mapping | config | likely | `verify/parity/capabilities.toml:868-884` |

### Blast-radius notes

- Command names and flags come from clap, but response shapes come from
  `RESPONSE_TYPES`. Adding a command needs both a parser entry and a response
  registry entry. `crates/microvms-cli/src/manifest.rs:51-85`,
  `crates/microvms-cli/src/commands/mod.rs:110-119`
- Global flags are gathered before clap propagates them to subcommands. Preserve
  their separate publication rather than duplicating them per command.
  `crates/microvms-cli/src/manifest.rs:97-103`,
  `crates/microvms-cli/src/manifest.rs:666-685`
- The streaming exec response is a separate contract published through
  `alternateResponse`; a consumer cannot use the ordinary response shape for
  that mode. `crates/microvms-cli/src/manifest.rs:61-79`,
  `crates/microvms-cli/src/manifest.rs:369-402`

## See also

These pages cite many of the same source files as this one.

- [contract map](contract-map.md)
- [module map](../architecture/module-map.md)
- [processes](../behavior/processes.md)
- [business logic](business-logic.md)
- [debugging guide](debugging-guide.md)
