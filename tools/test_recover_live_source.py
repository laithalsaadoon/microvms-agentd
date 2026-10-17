# SPDX-License-Identifier: Apache-2.0
"""Offline provenance grammar and manifest boundaries for incident recovery."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

REGION = "us-east-1"
PREFIX = "microvm-cli-capability-policy-default-89abcdef"
SHARED = "microvm-cli-conformance-01234567"
GROUP = "/aws/lambda-microvms/conformance-configured-01234567"
ROLE = "arn:aws:iam::000000000000:role/example-build"


def reference_identity(kind, region, name):
    return hashlib.sha256(
        b"\0".join(s.encode() for s in (kind, region, name))
    ).hexdigest()


def source_line(step, value, *, timestamp=True):
    stamp = "2026-10-17T16:00:00.123Z " if timestamp else ""
    return f"real AWS\t{step}\t{stamp}{value}\n"


def ownership_lines(*, suffix="-0123456789ab", separator=":"):
    return [
        source_line(
            "conformance suite",
            "PASS keep reported the identifiers the caller now owns — "
            f"image=arn:aws:lambda:{REGION}:000000000000:microvm-image{separator}{SHARED}",
        ),
        source_line(
            "conformance suite",
            f"PASS the configured build log group exists and is readable — {GROUP}",
        ),
        source_line(
            "conformance suite",
            "FAIL capability-policy-default: the ensured image is deleted — "
            f"prefix='{PREFIX}' remaining=1",
        ),
        source_line(
            "verify again after destroy", f"LEAK image {PREFIX}{suffix} in CREATED"
        ),
    ]


def manifest_value():
    return {
        "version": 1,
        "hash_scheme": "sha256-kind-nul-region-nul-name-v1",
        "source_run_id": 42,
        "parser_version": 2,
        "source_log_representation": "gh-run-view-job-log-python-text-utf8-v1",
        "workflow_path": ".github/workflows/live-conformance.yml",
        "region": REGION,
        "resources": [
            {"kind": kind, "proof": proof, "sha256": digit * 64}
            for digit, kind, proof in (
                ("1", "image", "shared-image"),
                ("2", "log-group", "shared-log"),
                ("3", "log-group", "configured-log"),
                ("4", "image", "capability-default-image"),
            )
        ],
        "excluded": ["f" * 64],
    }


def write_manifest(directory, value):
    raw = json.dumps(value).encode()
    (directory / "42.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("value", [True, 1, "present", [1]])
def test_require_accepts_truth_and_returns_none(value):
    assert recovery.require(value, "example_reason") is None


@pytest.mark.parametrize("value", [False, None, 0, "", [], {}])
def test_require_refuses_with_exact_reason(value):
    with pytest.raises(recovery.Refused, match="^example_reason$"):
        recovery.require(value, "example_reason")


@pytest.mark.parametrize("value", [b"", b"abc", b"a\0b", bytes(range(256))])
def test_digest_is_standard_sha256(value):
    assert recovery.digest(value) == hashlib.sha256(value).hexdigest()


@pytest.mark.parametrize(
    "kind,region,name",
    [
        ("image", REGION, "example"),
        ("log-group", REGION, "example"),
        ("image", "us-west-2", "example"),
        ("image", REGION, "Example"),
        ("image", REGION, "example "),
        ("image", "", "é"),
        ("ab", "c", "d"),
        ("a", "bc", "d"),
    ],
)
def test_identity_binds_exact_kind_region_and_name(kind, region, name):
    assert recovery.identity(kind, region, name) == reference_identity(
        kind, region, name
    )


@pytest.mark.parametrize("value", ["word", "0", " ", "é", "with\nnewline"])
def test_text_returns_original_nonempty_string(value):
    assert recovery.text(value) is value


@pytest.mark.parametrize("value", [None, False, True, 0, 1, [], {}, b"word", ""])
def test_text_refuses_nonstring_or_empty(value):
    with pytest.raises(recovery.Refused, match="^missing_text$"):
        recovery.text(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01T01:00:00+01:00", 0),
        ("1969-12-31T23:59:59.250Z", -0.75),
        ("1970-01-01T00:00:01.125+00:00", 1.125),
        (datetime(1970, 1, 1, tzinfo=timezone.utc), 0),
        (datetime(1970, 1, 1, tzinfo=timezone(timedelta(hours=-2))), 7200),
    ],
)
def test_stamp_retains_offsets_fraction_and_epoch(value, expected):
    assert recovery.stamp(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        0,
        {},
        [],
        "",
        "not-a-date",
        "1970-01-01T00:00:00XXZXX",
        "1970-01-01T00:00:00z",
        "1970-01-01",
        datetime(1970, 1, 1),
    ],
)
def test_stamp_refuses_missing_or_naive_timestamp(value):
    with pytest.raises(recovery.Refused, match="^invalid_timestamp$"):
        recovery.stamp(value)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_stamp_rejects_nonfinite_timestamp(number):
    class Nonfinite(datetime):
        def timestamp(self):
            return number

    with pytest.raises(recovery.Refused, match="^invalid_timestamp$"):
        recovery.stamp(Nonfinite(1970, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("region", ["us-east-1", "us-west-2"])
def test_manifest_accepts_exact_reviewed_document(tmp_path, region):
    value = manifest_value()
    value["region"] = region
    sha = write_manifest(tmp_path, value)
    with patch.object(recovery, "MANIFESTS", tmp_path):
        assert recovery.load_manifest("42", sha) == value


@pytest.mark.parametrize("run", ["", "0", "042", "-42", "42x", "42\n", "../42", "/42"])
def test_manifest_refuses_noncanonical_run_before_io(tmp_path, run):
    with patch.object(recovery, "MANIFESTS", tmp_path):
        with pytest.raises(recovery.Refused, match="^invalid_source_run$"):
            recovery.load_manifest(run, "a" * 64)


@pytest.mark.parametrize(
    "sha", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"]
)
def test_manifest_refuses_noncanonical_digest(tmp_path, sha):
    with patch.object(recovery, "MANIFESTS", tmp_path):
        with pytest.raises(recovery.Refused, match="^invalid_manifest_digest$"):
            recovery.load_manifest("42", sha)


def test_manifest_checks_content_hash_and_rejects_symlink(tmp_path):
    value = manifest_value()
    sha = write_manifest(tmp_path, value)
    with patch.object(recovery, "MANIFESTS", tmp_path):
        with pytest.raises(recovery.Refused, match="^manifest_digest$"):
            recovery.load_manifest("42", "a" * 64)
        original = tmp_path / "original.json"
        (tmp_path / "42.json").rename(original)
        (tmp_path / "42.json").symlink_to(original)
        with pytest.raises(recovery.Refused, match="^manifest_symlink$"):
            recovery.load_manifest("42", sha)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("version", 2, "manifest_schema"),
        ("hash_scheme", "sha256", "manifest_schema"),
        ("source_run_id", 43, "manifest_run"),
        ("parser_version", 1, "manifest_parser"),
        ("source_log_representation", "other", "manifest_parser"),
        ("workflow_path", ".github/workflows/other.yml", "manifest_workflow"),
        ("region", "eu-west-1", "manifest_region"),
        ("resources", [], "empty_resources"),
        ("resources", {}, "empty_resources"),
        ("resources", None, "empty_resources"),
        ("excluded", ["1" * 64], "excluded_resource"),
    ],
)
def test_manifest_refuses_wrong_boundary_field(tmp_path, field, value, reason):
    document = manifest_value()
    document[field] = value
    sha = write_manifest(tmp_path, document)
    with patch.object(recovery, "MANIFESTS", tmp_path):
        with pytest.raises(recovery.Refused, match=f"^{reason}$"):
            recovery.load_manifest("42", sha)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("extra", "resource_proofs"),
        ("missing", "resource_proofs"),
        ("proof", "resource_proofs"),
        ("duplicate", "resource_hashes"),
        ("short", "resource_hashes"),
        ("uppercase", "resource_hashes"),
        ("nonhex", "resource_hashes"),
        ("newline", "resource_hashes"),
        ("field", "resource_schema"),
        ("kind", "resource_kind"),
    ],
)
def test_manifest_refuses_resource_set_changes(tmp_path, change, reason):
    document = manifest_value()
    resources = document["resources"]
    if change == "extra":
        resources.append(copy.deepcopy(resources[0]))
    elif change == "missing":
        resources.pop()
    elif change == "proof":
        resources[0]["proof"] = "caller-says-owned"
    elif change == "duplicate":
        resources[1]["sha256"] = resources[0]["sha256"]
    elif change in {"short", "uppercase", "nonhex", "newline"}:
        resources[0]["sha256"] = {
            "short": "1" * 63,
            "uppercase": "A" * 64,
            "nonhex": "z" * 64,
            "newline": "1" * 64 + "\n",
        }[change]
    elif change == "field":
        resources[0]["current_run_owned"] = True
    else:
        resources[0]["kind"] = "log-group"
    sha = write_manifest(tmp_path, document)
    with patch.object(recovery, "MANIFESTS", tmp_path):
        with pytest.raises(recovery.Refused, match=f"^{reason}$"):
            recovery.load_manifest("42", sha)


def explicit_row(name="microvm-cli-slow-ready-deadbeef-0123"):
    return {
        "kind": "image",
        "proof": "explicit-incident-image",
        "sha256": reference_identity("image", REGION, name),
    }


def test_manifest_accepts_one_separately_reviewed_image(tmp_path):
    value = manifest_value()
    value["resources"].append(explicit_row())
    sha = write_manifest(tmp_path, value)
    with patch.object(recovery, "MANIFESTS", tmp_path):
        assert recovery.load_manifest("42", sha) == value


@pytest.mark.parametrize(
    "change,reason",
    [
        ("second", "resource_proofs"),
        ("log", "resource_kind"),
        ("duplicate_hash", "resource_hashes"),
        ("excluded", "excluded_resource"),
        ("extra_field", "resource_schema"),
    ],
)
def test_manifest_refuses_broadened_explicit_selection(tmp_path, change, reason):
    value = manifest_value()
    row = explicit_row()
    value["resources"].append(row)
    if change == "second":
        value["resources"].append(explicit_row("other"))
    elif change == "log":
        row["kind"] = "log-group"
    elif change == "duplicate_hash":
        row["sha256"] = value["resources"][0]["sha256"]
    elif change == "excluded":
        value["excluded"].append(row["sha256"])
    else:
        row["current_run_owned"] = True
    sha = write_manifest(tmp_path, value)
    with patch.object(recovery, "MANIFESTS", tmp_path):
        with pytest.raises(recovery.Refused, match=f"^{reason}$"):
            recovery.load_manifest("42", sha)


@pytest.mark.parametrize("timestamp", [False, True])
def test_source_leaked_images_reads_only_exact_post_destroy_records(timestamp):
    names = {"Example-A_b.1", "other-image"}
    log = "".join(
        source_line(
            "verify again after destroy",
            f"LEAK image {name} in UPDATED",
            timestamp=timestamp,
        )
        for name in names
    )
    noise = "unformatted\njust\ttwo\n"
    noise += source_line("conformance suite", "LEAK image not-owned in CREATED")
    noise += source_line(
        "verify again after destroy", "LEAK image prefix-only in lower"
    )
    noise += source_line(
        "verify again after destroy", "LEAK image spaced name in CREATED"
    )
    noise += source_line(
        "verify again after destroy", "LEAK log group other in CREATED"
    )
    assert recovery.source_leaked_images(noise + log + log) == names
    assert recovery.source_leaked_images(log.replace("\n", "\t\n")) == names
    assert recovery.source_leaked_images(log.replace("LEAK", "\x1b[KLEAK")) == names
    assert recovery.source_leaked_images("") == set()


def test_explicit_selection_needs_reviewed_authority_and_exact_observation():
    name = "microvm-cli-slow-ready-deadbeef-0123"
    row = explicit_row(name)
    log = "".join(ownership_lines())
    log += source_line(
        "verify again after destroy", f"LEAK image {name} in CREATE_FAILED"
    )
    log += source_line(
        "verify again after destroy", f"LEAK image {name}-other in CREATED"
    )
    proven = recovery.source_ownership(log, REGION)
    assert row["sha256"] not in proven
    assert recovery.explicit_image_selection(log, REGION, [], proven) == {}
    assert recovery.explicit_image_selection(log, REGION, [row], proven) == {
        row["sha256"]: ("explicit-incident-image", name, None)
    }
    assert recovery.source_ownership(log, REGION) == proven


@pytest.mark.parametrize("kind", ["log-group", "microvm", "bucket", "IMAGE"])
def test_explicit_selection_refuses_nonimage_authority(kind):
    row = explicit_row()
    row["kind"] = kind
    with pytest.raises(recovery.Refused, match="^explicit_image_kind$"):
        recovery.explicit_image_selection("", REGION, [row], {})


def test_explicit_selection_refuses_duplicate_or_already_proven_authority():
    row = explicit_row("name")
    log = source_line("verify again after destroy", "LEAK image name in CREATED")
    with pytest.raises(recovery.Refused, match="^explicit_image_conflict$"):
        recovery.explicit_image_selection(log, REGION, [row, row], {})
    with pytest.raises(recovery.Refused, match="^explicit_image_conflict$"):
        recovery.explicit_image_selection(log, REGION, [row], {row["sha256"]: ()})


@pytest.mark.parametrize("region,observed", [(REGION, "other"), ("us-west-2", "name")])
def test_explicit_selection_never_substitutes_family_or_region(region, observed):
    row = explicit_row("name")
    log = source_line("verify again after destroy", f"LEAK image {observed} in CREATED")
    with pytest.raises(recovery.Refused, match="^explicit_image_unresolved$"):
        recovery.explicit_image_selection(log, region, [row], {})


def test_explicit_selection_refuses_ambiguous_hash_resolution():
    row = explicit_row("name")
    log = "".join(
        source_line("verify again after destroy", f"LEAK image {name} in CREATED")
        for name in ("name", "other")
    )
    with patch.object(recovery, "identity", return_value=row["sha256"]):
        with pytest.raises(recovery.Refused, match="^explicit_image_unresolved$"):
            recovery.explicit_image_selection(log, REGION, [row], {})


@pytest.mark.parametrize(
    "separator,suffix", [(":", ""), (":", "-0123456789ab"), ("/", "-a.b_c")]
)
def test_source_ownership_derives_exact_four_names(separator, suffix):
    lines = ownership_lines(separator=separator, suffix=suffix)
    arn = f"arn:aws:lambda:{REGION}:000000000000:microvm-image{separator}{SHARED}"
    expected = {
        reference_identity("image", REGION, SHARED): ("shared-image", SHARED, arn),
        reference_identity("log-group", REGION, "/aws/lambda-microvms/" + SHARED): (
            "shared-log",
            "/aws/lambda-microvms/" + SHARED,
            None,
        ),
        reference_identity("log-group", REGION, GROUP): ("configured-log", GROUP, None),
        reference_identity("image", REGION, PREFIX + suffix): (
            "capability-default-image",
            PREFIX + suffix,
            None,
        ),
    }
    assert recovery.source_ownership("".join(lines), REGION) == expected
    assert recovery.source_ownership("".join(lines + lines), REGION) == expected
    assert (
        recovery.source_ownership("".join(line[:-1] + "\t\n" for line in lines), REGION)
        == expected
    )


def test_source_ownership_handles_only_the_active_unknown_step():
    lines = ownership_lines()
    expected = recovery.source_ownership("".join(lines), REGION)
    heading = source_line("UNKNOWN STEP", "##[group]Run ./conformance/run_rs.py")
    active = [
        line.replace("\tconformance suite\t", "\tUNKNOWN STEP\t") for line in lines
    ]
    assert recovery.source_ownership(heading + "".join(active), REGION) == expected
    wrong_heading = source_line("UNKNOWN STEP", "##[group]Run another-program")
    with pytest.raises(recovery.Refused, match="^source_ownership_incomplete$"):
        recovery.source_ownership(heading + wrong_heading + "".join(active), REGION)


def test_source_ownership_ignores_unrelated_and_colored_lines():
    lines = ownership_lines()
    colored = [
        line.replace("PASS", "\x1b[32mPASS\x1b[0m").replace(
            "FAIL", "\x1b[31mFAIL\x1b[0m"
        )
        for line in lines
    ]
    noise = "unformatted\njust\ttwo\n" + source_line(
        "conformance suite", "not a verdict"
    )
    noise += source_line(
        "another step",
        "PASS keep reported the identifiers the caller now owns — image=untrusted",
    )
    assert recovery.source_ownership(
        noise + "".join(colored), REGION
    ) == recovery.source_ownership("".join(lines), REGION)
    assert recovery.source_ownership(
        "".join(line.replace(".123Z ", ".123Z \x1b[K") for line in lines), REGION
    ) == recovery.source_ownership("".join(lines), REGION)


def test_source_ownership_preserves_mixed_case_image_name():
    name = SHARED.replace("conformance", "Conformance")
    actual = recovery.source_ownership(
        "".join(ownership_lines()).replace(SHARED, name), REGION
    )
    assert actual[reference_identity("image", REGION, name)] == (
        "shared-image",
        name,
        f"arn:aws:lambda:{REGION}:000000000000:microvm-image:{name}",
    )


@pytest.mark.parametrize(
    "old,new,reason",
    [
        (":lambda:us-east-1:", ":lambda:us-west-2:", "source_image_identity"),
        (
            "/conformance-configured-01234567",
            "/unrelated",
            "source_configured_identity",
        ),
        (f"prefix='{PREFIX}'", "prefix='unrelated'", "source_capability_identity"),
    ],
)
def test_source_ownership_reports_exact_invalid_identity_reason(old, new, reason):
    raw = "".join(ownership_lines())
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.source_ownership(raw.replace(old, new), REGION)


@pytest.mark.parametrize(
    "old,new",
    [
        ("PASS keep reported", "FAIL keep reported"),
        ("PASS the configured", "FAIL the configured"),
        ("FAIL capability-policy-default:", "PASS capability-policy-default:"),
        ("image=arn:", "other=arn:"),
        (
            "keep reported the identifiers the caller now owns",
            "keep reported the identifiers the caller now owns extra",
        ),
        (
            "the configured build log group exists and is readable",
            "the configured build log group exists and is readable extra",
        ),
        (
            "capability-policy-default: the ensured image is deleted",
            "capability-policy-default: the ensured image is deleted extra",
        ),
        (":lambda:us-east-1:", ":lambda:us-west-2:"),
        (":microvm-image:", ":microvm:"),
        ("/conformance-configured-01234567", "/unrelated-01234567"),
        (f"prefix='{PREFIX}'", "prefix='unrelated'"),
        ("verify again after destroy", "conformance suite"),
        (f"LEAK image {PREFIX}-", f"LEAK image {PREFIX}other-"),
    ],
)
def test_source_ownership_refuses_weakened_or_missing_positive_proof(old, new):
    raw = "".join(ownership_lines())
    assert old in raw
    with pytest.raises(recovery.Refused):
        recovery.source_ownership(raw.replace(old, new), REGION)


@pytest.mark.parametrize("index", range(4))
def test_source_ownership_refuses_omitted_proof(index):
    lines = ownership_lines()
    del lines[index]
    with pytest.raises(recovery.Refused):
        recovery.source_ownership("".join(lines), REGION)


@pytest.mark.parametrize(
    "index,old,new,reason",
    [
        (0, SHARED, SHARED + "-other", "source_image_conflict"),
        (1, GROUP, GROUP.replace("01234567", "89abcdef"), "source_configured_conflict"),
        (
            2,
            PREFIX,
            PREFIX.replace("89abcdef", "01234567"),
            "source_capability_conflict",
        ),
        (3, PREFIX + "-0123456789ab", PREFIX + "-other", "source_capability_ambiguous"),
    ],
)
def test_source_ownership_refuses_conflicting_positive_identities(
    index, old, new, reason
):
    lines = ownership_lines()
    lines.append(lines[index].replace(old, new))
    with pytest.raises(recovery.Refused, match=f"^{reason}$"):
        recovery.source_ownership("".join(lines), REGION)


def infrastructure_lines():
    return [
        source_line("provision", 's3_bucket = "source-bucket"'),
        source_line("conformance suite", "infra: bucket=source-bucket"),
        source_line("provision", f'build_role_arn = "{ROLE}"'),
    ]


def test_source_infrastructure_matches_exact_inputs():
    lines = infrastructure_lines()
    expected = {
        "bucket_sha256": reference_identity("bucket", REGION, "source-bucket"),
        "build_role_arn_sha256": hashlib.sha256(ROLE.encode()).hexdigest(),
    }
    assert recovery.source_infrastructure("".join(lines), REGION) == expected
    assert recovery.source_infrastructure("".join(lines + lines), REGION) == expected
    assert (
        recovery.source_infrastructure(
            "".join(line[:-1] + "\t\n" for line in lines), REGION
        )
        == expected
    )
    assert (
        recovery.source_infrastructure("noise\njust\ttwo\n" + "".join(lines), REGION)
        == expected
    )
    colored = (
        "".join(lines)
        .replace("s3_bucket", "\x1b[32ms3_bucket")
        .replace('source-bucket"', 'source-bucket"\x1b[0m')
    )
    assert recovery.source_infrastructure(colored, REGION) == expected
    assert (
        recovery.source_infrastructure(
            "".join(line.replace(".123Z ", ".123Z \x1b[K") for line in lines), REGION
        )
        == expected
    )
    west = recovery.source_infrastructure("".join(lines), "us-west-2")
    assert west["bucket_sha256"] == reference_identity(
        "bucket", "us-west-2", "source-bucket"
    )
    assert west["build_role_arn_sha256"] == expected["build_role_arn_sha256"]


def test_source_infrastructure_preserves_mixed_case_role_name():
    role = ROLE.replace("example-build", "Example-Build")
    actual = recovery.source_infrastructure(
        "".join(infrastructure_lines()).replace(ROLE, role), REGION
    )
    assert actual["build_role_arn_sha256"] == hashlib.sha256(role.encode()).hexdigest()


@pytest.mark.parametrize("index", range(3))
def test_source_infrastructure_requires_each_input(index):
    lines = infrastructure_lines()
    del lines[index]
    with pytest.raises(recovery.Refused, match="^source_infrastructure_ambiguous$"):
        recovery.source_infrastructure("".join(lines), REGION)


@pytest.mark.parametrize(
    "extra",
    [
        source_line("provision", 's3_bucket = "other-bucket"'),
        source_line("conformance suite", "infra: bucket=other-bucket"),
        source_line("provision", f'build_role_arn = "{ROLE}-other"'),
    ],
)
def test_source_infrastructure_refuses_conflicting_inputs(extra):
    with pytest.raises(recovery.Refused, match="^source_infrastructure_ambiguous$"):
        recovery.source_infrastructure("".join(infrastructure_lines()) + extra, REGION)


@pytest.mark.parametrize(
    "old,new",
    [
        ('s3_bucket = "source-bucket"', 's3_bucket = "UPPERCASE"'),
        ('s3_bucket = "source-bucket"', 's3_bucket = "source-bucket" extra'),
        ("infra: bucket=source-bucket", "infra: bucket=other-bucket"),
        ("arn:aws:iam::000000000000", "arn:aws:iam::0"),
        ("build_role_arn", "execution_role_arn"),
        ("example-build", "bad role"),
    ],
)
def test_source_infrastructure_refuses_malformed_or_mismatched_input(old, new):
    with pytest.raises(recovery.Refused, match="^source_infrastructure_ambiguous$"):
        recovery.source_infrastructure(
            "".join(infrastructure_lines()).replace(old, new), REGION
        )


def full_receipt():
    return {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "passed",
        "sourceRun": 42,
        "sourceSha": "a" * 40,
        "region": REGION,
        "manifestSha256": "b" * 64,
        "parserVersion": 2,
        "sourceLogRepresentation": "gh-run-view-job-log-python-text-utf8-v1",
        "resources": [
            {
                "kind": "image",
                "sha256": "c" * 64,
                "status": "absent",
                "deletionRequested": True,
            },
            {
                "kind": "log-group",
                "sha256": "d" * 64,
                "status": "absent",
                "deletionRequested": False,
            },
        ],
        "versionEvidence": [
            {
                "imageSha256": "c" * 64,
                "versionSha256": "d" * 64,
                "createdAt": 100.25,
                "buildRoleArnSha256": "e" * 64,
                "artifactBucketSha256": "f" * 64,
                "artifactKeySha256": "1" * 64,
                "logGroupSha256": "2" * 64,
                "sourceBucketMatches": True,
                "sourceBuildRoleMatches": False,
            }
        ],
        "roleDuration": {"status": "observed", "maxSessionDurationSeconds": 7200},
        "independentVerifier": {
            "resources": [
                {"kind": kind, "sha256": digit * 64, "category": category}
                for kind, digit, category in (
                    ("image", "3", "pending"),
                    ("log-group", "4", "unclassified"),
                    ("bucket", "5", "standing"),
                    ("iam-role", "6", "standing"),
                    ("microvm", "7", "leak"),
                )
            ],
            "summary_present": True,
            "leakCount": 1,
            "exitCode": 1,
        },
    }


def set_nested(value, path, replacement):
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement


def receipt_invalid_cases():
    cases = []

    def bad(path, *values):
        cases.extend((path, value) for value in values)

    bad(("private",), "unreviewed-provider-response")
    bad(("kind",), None, 1, "other")
    bad(("conformance_verified",), True, 0, None, "false")
    bad(("status",), None, "success", 0)
    bad(("reason",), "provider-secret", 0, None)
    for field in ("sourceRun", "parserVersion"):
        bad((field,), None, True, 0, -1, 1.5, "42")
    bad(
        ("sourceSha",), None, 0, "a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 40 + "\n"
    )
    bad(("region",), None, "eu-west-1", 1)
    bad(
        ("manifestSha256",),
        None,
        True,
        "b" * 63,
        "b" * 65,
        "B" * 64,
        "g" * 64,
        "b" * 64 + "\n",
    )
    bad(("sourceLogRepresentation",), None, "arbitrary-log", 1)
    for field in ("resources", "versionEvidence"):
        bad((field,), None, {}, "[]", 0)
        bad((field, 0), None, [], 1, {})
        bad((field, 0, "private"), "provider-secret")
    bad(("resources", 0, "kind"), None, "vm", "bucket", 0)
    bad(("resources", 0, "sha256"), None, "c" * 63, "C" * 64, "g" * 64)
    bad(("resources", 0, "status"), None, "deleted", "DELETING", 1)
    bad(("resources", 0, "deletionRequested"), None, 0, 1, "true", [], {})
    for field in (
        "imageSha256",
        "versionSha256",
        "buildRoleArnSha256",
        "artifactBucketSha256",
        "artifactKeySha256",
        "logGroupSha256",
    ):
        bad(
            ("versionEvidence", 0, field),
            None,
            1,
            "a" * 63,
            "A" * 64,
            "g" * 64,
            "a" * 64 + "\n",
        )
    bad(
        ("versionEvidence", 0, "createdAt"),
        None,
        True,
        "1",
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
    )
    for field in ("sourceBucketMatches", "sourceBuildRoleMatches"):
        bad(("versionEvidence", 0, field), None, 0, 1, "true")
    bad(("roleDuration",), None, [], "observed", {})
    bad(("roleDuration", "status"), None, "unavailable", "other")
    bad(("roleDuration", "private"), "provider-secret")
    bad(("roleDuration", "maxSessionDurationSeconds"), None, True, 0, -1, 1.5, "7200")
    bad(("independentVerifier",), None, [], "done", {})
    bad(("independentVerifier", "private"), "provider-secret")
    bad(("independentVerifier", "summary_present"), False, 1, None, "true")
    bad(("independentVerifier", "leakCount"), None, True, -1, 1.5, "1", 0, 2)
    bad(("independentVerifier", "exitCode"), None, True, -1, 0, 2, 1.0, "1")
    bad(("independentVerifier", "resources"), None, {}, "[]", 0)
    bad(("independentVerifier", "resources", 0), None, [], 0, {})
    bad(("independentVerifier", "resources", 0, "private"), "provider-secret")
    bad(("independentVerifier", "resources", 0, "kind"), None, "vm", 1)
    bad(
        ("independentVerifier", "resources", 0, "sha256"),
        None,
        "a" * 63,
        "A" * 64,
        "z" * 64,
    )
    bad(("independentVerifier", "resources", 0, "category"), None, "unknown", 0)
    return cases


def test_receipt_preserves_complete_sanitized_evidence():
    value = full_receipt()
    assert recovery.validate_receipt(value) is value
    for row in value["resources"]:
        for status in ("present", "deletion_requested", "absent"):
            row["status"] = status
            assert recovery.validate_receipt(value) is value
    value["status"] = "failed"
    value["reason"] = "independent_survivor"
    assert recovery.validate_receipt(value) is value
    value["apiFailures"] = [{"operation": "unknown", "code": "unknown"}]
    assert recovery.validate_receipt(value) is value


def test_receipt_accepts_version_identity_without_optional_observations():
    value = full_receipt()
    value["versionEvidence"] = [
        {"imageSha256": "c" * 64, "versionSha256": "d" * 64, "createdAt": 0}
    ]
    value["roleDuration"]["maxSessionDurationSeconds"] = 1
    assert recovery.validate_receipt(value) is value


def test_receipt_retains_legacy_resource_rows_without_action_history():
    value = full_receipt()
    for row in value["resources"]:
        del row["deletionRequested"]
    assert recovery.validate_receipt(value) is value


@pytest.mark.parametrize("reason", recovery.REASONS)
def test_receipt_accepts_only_fixed_failure_reasons(reason):
    value = {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "failed",
        "reason": reason,
    }
    assert recovery.validate_receipt(value) is value


@pytest.mark.parametrize("path,replacement", receipt_invalid_cases())
def test_receipt_refuses_unsanitized_or_inconsistent_field(path, replacement):
    value = full_receipt()
    set_nested(value, path, replacement)
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


@pytest.mark.parametrize("value", [None, [], 1, "raw-provider-output"])
def test_receipt_rejects_nonobject(value):
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


@pytest.mark.parametrize("field", ["kind", "conformance_verified", "status"])
def test_receipt_requires_identity_and_verdict(field):
    value = full_receipt()
    del value[field]
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


@pytest.mark.parametrize("field", ["imageSha256", "versionSha256", "createdAt"])
def test_receipt_requires_version_identity_and_timestamp(field):
    value = full_receipt()
    del value["versionEvidence"][0][field]
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


def test_receipt_accepts_minimal_failure_and_empty_verifier_success():
    failed = {
        "kind": "live-recovery",
        "conformance_verified": False,
        "status": "failed",
        "reason": "deadline",
    }
    assert recovery.validate_receipt(failed) is failed
    value = full_receipt()
    value["region"] = "us-west-2"
    value["roleDuration"] = {"status": "unavailable"}
    value["versionEvidence"][0]["createdAt"] = 0
    value["independentVerifier"] = {
        "resources": [],
        "summary_present": True,
        "leakCount": 0,
        "exitCode": 0,
    }
    assert recovery.validate_receipt(value) is value
    value["versionEvidence"][0]["createdAt"] = 1
    assert recovery.validate_receipt(value) is value


@pytest.mark.parametrize(
    "field", ["resources", "versionEvidence", "independentVerifier"]
)
def test_receipt_bounds_collection_lengths(field):
    value = full_receipt()
    if field == "independentVerifier":
        rows = value[field]["resources"]
        value[field]["leakCount"] = 0
        value[field]["exitCode"] = 0
        rows[:] = [rows[0]] * 10000
    else:
        rows = value[field]
        rows[:] = [rows[0]] * 10000
    assert recovery.validate_receipt(value) is value
    rows.append(rows[0])
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


@pytest.mark.parametrize("reason", ["provider-secret", "", None, 0, "DEADLINE"])
def test_failed_receipt_cannot_carry_arbitrary_error_text(reason):
    value = full_receipt()
    value.update(status="failed", reason=reason)
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


@pytest.mark.parametrize(
    "field",
    [
        "sourceRun",
        "sourceSha",
        "region",
        "manifestSha256",
        "parserVersion",
        "sourceLogRepresentation",
        "resources",
        "versionEvidence",
        "roleDuration",
        "independentVerifier",
    ],
)
def test_partial_receipt_preserves_available_safe_fields(field):
    value = full_receipt()
    value.update(status="failed", reason="deadline")
    del value[field]
    assert recovery.validate_receipt(value) is value


@pytest.mark.parametrize("operation", sorted(recovery.OPERATIONS | {"unknown"}))
@pytest.mark.parametrize("code", sorted(recovery.ERROR_CODES | {"unknown"}))
def test_receipt_accepts_only_fixed_api_failure_names(operation, code):
    value = full_receipt()
    value["apiFailures"] = [{"operation": operation, "code": code}]
    assert recovery.validate_receipt(value) is value


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        "failure",
        [None],
        [[]],
        [{}],
        [{"operation": "get_role"}],
        [{"code": "unknown"}],
        [{"operation": "get_role", "code": "unknown", "message": "provider-secret"}],
        [{"operation": "provider-secret", "code": "unknown"}],
        [{"operation": "get_role", "code": "provider-secret"}],
        [{"operation": 1, "code": "unknown"}],
        [{"operation": "get_role", "code": None}],
    ],
)
def test_receipt_refuses_unrecognized_api_failure_data(rows):
    value = full_receipt()
    value["apiFailures"] = rows
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)


def test_receipt_bounds_api_failure_records():
    value = full_receipt()
    value["apiFailures"] = [{"operation": "unknown", "code": "unknown"}] * 10000
    assert recovery.validate_receipt(value) is value
    value["apiFailures"].append(value["apiFailures"][0])
    with pytest.raises(recovery.Refused, match="^receipt_schema$"):
        recovery.validate_receipt(value)
