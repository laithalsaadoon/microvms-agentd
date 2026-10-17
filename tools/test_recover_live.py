# SPDX-License-Identifier: Apache-2.0
"""Offline adversarial checks for the hosted recovery boundary."""

from __future__ import annotations

import base64
import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

REGION = "us-east-1"
START = "2026-10-17T15:00:00Z"
END = "2026-10-17T17:00:00Z"
CREATED = datetime(2026, 10, 17, 16, tzinfo=timezone.utc)
SHARED = "microvm-cli-conformance-01234567"
CAPABILITY = "microvm-cli-capability-policy-default-89abcdef-0123456789ab"
CONFIGURED = recovery.SERVICE_PREFIX + "conformance-configured-01234567"
UNKNOWN = "somebody-else"


def arn(name):
    return f"arn:aws:lambda:{REGION}:0:microvm-image:{name}"


def source_log():
    def line(step, value):
        return f"real AWS\t{step}\t2026-10-17T16:00:00Z {value}\n"

    return "".join(
        [
            line("provision", 's3_bucket = "source-bucket"'),
            line(
                "provision",
                'build_role_arn = "arn:aws:iam::000000000000:role/source-build"',
            ),
            line("conformance suite", "infra: bucket=source-bucket"),
            line(
                "conformance suite",
                f"PASS keep reported the identifiers the caller now owns — image={arn(SHARED)}",
            ),
            line(
                "conformance suite",
                f"PASS the configured build log group exists and is readable — {CONFIGURED}",
            ),
            line(
                "conformance suite",
                "FAIL capability-policy-default: the ensured image is deleted — prefix='microvm-cli-capability-policy-default-89abcdef' remaining=1",
            ),
            line("verify again after destroy", f"LEAK image {CAPABILITY} in CREATED"),
        ]
    )


def source_representation(annotated):
    """Build test-only direct text and independently numbered contiguous spans."""
    raw = []
    spans = []
    steps = []
    for line in annotated.splitlines(keepends=True):
        _, name, payload = line.split("\t", 2)
        if not spans or spans[-1]["name"] != name:
            number = len(spans) + 1
            spans.append(
                {"number": number, "name": name, "start": len(raw) + 1, "count": 0}
            )
            steps.append(
                {
                    "number": number,
                    "name": name,
                    "status": "completed",
                    "conclusion": "success",
                }
            )
        spans[-1]["count"] += 1
        raw.append(payload)
    for span in spans:
        start = span["start"] - 1
        span["sha256"] = recovery.digest(
            "".join(raw[start : start + span["count"]]).encode()
        )
    return "".join(raw), steps, spans


