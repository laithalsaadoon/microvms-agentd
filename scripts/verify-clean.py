#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.40"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""Ask the account what this project left behind, rather than trusting teardown.

Teardown reporting success and the account being clean are different questions,
and the difference has cost us twice. `terraform destroy` once reported nine
resources destroyed while six CloudWatch log groups survived, because the service
creates those itself and Terraform never owned them. Separately an image deletion
retried past the point where the log-group delete had already run, leaving the
group behind again.

So this queries the account directly. It is what the live suite's own report
cannot be: independent of the code that did the cleanup.

Three outcomes rather than two, because collapsing them trains people to ignore
the script. A *leak* is something still costing money that nothing intends to
keep: a live MicroVM, an image, a log group. *Standing* is the Terraform stack,
which a caller may keep applied on purpose. *Pending* is a deletion still in
flight, where the right response is to re-run in a minute.

A fourth outcome, *unclassified*, exists for the service's own log-group namespace.
Every build writes `/aws/lambda-microvms/<image-name>`, and a fixed prefix list can
only recognise the image names this repo's own tools choose. Issue #158 measured the
hole: an image built with a custom `--name seam-probe-<epoch>` left its group behind,
`terminate --delete-image` did not remove it, and this script — sweeping three
prefixes — reported the account clean. So the sweep now covers the whole
`/aws/lambda-microvms/` namespace and classifies each group: a name under one of our
prefixes is ours; a name the local run ledger (`~/.microvm/runs/*.json`, the file
`run --keep` and every leaking run leave behind) recorded as an `imageName` is ours;
anything else is *unclassified* — listed, counted, and never deleted, because "not
provably ours" is not "ours". In the shared account this project measures in, that
list is other projects' groups (measured 2026-09-12: about eighty of them), so by the
rule two paragraphs up they are somebody else's and the verdict does not fail on them;
`--strict` makes it fail, for an account that exists only for this suite, where an
unclassified group can only be a record this script could not see.

Exit 0 when nothing attributable leaked (and, under `--strict`, nothing is
unclassified), 1 otherwise. `--delete` removes the leaks — the honest response to one
you have just proven exists — and leaves the Terraform stack to `terraform destroy`,
which owns it. `--self-test` runs the classification and verdict rules offline, so the
prefix-versus-ledger logic is proved without credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REGION = os.environ.get("AWS_REGION", "us-east-1")
# Everything this project creates carries one of these markers. Anything else in
# the account is somebody else's and must not be touched.
# Every prefix anything in this repo can create. Keeping this list complete is the
# whole correctness condition: a missing prefix makes the checker report "clean"
# while a resource bills, which is worse than having no checker at all — it
# converts an unknown into a false assurance. The `microvm-cli` entry was added
# after exactly that happened: a CLI run leaked a log group and this script said
# the account was clean, because it only knew the two names the conformance
# scripts used.
# `agent-vm-` is the stem `microvm agent-up` names its images by (docs/AGENT-VMS.md,
# AGENT-3); the live suite's `drive_agent_vm` builds one, so a leak of it must be visible.
NAME_PREFIXES = (
    "agentd-conformance",
    "agentd-probe",
    "microvm-cli",
    "microvm-",
    "agent-vm-",
)
#: The service's own namespace for build log groups (docs/PLATFORM.md, "Build logs go to
#: `/aws/lambda-microvms/<image-name>`"). Swept whole rather than by our prefixes, because
#: the prefixes describe the names *our tools* pick and say nothing about a `--name` a user
#: chose. Kept as one constant so the reader of a report can reproduce the sweep with
#: `aws logs describe-log-groups --log-group-name-prefix` and get the same list.
SERVICE_LOG_PREFIX = "/aws/lambda-microvms/"

#: What a classified group is. `ours` and `ledger` are leaks and are deletable under
#: `--delete`; `unknown` is reported and never touched.
OURS, LEDGER, UNKNOWN = "ours", "ledger", "unknown"


def ours(name: str | None) -> bool:
    return bool(name) and name.startswith(NAME_PREFIXES)


def default_state_dir() -> Path:
    """Where the CLI keeps its run ledger, by the CLI's own rule (`seam.rs`, `state_dir`)."""
    if env := os.environ.get("MICROVM_STATE_DIR"):
        return Path(env)
    return Path(os.environ.get("HOME", ".")) / ".microvm" / "runs"


