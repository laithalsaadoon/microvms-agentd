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
  "known_drift": { "py": { "issue": "#255", "keys": ["label"] } },
  "skip": { "ts": "why no offline call can answer this case there (IMAGE-12)" }
}
```

- `capability` names a row of `verify/parity/capabilities.toml`.
- `input` is what every surface is given. Its keys belong to the area's handlers.
- `expect` is the one answer. A refusal is `{"error": {...}}` with any of `code` (the `ERR_*`
  string), `wire_kind` (the daemon status class, `null` for a local refusal) and `retryable`;
  only the facets it names are compared.
- `ignore` lists dot paths to leave out on both sides, such as the clock-dependent `staleness`.
- `known_drift` marks a surface that disagrees today, with the issue that fixes it and the dot
  paths where it disagrees. Each of those paths must differ from `expect` and everything else
  must match. A path that starts agreeing fails with "now agrees; remove known_drift", so the
  fixing change removes the marker (pytest's `xfail(strict=True)`, in every runner). A marked
  path only has to differ, so the corpus doesn't pin today's wrong answer; the surface's own
  suite does (for `build --reuse`'s name, the CLI's reuse guards). A path
  covers everything under it: marking `items` leaves every line item uncompared on that
  surface, so mark the narrowest path the drift reaches, and give the inputs a drift hides
  their own case where the surface's defaults don't get in the way.
- `skip` names a surface the row names but no offline call can reach for this case, with the
  reason. The reason ends by naming the issue or trace id that holds the gap, as `(#N)` or
  `(IMAGE-12)`.

A marker's issue and a skip's reference are checked for their shape here. The ratchet counts
both as `parity-drift` (#320): one finding per marked path, keyed
`<area>/<case>/<surface>: known_drift <path>`, and one per skip, `<area>/<case>/<surface>: skip`.
So a change can't add a marker or a skip the merge base doesn't have, and the change that fixes
a surface deletes its marker and edits nothing else. A skip no open issue will close is a
decision in `verify/ratchet/decisions.toml`, with the trace id it cites in its reason. A marker
is counted beside the table's exemption for the same gap, not instead of it.

Numbers compare by value, and nothing else is coerced.

## Which surfaces run a case

- A surface the row names runs the case, unless `skip` names it.
- A surface the row exempts is skipped, and the runner prints the exemption's reason (the Rust
  runners write it straight to stderr, so it shows in a passing `cargo test`). When the
  exemption has an `issue` and `known_drift` names that same issue for the surface, the surface
  runs the case anyway and must disagree where the marker says: the table records the gap, and
  the case measures it.
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
- `cost`: estimate reports as JSON, the CLI's `cost --json` shape. `input.defaults` is what a
  caller gets for the `launched` and `label` arguments it leaves out; core has no defaults for
  them, so its runner passes these (#255 moves them into core). Every case passes
  `suspend_resume_cycles` to every surface: the CLI's `--cycles` defaults to 1 and the
  bindings' `suspend_resume_cycles` to 0, and no case holds that drift yet, because a marker
  needs the issue that decides which default is right (#255 for cost defaults, #300 for
  surface defaults).
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
