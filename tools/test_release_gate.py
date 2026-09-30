# SPDX-License-Identifier: Apache-2.0
"""The release gate's two rules: release.yml's job graph, and what counts as a live pass.

`graph` is exercised on a small workflow shaped like release.yml, one thing broken per case,
and once on the repository's own release.yml. `decide` is handed runs and markers the way the
Actions API and `gh run download` would answer, so every refusal has a case and the one pass
has its twin. The script's name has a dash, so it's loaded by path.
"""

import copy
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("release-gate.py")
ROOT = SCRIPT.parent.parent

_spec = importlib.util.spec_from_file_location("tools.release-gate", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

TAG = "v1.2.3"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SUMS = "aaaa  agentd\nbbbb  microvm-x86_64-unknown-linux-gnu.tar.gz\n"


def workflow() -> dict:
    """A job graph with release.yml's shape that the rules pass."""
    return {
        "jobs": {
            "draft": {
                "steps": [
                    {"run": "gh release create v1.2.3 --draft --title v1.2.3 agentd"},
                    {"uses": "actions/upload-artifact@abc"},
                ]
            },
            "live-gate": {
                "needs": ["draft"],
                "environment": "release",
                "steps": [{"run": "./tools/release-gate.py verify --tag v1.2.3"}],
            },
            "crates-io": {
                "needs": ["draft", "live-gate"],
                "environment": "release",
                "steps": [
                    {"uses": "rust-lang/crates-io-auth-action@abc"},
                    {"run": "cargo publish --workspace --locked"},
                ],
            },
            "pypi": {
                "needs": ["live-gate"],
                "environment": {"name": "release"},
                "steps": [{"uses": "pypa/gh-action-pypi-publish@abc"}],
            },
            "npm": {
                "needs": ["addons", "live-gate"],
                "environment": "release",
                "steps": [{"run": "npm publish --access public"}],
            },
            "github-release": {
                "needs": ["draft", "live-gate"],
                "environment": "release",
                "steps": [{"run": 'gh release edit "$TAG" --draft=false'}],
            },
        }
    }


def problems(doc: dict) -> list[str]:
    return gate.graph(doc)[0]


class GraphTests(unittest.TestCase):
    def assert_refused(self, doc: dict, *fragments: str) -> None:
        found = problems(doc)
        self.assertTrue(found, "the rules passed a graph they must refuse")
        text = "\n".join(found)
        for fragment in fragments:
            self.assertIn(fragment, text)

    def test_a_graph_where_every_publisher_waits_on_the_gate_passes(self):
        doc = workflow()
        found, publishing = gate.graph(doc)
        self.assertEqual(found, [])
        self.assertEqual(
            sorted(publishing), ["crates-io", "github-release", "npm", "pypi"]
        )

    def test_the_repository_release_workflow_passes(self):
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "graph", "--root", str(ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("each needs `live-gate`", done.stdout)

    def test_a_publishing_job_without_the_gate_in_its_needs_fails(self):
        doc = workflow()
        doc["jobs"]["npm"]["needs"] = "addons"
        self.assert_refused(doc, "job `npm` publishes (`npm publish`)", "`live-gate`")

    def test_the_gate_through_another_publisher_is_not_enough(self):
        doc = workflow()
        doc["jobs"]["npm"]["needs"] = ["addons", "github-release"]
        self.assert_refused(doc, "job `npm`", "doesn't name `live-gate`")

    def test_a_release_created_without_draft_publishes_and_fails(self):
        doc = workflow()
        doc["jobs"]["draft"]["steps"][0]["run"] = (
            "gh release create v1.2.3 --title v1.2.3 agentd"
        )
        self.assert_refused(
            doc, "job `draft` publishes (`gh release create without --draft`)"
        )

    def test_a_publishing_job_outside_the_release_environment_fails(self):
        for environment in (None, "live-aws", {"name": "staging"}):
            doc = workflow()
            if environment is None:
                del doc["jobs"]["github-release"]["environment"]
            else:
                doc["jobs"]["github-release"]["environment"] = environment
            with self.subTest(environment=environment):
                self.assert_refused(
                    doc, "job `github-release`", "outside the `release` environment"
                )

    def test_a_condition_that_runs_a_publisher_after_a_failure_fails(self):
        for condition in ("always()", "${{ !cancelled() }}", "failure() || success()"):
            doc = workflow()
            doc["jobs"]["crates-io"]["if"] = condition
            with self.subTest(condition=condition):
                self.assert_refused(doc, "job `crates-io`", f"`if: {condition}`")
        doc = workflow()
        doc["jobs"]["crates-io"]["if"] = "github.ref_type == 'tag'"
        self.assertEqual(problems(doc), [])

    def test_a_gate_that_verifies_nothing_or_is_missing_fails(self):
        doc = workflow()
        doc["jobs"]["live-gate"]["steps"] = [{"run": "echo approved"}]
        self.assert_refused(doc, "doesn't run `tools/release-gate.py verify`")
        doc = workflow()
        del doc["jobs"]["live-gate"]
        self.assert_refused(doc, "has no `live-gate` job")

    def test_continue_on_error_on_the_gate_fails(self):
        doc = workflow()
        doc["jobs"]["live-gate"]["continue-on-error"] = True
        self.assert_refused(doc, "sets `continue-on-error`")
        doc = workflow()
        doc["jobs"]["live-gate"]["steps"][0]["continue-on-error"] = (
            "${{ inputs.lenient }}"
        )
        self.assert_refused(doc, "sets `continue-on-error`")

    def test_a_workflow_with_no_jobs_fails(self):
        for doc in ({}, {"jobs": {}}, {"jobs": None}):
            with self.subTest(doc=doc):
                self.assert_refused(doc, "has no jobs")

    def test_a_release_the_detector_reads_no_cargo_publish_in_fails(self):
        doc = workflow()
        doc["jobs"]["crates-io"]["steps"] = [
            {"uses": "rust-lang/crates-io-auth-action@abc"}
        ]
        self.assert_refused(
            doc, "runs `cargo publish` (crates.io)", "detector stopped matching"
        )

    def test_dry_runs_downloads_and_drafts_publish_nothing(self):
        for run in (
            "cargo publish --workspace --dry-run",
            "npm publish --dry-run",
            "./tools/check-publishable.py --dry-run --tag=v1.2.3",
            'gh release download "$TAG" --dir draft',
            "gh release create v1.2.3 \\\n  --draft \\\n  agentd",
            "gh release create v1.2.3 -d agentd",
        ):
            with self.subTest(run=run):
                self.assertEqual(gate.publishes({"run": run}), [])
        self.assertEqual(
            gate.publishes(
                {"run": "gh api -X PATCH repos/o/r/releases/1 -F draft=false"}
            ),
            ["draft=false"],
        )
        self.assertEqual(
            gate.publishes({"uses": "pypa/gh-action-pypi-publish@abc"}),
            ["pypa/gh-action-pypi-publish"],
        )


def run(**overrides) -> dict:
    """A live-conformance run the gate accepts, with `overrides` applied."""
    base = {
        "id": 7,
        "html_url": "https://github.com/o/r/actions/runs/7",
        "path": ".github/workflows/live-conformance.yml",
        "event": "workflow_dispatch",
        "head_branch": TAG,
        "head_sha": COMMIT,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def marker(**overrides) -> dict:
    base = {"tag": TAG, "commit": COMMIT, "sha256sums": SUMS}
    base.update(overrides)
    return base


class DecideTests(unittest.TestCase):
    def decide(self, runs, markers=None):
        markers = markers if markers is not None else {7: marker()}
        return gate.decide(
            runs,
            lambda r: copy.deepcopy(markers.get(r["id"], "no marker")),
            TAG,
            COMMIT,
            SUMS,
        )

    def assert_refused(self, runs, fragment, markers=None):
        passed, lines = self.decide(runs, markers)
        self.assertFalse(passed, lines)
        self.assertIn(fragment, "\n".join(lines))

    def test_a_passing_run_on_the_draft_opens_the_gate(self):
        passed, lines = self.decide([run()])
        self.assertTrue(passed, lines)
        self.assertIn("passed on this draft", lines[-1])

    def test_a_run_on_another_commit_is_refused(self):
        self.assert_refused([run(head_sha="f" * 40)], "head_sha is 'ffff")

    def test_a_run_on_another_ref_is_refused(self):
        self.assert_refused([run(head_branch="main")], "head_branch is 'main'")

    def test_a_failed_or_unfinished_run_is_refused(self):
        self.assert_refused([run(conclusion="failure")], "conclusion is 'failure'")
        self.assert_refused(
            [run(status="in_progress", conclusion=None)], "status is 'in_progress'"
        )

    def test_a_run_of_another_workflow_or_event_is_refused(self):
        self.assert_refused(
            [run(path=".github/workflows/ci.yml")], "path is '.github/workflows/ci.yml'"
        )
        self.assert_refused([run(event="push")], "event is 'push'")

    def test_a_run_without_a_marker_is_refused(self):
        self.assert_refused([run()], "no marker", markers={})

    def test_a_marker_for_another_draft_is_refused(self):
        other = marker(sha256sums=SUMS.replace("aaaa", "cccc"))
        self.assert_refused([run()], "tested another draft", markers={7: other})

    def test_a_marker_for_another_tag_or_commit_is_refused(self):
        self.assert_refused(
            [run()], "tag is 'v1.2.2'", markers={7: marker(tag="v1.2.2")}
        )
        self.assert_refused(
            [run()], "commit is 'abc'", markers={7: marker(commit="abc")}
        )

    def test_one_passing_run_among_others_is_enough_and_the_others_are_explained(self):
        runs = [
            run(id=8, conclusion="failure", created_at="2026-10-02T00:00:00Z"),
            run(id=7),
        ]
        passed, lines = self.decide(runs)
        self.assertTrue(passed, lines)
        self.assertIn("run 8", lines[0])
        self.assertIn("conclusion is 'failure'", lines[0])

    def test_no_runs_keeps_the_gate_shut(self):
        passed, lines = self.decide([])
        self.assertFalse(passed)
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
