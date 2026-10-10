# The shared case corpus

`verify/parity/capabilities.toml` says which surface has which capability. These cases say whether the
surfaces give the same answer. Each file is one question with one expected answer, and core, the
CLI, Python and TypeScript each run it through their own entry point and compare with `expect`.
No runner calls another, so agreement between surfaces is transitive through the file.

The runners:

- core: `crates/microvms-core/tests/parity_cases.rs`, under `cargo test`.
- CLI: `crates/microvms-cli/tests/parity_cases.rs` for what a spawned `microvm` answers, and the
  `crates/microvms-cli/src/guards/parity.rs` for the cases that need a scripted control
  plane or daemon.
- Python: `bindings/microvms-py/tests/test_parity_cases.py`, under pytest.
- TypeScript: `bindings/microvms-js/__test__/parity_cases.mjs`, under `node --test`.

The Rust runners share `crates/microvms-core/tests/parity_corpus/mod.rs`; the Python and TypeScript
runners restate the same rules. This file is the contract all of them follow.

## A case

`<area>/<case>.json`, one JSON object:

```json
{
  "capability": "estimate",
  "input": { "size_mib": 2048, "running_seconds": 0 },
  "expect": { "label": "estimate" },
  "ignore": ["staleness"],
  "skip": { "ts": "why no offline call can answer this case there (IMAGE-12)" }
}
```

- `capability` names a row of `verify/parity/capabilities.toml`.
- `input` is what every surface is given. Its keys belong to the area's handlers.
- `expect` is the one answer. A refusal is `{"error": {...}}` with any of `code` (the `ERR_*`
  string), `wire_kind` (the daemon status class, `null` for a local refusal) and `retryable`;
  only the facets it names are compared.
- `ignore` lists dot paths to leave out on both sides, such as the clock-dependent `staleness`.
- No key marks a surface as disagreeing. Every runner refuses a `known_drift` key, because the
  ratchet enforces parity-drift (#258): a surface gives the case's answer, or `skip` names it.
- `skip` names a surface the row names but no offline call can reach for this case, with the
  reason. The reason ends by naming the issue or trace id that holds the gap, as `(#N)` or
  `(IMAGE-12)`.

A skip's reference is checked for its shape here. The ratchet counts each skip as
`parity-drift` (#320), keyed `<area>/<case>/<surface>: skip`, and enforces the category, so every
skip is a decision in `verify/ratchet/decisions.toml`, with the trace id it cites in its reason.

Numbers compare by value, and nothing else is coerced.

## Which surfaces run a case

- A surface the row names runs the case, unless `skip` names it.
- A surface the row exempts is skipped, and the runner prints the exemption's reason (the Rust
  runners write it straight to stderr, so it shows in a passing `cargo test`).
- A runner fails on a case it has to run in an area it has no handler for, so a surface can't
  drop out of the corpus without the table saying so.

Each runner fails when no case ran in an area it handles, when a case it planned was never
judged, and when `wrap-dockerfile/sentinel.json` wasn't loaded, or didn't run on a surface that
handles its area. A wrong path, a loader that finds nothing, or a skip on every case fails
instead of passing over an empty corpus.

The CLI's two tiers split the areas between them in one place, `CLI_PROCESS_AREAS` and
`CLI_FAKE_AREAS` in `parity_corpus/mod.rs`. A tier handed an area it has no handler for fails
on its first case there, and an area neither list names fails the no-handler rule.

The Python and TypeScript runners send HTTPS through a proxy on a loopback port nothing
listens on. A launch or image case is meant to be refused before any AWS call, and if that
refusal regresses the case fails on a connection error rather than a signed request.

## The areas

- `image-name`: the name `ensure_image` gives an image and the CLI's `build --reuse` gives the
  same inputs (`ensure-image`), and the name `AgentVm.image_name` gives an agent image
  (`agent-image-name`). The binary is `binary_hex`, built from the default Dockerfile on AL2023
  at the default agent port.
- `cost`: reports (`run-report`, with `input.running_seconds` measured) and estimates
  (`estimate`) as JSON, core's `CostReport::to_json` shape, which the CLI's `cost --json`,
  Python's `to_dict` and TypeScript's `toJson` all emit; and comparisons (`compare-residency`)
  as `cycles`, `ratio` and `render`, the accessors every surface has. Every surface leaves
  `launched` and the label out, so each case holds core's defaults (#255): the launch core
  infers from the plan, and `DEFAULT_RUN_LABEL` or `DEFAULT_ESTIMATE_LABEL`, which core's runner
  passes since core takes no optional arguments. A case that leaves out
  `suspend_resume_cycles` (a report) or `cycles` (a comparison) asks about each surface's
  default: the CLI leaves `--cycles` off, the bindings leave the argument out, and core's
  runner passes `DEFAULT_REPORT_CYCLES` or `DEFAULT_RESIDENCY_CYCLES`. A report then counts no
  cycle and a comparison prices one (#280).
- `error`: a daemon status answered to one call, as the error's code, wire kind and
  retryability.
- `egress`: a launch's egress options, refused or classified.
- `names`: a VM adopted by name (`from-name`) from a registry holding `input.record_text` as
  `<name>.json`, in `input.region`. A record from another region and a torn one are refused
  before any AWS call. Each refusal also answers `message_mentions`: for each string in
  `input.message_mentions`, whether the message contains it, so a case can require the file
  to inspect or both regions and forbid the agent token. The CLI answers through `exec --name`
  against a seam that refuses every door, and core through `names::resolve` and
  `Sandbox::adopt_record` on an offline plane, which is `Sandbox::from_name` with its plane
  swapped, so a regression that lets a record through fails without reaching AWS.
- `size-class`: the class a resource request selects, or the refusal.
- `wrap-dockerfile`: a task Dockerfile with the agentd stanza appended.
