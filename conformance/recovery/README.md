# Reviewed hosted recovery

The live workflow's `recover` mode is an incident-specific cleanup path. It runs
only from the main branch after the owner check, with a credential-free
preflight and the existing protected live environment. A recovery run cannot
produce the release gate's live-conformance marker. The full live suite remains
the acceptance path.

The hosted credential helper reruns preflight before requesting an OIDC token.
It retains credentials only in the recovery child process, captures provider
diagnostics, and preserves only a validated, sanitized recovery receipt. One
execution deadline covers both child phases and authentication; the core shares
its remaining budget across source validation, cleanup and verification.
Read-only AWS observations make at most six attempts after classified transient
failures, with equal jitter over capped exponential delays that fit strictly
within that deadline. Deletion calls retain their existing retry policy.
An optional IAM role-duration read can be unavailable; its fixed error code
does not grant authority or establish a configured session maximum.

The dispatch names a reviewed manifest by source run ID and the SHA-256 of its
exact file bytes. Manifests retain hashes, source-run metadata and proof labels;
raw source logs and resource names stay in memory. Parser versions 3 and 4 fetch the
pinned job directly with `gh api repos/<repository>/actions/jobs/<job>/logs`.
It captures `gh api --help` first and adds `--allow-escape-sequences` when that
exact option is present; newer CLI versions otherwise refuse escape characters
even in captured output. Both commands share the source-validation deadline.
The flag permits only in-memory capture and hashing, not terminal display.

The log digest covers the UTF-8 encoding of Python's captured text, after its
universal newline normalization and before parsing. The representation requires
LF-only text with a final LF. It preserves an initial BOM, blank lines, tabs,
escape characters and each line's LF. The manifest's `source_log_spans` assign
each raw line to one reviewed step with its exact number and name, one-based
`start`, positive `count`, and `sha256` of the corresponding UTF-8 slice including
line endings. Spans cover the entire log in order without gaps or overlap.
Timestamps do not determine attribution.

The `source_job_steps_sha256` pins the complete ordered API step list, including
skipped steps. Its canonical JSON projects `number`, `name`, `status` and
`conclusion`, sorts object keys, uses comma and colon separators without spaces,
uses JSON's default ASCII escapes, and is encoded as UTF-8 without a trailing LF.
Step numbers are unique and increasing; names may repeat. Each non-skipped step
must have one span with its exact number and name. Only after checking the raw
digest, step metadata, span coverage and slice hashes does the parser prefix
each original line with the job and step labels. It verifies the exact source workflow,
derives ownership again from positive source observations, and binds
observational infrastructure hashes to those same logs. A name prefix or absence
from an earlier inventory does not establish ownership.

The manifest also permits an exact `explicit-incident-image` selection when the
maintainer has directly authorized that image. Its cleanup-log occurrence only
resolves the hashed name; it does not prove creation by the source run. This
selection retains the same creation-time and dependency checks. An associated
log group requires its own reviewed manifest row and ownership evidence.

Parser version 4 also accepts the incident-owned log policy. Its reviewed
historical anchors identify the earlier authenticated image-recovery receipt,
producer, manifest, source run, region and exact image hashes. They record a
review already performed; the helper does not fetch or reauthenticate that
historical receipt at runtime. The original receipt remains separate, including
its original overall outcome and accepted image deletion requests.

The `incident-default-log` row binds the exact default log name to the
source-resolved incident image. This scope relies on trusted incident attribution,
the reviewed historical image evidence, and a fresh log creation time inside the
source job interval. Before deletion, the helper repeats the log fingerprint,
confirms every selected image absent with an exact Get, and checks a complete
regional VM census for any state other than `TERMINATED`. It checks every
selected log name for known image-version references, even when the log is
already absent. Concurrent image builds, malformed metadata, conflicting logging
configurations, incomplete pages and API failures still refuse cleanup.

Only this policy permits an unrelated version whose Get and List both omit
logging. The receipt labels that destination `unknown`; it does not mean disabled
or default logging. Every later version is still checked for explicit references.
The fresh receipt's `incidentLogEvidence` records log incarnation and predelete
checks, or an already-absent outcome without inventing a deletion. It does not
promise that an unknown future caller cannot select or recreate a deleted group.

The owner must assert that no other writer can create or replace the selected
names during recovery. This operational precondition is necessary because the
provider's deletion APIs have no atomic creation-time condition. It does not
supply ownership evidence. Recovery requires each surviving selected resource's
creation time to fall within the original job interval, rechecks its identity
before deletion, refuses active references to each selected image, deletes
images before checking log-group dependencies, records whether it requested each
deletion, and checks absence through a visibility interval and an independent
verifier. Other resources are reported by hash and left alone.

Image deletion does not depend on unrelated image versions' log configuration.
Before each log deletion, recovery checks complete regional VM and image-version
inventories. The ordinary policy refuses missing or ambiguous log destinations;
the incident policy handles only missing destinations as described above.
If that phase fails, the failed receipt retains accepted image deletion requests
and their exact Get absence results; those results do not substitute for the
final inventory window or independent verifier.

