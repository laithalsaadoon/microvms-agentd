# SPDX-License-Identifier: Apache-2.0
"""Verdict and input checks for the prose gate."""

import inspect
import io
import json
import runpy
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

P = runpy.run_path(str(Path(__file__).with_name("prose.py")), run_name="tools.prose")


# Mutation instrumentation wraps functions; patch the underlying module globals.
PROSE_GLOBALS = inspect.unwrap(P["main"]).__globals__


class GitTests(unittest.TestCase):
    def test_git_preserves_the_requested_tree_and_process_failure(self):
        root = Path("a-checkout")
        failed = subprocess.CompletedProcess([], 1, "", "missing ref")
        with patch.object(subprocess, "run", return_value=failed) as run:
            self.assertIs(P["git"](root, "show", "HEAD:file"), failed)
        run.assert_called_once_with(
            ["git", "show", "HEAD:file"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )


class ScopeTests(unittest.TestCase):
    def test_all_maintained_areas(self):
        for name in (
            "README.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/PROTOCOL.md",
            "docs/architecture/module-map.md",
            "bindings/microvms-py/README.md",
            "crates/model/README.md",
            "examples/background-coding-agent/README.md",
            "site/authored/learn/first-run.mdx",
        ):
            with self.subTest(name=name):
                self.assertTrue(P["in_scope"](name))

    def test_generated_and_unrelated_paths_are_out(self):
        for name in (
            "CHANGELOG.md",
            "docs/TRACEABILITY.md",
            "docs/schema.json",
            "site/src/content/docs/index.md",
            "crates/model/notes.md",
            "tools/README.md",
            "bindings/readme.txt",
            "docs/.keep",
        ):
            with self.subTest(name=name):
                self.assertFalse(P["in_scope"](name))

    def test_empty_or_unreadable_scan_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for code, output in ((1, ""), (0, ""), (0, "README.md\0")):
                result = subprocess.CompletedProcess([], code, output, "unreadable")
                with patch.dict(PROSE_GLOBALS, git=lambda *_: result):
                    with self.assertRaises(P["Failure"]):
                        P["sources"](root)
            (root / "README.md").write_text("Content")
            failed = subprocess.CompletedProcess([], 1, "README.md\0", "unreadable")
            with patch.dict(PROSE_GLOBALS, git=lambda *_: failed):
                with self.assertRaises(P["Failure"]):
                    P["sources"](root)
            (root / "README.md").write_text("")
            with patch.dict(PROSE_GLOBALS, git=lambda *_: result):
                with self.assertRaises(P["Failure"]):
                    P["sources"](root)

    def test_source_errors_explain_why_the_scan_did_not_run(self):
        root = Path("absent")
        for result, message in (
            (
                subprocess.CompletedProcess([], 1, "", " denied "),
                "Cannot enumerate prose: denied",
            ),
            (
                subprocess.CompletedProcess([], 0, "", ""),
                "No nonempty prose inputs; refusing an empty scan.",
            ),
        ):
            with patch.dict(PROSE_GLOBALS, git=lambda *_: result):
                with self.assertRaises(P["Failure"]) as error:
                    P["sources"](root)
                self.assertEqual(str(error.exception), message)

    def test_sources_deduplicate_and_ignore_deleted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Read this guide.")
            result = subprocess.CompletedProcess(
                [], 0, "README.md\0README.md\0SECURITY.md\0CHANGELOG.md\0", ""
            )
            run = Mock(return_value=result)
            with patch.dict(PROSE_GLOBALS, git=run):
                self.assertEqual(P["sources"](root), ["README.md"])
            run.assert_called_once_with(
                root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
            )


class VerdictTests(unittest.TestCase):
    def test_malformed_baselines_fail_closed(self):
        cases = {
            "not JSON": "Invalid warning baseline JSON.",
            "[]": "Warning baseline requires version 1.",
            "{}": "Warning baseline requires version 1.",
            '{"version":2}': "Warning baseline requires version 1.",
            '{"version":true,"warnings":{}}': "Warning baseline requires version 1.",
            '{"version":1}': "Warning baseline must map rules to ceilings.",
            '{"version":1,"warnings":[]}': "Warning baseline must map rules to ceilings.",
        }
        for rules in ({"A": True}, {"A": -1}, {"": 0}, {"A": "1"}):
            cases[json.dumps({"version": 1, "warnings": rules})] = (
                "Each warning ceiling must name a rule and a nonnegative integer."
            )
        for value, message in cases.items():
            with self.subTest(value=value), self.assertRaises(P["Failure"]) as error:
                P["ceilings"](value)
            self.assertEqual(str(error.exception), message)
        self.assertEqual(P["ceilings"]('{"version":1,"warnings":{"A":0}}'), {"A": 0})
        self.assertEqual(P["ceilings"]('{"version":1,"warnings":{}}'), {})

    def test_malformed_vale_reports_fail_closed(self):
        cases = {
            "invalid": "Vale returned invalid JSON.",
            "[]": "Vale report must map files to alerts.",
            '{"a":{}}': "Vale report contains an invalid file entry.",
            '{"a":[null]}': "Vale report contains an invalid alert.",
            '{"a":[{"Severity":"fatal","Check":"A"}]}': "Vale report contains an invalid alert.",
            '{"a":[{"Severity":"error"}]}': "Vale report contains an invalid alert.",
            '{"a":[{"Severity":"error","Check":""}]}': "Vale report contains an invalid alert.",
        }
        for value, message in cases.items():
            with self.subTest(value=value), self.assertRaises(P["Failure"]) as error:
                P["alerts"](value)
            self.assertEqual(str(error.exception), message)
        self.assertEqual(P["alerts"]("{}"), [])
        for level in ("suggestion", "warning", "error"):
            row = {"Severity": level, "Check": "A"}
            self.assertEqual(
                P["alerts"](json.dumps({"a": [row]})), [dict(row, File="a")]
            )

    def test_errors_always_fail(self):
        rows = P["alerts"](
            json.dumps(
                {
                    "README.md": [
                        {
                            "Severity": "error",
                            "Check": "Microvms.AmericanSpelling",
                            "Line": 7,
                            "Message": "Use behavior.",
                        }
                    ]
                }
            )
        )
        self.assertEqual(
            P["verdict"](rows, {"Microvms.AmericanSpelling": 100}),
            ["README.md:7: Microvms.AmericanSpelling: Use behavior."],
        )

    def test_minimal_error_still_has_a_location_and_message(self):
        self.assertEqual(
            P["verdict"]([{"File": "a", "Severity": "error", "Check": "A"}], {}),
            ["a:?: A: prose error"],
        )

    def test_each_warning_rule_has_its_own_ceiling(self):
        rows = [
            {"Severity": "warning", "Check": "A", "File": "a"},
            {"Severity": "warning", "Check": "A", "File": "b"},
            {"Severity": "suggestion", "Check": "B", "File": "a"},
        ]
        self.assertEqual(P["verdict"](rows, {"A": 2}), [])
        self.assertEqual(P["verdict"](rows, {"A": 3}), [])
        self.assertEqual(
            P["verdict"](rows, {"A": 1, "B": 100}), ["A: 2 warnings exceed ceiling 1"]
        )
        self.assertEqual(P["verdict"](rows, {}), ["A: 2 warnings exceed ceiling 0"])

    def test_baseline_cannot_increase_from_base(self):
        responses = iter(
            [
                subprocess.CompletedProcess([], 0, "abc\n", ""),
                subprocess.CompletedProcess(
                    [], 0, "100644 blob obj\t.vale/warnings.json\n", ""
                ),
                subprocess.CompletedProcess(
                    [], 0, '{"version":1,"warnings":{"A":2}}', ""
                ),
            ]
        )
        run = Mock(side_effect=lambda *_: next(responses))
        with patch.dict(PROSE_GLOBALS, git=run):
            self.assertEqual(
                P["baseline_growth"](Path("."), {"A": 3, "B": 1}, "main"),
                [
                    "A: warning ceiling increased from 2 to 3",
                    "B: warning ceiling increased from 0 to 1",
                ],
            )

        self.assertEqual(
            run.call_args_list,
            [
                call(Path("."), "merge-base", "HEAD", "main"),
                call(Path("."), "ls-tree", "abc", "--", ".vale/warnings.json"),
                call(Path("."), "show", "abc:.vale/warnings.json"),
            ],
        )

    def test_unchanged_and_reduced_baselines_pass(self):
        for current in ({"A": 2}, {"A": 1}, {}):
            results = [
                subprocess.CompletedProcess([], 0, text, "")
                for text in ("abc", "entry", '{"version":1,"warnings":{"A":2}}')
            ]
            with patch.dict(PROSE_GLOBALS, git=Mock(side_effect=results)):
                self.assertEqual(P["baseline_growth"](Path("."), current, "main"), [])

    def test_initial_baseline_is_allowed(self):
        responses = iter(
            [
                subprocess.CompletedProcess([], 0, "abc", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
        )
        with patch.dict(PROSE_GLOBALS, git=lambda *_: next(responses)):
            self.assertEqual(P["baseline_growth"](Path("."), {"A": 2}, "main"), [])

    def test_unreadable_base_never_silently_resets_ceiling(self):
        for failure_index in (0, 1, 2):
            responses = [
                subprocess.CompletedProcess([], 0, "abc", ""),
                subprocess.CompletedProcess([], 0, "entry", ""),
                subprocess.CompletedProcess(
                    [], 0, '{"version":1,"warnings":{"A":2}}', ""
                ),
            ]
            responses[failure_index] = subprocess.CompletedProcess([], 1, "", "failed")
            iterator = iter(responses)
            with patch.dict(PROSE_GLOBALS, git=lambda *_: next(iterator)):
                with self.assertRaises(P["Failure"]) as error:
                    P["baseline_growth"](Path("."), {"A": 2}, "main")
                self.assertEqual(
                    str(error.exception),
                    (
                        "Cannot resolve prose baseline base main.",
                        "Cannot inspect the base warning baseline.",
                        "Cannot read the base warning baseline.",
                    )[failure_index],
                )


class MainTests(unittest.TestCase):
    def invoke(self, report, code=0, args=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".vale").mkdir()
            (root / ".vale/warnings.json").write_text(
                '{"version":1,"warnings":{"A":1}}'
            )
            process = subprocess.CompletedProcess([], code, json.dumps(report), "")
            growth = Mock(return_value=[])
            with patch.dict(
                PROSE_GLOBALS,
                ROOT=root,
                sources=lambda _: ["README.md"],
                baseline_growth=growth,
            ):
                with patch.object(subprocess, "run", return_value=process) as run:
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        status = P["main"](args or [])
            self.last_stdout, self.last_stderr = stdout.getvalue(), stderr.getvalue()
            self.last_growth = growth
            self.last_root = root
            return status, run.call_args

    def test_clean_and_budgeted_warnings_pass(self):
        self.assertEqual(self.invoke({})[0], 0)
        warning = {"Severity": "warning", "Check": "A"}
        self.assertEqual(self.invoke({"README.md": [warning]})[0], 0)

    def test_errors_excess_warnings_and_process_errors_fail(self):
        for report, code in [
            ({"README.md": [{"Severity": "error", "Check": "A"}]}, 1),
            ({"README.md": [{"Severity": "warning", "Check": "B"}]}, 0),
            ({}, 2),
            ({}, 1),
            ([], 0),
        ]:
            with self.subTest(report=report, code=code):
                self.assertEqual(self.invoke(report, code)[0], 1)

    def test_cli_reports_counts_and_preserves_stderr_diagnostics(self):
        rows = [
            {"Severity": level, "Check": "A"}
            for level in ("error", "warning", "suggestion")
        ]
        self.assertEqual(self.invoke({"README.md": rows}, 1)[0], 1)
        self.assertEqual(
            self.last_stdout, "Prose: 1 errors, 1 warnings, 1 suggestions.\n"
        )
        self.assertEqual(self.last_stderr, "README.md:?: A: prose error\n")
        self.assertEqual(self.invoke({"README.md": [rows[0]]}, 1)[0], 1)
        self.assertEqual(self.last_stderr, "README.md:?: A: prose error\n")
        self.assertEqual(
            self.last_stdout, "Prose: 1 errors, 0 warnings, 0 suggestions.\n"
        )
        self.assertEqual(self.invoke({}, 1)[0], 1)
        self.assertEqual(
            self.last_stderr, "prose: Vale failed without an error alert.\n"
        )
        self.assertEqual(self.invoke({}, 2)[0], 1)
        self.assertEqual(self.last_stderr, "prose: Vale failed (2): \n")

    def test_check_uses_the_requested_base_and_returns_machine_output(self):
        for args, environment, expected in (
            ([], {}, "origin/main"),
            ([], {"GATE_BASE": "origin/stack"}, "origin/stack"),
            (["--base", "tag"], {"GATE_BASE": "origin/stack"}, "tag"),
        ):
            with patch.dict("os.environ", environment, clear=True):
                status, run = self.invoke({}, args=args)
            self.assertEqual(status, 0)
            self.last_growth.assert_called_once_with(self.last_root, {"A": 1}, expected)
            self.assertEqual(
                run.args[0],
                [
                    "vale",
                    "--no-global",
                    "--minAlertLevel=warning",
                    "--output=JSON",
                    "README.md",
                ],
            )
            self.assertEqual(
                run.kwargs,
                {
                    "cwd": self.last_root,
                    "capture_output": True,
                    "text": True,
                    "check": False,
                },
            )

    def test_help_describes_the_scan_modes(self):
        with (
            redirect_stdout(io.StringIO()) as stdout,
            self.assertRaises(SystemExit) as exit_status,
        ):
            P["main"](["--help"])
        self.assertEqual(exit_status.exception.code, 0)
        self.assertIn(
            "Run Vale over maintained Markdown and enforce per-rule warning ceilings.",
            stdout.getvalue(),
        )
        self.assertRegex(
            stdout.getvalue(),
            r"(?m)^\s+--lint\s+display every alert without warning ceilings$",
        )

    def test_lint_forwards_exit_and_requests_every_level(self):
        status, call = self.invoke({}, 1, ["--lint"])
        self.assertEqual(status, 1)
        self.assertIn("--minAlertLevel=suggestion", call.args[0])
        self.assertNotIn("--output=JSON", call.args[0])
        self.assertEqual(call.kwargs, {"cwd": self.last_root, "check": False})


class ValeIntegrationTests(unittest.TestCase):
    def test_real_vale_errors_and_per_rule_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".vale/styles/Fixture").mkdir(parents=True)
            (root / ".vale.ini").write_text(
                "StylesPath = .vale/styles\n[*.md]\nBasedOnStyles = Fixture\n"
            )
            (root / ".vale/styles/Fixture/Error.yml").write_text(
                "extends: existence\nmessage: Forbidden token\nlevel: error\ntokens:\n  - barred\n"
            )
            (root / ".vale/styles/Fixture/Warning.yml").write_text(
                "extends: existence\nmessage: Wordy token\nlevel: warning\ntokens:\n  - wordy\n"
            )
            (root / ".vale/styles/Fixture/Other.yml").write_text(
                "extends: existence\nmessage: Other token\nlevel: warning\ntokens:\n  - unclear\n"
            )
            (root / ".vale/warnings.json").write_text(
                '{"version":1,"warnings":{"Fixture.Warning":1}}'
            )
            real_run = subprocess.run

            def run_in_fixture(command, **kwargs):
                # A missing cwd must fail before Vale scans the repository instead of this fixture.
                self.assertEqual(kwargs.get("cwd"), root)
                return real_run(command, **kwargs)

            for content, expected in [
                ("Clear prose.", 0),
                ("A wordy sentence.", 0),
                ("A wordy wordy sentence.", 1),
                ("An unclear sentence.", 1),
                ("A barred token.", 1),
            ]:
                (root / "README.md").write_text(content)
                with self.subTest(content=content):
                    with patch.dict(
                        PROSE_GLOBALS, ROOT=root, baseline_growth=lambda *_: []
                    ):
                        with (
                            patch.object(subprocess, "run", side_effect=run_in_fixture),
                            redirect_stdout(io.StringIO()),
                            redirect_stderr(io.StringIO()),
                        ):
                            self.assertEqual(P["main"]([]), expected)

    def test_correction_trail_phrasing_is_an_error(self):
        # The repository's own rule, run by real Vale: platform docs give one current answer,
        # so phrasing that keeps a replaced answer beside it fails the gate.
        rule = (
            Path(__file__).resolve().parents[1]
            / ".vale/styles/Microvms/CorrectionTrail.yml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".vale/styles/Microvms").mkdir(parents=True)
            (root / ".vale.ini").write_text(
                "StylesPath = .vale/styles\n[*.md]\nBasedOnStyles = Microvms\n"
            )
            (root / ".vale/styles/Microvms/CorrectionTrail.yml").write_text(
                rule.read_text()
            )
            (root / ".vale/warnings.json").write_text('{"version":1,"warnings":{}}')
            for content, expected in [
                ("Measured 2026-08-15. The limit is 4096 bytes.", 0),
                ("Output retained in the hook log is capped.", 0),
                ("**Correction, 2026-09-16:** the limit is 4096 bytes.", 1),
                ("**Correction candidate, 2026-09-23:** it resumed.", 1),
                ("**Later evidence, 2026-09-12:** the set lacked it.", 1),
                ("This corrects the earlier 16 KB claim.", 1),
                ("These observations corrected the earlier claim.", 1),
                ("Measured again 2026-09-24 with a busy exec.", 1),
                ("Re-measured 2026-09-24 against a running VM.", 1),
                ("The old claim was disproved in September.", 1),
                ("It was not live-measured in this refresh.", 1),
                ("Earlier measurements are retained in Platform.", 1),
                ("Those observations are both retained in Platform.", 1),
                ("The older claim that it existed only in requests was wrong.", 1),
                ("This heading remains for existing links.", 1),
            ]:
                (root / "README.md").write_text(content + "\n")
                with self.subTest(content=content):
                    with patch.dict(
                        PROSE_GLOBALS, ROOT=root, baseline_growth=lambda *_: []
                    ):
                        with (
                            redirect_stdout(io.StringIO()),
                            redirect_stderr(io.StringIO()),
                        ):
                            self.assertEqual(P["main"]([]), expected)


if __name__ == "__main__":
    unittest.main()