class Oracle:
    """What the local run ledger knows this project created.

    A record survives exactly when something was left behind: `run --keep` marks its VM and
    image outstanding and never clears the file, a teardown that failed to delete keeps the
    survivors listed, and every teardown that could only *name* its build log group lists
    that group under `leaked` (core's teardown report does, and so does `terminate`). So the
    names in here are the names of things that may well still exist — including the custom
    `--name` image and its group that a prefix list cannot know (issue #158). Name records
    under `names/` are not read: they carry no image.
    """

    def __init__(self) -> None:
        self.image_names: set[str] = set()
        self.image_arns: set[str] = set()
        self.log_groups: set[str] = set()
        #: record path -> the parsed record, for `prune_records`.
        self.records: dict[Path, dict[str, Any]] = {}

    def knows_image(self, name: str | None, arn: str | None) -> bool:
        return (name is not None and name in self.image_names) or (
            arn is not None and arn in self.image_arns
        )


def ledger_oracle(state_dir: Path) -> Oracle:
    oracle = Oracle()
    if not state_dir.is_dir():
        return oracle
    for path in sorted(state_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        # A record from another region names that region's resources; a bare image name
        # matched across regions would attribute (and under --delete, delete) a same-named
        # image here. Records with no region predate the field and are read.
        if record.get("region") not in (None, REGION):
            continue
        oracle.records[path] = record
        if isinstance(record.get("imageName"), str):
            oracle.image_names.add(record["imageName"])
        if isinstance(record.get("imageIdentifier"), str):
            oracle.image_arns.add(record["imageIdentifier"])
        for entry in record.get("leaked") or []:
            if isinstance(entry, str) and entry.startswith(SERVICE_LOG_PREFIX):
                oracle.log_groups.add(entry)
            elif isinstance(entry, str) and ":microvm-image:" in entry:
                oracle.image_arns.add(entry)
    return oracle


def ledger_image_names(state_dir: Path) -> set[str]:
    """The `imageName` values alone; kept for the self-test's narrow case."""
    return ledger_oracle(state_dir).image_names


def prune_records(oracle: Oracle, removed: set[str]) -> list[Path]:
    """Drop what `--delete` just removed from every record, and the file once it is empty.

    The ledger's own rule is that a file is removed only when nothing is outstanding
    (`ledger.rs`, module docs). Deleting a log group here and leaving it listed would make
    `microvm ls` report a leak this script removed — the stale-entry shape issue #159
    measured at 68 entries, most of them build log groups the live suite had deleted.
    """
    unlinked: list[Path] = []
    for path, record in oracle.records.items():
        leaked = [e for e in (record.get("leaked") or []) if isinstance(e, str)]
        kept = [e for e in leaked if e not in removed]
        if kept == leaked:
            continue
        if kept:
            record["leaked"] = kept
            path.write_text(json.dumps(record, indent=2) + "\n")
        else:
            path.unlink(missing_ok=True)
            unlinked.append(path)
    return unlinked


def classify_log_group(group_name: str, ledger: set[str] | Oracle) -> str:
    """Whose a `/aws/lambda-microvms/<stem>` group is.

    Prefix first, then ledger, then unknown — the order only matters for the label, since
    both of the first two are leaks. The ledger matches either way a record can name the
    group: by the image's name (the group is `<prefix>/<imageName>`) or by the group's own
    full name under `leaked`, which is how a teardown that could only name it records it. A
    group outside the namespace is never passed here; the sweep is scoped to
    `SERVICE_LOG_PREFIX`.
    """
    stem = group_name.removeprefix(SERVICE_LOG_PREFIX)
    if ours(stem):
        return OURS
    names = ledger.image_names if isinstance(ledger, Oracle) else ledger
    groups = ledger.log_groups if isinstance(ledger, Oracle) else set()
    if stem in names or group_name in groups:
        return LEDGER
    return UNKNOWN


def all_items(client: Any, operation: str) -> list[dict[str, Any]]:
    """Every `items` entry of a paginated list operation, across pages.

    The bare call returns one page, and a 51st MicroVM or image would read as absent — the
    same false-assurance shape the log-group sweep pages for.
    """
    items: list[dict[str, Any]] = []
    for page in client.get_paginator(operation).paginate():
        items.extend(page.get("items", []))
    return items


def service_log_groups(logs: Any) -> list[dict[str, Any]]:
    """Every group under the namespace, across pages.

    Paginated because the un-paginated call returns at most 50 and the failure mode is the
    same one the whole script exists to prevent: a 51st group reads as absent.
    """
    groups: list[dict[str, Any]] = []
    paginator = logs.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=SERVICE_LOG_PREFIX):
        groups.extend(page.get("logGroups", []))
    return groups


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--delete",
        action="store_true",
        help="remove what is found instead of only reporting it",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="the CLI state directory whose run ledger names custom images "
        "(default: $MICROVM_STATE_DIR or ~/.microvm/runs)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail the verdict on an unclassified log group too (for an account that exists "
        "only for this suite)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="prove the classification rules offline, without credentials, and exit",
    )
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    import boto3  # deferred so --self-test needs no credentials and no network

    state_dir = args.state_dir or default_state_dir()
    oracle = ledger_oracle(state_dir)

    session = boto3.Session(region_name=REGION)
    mv = session.client("lambda-microvms")
    s3 = session.client("s3")
    iam = session.client("iam")
    logs = session.client("logs")

    leaks: list[str] = []

    # MicroVMs. TERMINATED is not a leak: billing stops at terminate and the
    # record is history the service keeps. Anything else is still costing money.
    live_states = {"PENDING", "RUNNING", "SUSPENDING", "SUSPENDED", "TERMINATING"}
    for vm in all_items(mv, "list_microvms"):
        if vm.get("state") in live_states:
            leaks.append(f"microvm {vm.get('microvmId')} in {vm.get('state')}")

    # Images. DELETING is in progress rather than leaked, so it is reported
    # separately: re-running this a minute later is the right response.
    pending: list[str] = []
    for image in all_items(mv, "list_microvm_images"):
        name = image.get("name")
        # The ledger is the second oracle for images for the same reason it is for log
        # groups: a custom `--name` is invisible to the prefix list.
        if not (ours(name) or oracle.knows_image(name, image.get("imageArn"))):
            continue
        state = image.get("state")
        if state == "DELETING":
            pending.append(f"image {name} still DELETING")
        else:
            leaks.append(f"image {name} in {state}")

    # The bucket and roles belong to the Terraform stack, which a caller may keep
    # applied deliberately between runs — re-applying costs a couple of minutes and
    # the idle cost is pennies. So they are reported as standing infrastructure
    # rather than as leaks: calling a deliberate choice a leak trains people to
    # ignore this script, and an ignored leak check is the same as none.
    standing: list[str] = []
    for bucket in s3.list_buckets().get("Buckets", []):
        if ours(bucket.get("Name")):
            standing.append(f"bucket {bucket['Name']}")

    paginator = iam.get_paginator("list_roles")
    for page in paginator.paginate():
        for role in page.get("Roles", []):
            if ours(role.get("RoleName")):
                standing.append(f"iam role {role['RoleName']}")

    # The one Terraform cannot see, and therefore the one most likely to survive. The whole
    # namespace, classified, rather than three prefixes: see the module docstring for the
    # `seam-probe-<epoch>` group this script called clean.
    log_groups: list[dict[str, Any]] = []
    unclassified: list[str] = []
    for group in service_log_groups(logs):
        name = group["logGroupName"]
        owner = classify_log_group(name, oracle)
        if owner == UNKNOWN:
            unclassified.append(f"log group {name}")
            continue
        log_groups.append(group)
        label = "ours by prefix" if owner == OURS else f"named in {state_dir}"
        leaks.append(f"log group {name} ({label})")

    for note in pending:
        print(f"  pending: {note}")
    for note in standing:
        print(f"  standing (terraform-owned): {note}")
    for note in unclassified:
        # Never deleted. It is under the service's namespace, so something built an image
        # with that name; whether it was this repo from another state directory or another
        # client entirely, this script cannot prove it and says so.
        print(f"  UNCLASSIFIED (not deleted, not provably ours): {note}")

    code, summary = verdict(len(leaks), len(unclassified), args.strict)
    print(summary)
    if not leaks:
        return code
    for leak in leaks:
        print(f"  LEAK {leak}")

    if not args.delete:
        print("\nre-run with --delete to remove them")
        return 1

    print("\ndeleting")
    removed: set[str] = set()
    for group in log_groups:
        if _try(logs.delete_log_group, logGroupName=group["logGroupName"]):
            removed.add(group["logGroupName"])
    for image in all_items(mv, "list_microvm_images"):
        name = image.get("name")
        arn = image.get("imageArn")
        if (ours(name) or oracle.knows_image(name, arn)) and image.get(
            "state"
        ) != "DELETING":
            if _try(
                mv.delete_microvm_image,
                imageIdentifier=image.get("imageIdentifier") or arn,
            ):
                removed.add(str(arn))
    for vm in all_items(mv, "list_microvms"):
        if vm.get("state") in live_states:
            _try(mv.terminate_microvm, microvmIdentifier=vm["microvmId"])
    # What was just removed leaves the ledger too, so `microvm ls` stops reporting it.
    for path in prune_records(oracle, removed):
        print(f"  pruned run record {path.name}: nothing it named survives")
    # Buckets and roles are Terraform-owned, so `terraform destroy` is the right
    # tool and deleting them here would desync the state file.
    print("buckets and IAM roles are Terraform-owned: run")
    print("  terraform -chdir=conformance/infra destroy")
    return 1


