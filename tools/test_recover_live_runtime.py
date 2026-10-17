# SPDX-License-Identifier: Apache-2.0
"""Offline execution, diagnostic, cancellation and clock boundary verdicts."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "recovery_fixtures", Path(__file__).with_name("test_recover_live.py")
)
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RuntimeTests(unittest.TestCase):
    def run_main(
        self,
        *,
        mode="apply",
        exclusive=True,
        provenance=None,
        iam=None,
        verifier=None,
        checkout=None,
        clock=None,
        apply=None,
        inherited=None,
        expect_load=True,
        env_changes=None,
    ):
        manifest = fixtures.manifest()
        env = fixtures.environment() | {
            "GITHUB_SHA": "b" * 40,
            "GITHUB_REPOSITORY": "owner/repository",
            "RECOVERY_ROLE_ARN": "arn:aws:iam::0:role/private-role",
            "GH_TOKEN": "private-token",
            "GITHUB_TOKEN": "other-token",
        }
        env.update(env_changes or {})
        env = {key: value for key, value in env.items() if value is not None}
        if inherited is not None:
            env["RECOVERY_DEADLINE_MONOTONIC"] = inherited
        session = Mock()
        clients = {"lambda-microvms": Mock(), "logs": Mock(), "iam": iam or Mock()}
        clients["iam"].get_role.return_value = {
            "Role": {"Arn": env["RECOVERY_ROLE_ARN"], "MaxSessionDuration": 3600}
        }
        session.client.side_effect = lambda name, **kwargs: clients[name]
        boto = types.SimpleNamespace(Session=Mock(return_value=session))
        config = Mock(return_value="configuration")
        clock = clock or fixtures.Clock()
        expected_deadline = (
            min(recovery.TOTAL_SECONDS, float(inherited))
            if inherited is not None
            else recovery.TOTAL_SECONDS
        )
        api_type = recovery.Api

        def api(*args, **kwargs):
            self.assertEqual(args, (clients["lambda-microvms"], clients["logs"]))
            self.assertEqual(kwargs, {"deadline": expected_deadline})
            return api_type(*args, clock=clock, sleep=clock.sleep, **kwargs)

        applied = Mock(side_effect=apply)
        fetched = Mock(side_effect=provenance, return_value={})
        responses = [
            checkout or subprocess.CompletedProcess([], 0, "b" * 40 + "\n", "")
        ]
        responses.append(
            verifier
            or subprocess.CompletedProcess(
                [],
                0,
                f"account {fixtures.REGION}: clean — no conformance resources survive\n",
                "",
            )
        )
        runner = Mock(side_effect=responses)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "receipt.json"
            args = [
                "recover-live.py",
                mode,
                "--source-run",
                "9",
                "--manifest-sha256",
                "a" * 64,
                "--report",
                str(report),
            ]
            if exclusive:
                args.append("--exclusive-names")
            old_handler = signal.getsignal(signal.SIGTERM)
            with (
                patch("sys.argv", args),
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    recovery, "load_manifest", return_value=manifest
                ) as loader,
                patch.object(recovery, "provenance", fetched),
                patch.object(recovery, "recover", applied),
                patch.object(recovery, "Api", side_effect=api),
                patch.object(recovery.subprocess, "run", runner),
                patch.object(recovery.time, "monotonic", clock),
                patch.dict(
                    "sys.modules",
                    {
                        "boto3": boto,
                        "botocore": types.SimpleNamespace(),
                        "botocore.config": types.SimpleNamespace(Config=config),
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                code = recovery.main()
            self.assertIs(signal.getsignal(signal.SIGTERM), old_handler)
            value = json.loads(report.read_text())
            self.assertEqual(json.loads(output.getvalue()), value)
        if expect_load:
            loader.assert_called_once_with("9", "a" * 64)
        else:
            loader.assert_not_called()
        if fetched.called:
            self.assertEqual(len(fetched.call_args.args), 3)
            selected_manifest, github, actual_env = fetched.call_args.args
            self.assertEqual(selected_manifest, manifest)
            self.assertIsInstance(github, recovery.Github)
            self.assertEqual(
                (github.repository, github.deadline),
                (env["GITHUB_REPOSITORY"], expected_deadline),
            )
            self.assertEqual(actual_env, env)
        if applied.called:
            self.assertEqual(len(applied.call_args.args), 4)
            selected_manifest, owned, client, receipt = applied.call_args.args
            self.assertEqual(selected_manifest, manifest)
            self.assertEqual(owned, {})
            self.assertIsInstance(client, api_type)
            self.assertEqual(receipt, value)
        if boto.Session.called:
            self.assertEqual(
                session.client.call_args_list,
                [
                    unittest.mock.call("lambda-microvms", config="configuration"),
                    unittest.mock.call("logs", config="configuration"),
                    unittest.mock.call("iam", config="configuration"),
                ],
            )
        return code, value, boto, runner, applied, fetched, config

    def test_command_line_requires_explicit_mode_and_input_contract(self):
        base = [
            "recover-live.py",
            "preflight",
            "--source-run",
            "9",
            "--manifest-sha256",
            "a" * 64,
            "--report",
            "receipt.json",
        ]
        for flag in ("--source-run", "--manifest-sha256", "--report"):
            argv = base.copy()
            index = argv.index(flag)
            del argv[index : index + 2]
            with (
                patch("sys.argv", argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                recovery.main()
            self.assertEqual(caught.exception.code, 2)
        with (
            patch("sys.argv", ["recover-live.py", "unknown", *base[2:]]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            recovery.main()
        self.assertEqual(caught.exception.code, 2)
        with (
            patch("sys.argv", ["recover-live.py", "--help"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as caught,
        ):
            recovery.main()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn(
            " ".join(recovery.__doc__.split()), " ".join(output.getvalue().split())
        )

    def test_main_missing_sha_and_region_drift_stop_before_aws(self):
        code, value, boto, *_ = self.run_main(env_changes={"GITHUB_SHA": None})
        self.assertEqual((code, value["reason"]), (1, "recovery_checkout"))
        boto.Session.assert_not_called()

        def drift(*args):
            os.environ["AWS_REGION"] = "us-west-2"
            return {}

        code, value, boto, *_ = self.run_main(provenance=drift)
        self.assertEqual((code, value["reason"]), (1, "aws_region"))
        boto.Session.assert_not_called()

    def test_main_apply_validates_checkout_provenance_and_sanitized_verifier(self):
        code, value, boto, runner, applied, fetched, config = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(
            value,
            {
                "kind": "live-recovery",
                "conformance_verified": False,
                "status": "passed",
                "sourceRun": 9,
                "sourceSha": "a" * 40,
                "region": fixtures.REGION,
                "manifestSha256": "a" * 64,
                "parserVersion": recovery.PARSER_VERSION,
                "sourceLogRepresentation": recovery.LOG_REPRESENTATION,
                "versionEvidence": [],
                "apiFailures": [],
                "roleDuration": {
                    "status": "observed",
                    "maxSessionDurationSeconds": 3600,
                },
                "independentVerifier": {
                    "resources": [],
                    "summary_present": True,
                    "leakCount": 0,
                    "exitCode": 0,
                },
            },
        )
        self.assertEqual(fetched.call_count, 1)
        self.assertEqual(applied.call_count, 1)
        boto.Session.assert_called_once_with(region_name=fixtures.REGION)
        config.assert_called_once_with(
            connect_timeout=5, read_timeout=5, retries={"total_max_attempts": 1}
        )
        first, last = runner.call_args_list
        self.assertEqual(first.args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(
            first.kwargs,
            {
                "cwd": recovery.ROOT,
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "check": False,
            },
        )
        self.assertEqual(
            last.args[0],
            [recovery.sys.executable, str(recovery.ROOT / "tools/verify-clean.py")],
        )
        self.assertEqual(last.kwargs["timeout"], 120)
        self.assertTrue(last.kwargs["capture_output"])
        self.assertTrue(last.kwargs["text"])
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "RECOVERY_ROLE_ARN"):
            self.assertNotIn(key, last.kwargs["env"])

    def test_preflight_and_refusals_never_construct_aws_session(self):
        code, value, boto, runner, applied, _, _ = self.run_main(mode="preflight")
        self.assertEqual((code, value["status"]), (0, "passed"))
        boto.Session.assert_not_called()
        applied.assert_not_called()
        self.assertEqual(runner.call_count, 1)
        for kwargs, reason in (
            ({"exclusive": False}, "exclusive_names_required"),
            (
                {"checkout": subprocess.CompletedProcess([], 0, "wrong", "")},
                "recovery_checkout",
            ),
            ({"provenance": recovery.Refused("origin_id")}, "origin_id"),
        ):
            with self.subTest(reason=reason):
                code, value, boto, _, applied, _, _ = self.run_main(**kwargs)
                self.assertEqual(
                    (code, value["status"], value["reason"]), (1, "failed", reason)
                )
                boto.Session.assert_not_called()
                applied.assert_not_called()

    def test_total_deadline_includes_successful_provenance_before_aws(self):
        clock = fixtures.Clock()

        def slow(*args):
            clock.sleep(recovery.TOTAL_SECONDS)
            return {}

        code, value, boto, _, applied, _, _ = self.run_main(
            clock=clock, provenance=slow
        )
        self.assertEqual((code, value["reason"]), (1, "deadline"))
        boto.Session.assert_not_called()
        applied.assert_not_called()

    def test_inherited_deadline_is_finite_and_shared_with_apply(self):
        for inherited, reason in (
            ("nan", "invalid_deadline"),
            ("inf", "invalid_deadline"),
            ("0", "deadline"),
            ("-1", "deadline"),
        ):
            with self.subTest(inherited=inherited):
                code, value, boto, runner, applied, _, _ = self.run_main(
                    inherited=inherited, expect_load=False
                )
                self.assertEqual((code, value["reason"]), (1, reason))
                boto.Session.assert_not_called()
                runner.assert_not_called()
                applied.assert_not_called()
        for inherited, expected in (("10", 10), ("1000", recovery.TOTAL_SECONDS)):

            def deadline_observed(manifest, ownership, api, report):
                self.assertEqual(api.deadline, expected)

            self.assertEqual(
                self.run_main(inherited=inherited, apply=deadline_observed)[0], 0
            )
        clock = fixtures.Clock()

        def expired_after_deletes(*args):
            clock.now = recovery.TOTAL_SECONDS

        code, value, _, runner, _, _, _ = self.run_main(
            clock=clock, apply=expired_after_deletes
        )
        self.assertEqual((code, value["reason"]), (1, "deadline"))
        self.assertEqual(runner.call_count, 1)

    def test_sigterm_or_interrupted_iam_observation_never_reaches_deletion(self):
        for interruption in (InterruptedError, KeyboardInterrupt, "signal"):
            iam = Mock()

            def stop(**kwargs):
                if interruption == "signal":
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                raise interruption("private-error")

            iam.get_role.side_effect = stop
            code, value, _, _, applied, _, _ = self.run_main(iam=iam)
            self.assertEqual(
                (code, value["status"], value["reason"]),
                (1, "failed", "interrupted_or_invalid_input"),
            )
            applied.assert_not_called()
            self.assertNotIn("private", json.dumps(value))

    def test_main_retains_safe_independent_rows_when_selected_survivor_refused(self):
        output = (
            f"LEAK image {fixtures.SHARED} in CREATED\n"
            f"account {fixtures.REGION}: 1 leaked resource(s)\n"
        )
        code, value, *_ = self.run_main(
            verifier=subprocess.CompletedProcess([], 1, output, "")
        )
        self.assertEqual((code, value["reason"]), (1, "independent_survivor"))
        self.assertEqual(
            value["independentVerifier"]["resources"],
            [
                {
                    "kind": "image",
                    "sha256": recovery.identity(
                        "image", fixtures.REGION, fixtures.SHARED
                    ),
                    "category": "leak",
                }
            ],
        )
        self.assertNotIn(fixtures.SHARED, json.dumps(value))

    def test_github_captured_commands_are_exact_and_share_deadline(self):
        clock = fixtures.Clock()
        client = recovery.Github("owner/repository", deadline=10, clock=clock)
        with patch.object(
            recovery.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "{}", "private"),
        ) as run:
            self.assertEqual(client.api("actions/runs/9"), {})
            self.assertEqual(
                run.call_args.args[0],
                ["gh", "api", "repos/owner/repository/actions/runs/9"],
            )
            self.assertEqual(
                run.call_args.kwargs,
                {"capture_output": True, "text": True, "timeout": 10, "check": False},
            )
            self.assertEqual(client.log(fixtures.manifest()), "{}")
            self.assertEqual(
                run.call_args.args[0],
                [
                    "gh",
                    "run",
                    "view",
                    "9",
                    "--repo",
                    "owner/repository",
                    "--job",
                    "10",
                    "--log",
                ],
            )
        clock.now = 10
        with patch.object(recovery.subprocess, "run") as run:
            with self.assertRaisesRegex(recovery.Refused, r"^deadline$"):
                client.command([])
            run.assert_not_called()
        for name in ("", "owner", "owner/repo/extra", "bad name/repo"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(recovery.Refused, r"^repository_name$"),
            ):
                recovery.Github(name)

    def test_github_failure_and_late_success_are_refused_without_output(self):
        clock = fixtures.Clock()
        client = recovery.Github("owner/repository", deadline=5, clock=clock)
        with patch.object(
            recovery.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "private", "secret"),
        ):
            with self.assertRaisesRegex(recovery.Refused, r"^source_fetch_failed$"):
                client.command([])

        def late(*args, **kwargs):
            clock.now = 5
            return subprocess.CompletedProcess([], 0, "private", "")

        with patch.object(recovery.subprocess, "run", side_effect=late):
            with self.assertRaisesRegex(recovery.Refused, r"^deadline$"):
                client.command([])

    def test_duration_observation_exact_role_value_and_interruption(self):
        clock = fixtures.Clock()
        api = recovery.Api(None, None, clock=clock, sleep=clock.sleep)
        role = "arn:aws:iam::0:role/nested/name"
        iam = Mock()
        for seconds, arn in (
            (0, role),
            (-1, role),
            (True, role),
            (1.5, role),
            ("3600", role),
            (3600, "other"),
        ):
            iam.get_role.return_value = {
                "Role": {"Arn": arn, "MaxSessionDuration": seconds}
            }
            self.assertEqual(
                recovery.observe_duration(iam, role, api), {"status": "unavailable"}
            )
        iam.get_role.return_value = {"Role": {"Arn": role, "MaxSessionDuration": 3600}}
        self.assertEqual(
            recovery.observe_duration(iam, role, api),
            {"status": "observed", "maxSessionDurationSeconds": 3600},
        )
        iam.get_role.assert_called_with(RoleName="name")
        iam.get_role.return_value = {"Role": {"Arn": role, "MaxSessionDuration": 1}}
        self.assertEqual(
            recovery.observe_duration(iam, role, api),
            {"status": "observed", "maxSessionDurationSeconds": 1},
        )
        iam.get_role.side_effect = InterruptedError
        with self.assertRaises(InterruptedError):
            recovery.observe_duration(iam, role, api)

    def test_provenance_rejects_each_boundary_with_exact_reason(self):
        expected = {
            "GITHUB_ACTIONS": "hosted_main_required",
            "GITHUB_REF": "hosted_main_required",
            "GITHUB_REPOSITORY_ID": "repository_id",
            "GITHUB_RUN_ID": "current_run_id",
            "GITHUB_ACTOR": "recovery_owner",
            "GITHUB_TRIGGERING_ACTOR": "recovery_owner",
            "AWS_REGION": "selected_region",
        }
        manifest = fixtures.manifest()
        for key, reason in expected.items():
            env = fixtures.environment()
            env[key] = "bad"
            with self.subTest(key=key), self.assertRaises(recovery.Refused) as caught:
                recovery.provenance(manifest, fixtures.FakeGithub(), env)
            self.assertEqual(str(caught.exception), reason)
        env = fixtures.environment()
        env["GITHUB_RUN_ID"] = "9"
        with self.assertRaises(recovery.Refused) as caught:
            recovery.provenance(manifest, fixtures.FakeGithub(), env)
        self.assertEqual(str(caught.exception), "source_is_current_run")
        run = "actions/runs/9/attempts/1"
        job = "actions/jobs/10"
        for path, fields, prefix in (
            (
                run,
                (
                    "id",
                    "run_attempt",
                    "workflow_id",
                    "path",
                    "head_sha",
                    "event",
                    "status",
                    "conclusion",
                ),
                "origin_",
            ),
            (
                job,
                (
                    "run_id",
                    "run_attempt",
                    "name",
                    "status",
                    "conclusion",
                    "started_at",
                    "completed_at",
                ),
                "origin_job_",
            ),
        ):
            for key in fields:
                fake = fixtures.FakeGithub()
                fake.values[path][key] = "bad"
                with (
                    self.subTest(path=path, key=key),
                    self.assertRaises(recovery.Refused) as caught,
                ):
                    recovery.provenance(manifest, fake, fixtures.environment())
                self.assertEqual(str(caught.exception), prefix + key)
        for path, key, nested, reason in (
            ("", "id", None, "repository_id"),
            (run, "repository", "id", "origin_repository"),
            (run, "head_repository", "id", "origin_repository"),
            (run, "actor", "login", "origin_owner"),
            (run, "triggering_actor", "login", "origin_owner"),
            ("actions/runs/9", "run_attempt", None, "origin_attempt_changed"),
            ("actions/runs/9", "status", None, "origin_attempt_changed"),
            (
                f"contents/{recovery.WORKFLOW}?ref={'a' * 40}",
                "encoding",
                None,
                "source_encoding",
            ),
            (
                f"contents/{recovery.WORKFLOW}?ref={'a' * 40}",
                "content",
                None,
                "source_workflow_digest",
            ),
        ):
            fake = fixtures.FakeGithub()
            if nested:
                fake.values[path][key][nested] = "bad"
            else:
                fake.values[path][key] = "YmFk" if key == "content" else "bad"
            with (
                self.subTest(path=path, key=key),
                self.assertRaises(recovery.Refused) as caught,
            ):
                recovery.provenance(manifest, fake, fixtures.environment())
            self.assertEqual(str(caught.exception), reason)
        fake = fixtures.FakeGithub()
        fake.log_value += "changed"
        with self.assertRaises(recovery.Refused) as caught:
            recovery.provenance(manifest, fake, fixtures.environment())
        self.assertEqual(str(caught.exception), "source_log_digest")
        for change, reason in (
            ("proof", "unproven_resource"),
            ("extra", "ownership_set"),
        ):
            value = fixtures.manifest()
            if change == "proof":
                value["resources"][0]["proof"] = "bad"
            else:
                value["resources"].pop()
            with self.assertRaises(recovery.Refused) as caught:
                recovery.provenance(
                    value, fixtures.FakeGithub(), fixtures.environment()
                )
            self.assertEqual(str(caught.exception), reason)

    def test_default_github_budget_and_repository_case(self):
        clock = fixtures.Clock()
        client = recovery.Github("Owner/Repository", clock=clock)
        self.assertEqual(client.repository, "Owner/Repository")
        self.assertIs(client.clock, clock)
        self.assertEqual(client.deadline, recovery.TOTAL_SECONDS)

    def test_api_deadline_reason_is_fixed(self):
        clock = fixtures.Clock()
        api = recovery.Api(None, None, clock=clock, deadline=0)
        with self.assertRaises(recovery.Refused) as caught:
            api.check_time()
        self.assertEqual(str(caught.exception), "deadline")

    def test_provenance_fetches_manifest_and_refuses_missing_run_and_infrastructure(
        self,
    ):
        manifest = fixtures.manifest()
        fake = fixtures.FakeGithub()
        fake.log = Mock(wraps=fake.log)
        recovery.provenance(manifest, fake, fixtures.environment())
        fake.log.assert_called_once_with(manifest)
        env = fixtures.environment()
        env.pop("GITHUB_RUN_ID")
        with self.assertRaisesRegex(recovery.Refused, r"^current_run_id$"):
            recovery.provenance(manifest, fixtures.FakeGithub(), env)
        manifest["source_infrastructure"]["bucket_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            recovery.Refused, r"^source_infrastructure_digest$"
        ):
            recovery.provenance(manifest, fixtures.FakeGithub(), fixtures.environment())

    def test_github_short_positive_deadline_and_fixed_call_cap(self):
        clock = fixtures.Clock()
        for deadline, expected in ((0.5, 0.5), (600, 120)):
            client = recovery.Github("owner/repository", deadline=deadline, clock=clock)
            with patch.object(
                recovery.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "ok", ""),
            ) as run:
                self.assertEqual(client.command([]), "ok")
                self.assertEqual(run.call_args.kwargs["timeout"], expected)

    def test_api_clock_and_error_classification_are_exact(self):
        clock = fixtures.Clock()
        plane = Mock()
        api = recovery.Api(plane, None, clock=clock, sleep=clock.sleep)
        self.assertEqual(
            (
                api.deadline,
                api.region,
                api.source_infrastructure,
                api.version_evidence,
                api._observed_versions,
            ),
            (recovery.TOTAL_SECONDS, "", {}, [], set()),
        )
        explicit = recovery.Api(plane, None, clock=clock, sleep=clock.sleep, deadline=5)
        self.assertEqual(explicit.deadline, 5)
        self.assertEqual(recovery.error_code(RuntimeError()), "unknown")
        for response, expected in (
            ({}, "unknown"),
            ({"Error": {}}, "unknown"),
            ([], "unknown"),
            ({"Error": {"Code": "Specific"}}, "Specific"),
        ):
            error = RuntimeError()
            error.response = response
            self.assertEqual(recovery.error_code(error), expected)
        for code, attempts in (
            ("ThrottlingException", 3),
            ("TooManyRequestsException", 3),
            ("ServiceUnavailableException", 3),
            ("RequestTimeout", 3),
            ("ResourceNotFoundException", 1),
            ("AccessDeniedException", 1),
        ):
            error = RuntimeError()
            error.response = {"Error": {"Code": code}}
            plane.method.side_effect = error
            plane.method.reset_mock()
            with self.assertRaises(recovery.Refused) as caught:
                api.call(plane, "method", exact="input")
            self.assertEqual(
                str(caught.exception),
                "not_found" if code == "ResourceNotFoundException" else "api_failed",
            )
            self.assertEqual(plane.method.call_count, attempts)
            plane.method.assert_called_with(exact="input")
        plane.method.side_effect = None
        plane.method.return_value = []
        with self.assertRaises(recovery.Refused) as caught:
            api.call(plane, "method")
        self.assertEqual(str(caught.exception), "invalid_response")
        plane.method.side_effect = InterruptedError
        with self.assertRaises(InterruptedError):
            api.call(plane, "method")

    def test_verifier_exact_categories_grammar_and_summary_reconciliation(self):
        names = [
            ("pending", "image", "pending: image one still DELETING"),
            ("leak", "image", "LEAK image two in CREATED"),
            ("leak", "microvm", "LEAK microvm three in RUNNING"),
            ("leak", "log-group", "LEAK log group four (ours by prefix)"),
            ("leak", "log-group", "LEAK log group five (named in location)"),
            (
                "leak",
                "log-group",
                "LEAK log group six (per-VM, named with --vm-log-group)",
            ),
            (
                "unclassified",
                "log-group",
                "UNCLASSIFIED (not deleted, not provably ours): log group seven",
            ),
            ("standing", "bucket", "standing (terraform-owned): bucket eight"),
            ("standing", "iam-role", "standing (terraform-owned): iam role Nine"),
            (
                "standing",
                "log-group",
                "standing (terraform-owned): per-VM log group ten (named in location)",
            ),
        ]
        raw = (
            "\n".join(row[2] for row in names)
            + f"\naccount {fixtures.REGION}: 5 leaked resource(s)\n"
        )
        value = recovery.safe_verifier(raw, fixtures.REGION)
        self.assertEqual(
            value,
            {
                "summary_present": True,
                "leakCount": 5,
                "resources": [
                    {
                        "kind": kind,
                        "sha256": recovery.identity(kind, fixtures.REGION, name),
                        "category": category,
                    }
                    for (category, kind, _), name in zip(
                        names,
                        (
                            "one",
                            "two",
                            "three",
                            "four",
                            "five",
                            "six",
                            "seven",
                            "eight",
                            "Nine",
                            "ten",
                        ),
                        strict=True,
                    )
                ],
            },
        )
        unknown = "UNCLASSIFIED (not deleted, not provably ours): log group seven\n"
        summary = (
            f"account {fixtures.REGION}: clean of everything this project can name; 1 log group(s) under "
            "/aws/lambda-microvms/ belong to no known prefix and no ledger record and were left alone (listed above; --strict fails on them)"
        )
        self.assertEqual(
            recovery.captured_verifier(
                subprocess.CompletedProcess([], 0, unknown + summary, ""),
                fixtures.REGION,
            )["exitCode"],
            0,
        )
        for text, reason in (
            (raw + "private", "verifier_output_unrecognized"),
            (
                raw + f"account {fixtures.REGION}: 5 leaked resource(s)\n",
                "verifier_summary",
            ),
            (raw.replace("5 leaked", "4 leaked"), "verifier_count_mismatch"),
            (summary, "verifier_count_mismatch"),
            (raw + names[0][2], "verifier_duplicate"),
        ):
            with (
                self.subTest(reason=reason),
                self.assertRaises(recovery.Refused) as caught,
            ):
                recovery.safe_verifier(text, fixtures.REGION)
            self.assertEqual(str(caught.exception), reason)

    def test_captured_verifier_refusal_reasons_are_fixed(self):
        clean = f"account {fixtures.REGION}: clean — no conformance resources survive\n"
        for code, stderr, reason in (
            (-15, "", "verifier_execution"),
            (2, "", "verifier_execution"),
            (0, "private", "verifier_execution"),
            (1, "", "verifier_exit_mismatch"),
        ):
            with self.subTest(code=code), self.assertRaises(recovery.Refused) as caught:
                recovery.captured_verifier(
                    subprocess.CompletedProcess([], code, clean, stderr),
                    fixtures.REGION,
                )
            self.assertEqual(str(caught.exception), reason)

    def test_terminal_api_diagnostics_use_only_allowlisted_names(self):
        clock = fixtures.Clock()
        api = recovery.Api(None, None, clock=clock, sleep=clock.sleep)
        for operation, code, expected_operation, expected_code in (
            (
                "delete_microvm_image",
                "AccessDeniedException",
                "delete_microvm_image",
                "AccessDeniedException",
            ),
            ("private-operation", "private-code", "unknown", "unknown"),
        ):
            error = RuntimeError("private-message")
            error.response = {"Error": {"Code": code, "Message": "private-message"}}
            client = Mock()
            getattr(client, operation).side_effect = error
            with self.assertRaises(recovery.Refused) as caught:
                api.call(client, operation)
            self.assertEqual(str(caught.exception), "api_failed")
            self.assertEqual(
                api.failures[-1],
                {"operation": expected_operation, "code": expected_code},
            )
        self.assertNotIn("private", json.dumps(api.failures))

    def test_optional_role_observation_rejects_before_api_and_sanitizes_api_errors(
        self,
    ):
        clock = fixtures.Clock()
        api = recovery.Api(None, None, clock=clock, sleep=clock.sleep)
        iam = Mock()
        for role in ("not-an-arn/name", "", None, 4):
            with self.subTest(role=role):
                self.assertEqual(
                    recovery.observe_duration(iam, role, api), {"status": "unavailable"}
                )
                iam.get_role.assert_not_called()
        iam.get_role.side_effect = RuntimeError("private-credentials")
        role = "arn:aws:iam::0:role/name"
        self.assertEqual(
            recovery.observe_duration(iam, role, api), {"status": "unavailable"}
        )
        iam.get_role.assert_called_once_with(RoleName="name")

    def test_remaining_fractional_budget_reaches_independent_verifier(self):
        clock = fixtures.Clock()

        def nearly_spent(*args):
            clock.now = recovery.TOTAL_SECONDS - 0.5

        code, value, _, runner, *_ = self.run_main(clock=clock, apply=nearly_spent)
        self.assertEqual((code, value["status"]), (0, "passed"))
        self.assertEqual(runner.call_args.kwargs["timeout"], 0.5)

    def test_version_observation_is_exact_deduplicated_metadata_only(self):
        clock = fixtures.Clock()
        api = recovery.Api(None, None, clock=clock, sleep=clock.sleep)
        api.region = fixtures.REGION
        role, bucket, key, group = (
            "private-role",
            "private-bucket",
            "Xprivate-key",
            "private-group",
        )
        api.source_infrastructure = {
            "bucket_sha256": recovery.identity("bucket", fixtures.REGION, bucket),
            "build_role_arn_sha256": recovery.digest(role.encode()),
        }
        record = {
            "createdAt": fixtures.CREATED,
            "buildRoleArn": role,
            "codeArtifact": {"uri": f"s3://{bucket}/{key}"},
            "logging": {"cloudWatch": {"logGroup": group}},
        }
        api.observe_version("name", "1", record)
        self.assertEqual(
            api.version_evidence,
            [
                {
                    "imageSha256": recovery.identity("image", fixtures.REGION, "name"),
                    "versionSha256": recovery.digest(b"1"),
                    "createdAt": fixtures.CREATED.timestamp(),
                    "buildRoleArnSha256": recovery.digest(role.encode()),
                    "artifactBucketSha256": recovery.identity(
                        "bucket", fixtures.REGION, bucket
                    ),
                    "artifactKeySha256": recovery.digest(key.encode()),
                    "logGroupSha256": recovery.identity(
                        "log-group", fixtures.REGION, group
                    ),
                    "sourceBucketMatches": True,
                    "sourceBuildRoleMatches": True,
                }
            ],
        )
        api.observe_version("name", "1", copy.deepcopy(record))
        self.assertEqual(len(api.version_evidence), 1)
        api.observe_version(
            "other",
            "1",
            {
                "createdAt": fixtures.CREATED,
                "codeArtifact": {"uri": "https://private/x"},
            },
        )
        self.assertEqual(
            api.version_evidence[-1],
            {
                "imageSha256": recovery.identity("image", fixtures.REGION, "other"),
                "versionSha256": recovery.digest(b"1"),
                "createdAt": fixtures.CREATED.timestamp(),
                "sourceBucketMatches": False,
                "sourceBuildRoleMatches": False,
            },
        )
        self.assertNotIn("private", json.dumps(api.version_evidence))
        api.observe_version(
            "key-case",
            "1",
            {
                "createdAt": fixtures.CREATED,
                "codeArtifact": {"uri": "s3://bucket/XXname"},
            },
        )
        self.assertEqual(
            api.version_evidence[-1]["artifactKeySha256"], recovery.digest(b"XXname")
        )
