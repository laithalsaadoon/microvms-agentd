# SPDX-License-Identifier: Apache-2.0
"""The pull request body check fails a body missing a section, an empty section, an oversized
change with no `Size:` line, and an event it can't read, and passes what it should.

The section rules are called in process, through the script's own functions. The size and
event rules run the real script in a throwaway git repo whose `origin/main` is its first commit
and which holds this repo's pull request template.
"""

import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("check-pr-body.py")
REPO = SCRIPT.parent.parent
CONSTANTS = runpy.run_path(str(SCRIPT))
TEMPLATE = CONSTANTS["TEMPLATE"]
SOFT_CAP = CONSTANTS["SOFT_CAP"]
DEPENDABOT = CONSTANTS["DEPENDABOT"]
# Written out rather than read from the script's REQUIRED: these are the sections the rules in
# CONTRIBUTING.md name, and a script that stopped requiring one should fail here.
SECTIONS = ("What and why", "Evidence", "Guards", "Follow-ups")
# A product file, a test file beside the crates, and a generated surface, as the shipped set
# in tools/changelog.py has them.
PRODUCT = "crates/agentd/src/lib.rs"
NOT_PRODUCT = "crates/agentd/tests/big.rs"
SURFACE = "bindings/microvms-py/microvms.pyi"

GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
)


def body(*, without: str = "", extra: str = "") -> str:
    """A body with every section filled in, less the one named `without`."""
    parts = {
        "What and why": "Fixes the thing, measured on the fixture.",
        "Evidence": "- [x] `cargo test --all`",
        "Guards": "None: this adds no guard.",
        "Follow-ups": "None.",
    }
    text = "".join(
        f"## {title}\n\n{content}\n\n"
        for title, content in parts.items()
        if title != without
    )
    return text + extra


def clean_env() -> dict[str, str]:
    """`os.environ` without git's hook pointers or the runner's event.

    CI's `security` job runs these tests with `GITHUB_EVENT_PATH` naming the pull request's own
    event, which a case that means to pass no event would otherwise read.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in GIT_ENV_LEAKS and k != "GITHUB_EVENT_PATH"
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=clean_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class SectionTests(unittest.TestCase):
    """Every section the template has is required, with something in it."""

    problems = staticmethod(CONSTANTS["section_problems"])

    def test_a_filled_in_body_passes(self):
        self.assertEqual(self.problems(body()), [])

    def test_each_section_the_template_has_is_required(self):
        for title in SECTIONS:
            with self.subTest(title=title):
                problems = self.problems(body(without=title))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"no `## {title}` section", problems[0])

    def test_a_body_without_follow_ups_fails(self):
        problems = self.problems(body(without="Follow-ups"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no `## Follow-ups` section", problems[0])

    def test_an_empty_body_fails_on_every_section(self):
        problems = self.problems("")
        self.assertEqual(len(problems), len(SECTIONS), problems)

    def test_a_section_holding_only_the_template_comment_fails(self):
        text = body().replace("None.\n", "<!-- Each finding, or `None.` -->\n")
        problems = self.problems(text)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("`## Follow-ups` has nothing in it", problems[0])

    def test_a_comment_left_open_hides_the_rest(self):
        text = body().replace("## Guards", "<!-- unclosed\n\n## Guards")
        problems = self.problems(text)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("no `## Guards` section", problems[0])
        self.assertIn("no `## Follow-ups` section", problems[1])

    def test_a_heading_in_a_code_block_is_not_a_section(self):
        text = body(without="Follow-ups") + "```\n## Follow-ups\n\nNone.\n```\n"
        problems = self.problems(text)
        self.assertTrue(any("no `## Follow-ups`" in p for p in problems), problems)

    def test_a_code_block_is_content(self):
        text = body().replace("None: this adds no guard.", "```\nfired: x (1.0 s)\n```")
        self.assertEqual(self.problems(text), [])

    def test_a_deeper_heading_stays_in_its_section(self):
        text = body().replace("None.\n", "### Later\n\nOne thing.\n")
        self.assertEqual(self.problems(text), [])

    def test_a_level_one_heading_ends_a_section(self):
        text = body().replace(
            "## Follow-ups\n\nNone.\n", "## Follow-ups\n\n# Notes\n\nx\n"
        )
        problems = self.problems(text)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("`## Follow-ups` has nothing in it", problems[0])

    def test_titles_match_without_case_and_crlf_is_read(self):
        text = (
            body().replace("## Follow-ups", "##  follow-ups ##").replace("\n", "\r\n")
        )
        self.assertEqual(self.problems(text), [])


class Fixture(unittest.TestCase):
    """A throwaway repo with the template, one product file, and `origin/main` at its root."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="pr-body-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "fixture@localhost")
        git(self.repo, "config", "user.name", "fixture")
        (self.repo / TEMPLATE).parent.mkdir(parents=True)
        shutil.copy(REPO / TEMPLATE, self.repo / TEMPLATE)
        self.write(PRODUCT, 10)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    def write(self, path: str, lines: int, start: int = 0) -> None:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("".join(f"line {n}\n" for n in range(start, start + lines)))

    def run_script(
        self, *args: str, text: str | None = None, author: str | None = None
    ):
        argv = [sys.executable, str(SCRIPT), *args]
        if text is not None:
            path = self.repo.parent / f"{self.repo.name}-body.md"
            path.write_text(text)
            self.addCleanup(path.unlink, missing_ok=True)
            argv += ["--body", str(path)]
        if author is not None:
            argv += ["--author", author]
        return subprocess.run(
            argv, cwd=self.repo, env=clean_env(), capture_output=True, text=True
        )

    def event(self, payload: object) -> str:
        path = self.repo.parent / f"{self.repo.name}-event.json"
        path.write_text(json.dumps(payload))
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path)


