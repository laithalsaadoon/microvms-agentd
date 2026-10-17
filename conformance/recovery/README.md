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
raw source logs and resource names stay in memory. Parser version 3 fetches the
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
selection retains the same creation-time and dependency checks and does not
authorize any associated log group.

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
Before each log deletion, recovery still checks the complete regional VM and
image-version inventories and refuses missing or ambiguous log destinations.
If that phase fails, the failed receipt retains accepted image deletion requests
and their exact Get absence results; those results do not substitute for the
final inventory window or independent verifier.

`RunMicrovm` accepts an explicit per-VM log destination, but the VM list and get
responses do not expose it. Before deleting any selected log group, recovery
therefore requires every VM in the complete regional inventory to be
`TERMINATED`, including VMs using unrelated images. It repeats this check before
each log deletion. The check cannot exclude a VM launched after the last read,
so the no-other-writer precondition also covers launches that use selected log
names. Image deletion always retains its ordinary image-reference dependency
check.

Run `uv run --locked pytest tools/test_recover_live*.py tools/test_recovery_credentials.py`
for the offline selector, provenance, failure, credential and privacy cases. The seeded faults are in
`verify/guards/faults/recovery.toml` and `verify/guards/faults/recovery-phases.toml`.
These tests do not establish AWS behavior;
the hosted recovery path remains unverified against AWS until a run succeeds.
The source incident's cancelled run is not acceptance evidence.
