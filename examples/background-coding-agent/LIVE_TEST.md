# Live validation

## 2026-10-11, us-east-1, API 2025-09-09: the async steps on microvms 0.12.0

The workflow ran end to end with every VM step on the binding's awaitables. The
Lambda was the arm64 build packaged from published wheels (`microvms` 0.12.0).
The image was built with the 0.12.0 CLI and no daemon path, so `microvm build`
fetched the `agentd` 0.12.0 release asset and verified its Sigstore attestation.
Jobs ran against a private scratch repository and were driven only with
`cli.py submit`, `list`, `show`, `fetch`, and `cancel`.

| Job | Outcome | Wall time | Estimated VM cost |
| --- | --- | --- | --- |
| Issue asking for `clamp(value, low, high)` with tests, Claude Code | `SUCCEEDED`; a draft PR with `Closes #5` changing only `calc.py` and `test_calc.py`; its six tests pass from the PR branch | 2 min 2 s | about $0.007 |
| Pull request whose `divide` uses `//` behind a test with evenly divisible arguments, Codex | `SUCCEEDED`; one `COMMENT` review with a line comment on `calc.py:10` naming the floor division | 1 min 56 s | about $0.007 |
| The same pull request, Claude Code, cancelled while the agent worked | `CANCELLED`; the durable execution read back `STOPPED`, the VM `TERMINATED`, and no review was posted | | |

Step durations for the first two jobs, from the durable execution history:
`prepare`, which waits for the boot while the inputs download, took 9.3 s and
8.9 s; the agent's durable wait took 91.5 s and 91.9 s; `cleanup` took 5.2 s
each. There is no step-level baseline for the blocking version, so these
are measurements, not a comparison.

- The first issue run, on this branch before the fix, published two
  `__pycache__/*.cpython-39.pyc` files the agent's test run had left in the
  working tree; the scratch repository has no `.gitignore`. The baseline now
  ignores test-run caches and dependencies after its commit, a test covers
  both the caches and a tracked vendored file, and the rerun's PR had only
  `calc.py` and `test_calc.py`.
- Six VMs ran across the session, and each read back `TERMINATED` from the
  control plane.
- `cli.py fetch` returned the report, the transcripts, and for the issue the
  patch and exported changes.
- Teardown, checked independently: the stack destroyed, the image, its build
  log group, and its build artifact deleted, and `tools/verify-clean.py` clean
  of everything the project can name. Direct queries found no function, table,
  bucket, role, secret, or log group left under the stack's name.

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
  `tools/verify-clean.py` clean of everything the project can name.

## 2026-09-18, us-east-1: earlier design

The previous version took a local Git checkout and returned a patch, and it
shared this version's preparation, agent start, polling, and collection code.
An implementation task succeeded in about 2 minutes 16 seconds with durable
waits between polls; the patch applied to a fresh clone and its 10 tests
passed, and the VM read back `TERMINATED`.
