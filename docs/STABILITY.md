# Stability from 1.0

Starting with 1.0.0, microvms uses semantic versioning for its published client
surfaces and daemon protocol. A patch release corrects behavior within the
published contract. A minor release adds compatible capabilities. Removing a
supported capability or changing its contract requires a major release.

The Cargo, Python, Node.js, and daemon releases share a version. The wire
protocol also carries `PROTOCOL_VERSION`, which changes when the wire contract
breaks. A protocol version and a package version serve different purposes;
clients must check the daemon contract when selecting compatible versions.
The `/v1/` route prefix alone does not establish compatibility.

## Covered surfaces

| Surface | Compatibility promise |
| --- | --- |
| `microvm` CLI | Documented commands, flags, environment settings, defaults, exit codes, and JSON response fields remain compatible within a major release. New optional flags, commands, and response fields may be added. Human-readable diagnostics and help layout may change. |
| PyPI `microvms` | Documented public names, call signatures, keyword defaults, return values, exceptions, and synchronous and asynchronous behavior remain compatible. The generated `microvms.pyi` describes the typed surface. |
| npm `microvms` | Documented public exports, signatures, defaults, return values, errors, and asynchronous behavior remain compatible. The generated `index.d.ts` describes the typed surface. |
| Published Rust crates | Public APIs of `microvms-protocol`, `microvms-domain`, `microvms-app`, `microvms-edges`, `microvms-core`, and `microvms-cli` remain source-compatible with the supported Rust toolchain within a major release. Documented behavior, feature names, and defaults are covered too. |
| Daemon wire protocol | Routes, methods, authentication requirements, request and response shapes, status meanings, streaming frames, and lifecycle semantics in [the wire schema](schema.json) and [Protocol](PROTOCOL.md) remain compatible within a major release. |

A documented minimum Rust, Python, or Node.js version is part of the supported
runtime contract. Raising that minimum requires a major release. Dependencies
may change when they preserve the supported surface and runtime contract.
Private implementation details, generated reference formatting, test support,
and features explicitly documented as experimental are outside this promise.
Experimental features must be identified before they are released; an existing
stable feature cannot become experimental to bypass this policy.

The promise covers this project's behavior. AWS service availability, quotas,
pricing, and service changes are external constraints. [Platform](PLATFORM.md)
records dated observations rather than promising future AWS behavior.

## Wire compatibility

An upgrade must support both an older client with a newer daemon and a newer
client with an older daemon within the same major release. New clients must
handle the absence of optional capabilities that older daemons do not offer.
The schema describes the contract; [Protocol](PROTOCOL.md) explains the
serialization rules and stream behavior.

A wire change is breaking when a previously supported peer can no longer
complete the same supported operation, or interprets its result differently.
Examples include:

- Removing or renaming a route, method, field, status meaning, or stream event.
- Requiring a request field, authentication step, or handshake message that
  an older peer does not send.
- Removing an accepted value, narrowing a field's type or range, or changing
  the meaning of a default.
- Omitting a response field that an older client requires, or changing its
  type, units, or meaning.
- Changing frame encoding, handshake context, ordering, completion signals,
  or lifecycle semantics so an older peer cannot communicate correctly.

Adding an optional request field or a response field with compatible decoding
is ordinarily compatible. Adding an enum value requires checking every reader;
readers that reject unknown variants make that addition breaking. Changes to
limits require review of the documented accepted inputs and stream behavior.

Intentional breaks ship in a major release, increment `PROTOCOL_VERSION`, and
are described in release notes with a migration path. The schema-detected
breaks are recorded in `docs/schema-breaks.toml`. Protocol changes outside that
check's coverage still require the same versioning and review.

## Deprecation and removal

A stable capability is deprecated before it is removed. Its replacement and
migration steps appear in its reference documentation and release notes.
Language-level deprecation annotations or warnings are used where available.

The deprecated capability continues to work for at least six calendar months
and two minor releases after the first stable release that announces the
deprecation. Removal may occur only in a later major release after both parts
of that window have elapsed. A deprecation announcement names the earliest
eligible removal version; reaching that version does not require removal.

## Enforcement

- `mise run semver:check` checks the library crates against their published
  Rust APIs. The CLI executable and behavioral compatibility also require
  regression tests and review; a successful API comparison alone is not proof.
- `mise run schema:compat` compares the generated wire schema with the previous
  release. It covers the structural rules documented in [Protocol](PROTOCOL.md).
  Review and protocol tests cover framing, authentication, semantics, and
  other changes that the structural comparison cannot establish.
- `mise run parity:check` checks enrolled capabilities and defaults across
  the Rust, CLI, Python, and Node.js surfaces. The shared cases in
  `verify/parity/cases/` run against a common expected result on each surface.
  New behavior fixes add cases when the behavior is shared.

A change must satisfy these checks and the compatibility promise. A passing
check does not authorize an incompatible change outside its coverage. Release
review verifies deprecations, migration guidance, runtime requirements, and
behavior against this policy.
