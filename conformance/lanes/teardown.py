# SPDX-License-Identifier: Apache-2.0
"""The kept VM's end: `terminate --delete-image`, the log groups it names as left behind
deleted by the suite, and the daemon's own log lines read from CloudWatch."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness.cli import Cli
from harness.constants import REPO
from harness.envelope import Envelope
from harness.results import Results


def drive_teardown(
    cli: Cli,
    launched: Envelope,
    results: Results,
    logs: Any = None,
    extra_log_groups: Sequence[str] = (),
) -> None:
    """`microvm terminate --delete-image`, what it names as left behind, and then this
    suite deleting that.

    The build log group appearing in `undeletedLogGroups` is a **normal outcome for the
    client**, not a failure: neither `microvms-core` nor the CLI carries a CloudWatch
    client, so the group is named rather than deleted. That is the whole reason it is
    named — the service created it, no Terraform stack owns it, and `terraform destroy`
    leaves it behind. Six of them accumulated before anyone noticed.

    **But naming is where the client's responsibility ends and this suite's begins, and
    it did not pick it up.** Measured 2026-08-15, once `mise run live` was fixed to run
    its leak check *after* the suite rather than beside it: five log groups from five
    conformance runs, and `scripts/verify-clean.py` calls every one a leak — correctly, since
    a service-created group nothing owns is exactly what that script exists to find. So
    the two halves of one tier disagreed by construction. `mise run live` could not be
    green on a clean account no matter what the code did, and the only stable responses to
    a gate that always fails are to stop reading it or to weaken it.

    The suite deletes its own group, which is the resolution that keeps both claims: the
    client still refuses CloudWatch (CLI-2) and still names what it cannot remove, and the
    thing that *created* the group is the thing that removes it. `logs` is the boto3 client
    already built for `read_daemon_logs` — this needs no new dependency, only for the suite
    to finish the job the report handed it.
    """
    print("\n== teardown ==")
    # Neither `--image-identifier` nor `--image-name`: the kept run's record in the state
    # directory names both, and issue #160 is the finding that demanding them back was
    # asking for information the CLI already held. The two assertions after the call are
    # what make the omission a check rather than a convenience.
    torn = cli.call(
        "terminate",
        str(launched.data["microvmId"]),
        "--delete-image",
        "--wait",
        "--region",
        cli.region,
        timeout=15 * 60,
    )
    results.eq("terminate emitted its teardown envelope", torn.type, "microvm.teardown")
    results.eq(
        "terminate --delete-image read the image off the run record (issue #160)",
        torn.data.get("imageIdentifier"),
        str(launched.data["imageIdentifier"]),
    )
    expected_group = f"/aws/lambda-microvms/{launched.data['imageName']}"
    results.check(
        "the record's image name named the build log group without --image-name (issue #160)",
        expected_group in (torn.data.get("undeletedLogGroups") or []),
        f"undeletedLogGroups={torn.data.get('undeletedLogGroups')!r} expected {expected_group}",
    )
    results.check(
        "the VM and image were deleted",
        not torn.data.get("leaked"),
        f"leaked={torn.data.get('leaked')!r}",
    )
    # Named rather than absent, which is the assertion. An empty list here would mean
    # the CLI had quietly stopped reporting a group it still cannot delete.
    undeleted = torn.data.get("undeletedLogGroups") or []
    results.check(
        "the build log group was named rather than silently left",
        bool(undeleted),
        f"{undeleted!r} — this suite deletes it below, through boto3",
    )

    if logs is None:
        results.skip(
            "the suite deleted the build log group the CLI could not",
            "no CloudWatch client was passed to drive_teardown",
        )
        return

    # The leak check, asked while the groups still exist (issue #158). Two lines of its
    # report are asserted: the suite's own group is a LEAK it can attribute, and the
    # configured `--log-group` — which matches no prefix and appears in no ledger record,
    # because `terminate` names only the default `<prefix>/<image-name>` — is reported
    # UNCLASSIFIED rather than left out. Before the fix the script swept three prefixes and
    # called an account with a custom-named group clean; the second assertion is the one
    # that fails against that script.
    verify_clean = REPO / "scripts" / "verify-clean.py"
    swept = subprocess.run(
        [str(verify_clean)],
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_REGION": cli.region},
        timeout=300,
        check=False,
    )
    results.check(
        "verify-clean names the suite's build log group as a leak while it exists (issue #158)",
        swept.returncode == 1 and f"LEAK log group {expected_group}" in swept.stdout,
        f"exit={swept.returncode} named={f'LEAK log group {expected_group}' in swept.stdout}",
    )
    # The ledger path itself, live: a state directory holding one record whose `leaked`
    # names the configured group — the shape a teardown that could only name its group
    # leaves — turns that same UNCLASSIFIED group into an attributed LEAK. The suite's own
    # group cannot show this (its `microvm-cli-` prefix is matched first), which is why the
    # configured group, matching no prefix, is the one that proves the ledger rule against
    # the real account rather than only in `--self-test`.
    ledger_only: list[str] = []
    with tempfile.TemporaryDirectory(prefix="verify-clean-ledger-") as tmp:
        (Path(tmp) / "1700000000-1.json").write_text(
            json.dumps(
                {
                    "runId": "1700000000-1",
                    "region": cli.region,
                    "leaked": list(extra_log_groups),
                }
            )
        )
        attributed = subprocess.run(
            [str(verify_clean), "--state-dir", tmp],
            capture_output=True,
            text=True,
            env={**os.environ, "AWS_REGION": cli.region},
            timeout=300,
            check=False,
        )
        ledger_only = [
            f"LEAK log group {group} (named in {tmp})" for group in extra_log_groups
        ]
        results.check(
            "verify-clean attributes a non-prefixed group through a ledger record that names it (issue #158)",
            bool(extra_log_groups)
            and all(line in attributed.stdout for line in ledger_only),
            f"exit={attributed.returncode} wanted={ledger_only!r}",
        )
    results.check(
        "verify-clean reports a group it cannot attribute as unclassified, not as clean (issue #158)",
        all(
            f"UNCLASSIFIED (not deleted, not provably ours): log group {group}"
            in swept.stdout
            for group in extra_log_groups
        )
        and bool(extra_log_groups),
        f"extra={list(extra_log_groups)!r} exit={swept.returncode} "
        f"tail={swept.stdout.strip().splitlines()[-1] if swept.stdout.strip() else swept.stderr[-200:]!r}",
    )

    # The suite's own residue, removed by the suite. Asserted rather than best-effort:
    # a delete that quietly failed would put the tier back where it was, red on a leak
    # nobody meant to leave.
    #
    # `extra_log_groups` rides along: the configured build-logging group (issue #98) is
    # one the CLI's terminate never names — its `undeletedLogGroups` derives the default
    # `/aws/lambda-microvms/<image-name>` — so the suite that configured it is the only
    # party that knows to delete it. Same delete, same assertion, same tolerance for a
    # group the service never created.
    to_delete = [str(group) for group in undeleted] + [
        group for group in extra_log_groups if group not in undeleted
    ]
    deleted: list[str] = []
    failures: list[str] = []
    for group in to_delete:
        try:
            logs.delete_log_group(logGroupName=group)
            deleted.append(group)
        except Exception as exc:  # noqa: BLE001 - the reason is the finding
            # An already-absent group is the desired end state, not a failure: the
            # service may never have created one for a build that produced no events.
            if type(exc).__name__ == "ResourceNotFoundException":
                deleted.append(f"{group} (already absent)")
            else:
                failures.append(f"{group}: {type(exc).__name__}: {exc}")
    results.check(
        "the suite deleted the build log group the CLI could not",
        not failures and len(deleted) == len(to_delete),
        f"deleted={deleted!r} failures={failures!r}",
    )


def read_daemon_logs(
    logs: Any, image_name: str, extra_groups: Sequence[str] = ()
) -> list[str]:
    """The daemon's own log lines, through boto3.

    Through boto3 rather than through the CLI on purpose: `microvm logs` refuses to read
    CloudWatch by design (CLI-2), and this check is about whether the *daemon* wrote
    anything. Same shape and same reason as the oracle's own log read.

    `extra_groups` is checked first: the model documents the image's `logging` member as
    covering "build-time and runtime logs", so a suite build that configured a group
    (issue #98) may find the daemon's runtime lines there rather than in the default
    location — the defaults stay in the list because that half of the claim is unmeasured.
    """
    lines: list[str] = []
    for group in (
        *extra_groups,
        f"/aws/lambda-microvms/{image_name}",
        "/aws/lambda-microvms",
    ):
        try:
            streams = logs.describe_log_streams(
                logGroupName=group, orderBy="LastEventTime", descending=True, limit=5
            )
            for stream in streams.get("logStreams", []):
                events = logs.get_log_events(
                    logGroupName=group,
                    logStreamName=stream["logStreamName"],
                    limit=200,
                    startFromHead=False,
                )
                lines.extend(e["message"] for e in events.get("events", []))
            if lines:
                print(f"    log group {group}: {len(lines)} lines")
                return lines
        except Exception as exc:  # noqa: BLE001 - a missing group is data, not a crash
            print(f"    log group {group} unavailable: {type(exc).__name__}")
    return lines