def set_source_manifest(value, annotated):
    raw, steps, spans = source_representation(annotated)
    value["source_log_sha256"] = recovery.digest(raw.encode())
    value["source_job_steps_sha256"] = recovery.digest(
        json.dumps(
            steps, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    value["source_log_spans"] = spans
    return raw, steps


def manifest():
    result = {
        "version": 1,
        "parser_version": recovery.PARSER_VERSION,
        "source_log_representation": recovery.LOG_REPRESENTATION,
        "source_infrastructure": recovery.source_infrastructure(source_log(), REGION),
        "hash_scheme": recovery.SCHEME,
        "region": REGION,
        "repository_id": 7,
        "workflow_id": 8,
        "workflow_path": recovery.WORKFLOW,
        "source_run_id": 9,
        "source_attempt": 1,
        "source_sha": "a" * 40,
        "source_job_id": 10,
        "source_job_name": "real AWS",
        "source_job_started_at": START,
        "source_job_completed_at": END,
        "source_conclusion": "cancelled",
        "source_job_conclusion": "cancelled",
        "source_workflow_sha256": recovery.digest(b"workflow"),
        "excluded": ["f" * 64],
    }
    set_source_manifest(result, source_log())
    owned = recovery.source_ownership(source_log(), REGION)
    result["resources"] = [
        {
            "kind": "image" if proof.endswith("image") else "log-group",
            "sha256": key,
            "proof": proof,
        }
        for key, (proof, _, _) in owned.items()
    ]
    return result


class FakeGithub:
    def __init__(self):
        self.log_value, steps, _ = source_representation(source_log())
        self.values = {
            "": {"id": 7, "owner": {"login": "maintainer"}},
            "actions/runs/9/attempts/1": {
                "id": 9,
                "run_attempt": 1,
                "workflow_id": 8,
                "path": recovery.WORKFLOW,
                "head_sha": "a" * 40,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "cancelled",
                "repository": {"id": 7},
                "head_repository": {"id": 7},
                "actor": {"login": "maintainer"},
                "triggering_actor": {"login": "maintainer"},
            },
            "actions/runs/9": {"run_attempt": 1, "status": "completed"},
            "actions/jobs/10": {
                "steps": steps,
                "run_id": 9,
                "run_attempt": 1,
                "name": "real AWS",
                "status": "completed",
                "conclusion": "cancelled",
                "started_at": START,
                "completed_at": END,
            },
            f"contents/{recovery.WORKFLOW}?ref={'a' * 40}": {
                "encoding": "base64",
                "content": base64.b64encode(b"workflow").decode(),
            },
        }

    def api(self, path):
        return copy.deepcopy(self.values[path])

    def log(self, unused):
        return self.log_value


def environment():
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY_ID": "7",
        "GITHUB_RUN_ID": "11",
        "GITHUB_ACTOR": "maintainer",
        "GITHUB_TRIGGERING_ACTOR": "maintainer",
        "AWS_REGION": REGION,
    }


class Missing(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Plane:
    def __init__(self):
        self.images = {
            arn(name): {
                "imageArn": arn(name),
                "name": name,
                "state": "CREATED",
                "createdAt": CREATED,
            }
            for name in (SHARED, CAPABILITY, UNKNOWN)
        }
        self.vms = []
        self.calls = []
        self.stuck = False
        self.version_logging = {key: {"disabled": {}} for key in self.images}
        self.extra_image_page = []

    def list_microvm_images(self, **kwargs):
        self.calls.append(("list", kwargs))
        rows = list(self.images.values())
        if self.extra_image_page:
            return (
                {"items": self.extra_image_page}
                if kwargs.get("nextToken")
                else {"items": rows, "nextToken": "second"}
            )
        return {"items": copy.deepcopy(rows)}

    def list_microvms(self, **kwargs):
        return {"items": copy.deepcopy(self.vms)}

    def get_microvm_image(self, imageIdentifier):
        if imageIdentifier not in self.images:
            raise Missing()
        return copy.deepcopy(self.images[imageIdentifier])

    def list_microvm_image_versions(self, imageIdentifier, **kwargs):
        return {"items": [{"imageVersion": "1"}]}

    def get_microvm_image_version(self, imageIdentifier, imageVersion):
        return {
            "imageArn": imageIdentifier,
            "imageVersion": imageVersion,
            "state": "SUCCESSFUL",
            "createdAt": CREATED,
            "logging": self.version_logging[imageIdentifier],
            "buildRoleArn": "not-printed",
            "codeArtifact": {"uri": "s3://private-bucket/private-key"},
        }

    def delete_microvm_image(self, imageIdentifier):
        self.calls.append(("delete_image", imageIdentifier))
        if not self.stuck:
            self.images.pop(imageIdentifier)
        return {"state": "DELETING"}


class Logs:
    def __init__(self, mutations):
        self.groups = {
            name: {
                "logGroupName": name,
                "creationTime": int(CREATED.timestamp() * 1000),
            }
            for name in (
                CONFIGURED,
                recovery.SERVICE_PREFIX + SHARED,
                recovery.SERVICE_PREFIX + UNKNOWN,
            )
        }
        self.mutations = mutations

    def describe_log_groups(self, **kwargs):
        return {"logGroups": copy.deepcopy(list(self.groups.values()))}

    def delete_log_group(self, logGroupName):
        self.mutations.append(("delete_group", logGroupName))
        self.groups.pop(logGroupName)
        return {}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.manifest = manifest()
        self.owned = recovery.source_ownership(source_log(), REGION)
        self.plane = Plane()
        self.logs = Logs(self.plane.calls)
        self.clock = Clock()
        self.api = recovery.Api(
            self.plane, self.logs, clock=self.clock, sleep=self.clock.sleep
        )

    def assert_no_deletes(self):
        self.assertFalse([c for c in self.plane.calls if c[0].startswith("delete")])

    def test_manifest_digest_and_exact_schema_gate(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(recovery, "MANIFESTS", Path(directory)),
        ):
            path = Path(directory) / "9.json"
            path.write_text(json.dumps(self.manifest))
            good = recovery.digest(path.read_bytes())
            self.assertEqual(recovery.load_manifest("9", good), self.manifest)
            for run, sha in (("../9", good), ("9", "0" * 64), ("9", "missing")):
                with self.subTest(run=run), self.assertRaises(recovery.Refused):
                    recovery.load_manifest(run, sha)
            self.manifest["resources"][0]["current_run_owned"] = True
            path.write_text(json.dumps(self.manifest))
            with self.assertRaises(recovery.Refused):
                recovery.load_manifest("9", recovery.digest(path.read_bytes()))

    def test_provenance_requires_current_fetch_and_every_origin_field(self):
        self.assertEqual(
            recovery.provenance(self.manifest, FakeGithub(), environment()), self.owned
        )
        for path, fields in {
            "actions/runs/9/attempts/1": (
                "id",
                "run_attempt",
                "workflow_id",
                "path",
                "head_sha",
                "event",
                "status",
                "conclusion",
            ),
            "actions/jobs/10": (
                "run_id",
                "run_attempt",
                "name",
                "status",
                "conclusion",
                "started_at",
                "completed_at",
            ),
            "actions/runs/9": ("run_attempt", "status"),
        }.items():
            for field in fields:
                with self.subTest(path=path, field=field):
                    fake = FakeGithub()
                    fake.values[path][field] = "wrong"
                    with self.assertRaises(recovery.Refused):
                        recovery.provenance(self.manifest, fake, environment())
        for key in environment():
            env = environment()
            env[key] = "wrong"
            with self.subTest(key=key), self.assertRaises(recovery.Refused):
                recovery.provenance(self.manifest, FakeGithub(), env)
        for person in ("actor", "triggering_actor"):
            fake = FakeGithub()
            fake.values["actions/runs/9/attempts/1"][person]["login"] = "other"
            with self.assertRaises(recovery.Refused):
                recovery.provenance(self.manifest, fake, environment())
        fake = FakeGithub()
        fake.log_value += "changed"
        with self.assertRaisesRegex(recovery.Refused, "source_log_digest"):
            recovery.provenance(self.manifest, fake, environment())

    def test_source_proofs_cannot_be_replaced_by_family_or_cleanup_alone(self):
        with self.assertRaises(recovery.Refused):
            recovery.source_ownership("", REGION)
        for remove in (
            "keep reported",
            "configured build",
            "capability-policy-default: the ensured",
            "LEAK image",
        ):
            value = "\n".join(
                line for line in source_log().splitlines() if remove not in line
            )
            with self.subTest(remove=remove), self.assertRaises(recovery.Refused):
                recovery.source_ownership(value, REGION)
        with self.assertRaises(recovery.Refused):
            recovery.source_ownership(
                source_log().replace("conformance suite", "different step"), REGION
            )
        with self.assertRaises(recovery.Refused):
            recovery.source_ownership(source_log(), "us-west-2")

    def test_recovery_deletes_exact_images_then_groups_preserving_unknowns(self):
        report = {}
        recovery.recover(self.manifest, self.owned, self.api, report)
        actions = [row for row in self.plane.calls if row[0].startswith("delete")]
        self.assertEqual(
            actions,
            [
                ("delete_image", arn(SHARED)),
                ("delete_image", arn(CAPABILITY)),
                ("delete_group", recovery.SERVICE_PREFIX + SHARED),
                ("delete_group", CONFIGURED),
            ],
        )
        self.assertEqual(set(self.plane.images), {arn(UNKNOWN)})
        self.assertEqual(set(self.logs.groups), {recovery.SERVICE_PREFIX + UNKNOWN})
        self.assertTrue(all(row["status"] == "absent" for row in report["resources"]))
        self.assertGreaterEqual(self.clock.now, recovery.VISIBILITY_SECONDS)
        serialized = json.dumps(self.api.version_evidence)
        self.assertNotIn("private-bucket", serialized)
        self.assertNotIn("private-key", serialized)
        self.assertNotIn("not-printed", serialized)

    def test_preexisting_and_replaced_incarnations_never_delete(self):
        self.plane.images[arn(SHARED)]["createdAt"] = datetime(
            2020, 1, 1, tzinfo=timezone.utc
        )
        with self.assertRaisesRegex(recovery.Refused, "outside_source_lifetime"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()
        self.plane.images[arn(SHARED)]["createdAt"] = CREATED
        self.logs.groups[CONFIGURED]["creationTime"] = 1
        with self.assertRaisesRegex(recovery.Refused, "outside_source_lifetime"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()

    def test_dependency_veto_covers_vm_and_version_references(self):
        self.plane.vms = [
            {"microvmId": "other", "imageArn": arn(SHARED), "state": "RUNNING"}
        ]
        with self.assertRaisesRegex(recovery.Refused, "image_in_use"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()
        self.plane.vms = []
        self.plane.version_logging[arn(UNKNOWN)] = {
            "cloudWatch": {"logGroup": CONFIGURED}
        }
        with self.assertRaisesRegex(recovery.Refused, "configured_group_in_use"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()
        self.plane.version_logging[arn(UNKNOWN)] = {
            "cloudWatch": {"logGroup": recovery.SERVICE_PREFIX + SHARED}
        }
        with self.assertRaisesRegex(recovery.Refused, "configured_group_in_use"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()
        self.plane.version_logging[arn(UNKNOWN)] = None
        with self.assertRaisesRegex(recovery.Refused, "version_logging_missing"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()

    def test_every_page_and_bad_page_fail_before_mutation(self):
        self.plane.extra_image_page = [
            {
                "imageArn": arn("later"),
                "name": "later",
                "state": "CREATED",
                "createdAt": CREATED,
            }
        ]
        self.plane.version_logging[arn("later")] = {
            "cloudWatch": {"logGroup": CONFIGURED}
        }
        with self.assertRaisesRegex(recovery.Refused, "configured_group_in_use"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assertTrue(
            any(row == ("list", {"nextToken": "second"}) for row in self.plane.calls)
        )
        self.assert_no_deletes()
        with patch.object(
            self.plane,
            "list_microvm_images",
            side_effect=[
                {"items": [], "nextToken": "same"},
                {"items": [], "nextToken": "same"},
            ],
        ):
            with self.assertRaisesRegex(recovery.Refused, "pagination_token"):
                recovery.recover(self.manifest, self.owned, self.api, {})
        with patch.object(self.plane, "list_microvm_images", return_value={}):
            with self.assertRaisesRegex(recovery.Refused, "missing_page_items"):
                recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()

    def test_pending_deletion_and_api_error_do_not_prove_absence(self):
        self.plane.stuck = True
        self.api.deadline = 6
        with self.assertRaisesRegex(recovery.Refused, "deadline"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assertFalse(any(row[0] == "delete_group" for row in self.plane.calls))
        self.api.deadline = self.clock() + 10
        with patch.object(
            self.plane, "get_microvm_image", side_effect=ValueError("secret-message")
        ):
            with self.assertRaisesRegex(recovery.Refused, "api_failed"):
                self.api.image_absent(arn(SHARED))

    def test_incarnation_rechecked_before_delete(self):
        real = self.plane.get_microvm_image
        calls = 0

        def changed(**kwargs):
            nonlocal calls
            calls += 1
            row = real(**kwargs)
            if calls > 2:
                row["createdAt"] = datetime(2026, 10, 17, 16, 1, tzinfo=timezone.utc)
            return row

        with patch.object(self.plane, "get_microvm_image", side_effect=changed):
            with self.assertRaises(recovery.Refused):
                recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()

    def test_verifier_is_sanitized_and_unrecognized_output_refused(self):
        raw = f"  LEAK image private-image in CREATED\naccount {REGION}: 1 leaked resource(s)\n\nre-run with --delete to remove them\n"
        safe = recovery.safe_verifier(raw, REGION)
        self.assertEqual(
            safe["resources"][0]["sha256"],
            recovery.identity("image", REGION, "private-image"),
        )
        self.assertNotIn("private-image", json.dumps(safe))
        for value in ("", raw + "private secret", raw.replace(REGION, "us-west-2")):
            with self.subTest(value=value), self.assertRaises(recovery.Refused):
                recovery.safe_verifier(value, REGION)

    def test_failure_and_interrupt_write_only_sanitized_receipt(self):
        for failure in (
            RuntimeError("private-secret"),
            KeyboardInterrupt(),
            InterruptedError(),
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "result.json"
                with (
                    patch(
                        "sys.argv",
                        [
                            "recover-live.py",
                            "preflight",
                            "--source-run",
                            "9",
                            "--manifest-sha256",
                            "a" * 64,
                            "--report",
                            str(path),
                        ],
                    ),
                    patch.object(recovery, "load_manifest", side_effect=failure),
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    self.assertEqual(recovery.main(), 1)
                self.assertEqual(
                    json.loads(path.read_text())["reason"],
                    "interrupted_or_invalid_input",
                )
                self.assertNotIn("private-secret", out.getvalue())

    def test_independent_verifier_refuses_approved_survivor_but_reports_unrelated(self):
        for name, fails in ((SHARED, True), (UNKNOWN, False)):
            output = f"LEAK image {name} in CREATED\naccount {REGION}: 1 leaked resource(s)\n"
            if fails:
                with self.assertRaisesRegex(recovery.Refused, "independent_survivor"):
                    recovery.independent_absence(self.manifest, output)
            else:
                self.assertEqual(
                    len(
                        recovery.independent_absence(self.manifest, output)["resources"]
                    ),
                    1,
                )

    def test_deleted_image_stale_list_waits_for_convergence(self):
        original = self.plane.list_microvm_images
        stale = copy.deepcopy(self.plane.images[arn(SHARED)])
        shown = False

        def listing(**kwargs):
            nonlocal shown
            result = original(**kwargs)
            if arn(SHARED) not in self.plane.images and not shown:
                result["items"].append(stale)
                shown = True
            return result

        with patch.object(self.plane, "list_microvm_images", side_effect=listing):
            report = {}
            recovery.recover(self.manifest, self.owned, self.api, report)
        self.assertTrue(shown)
        self.assertTrue(all(row["status"] == "absent" for row in report["resources"]))

    def test_deadline_rejects_calls_before_provider_execution(self):
        self.api.deadline = self.clock()
        with self.assertRaisesRegex(recovery.Refused, "deadline"):
            self.api.inventory()
        self.assertEqual(self.plane.calls, [])

    def test_sdk_retries_are_bounded_and_only_classified_errors_retry(self):
        class Throttled(Exception):
            response = {
                "Error": {"Code": "ThrottlingException", "Message": "private-secret"}
            }

        with patch.object(
            self.plane, "list_microvm_images", side_effect=Throttled()
        ) as call:
            with self.assertRaisesRegex(recovery.Refused, "api_failed"):
                self.api.inventory()
            self.assertEqual(call.call_count, 3)
            self.assertEqual(self.clock.now, 4)
        self.assert_no_deletes()

    def test_every_collection_consumes_later_pages_and_rejects_missing_fields(self):
        for operation, field in (
            ("list_microvms", "items"),
            ("list_microvm_images", "items"),
            ("list_microvm_image_versions", "items"),
            ("describe_log_groups", "logGroups"),
        ):

            class Client:
                def __getattr__(self, name):
                    def pages(**kwargs):
                        return (
                            {field: [{"item": "later"}]}
                            if kwargs.get("nextToken")
                            else {field: [], "nextToken": "second"}
                        )

                    return pages

            with self.subTest(operation=operation):
                self.assertEqual(
                    self.api.pages(Client(), operation, field), [{"item": "later"}]
                )

        class Malformed:
            def list_microvms(self, **kwargs):
                return (
                    {}
                    if kwargs.get("nextToken")
                    else {"items": [], "nextToken": "second"}
                )

        with self.assertRaisesRegex(recovery.Refused, "missing_page_items"):
            self.api.pages(Malformed(), "list_microvms", "items")

    def test_missing_incarnation_and_ambiguous_identity_are_refused(self):
        del self.plane.images[arn(SHARED)]["createdAt"]
        with self.assertRaisesRegex(recovery.Refused, "invalid_timestamp"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()
        self.plane.images[arn(SHARED)]["createdAt"] = CREATED
        self.plane.extra_image_page = [copy.deepcopy(self.plane.images[arn(SHARED)])]
        with self.assertRaisesRegex(recovery.Refused, "duplicate_identity"):
            recovery.recover(self.manifest, self.owned, self.api, {})
        self.assert_no_deletes()

    def test_exact_verifier_summary_contract_is_supported(self):
        spec = importlib.util.spec_from_file_location(
            "verify_clean", Path(__file__).with_name("verify-clean.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for leaks, unknown in ((0, 0), (0, 3), (2, 3)):
            _, line = module.verdict(leaks, unknown, False)
            rows = "".join(
                f"LEAK image synthetic-{i} in CREATED\n" for i in range(leaks)
            )
            rows += "".join(
                "UNCLASSIFIED (not deleted, not provably ours): log group "
                f"/aws/lambda-microvms/unknown-{i}\n"
                for i in range(unknown)
            )
            self.assertTrue(
                recovery.safe_verifier(rows + line, REGION)["summary_present"]
            )

    def test_source_infrastructure_is_derived_from_current_pinned_log(self):
        self.manifest["source_infrastructure"]["bucket_sha256"] = "f" * 64
        with self.assertRaisesRegex(recovery.Refused, "source_infrastructure_digest"):
            recovery.provenance(self.manifest, FakeGithub(), environment())
        for value in (
            source_log().replace("infra: bucket=source-bucket", "infra: bucket=other"),
            source_log().replace("build_role_arn", "missing_role"),
            source_log() + source_log().replace("source-bucket", "other"),
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    recovery.Refused, "source_infrastructure_ambiguous"
                ),
            ):
                recovery.source_infrastructure(value, REGION)

    def test_verifier_counts_exit_and_duplicate_rows_fail_closed(self):
        clean = f"account {REGION}: clean — no conformance resources survive\n"
        leak = "LEAK image sample in CREATED\n"
        summary = f"account {REGION}: 1 leaked resource(s)\n"
        for output in (leak + clean, summary, leak + leak + summary):
            with self.subTest(output=output), self.assertRaises(recovery.Refused):
                recovery.safe_verifier(output, REGION)
        for code, output, stderr in (
            (-15, clean, ""),
            (2, clean, ""),
            (1, clean, ""),
            (0, leak + summary, ""),
            (0, clean, "private"),
        ):
            with self.subTest(code=code), self.assertRaises(recovery.Refused):
                recovery.captured_verifier(
                    subprocess.CompletedProcess([], code, output, stderr), REGION
                )
        separate = (
            "pending: image pending-image still DELETING\n"
            "standing (terraform-owned): bucket standing-bucket\n"
            "UNCLASSIFIED (not deleted, not provably ours): log group /aws/lambda-microvms/unknown\n"
            + leak
            + summary
        )
        result = recovery.captured_verifier(
            subprocess.CompletedProcess([], 1, separate, ""), REGION
        )
        self.assertEqual(result["leakCount"], 1)
        self.assertEqual(
            {row["category"] for row in result["resources"]},
            {"pending", "standing", "unclassified", "leak"},
        )

    def test_current_role_observation_emits_only_duration_and_never_mutates(self):
        class Iam:
            def get_role(self, **kwargs):
                return {
                    "Role": {
                        "Arn": "arn:aws:iam::0:role/private",
                        "MaxSessionDuration": 7200,
                    }
                }

        value = recovery.observe_duration(
            Iam(), "arn:aws:iam::0:role/private", self.api
        )
        self.assertEqual(
            value, {"status": "observed", "maxSessionDurationSeconds": 7200}
        )
        self.assertNotIn("private", json.dumps(value))
        self.assertEqual(
            recovery.observe_duration(Iam(), "wrong", self.api),
            {"status": "unavailable"},
        )


if __name__ == "__main__":
    unittest.main()