class SizeTests(Fixture):
    """Past the soft cap, the body carries a `Size:` line; below it, nothing is asked."""

    def over(self) -> None:
        self.write(PRODUCT, SOFT_CAP + 1, start=100)

    def test_over_the_cap_without_a_size_line_fails(self):
        self.over()
        done = self.run_script(text=body())
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f"over the soft cap of {SOFT_CAP}", done.stdout)
        self.assertIn(PRODUCT, done.stdout)

    def test_over_the_cap_with_a_size_line_passes(self):
        self.over()
        done = self.run_script(text=body(extra="Size: one move, split by #1.\n"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("with its `Size:` line", done.stdout)

    def test_a_size_line_in_a_comment_or_a_code_block_does_not_count(self):
        self.over()
        for extra in (
            "<!--\nSize: hidden\n-->\n",
            "```\nSize: quoted\n```\n",
            "Size:\n",
        ):
            with self.subTest(extra=extra):
                done = self.run_script(text=body(extra=extra))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_at_the_cap_passes(self):
        # Replacing the base's lines removes them too, so write the cap's worth on top.
        self.write(PRODUCT, 10 + SOFT_CAP)
        done = self.run_script(text=body())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"{SOFT_CAP} changed lines of product code", done.stdout)

    def test_removed_lines_count(self):
        self.write(PRODUCT, SOFT_CAP + 10)
        git(self.repo, "commit", "-q", "-am", "grow")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.write(PRODUCT, 0)
        done = self.run_script(text=body())
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_committed_and_untracked_product_lines_count(self):
        self.write(PRODUCT, SOFT_CAP // 2, start=100)
        git(self.repo, "commit", "-q", "-am", "half")
        self.write("crates/agentd/src/new.rs", SOFT_CAP)
        done = self.run_script(text=body())
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("crates/agentd/src/new.rs", done.stdout)

    def test_code_outside_the_shipped_source_does_not_count(self):
        for path in (
            NOT_PRODUCT,
            SURFACE,
            "docs/big.md",
            "crates/agentd/src/x_fuzz.rs",
        ):
            self.write(path, SOFT_CAP * 3)
        done = self.run_script(text=body())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("0 changed lines of product code", done.stdout)

    def test_the_generated_surfaces_do_not_count(self):
        self.write(SURFACE, SOFT_CAP * 3)
        done = self.run_script(text=body())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_base_that_names_nothing_fails(self):
        done = self.run_script("--base", "origin/nowhere", text=body())
        self.assertEqual(done.returncode, 1)
        self.assertIn("--base origin/nowhere doesn't name a commit", done.stderr)


class EventTests(Fixture):
    """A pull request's event is its body; any other event, or none, holds the template."""

    def pull(self, text: object, login: str = "bonk-ai[bot]") -> dict:
        return {"pull_request": {"body": text, "user": {"login": login}}}

    def test_a_pull_request_events_body_is_checked(self):
        event = self.event(self.pull(body(without="Follow-ups")))
        done = self.run_script("--event", event)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("no `## Follow-ups` section", done.stdout)

    def test_a_pull_request_event_with_every_section_passes(self):
        done = self.run_script("--event", self.event(self.pull(body())))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("every section is there", done.stdout)

    def test_the_event_is_read_from_the_runners_variable(self):
        env = clean_env()
        env["GITHUB_EVENT_PATH"] = self.event(self.pull(""))
        done = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_a_null_or_empty_body_fails(self):
        for text in (None, "", "   \n"):
            with self.subTest(text=text):
                done = self.run_script("--event", self.event(self.pull(text)))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn("no `## What and why` section", done.stdout)

    def test_the_dependabot_skip_is_for_dependabot_only(self):
        done = self.run_script("--event", self.event(self.pull("", DEPENDABOT)))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for login in ("renovate[bot]", "bonk-ai[bot]", "dependabot"):
            with self.subTest(login=login):
                done = self.run_script("--event", self.event(self.pull("", login)))
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_a_push_event_holds_the_template(self):
        event = self.event({"ref": "refs/heads/main", "commits": []})
        done = self.run_script("--event", event)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("the event isn't a pull request's", done.stdout)
        template = self.repo / TEMPLATE
        template.write_text(template.read_text().replace("## Follow-ups\n", ""))
        done = self.run_script("--event", event)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("has no `## Follow-ups`", done.stdout)

    def test_no_event_holds_the_template(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("no pull request event", done.stdout)

    def test_an_event_file_that_cant_be_read_fails(self):
        broken = self.repo.parent / f"{self.repo.name}-broken.json"
        broken.write_text("{not json")
        self.addCleanup(broken.unlink, missing_ok=True)
        cases = {
            str(self.repo / "absent.json"): "doesn't exist",
            str(broken): "isn't JSON",
            self.event(["a", "list"]): "isn't a JSON object",
        }
        for path, message in cases.items():
            with self.subTest(path=path):
                done = self.run_script("--event", path)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn(message, done.stderr)


class DocsTests(unittest.TestCase):
    """The template and CONTRIBUTING.md state the rules the script holds."""

    def test_the_repo_template_has_every_section(self):
        text = (REPO / TEMPLATE).read_text(encoding="utf-8")
        found = CONSTANTS["sections"](CONSTANTS["prose_lines"](text))
        for title in SECTIONS:
            self.assertIn(title.casefold(), found)

    def test_the_docs_state_the_cap_the_script_holds(self):
        for doc in (TEMPLATE, "CONTRIBUTING.md"):
            with self.subTest(doc=doc):
                text = " ".join((REPO / doc).read_text(encoding="utf-8").split())
                phrase = f"about {SOFT_CAP} changed lines"
                self.assertTrue(phrase in text, f"{doc} doesn't say {phrase!r}")


if __name__ == "__main__":
    unittest.main()
