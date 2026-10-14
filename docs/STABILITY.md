# Stability from 1.0

From 1.0.0, microvms uses semantic versioning. This covers its public clients and
the daemon's wire API. A patch fixes a fault within the rules we state. Minor releases
add features that work with existing clients. Removing a feature or breaking
these rules needs a major release.

Adding an optional flag fits a minor release. Removing a public flag needs a
major release.

The Rust, Python, Node.js, and daemon packages use the same release number.
`PROTOCOL_VERSION` tracks breaks in the wire contract. These version
values need not match. Check the daemon's contract when choosing
client and daemon versions. The `/v1/` path alone does not prove they work together.

## Covered contracts

| Package or wire API | Rules within a major release |
| --- | --- |
| `microvm` command-line tool | Public commands, flags, settings from the environment, defaults, exit codes, and JSON fields keep their contract. New commands, optional flags, and extra JSON fields may appear. Help layout and text for people may change. |
| PyPI `microvms` | Public names and valid calls we state, keyword defaults, return values, exceptions, and sync and async behavior keep their contract. `microvms.pyi` gives the types. |
| npm `microvms` | Public exports and valid calls we state, defaults, return values, errors, and async behavior keep their contract. `index.d.ts` gives the types. |
| Rust crates | Public APIs must keep users' code building with the Rust toolchain we support. The promise also covers the behavior we state, feature names, and defaults. |
| Daemon wire API | Routes, methods, required auth steps, request and response fields, status meanings, stream frames, and lifecycle rules keep their contract. See [the wire schema](schema.json) and [the wire guide](PROTOCOL.md). |

The promise covers these Rust crates: `microvms-protocol`, `microvms-domain`, `microvms-app`,
`microvms-edges`, `microvms-core`, and `microvms-cli`.

We state the oldest Rust, Python, and Node.js versions we support. Raising the
Rust or Node.js floor needs a major release. The Python floor follows
[CPython's support lifecycle](https://devguide.python.org/versions/): a minor
release may drop a Python version after CPython ends support for it. The changelog
lists that drop under Removed. Other changes to the packages we use must preserve
these runtime needs and public contracts.

This promise does not cover private code, the layout of API docs, or
test support. It does not cover features we mark experimental before their first release.
Once a feature is stable, we cannot use that label to avoid this policy.

The promise covers this project's behavior. AWS sets which services run where,
its quotas, and its prices. AWS may change its services. [Platform](PLATFORM.md)
records dated findings. It doesn't promise future AWS behavior.

## Wire changes

Within a major release, an older client must work with a newer daemon, and a
newer client must work with an older daemon. New clients must handle the absence
of optional features in older daemons. The schema states the rules.
[The wire guide](PROTOCOL.md) explains how to encode fields and exchange stream frames.

A change breaks the wire contract if a supported peer can no longer complete a
supported task. It also breaks the contract if the peer reads the result with a
new meaning. Examples include:

- Remove or rename a route, method, field, status meaning, or stream event.
- Require a field, auth step, or handshake message an older peer does not send.
- Reject a value we accept now or narrow a field's type or range.
- Change a default's meaning.
- Omit a response field an older client needs, or change its type, units, or meaning.
- Change frame encoding, handshake context, order, end signals, or lifecycle rules
  so an older peer cannot exchange valid messages.

An optional request field or an extra response field can work with older peers
when they can decode it. Check each reader before adding an enum value. A reader
that rejects unknown variants makes that change a break. Changes to limits need
a review of the inputs we promise to accept and the stream behavior we state.

An intended break needs a major release and a new `PROTOCOL_VERSION`. Release
notes must describe the break and how to migrate. Record breaks that the schema
check detects in `docs/schema-breaks.toml`. A break that the check cannot detect
still needs the same version change and review.

## Before removal

For Python versions, the CPython support lifecycle above governs removal.
Announce the deprecation of any other stable feature before removing it. State what to
use in its place and how to move to it in the docs and release notes.
Use tags or warnings to mark the feature when the language has them.

Keep the feature working for at least six calendar months and two minor releases
from the first stable release that announces the change. Removal must wait
for a later major release after both parts of that window have passed. The
notice must state the first version that could remove the feature. We may
keep the feature past that version.

## Checks and review

- `mise run semver:check` compares the library crates with their published Rust
  APIs. The command-line tool and changes in behavior also need tests and review.
  A passing API check alone does not prove they keep their contracts.
- `mise run schema:compat` compares the wire schema with the last release.
  It checks the rules for routes and message fields in [the wire guide](PROTOCOL.md).
  Reviews and wire tests must also check framing, auth, meaning, and other rules
  it cannot prove.
- `mise run parity:check` checks enrolled names and defaults across Rust, the
  command-line tool, Python, and Node.js. Cases in `verify/parity/cases/` check a
  shared expected result through each client. A fix to shared behavior adds a
  shared case.

Changes must pass these checks and keep this promise. A passing check does not
permit a break outside its scope. Release review must check each planned removal
against the stated window. It must also check the steps for users who need to move
to a new API. Reviewers must check runtime needs and behavior against this policy.