def verdict(leaks: int, unclassified: int, strict: bool) -> tuple[int, str]:
    """The exit code and the one line a caller reads.

    Attributable leaks always fail. Unclassified groups fail only under `--strict`; without
    it they are counted in the summary so a reader knows the account was not proven empty,
    which is a different sentence from "clean".
    """
    if leaks:
        return 1, f"account {REGION}: {leaks} leaked resource(s)"
    if unclassified and strict:
        return 1, (
            f"account {REGION}: no attributable leak, but {unclassified} unclassified log "
            f"group(s) under {SERVICE_LOG_PREFIX} and --strict is set — not clean"
        )
    if unclassified:
        return 0, (
            f"account {REGION}: clean of everything this project can name; {unclassified} "
            f"log group(s) under {SERVICE_LOG_PREFIX} belong to no known prefix and no "
            "ledger record and were left alone (listed above; --strict fails on them)"
        )
    return 0, f"account {REGION}: clean — no conformance resources survive"


def _try(fn: Any, **kwargs: Any) -> bool:
    label = ", ".join(f"{k}={v}" for k, v in kwargs.items())
    try:
        fn(**kwargs)
        print(f"  deleted {label}")
        return True
    except Exception as exc:  # noqa: BLE001 - report and continue; one failure is not fatal
        print(f"  could not delete {label}: {type(exc).__name__}")
        return False


