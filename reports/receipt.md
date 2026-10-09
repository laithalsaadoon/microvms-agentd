# Fleet run 12 receipt: the semver gate after v0.11.0

## 1. For the owner

**The run found nothing to change in the product.** The five library crates (`microvms-protocol`, `microvms-domain`, `microvms-app`, `microvms-edges`, `microvms-core`) are already checked against the published 0.11.0 at base, since def2815 (#536). `semver:check` has no `|| echo` form left, and the checker refuses a planted breaking change and missing or empty input. The tree gains only this receipt, `reports/receipt.md`.

**End state: Blocked, on K3 and K5.** The check named "published crates keep their API" runs on every pull request and every push to main, but GitHub is not told to require it before a merge. Ruleset 21934766 requires 13 contexts, and this is not one of them. The one condition that clears both: you add "published crates keep their API" (GitHub Actions, integration 15368, as for the other 13) to the required status checks of ruleset 21934766 (Settings, Rules, Rulesets, main, Require status checks to pass, Add checks). The fleet never changes rulesets. K3 and K5 then rerun green with no change to the tree.

**What the one final card asks.** In plain words, it asks you to confirm you added that required check, so the fleet rechecks K3 and K5 and ships. It also names need N-12-1 (below). Residuals: none.

**PR:** the coordinator opens one draft PR to main from `vdd/microvms-12` after this commit; its link and CI results are recorded in the Fleet view (`mv_fleet_runs.receipt`), not in this file.

## 2. Decisions the fleet made for you

- **D-12-1:** The code already checks all five libraries against the published 0.11.0. What is left is one switch only you can flip: make that check required before anything merges. The fleet proved everything else and hands you that switch.
- **D-12-2:** A check only stops a bad change if GitHub is told it must pass before merging. The check exists and runs on every pull request, but GitHub is not told to wait for it. That is a settings change only you can make, and releases are always cut from main, so that one setting covers the release too.
- **D-12-3:** We fed the checker five kinds of nothing: a library never published, a version that does not exist, no internet, an empty folder, and a program with no library. It refused all five instead of saying all good.
- **D-12-4:** The safety check works, but nothing checks the safety check: someone could quietly remove one library from it and every other check would stay green. That needs its own small fix, which you start when you want.
- **D-12-5:** Nothing gets published in this run, so the extra checks that only matter when shipping a new version do not apply.
- **D-12-6:** GitHub lets a change merge if its checks passed a little while ago, even if main moved since. That is a general setting for the whole repository; we wrote it down for you rather than act on it here.
- **D-12-7:** GitHub was still finishing its checks on the starting point. Almost all had already passed, and we wrote down how to score the last one when it finishes.
- **D-12-8:** Two checks wait on your one settings change. Everything else goes ahead, and the last card asks you to flip that switch.

## 3. Blockers, needs, residuals, follow-ups

### Blockers

- **K3 is blocked.** Clearing condition: you add "published crates keep their API" (GitHub Actions, integration 15368) to the required status checks of ruleset 21934766 ("main", target `~DEFAULT_BRANCH`). Read back with `gh api repos/laithalsaadoon/microvms-agentd/rules/branches/main --jq '[.[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context]'`: 14 contexts including it. Then K3 reruns green with no change to the tree.
- **K5 is blocked on the same condition.** It reads the required list of the run's PR live, so your edit turns it green on the existing PR without a new CI run.

### Needs

- **N-12-1: `semver:check` has no anti-vacuity.** The gate never counts what it examined and nothing in G0 ties it to the publish set. Deleting or emptying a `semver:check` line, giving one the pre-baseline `|| echo` form, or adding a crate to `PUBLISHED` without a line leaves every G0 leg and required context green, because the five semver faults in `verify/guards/faults/semver.toml` run their own argv, not the task, and cargo-semver-checks passes an empty workspace selection (exit 0, no output). Proposed change, for a gate run you start: point the semver fault families at `mise run semver:check`, or add a unit check under `tools/` wired into a check leg that parses `semver:check` and fails when its crates differ from the library crates in `PUBLISHED`, when a line is not `cargo semver-checks -p <crate> --release-type minor`, or when the list is empty, with its own planted faults registered. It blocks nothing in this run.

### Residuals

- None.

### Follow-ups

- **F-12-1:** Ruleset 21934766 has `strict_required_status_checks_policy` false, so a required context may be green from a run on a stale merge base. Options: strict mode, a merge queue, or `release:tag` refusing a main tip whose own ci run is not green. It breaks no criterion: repository-wide merge policy over all 13 contexts, and the semver job also runs on every push to main.
- **F-12-2:** Defense in depth on the tag path: `release.yml`'s guard job could run `mise run ci:semver` against the exact tagged commit. A tier 3 run (an id-token workflow) the owner can start. Not in the plan of #298, which the story says to honor.
- **F-12-3:** `CONTRIBUTING.md` has no convention for a crate new to `PUBLISHED` before its first publish: in the enforced form it fails closed (`not found in registry`, exit 101), which with the context required blocks every PR until the manual first publish. Document the pre-baseline window next to "Releases and reviews". No pre-baseline crate exists at H.
- **F-12-4:** Issue #298 stays open for its step 3 (the live skew section passing on a real run); after your ruleset change its step 2 can be ticked off by you (the fleet comments on no issue).
- **F-12-5:** A stray `/tmp/mise.toml` on the fleet host (min_version 2026.10.2) fails `live:check` for any job whose `TMPDIR` is `/tmp`. Every role of this run sets `TMPDIR` inside its own scratch. Host environment, not the repository: a void cause, never a red verdict.

## 4. Evidence

### Story

Complete the compatibility gate after v0.11.0. Outcome and scope: honor the existing semver cutover plan once the v0.11.0 baseline is published, with failures enforced by the release path. Acceptance: confirm v0.11.0 artifacts and baseline exist; prove the compatibility checker rejects a planted breaking API change and rejects missing or empty input; verify which CI context must fail and which branch and release rules require it; preserve the intended distinction between pre-baseline advisory checks and enforced compatibility.

### Run contract

Contract hash `41cf27d5f57e`, tier 1, write scope `reports/receipt.md`. Base `cae2ec73845cd3d8ad2d68417ab77cef5b677dab` (chore(deps): Bump jdx/mise-action (#525)). Branch `vdd/microvms-12`. Changed paths, base..head: `reports/receipt.md` only.

| Criterion | Line | Verdict | Proving exit record |
|---|---|---|---|
| K1 | v0.11.0 artifacts and baseline exist | green, exit 0 | verify-1 (job 2084) |
| K2 | checker rejects 5 planted breaks and 5 missing or empty inputs | green, exit 0 | verify-1 (job 2084) |
| K3 | which CI context must fail and which rules require it | blocked, exit 1 with `K3 FAIL: no rule on main requires 'published crates keep their API' (ruleset 21934766 requires 13 contexts, not this one)` | proving phase ci; verify-1 ran it and saw the blocked line |
| K4 | distinction between advisory and enforced preserved | green, exit 0 | verify-1 (job 2084) |
| K5 | outcome: the context is required on the run's PR and passed | pending the ci phase on the run's PR; blocked on the same condition as K3 | proving phase ci |

K1 line: `K1: v0.11.0 published (tag -> f882b52, 19 assets incl SHA256SUMS); 0.11.0 not yanked on crates.io (6 of 6 crates), PyPI and npm; 5 lib crates at 0.11.0, baseline 0.11.0`.
K2 line: `K2: 5 of 5 planted breaks fail ci:semver with their own message (clean exit 0); 5 of 5 missing or empty inputs refused with exit 101`.
K4 line: `K4: 12 semver paths unchanged base..H; enforced: microvms-protocol microvms-domain microvms-app microvms-edges microvms-core (5, --release-type minor, no lint allowed); advisory: none`.

### G0 legs of `mise run -c check`: 28 legs, base and head

Base verdicts come from intake job 2062 and absence job 2078. Head verdicts come from verify job 2084 on H (the base commit, because the build phase changed nothing). All 28 are green at both.

| Leg | Base | Head |
|---|---|---|
| `lint` | green | green |
| `security` | green | green |
| `test` | green | green |
| `schema:check` | green | green |
| `schema:compat` | green | green |
| `manifest:check` | green | green |
| `core-api:check` | green | green |
| `parity:check` | green | green |
| `stubs:check` | green | green |
| `dts:check` | green | green |
| `model:check` | green | green |
| `publish:check` | green | green |
| `release:check` | green | green |
| `live:check` | green | green |
| `conformance:self-test` | green | green |
| `agents:check` | green | green |
| `changelog:check` | green | green |
| `pr-body:check` | green | green |
| `build` | green | green |
| `background:check` | green | green |
| `trace:check` | green | green |
| `ratchet:check` | green | green |
| `guards:list` | green | green |
| `fail-to-pass:check` | green | green |
| `mutants:check` | green | green |
| `mutmut:check` | green | green |
| `targets:check` | green | green |
| `ci:parity` | green | green |

Required CI contexts: 13 in ruleset 21934766, each judged at base from the run on `cae2ec7` (or on PR #525 for `CodeQL`, which has no push run); their head results are recorded by the coordinator after the push.

### Absence

Base verdicts (absence job 2078): K1 green, K2 green, K3 red as expected, K4 green, K5 red as expected (on PR #525); `mise run -c check` exit 0 with 28 of 28 legs green.

### Matrix and mutation

Matrix: not at tier 1. Mutation: not at tier 1 (no product path is written).

### Review

Verify job 2084 (codex, a family other than the Claude builder row): 2 neutral items, 3574 and 3585 lines read in full, canary caught (item-2 planted `|| echo 'reported, not gated'` on the `microvms-app` line of `.config/mise/tasks/contracts.toml`; item-1 was the clean diff).

Timeline check on job 2084 (clerk job 2086): `_events` kind `tool` for job 2084 holds 84 events, 0 hits for `run-12-1.key`, `job-2058/canary` or `"canary":"item-`. The transcript read through `trace_query` is recorded in the clerk's board result.

### CI

Recorded by the coordinator after the push, in `mv_fleet_runs.evidence.ci` and the Fleet view.

### Live run

No live run needed: this run changes no wire protocol or AWS lifecycle. None dispatched.

### Fetch

```
git fetch https://github.com/laithalsaadoon/microvms-agentd vdd/microvms-12:vdd/microvms-12
```
