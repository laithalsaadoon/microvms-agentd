# Live validation

## 2026-09-24, us-east-1, API 2025-09-09: end to end against GitHub and AWS

The current design ran end to end: a deployed stack, a coding-agent image built
from this repository's daemon, and a private scratch repository with one issue
and one pull request. Both jobs were submitted with `cli.py submit`, and no
client process stayed running; progress was read only with `cli.py show` and
`list`. The Lambda was the arm64 build packaged from published wheels
(`microvms` 0.9.0), and the VM was managed only through the SDK
(`Sandbox.run`, `Sandbox.adopt`, `ControlPlane`).

| Job | Outcome | Wall time | Estimated VM cost |
| --- | --- | --- | --- |
| Pull request with a deliberate bug (`subtract` returning `b - a`, hidden by a test with equal arguments) | `SUCCEEDED`; one `COMMENT` review with two line comments naming the swapped operands and the test that cannot see them | 1 min 58 s | about $0.007 |
| Issue asking for `multiply` with tests | `SUCCEEDED`; one draft PR with `Closes #1`, changing only the module and its tests; the four tests passed when run from the PR branch | 1 min 29 s | about $0.006 |

- Both jobs' VMs read back `TERMINATED` from the control plane afterwards.
- `cli.py fetch` returned `REPORT.md`, the transcripts, and for the issue the
  patch and exported changes.
- The first issue submission failed in staging with `list index out of range`:
  PyGithub's slice of an issue with no comments raises. Staging now iterates
  the comments, a regression test covers zero, a few, and more than 30
  comments, and the resubmitted job is the one in the table. It failed before
  launching, so it left no VM.
- Teardown was verified independently: the stack destroyed, the image and its
  build log group deleted, the build artifact removed, and
  `scripts/verify-clean.py` clean of everything the project can name.

## 2026-09-18, us-east-1: earlier design

The previous version took a local Git checkout and returned a patch, and it
shared this version's preparation, agent start, polling, and collection code.
An implementation task succeeded in about 2 minutes 16 seconds with durable
waits between polls; the patch applied to a fresh clone and its 10 tests
passed, and the VM read back `TERMINATED`.