When an exact version Get omits its logging field, recovery can use an explicit
logging configuration from that version's List row. The image ARN, version,
creation timestamp and known state must agree. A present malformed Get value,
missing configuration in both responses under the ordinary policy, or conflicting
explicit configurations under either policy, still refuse log deletion. This is
positive configuration evidence, never an inference that missing logging means disabled or names a default destination.
Optional version evidence retains Get-only identity and build metadata and
`logGroupSha256`; `loggingStatus` summarizes the validated paired Get/List evidence.

`RunMicrovm` accepts an explicit per-VM log destination, but the VM list and get
responses do not expose it. Before deleting any selected log group, recovery
therefore requires every VM in the complete regional inventory to be
`TERMINATED`, including VMs using unrelated images. It repeats this check before
each log deletion. The check cannot exclude a VM launched after the last read,
so the no-other-writer precondition also covers launches that use selected log
names. Image deletion always retains its ordinary image-reference dependency
check.

## Read-only observations

Dispatch with `mode: recover` and `observe_only: true` to collect a separate
`recovery-observation` artifact containing `recovery-observation.json`. The flag
defaults to false and is rejected with full-suite mode. The same main-branch,
owner, manifest, source-provenance, and protected-environment checks apply. The
credential wrapper selects `recover-live.py observe` explicitly; it never falls
back to cleanup after a failed observation.

The observer first captures the independent verifier's classified census, then
collects regional images, their versions, and the service log-group namespace
before scanning VM history. These reads are not an atomic snapshot. Every
regional image's full version list is recorded, including deleted versions, with
an exact Get for each version. Per-image coverage records distinguish an empty list from a failed or
partial list. Approved image Get calls also run when the image is absent from
List, using the source ARN when available. An approved name without an original
ARN is read using the authenticated shared image's canonical account and region
namespace. A missing or invalid source namespace leaves the observation
incomplete. This derived request does not add an original ARN or deletion
authority to the source proof. If Get finds an image absent from List, its
version coverage is unknown and the image and version sections remain
incomplete. If the name now resolves to another image, the source and current
List identities remain separately identifiable by their hashes.

A separate API boundary permits only the observer's List, Get, and log-group
description operations. The observer does not invoke cleanup selectors or log
dependency decisions. Missing, null, malformed, and unknown optional state or
logging fields remain classified data; missing logging never means disabled.
Version availability is read from the provider's `status` field. List and Get
projections retain only recognized values, finite timestamps, comparison flags,
and identity hashes. Raw names, ARNs, configurations, and provider messages are
excluded from the artifact.

New `live-recovery-observation` artifacts use schema version 2. The validator
continues accepting version 1 with its original rules and full VM rows. Version
2 retains each non-`TERMINATED` VM, including missing, null, malformed, and
unknown states, and compacts only exact `TERMINATED` identities into
`microvmCoverage`. That object records pages scanned, processed, terminated and
retained counts, a terminal identity digest, and pagination completion.

The VM scan requests `maxResults=50`, the maximum documented for
[`ListMicrovms`](https://docs.aws.amazon.com/lambda/latest/microvm-api/API_ListMicrovms.html),
and is bounded by the existing API deadline,
a thousand received pages, at most fifty rows per page, and an eight-KiB UTF-8
token. The response-page and token limits are observer limits, not AWS service
guarantees. Repeated tokens, duplicate VM identities, malformed pages, and any
limit or read failure leave the scan incomplete. Historical terminal rows do not
consume the shared retained-metadata budget; the retained-row, artifact-byte,
append-reserve, and independent-verifier limits are unchanged.

Coverage describes the successfully accounted unique prefix, including on
failure or interruption. The processed count equals the terminated plus retained
counts; the retained count equals the VM rows in the artifact. Completed
pagination requires a naturally ended scan with at least one received page,
even when empty. The terminal digest is SHA-256 over
`b"live-recovery-terminated-microvms-v1\0"` followed by the sorted, fixed-width
binary identity digests for the accounted terminal rows. The existing identity
scheme binds each digest to its region. This is the authenticated producer's
commitment to omitted identities; the artifact cannot independently reconstruct
them. Empty coverage uses the digest of that domain prefix alone. Coverage makes
no atomic-snapshot or stable-ordering claim. The duplicate-page measurement in
[PLATFORM.md](../../docs/PLATFORM.md#a-paginated-listmicrovms-can-return-one-vm-on-two-of-its-pages)
explains why duplicate identities remain an incomplete scan rather than proof of
a consistent census.

The schema has its own bounded validator. Its `completed` status means that all collection sections completed; it can contain
leaks and unknown configuration. An API, pagination, verifier, or output-limit
failure produces `incomplete` and preserves earlier safe observations. The
artifact always records `conformance_verified`, `recoveryAccepted`, and
`mutationsPerformed` as false. It does not replace a partial recovery receipt,
establish cleanup, or authorize deletion. This path is verified offline only
until its hosted exercise succeeds.

Run `uv run --locked pytest tools/test_recover_live*.py tools/test_recovery_credentials.py`
for the offline selector, provenance, failure, credential and privacy cases. The seeded faults are in
`verify/guards/faults/recovery.toml`, `verify/guards/faults/recovery-phases.toml`,
`verify/guards/faults/recovery-version-logging.toml`, and
`verify/guards/faults/recovery-integration.toml`. These tests do not establish AWS behavior;
the hosted recovery path remains unverified against AWS until a run succeeds.
The source incident's cancelled run is not acceptance evidence.
