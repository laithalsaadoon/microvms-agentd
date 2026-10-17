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

The dispatch names a reviewed manifest by source run ID and the SHA-256 of its
exact file bytes. Manifests retain hashes, source-run metadata and proof labels;
raw source logs and resource names stay in memory. Parser version 2 hashes the
UTF-8 encoding of Python's text output from `gh run view <run> --job <job> --log`,
with universal newline normalization. It verifies the exact source workflow and
log bytes, derives ownership again from positive source observations, and binds
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
before deletion, refuses active VM and image-version log dependencies, deletes
images before log groups, records whether it requested each deletion, and checks
absence through a visibility interval and an independent verifier. Other
resources are reported by hash and left alone.

`RunMicrovm` accepts an explicit per-VM log destination, but the VM list and get
responses do not expose it. While any selected log group is present, recovery
therefore requires every VM in the complete regional inventory to be
`TERMINATED`, including VMs using unrelated images. It repeats this check before
each log deletion. The check cannot exclude a VM launched after the last read,
so the no-other-writer precondition also covers launches that use selected log
names. When every selected log is absent, image recovery retains its ordinary
image-reference dependency check.

Run `uv run --locked pytest tools/test_recover_live*.py tools/test_recovery_credentials.py`
for the offline selector, provenance, failure, credential and privacy cases. The seeded faults are in
`verify/guards/faults/recovery.toml`. These tests do not establish AWS behavior;
the hosted recovery path remains unverified against AWS until a run succeeds.
The source incident's cancelled run is not acceptance evidence.