def self_test() -> int:
    """The classification rules, proved offline.

    The cases are the ones that have bitten: a prefixed group is ours; the issue #158 group
    (`seam-probe-<epoch>`, a custom `--name`) is ours only when the ledger names it, and
    unknown otherwise; a group nothing names stays unknown; a ledger with an unreadable file
    or a name record under `names/` neither crashes nor invents an image name.
    """
    failures: list[str] = []

    def expect(label: str, ok: bool) -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="verify-clean-selftest-") as tmp:
        state = Path(tmp)
        (state / "1757600000-100.json").write_text(
            json.dumps(
                {
                    "runId": "1757600000-100",
                    "region": "us-east-1",
                    "imageIdentifier": "arn:aws:lambda:us-east-1:1:microvm-image:seam-probe-1757600000",
                    "imageName": "seam-probe-1757600000",
                    "microvmId": "microvm-abc",
                    "leaked": [
                        "microvm-abc",
                        "arn:aws:lambda:us-east-1:1:microvm-image:seam-probe-1757600000",
                    ],
                }
            )
        )
        (state / "1757600001-101.json").write_text("{not json")
        (state / "1757600002-102.json").write_text(
            json.dumps({"runId": "x", "region": "us-east-1"})
        )
        (state / "names").mkdir()
        (state / "names" / "probe.json").write_text(
            json.dumps(
                {
                    "name": "probe",
                    "microvmId": "microvm-abc",
                    "imageName": "not-an-image-record",
                }
            )
        )
        (state / "1757600003-103.json").write_text(
            json.dumps(
                {
                    "runId": "1757600003-103",
                    "region": "us-east-1",
                    "imageIdentifier": "arn:aws:lambda:us-east-1:1:microvm-image:torn-down",
                    "imageName": "torn-down",
                    "microvmId": "microvm-def",
                    # The shape every clean teardown leaves: only the group it could name.
                    "leaked": ["/aws/lambda-microvms/torn-down"],
                }
            )
        )
        (state / "1757600004-104.json").write_text(
            json.dumps(
                {
                    "runId": "1757600004-104",
                    "region": "eu-west-1",
                    "imageName": "elsewhere",
                    "leaked": ["/aws/lambda-microvms/elsewhere"],
                }
            )
        )
        names = ledger_image_names(state)
        expect(
            "the ledger yields exactly the imageName values of readable run records in this region",
            names == {"seam-probe-1757600000", "torn-down"},
        )
        expect(
            "a record from another region is not this region's oracle",
            "elsewhere" not in names
            and "/aws/lambda-microvms/elsewhere" not in ledger_oracle(state).log_groups,
        )
        expect(
            "a missing state dir yields no names rather than raising",
            ledger_image_names(state / "absent") == set(),
        )
        oracle = ledger_oracle(state)
        expect(
            "a group listed under leaked is known by its full name",
            "/aws/lambda-microvms/torn-down" in oracle.log_groups,
        )
        expect(
            "an image is known by name or by ARN",
            oracle.knows_image("torn-down", None)
            and oracle.knows_image(
                None, "arn:aws:lambda:us-east-1:1:microvm-image:seam-probe-1757600000"
            )
            and not oracle.knows_image("other", "arn:other"),
        )
        only_leaked = Oracle()
        only_leaked.log_groups.add("/aws/lambda-microvms/torn-down")
        expect(
            "a group named only under leaked (no imageName) classifies as ledger",
            classify_log_group("/aws/lambda-microvms/torn-down", only_leaked) == LEDGER,
        )
        # --delete's bookkeeping: what was removed leaves the records, and an emptied record goes.
        unlinked = prune_records(oracle, {"/aws/lambda-microvms/torn-down"})
        expect(
            "pruning the deleted group removes the record that named only it",
            [p.name for p in unlinked] == ["1757600003-103.json"]
            and not (state / "1757600003-103.json").exists(),
        )
        survivor = json.loads((state / "1757600000-100.json").read_text())
        expect(
            "a record still naming a live resource is left in place",
            survivor["leaked"]
            == [
                "microvm-abc",
                "arn:aws:lambda:us-east-1:1:microvm-image:seam-probe-1757600000",
            ],
        )
        narrowed = prune_records(ledger_oracle(state), {"microvm-abc"})
        expect(
            "pruning one of two entries narrows the record rather than removing it",
            narrowed == []
            and json.loads((state / "1757600000-100.json").read_text())["leaked"]
            == ["arn:aws:lambda:us-east-1:1:microvm-image:seam-probe-1757600000"],
        )

    ledger = {"seam-probe-1757600000"}
    expect(
        "a group under one of our prefixes is ours",
        classify_log_group("/aws/lambda-microvms/microvm-cli-conformance-ab12", ledger)
        == OURS,
    )
    expect(
        "the issue #158 group is a leak when the ledger names its image",
        classify_log_group("/aws/lambda-microvms/seam-probe-1757600000", ledger)
        == LEDGER,
    )
    expect(
        "the same group with no ledger record is unclassified, never clean",
        classify_log_group("/aws/lambda-microvms/seam-probe-1757600000", set())
        == UNKNOWN,
    )
    expect(
        "a group nothing names is unclassified",
        classify_log_group("/aws/lambda-microvms/somebody-elses-image", ledger)
        == UNKNOWN,
    )
    expect(
        "the agent-vm stem stays ours",
        classify_log_group(
            "/aws/lambda-microvms/agent-vm-claude-code-0123456789ab", set()
        )
        == OURS,
    )
    expect(
        "a ledger name is matched whole, not as a prefix",
        classify_log_group("/aws/lambda-microvms/seam-probe-1757600000-extra", ledger)
        == UNKNOWN,
    )

    expect(
        "a leak fails whatever else is true",
        verdict(1, 0, False)[0] == 1 and verdict(1, 5, True)[0] == 1,
    )
    expect(
        "unclassified alone passes without --strict and says how many were left alone",
        verdict(0, 3, False)[0] == 0 and "3 log group(s)" in verdict(0, 3, False)[1],
    )
    expect("unclassified alone fails under --strict", verdict(0, 3, True)[0] == 1)
    expect(
        "nothing at all is the plain clean sentence",
        verdict(0, 0, True)
        == (0, f"account {REGION}: clean — no conformance resources survive"),
    )

    if failures:
        print(f"verify-clean self-test: {len(failures)} failure(s)")
        return 1
    print("verify-clean self-test: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
