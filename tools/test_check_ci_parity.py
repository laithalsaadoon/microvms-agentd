# SPDX-License-Identifier: Apache-2.0
"""Tests for `check-ci-parity.py` (#315), and CI's `mise run` steps run through real mise.

`ParityTests` runs the checker over fixture trees: each case starts from one that passes and
changes one thing, so a failure names the rule that stopped holding. `edit` refuses an anchor
the fixture doesn't contain: a case whose edit matched nothing would pass while testing the
unchanged tree.

`CiCommands` is the half reading the files can't give. For each `mise run` step of ci.yml and
fuzz.yml, on each matrix leg, it runs the step as the workflow writes it, through the mise on
PATH, in a fixture whose tasks are this repository's with every command replaced by a stub
that records it ran and fails when told to. With no stub told to fail, the step passes and
every command its task reaches runs; with any one told to, the job fails. So a job that
swallows a failure (`|| true`, `continue-on-error`), skips a task (`--skip-deps`, a
`MISE_TASK_SKIP`), or waits for one nothing runs is caught by running it, not by a pattern.
"""

import contextlib
import io
import os
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PARITY = runpy.run_path(
    str(Path(__file__).with_name("check-ci-parity.py")),
    run_name="tools.check-ci-parity",
)
REPO = Path(__file__).resolve().parents[1]

MISE = """\
[tools]
rust = "stable"
uv = "0.12.13"
node = "22"

[vars]
tool = "1.0.0"

[tasks.lint]
run = ["cargo fmt --check", "uvx ruff@0.15.22 check ."]

[tasks.test]
run = "cargo test"

[tasks.gated]
tools = { "cargo:thing" = "{{vars.tool}}" }
run = "thing"

[tasks.after]
run = "true"

[tasks."ci:one"]
run = [{ task = "lint" }, { task = "test" }]

[tasks."ci:two"]
depends = ["gated"]
run = "true"

[tasks."ci:leg"]
run = "cargo test -p client"

[tasks.check]
depends = ["lint", "test", "gated"]
"""

CI = """\
name: ci
on:
  pull_request:
jobs:
  one:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: dtolnay/rust-toolchain@4360b52568e2003a75bf9bc1d59f33a8e3fc893c # stable
        with:
          toolchain: stable
      # mise run ci:nothing in a comment isn't a step
      - run: mise run ci:one
  matrix:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        include:
          - os: ubuntu-latest
            task: ci:two
          - os: macos-latest
            task: ci:leg
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
      - name: the leg's task
        run: mise run ${{ matrix.task }}
"""

FUZZ = """\
name: fuzz
on:
  pull_request:
jobs:
  fuzz:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@4360b52568e2003a75bf9bc1d59f33a8e3fc893c # stable
        with:
          toolchain: nightly
      - run: mise run ci:leg
"""

TOOLCHAIN = '[toolchain]\nchannel = "stable"\n'
REGISTRY = """\
[[fault]]
id = "py-entry"
run = [
  ["uvx", "maturin@1.14.1", "develop", "-q", "-m", "bindings/microvms-py/Cargo.toml"],
  ["python", "-m", "pytest", "-q"],
]
"""


def edit(text, old, new):
    """`text` with `old` replaced once; fails when `old` isn't there."""
    if text.count(old) != 1:
        raise AssertionError(f"fixture anchor not found exactly once: {old!r}")
    return text.replace(old, new)


class ParityTests(unittest.TestCase):
    def run_tree(
        self,
        mise=MISE,
        ci=CI,
        fuzz=FUZZ,
        toolchain=TOOLCHAIN,
        registry=REGISTRY,
        extra=None,
    ):
        """The exit code and output of the checker over one fixture tree."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        files = {
            "mise.toml": mise,
            ".github/workflows/ci.yml": ci,
            ".github/workflows/fuzz.yml": fuzz,
            "rust-toolchain.toml": toolchain,
            "verify/guards/faults/bindings.toml": registry,
            **(extra or {}),
        }
        for name, text in files.items():
            if text is None:
                continue
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        # The task loader asks git which files are tracked.
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = PARITY["main"](["--root", str(root)])
        return code, out.getvalue()

    def assertFails(self, needle, **files):
        code, out = self.run_tree(**files)
        self.assertEqual(code, 1, out)
        self.assertIn("ci parity: FAILED", out)
        self.assertIn(needle, out)
        return out

    def assertPasses(self, **files):
        code, out = self.run_tree(**files)
        self.assertEqual(code, 0, out)
        return out

    # ── reach: every task `check` depends on runs in CI ──────────────────────

    def test_a_tree_whose_jobs_reach_every_check_task_passes(self):
        out = self.assertPasses()
        self.assertIn("reach all 3 of `check`'s", out)

    def test_a_check_task_no_job_reaches_fails(self):
        mise = edit(
            MISE,
            'run = [{ task = "lint" }, { task = "test" }]',
            'run = [{ task = "lint" }]',
        )
        self.assertFails(
            "`check` depends on `test`, which no task a CI job runs reaches", mise=mise
        )

    def test_a_task_reached_only_on_a_matrix_include_leg_counts(self):
        # `gated` is reached only through `ci:two`, the include entry's task on the ubuntu leg.
        self.assertPasses()
        ci = edit(CI, "            task: ci:two\n", "            task: ci:leg\n")
        self.assertFails("`check` depends on `gated`", ci=ci)

    def test_an_include_with_only_new_keys_joins_every_leg(self):
        ci = edit(
            CI,
            "        include:\n          - os: ubuntu-latest\n            task: ci:two\n"
            "          - os: macos-latest\n            task: ci:leg\n",
            "        include:\n          - task: ci:two\n",
        )
        self.assertPasses(ci=ci)

    def test_an_excluded_leg_isnt_read(self):
        ci = edit(
            CI,
            "          - os: macos-latest\n            task: ci:leg\n",
            "          - os: macos-latest\n            task: ci:leg\n"
            "        exclude:\n          - os: ubuntu-latest\n",
        )
        self.assertFails("`check` depends on `gated`", ci=ci)

    def test_depends_post_and_a_run_list_of_tasks_reach(self):
        mise = edit(
            MISE,
            'run = [{ task = "lint" }, { task = "test" }]',
            'depends_post = ["lint"]\nrun = [{ tasks = ["test"] }]',
        )
        self.assertPasses(mise=mise)

    def test_a_dependency_with_arguments_reaches_its_task(self):
        mise = edit(MISE, 'depends = ["gated"]', 'depends = ["gated --quick"]')
        self.assertPasses(mise=mise)

    def test_wait_for_doesnt_reach(self):
        # `wait_for` waits for a task only when something else runs it.
        mise = edit(MISE, 'depends = ["gated"]', 'wait_for = ["gated"]')
        self.assertFails("`check` depends on `gated`", mise=mise)

    def test_a_task_in_an_included_file_is_read(self):
        mise = edit(MISE, '[tasks.test]\nrun = "cargo test"\n', "") + (
            '\n[task_config]\nincludes = ["tasks.toml"]\n'
        )
        self.assertPasses(
            mise=mise, extra={"tasks.toml": '[test]\nrun = "cargo test"\n'}
        )

    def test_a_step_naming_a_task_mise_lacks_fails(self):
        ci = edit(
            CI, "      - run: mise run ci:one\n", "      - run: mise run ci:gone\n"
        )
        self.assertFails(
            "runs `mise run ci:gone`, and mise.toml has no `ci:gone` task", ci=ci
        )

    def test_a_flag_before_the_task_fails(self):
        for flag in ("-c", "--skip-deps"):
            with self.subTest(flag=flag):
                ci = edit(CI, "mise run ci:one", f"mise run {flag} ci:one")
                self.assertFails(
                    f"passes `mise run` the flag `{flag}` before its task", ci=ci
                )

    def test_a_task_named_through_another_expression_fails(self):
        ci = edit(CI, "mise run ${{ matrix.task }}", "mise run ${{ inputs.task }}")
        self.assertFails("names its task as `${{ inputs.task }}`", ci=ci)

    def test_every_spelling_of_a_mise_run_step_is_read(self):
        for run in (
            "run: echo start && mise run ci:one",
            "run: |\n          echo start\n          mise   run ci:one --quick",
            "shell: bash\n        run: mise run ci:one",
        ):
            with self.subTest(run=run):
                ci = edit(CI, "run: mise run ci:one", run)
                self.assertPasses(ci=ci)

    def test_no_step_running_mise_fails(self):
        # The floor: the reader finding nothing looks like this.
        ci = edit(CI, "      - run: mise run ci:one\n", "      - run: cargo test\n")
        ci = edit(
            ci,
            "        run: mise run ${{ matrix.task }}\n",
            "        run: cargo test\n",
        )
        fuzz = edit(FUZZ, "mise run ci:leg", "cargo test")
        self.assertFails("runs `mise run`, so CI runs no task", ci=ci, fuzz=fuzz)

    def test_a_check_with_no_depends_fails(self):
        mise = edit(
            MISE,
            '[tasks.check]\ndepends = ["lint", "test", "gated"]\n',
            "[tasks.check]\n",
        )
        self.assertFails("`check` depends on no task", mise=mise)

    # ── the Rust channel ─────────────────────────────────────────────────────

    def test_a_toolchain_input_off_the_channel_fails(self):
        ci = edit(CI, "          toolchain: stable\n", "          toolchain: beta\n")
        self.assertFails(
            "rust: .github/workflows/ci.yml job `one` installs 'beta'", ci=ci
        )

    def test_mises_rust_off_the_channel_fails(self):
        mise = edit(MISE, 'rust = "stable"', 'rust = "1.97"')
        self.assertFails(
            "rust: mise.toml pins rust = '1.97', and rust-toolchain.toml says stable",
            mise=mise,
        )

    def test_the_toolchain_file_off_the_channel_fails(self):
        self.assertFails(
            "rust: mise.toml pins rust = 'stable', and rust-toolchain.toml says beta",
            toolchain='[toolchain]\nchannel = "beta"\n',
        )

    def test_a_toolchain_file_with_no_channel_fails(self):
        self.assertFails(
            "rust-toolchain.toml has no [toolchain] channel", toolchain="[toolchain]\n"
        )

    def test_ci_with_no_toolchain_step_fails(self):
        ci = re.sub(
            r"      - uses: dtolnay.*\n        with:\n          toolchain: stable\n",
            "",
            CI,
        )
        self.assertFails(
            "rust: .github/workflows/ci.yml has no dtolnay/rust-toolchain step", ci=ci
        )

    def test_fuzz_runs_nightly_on_purpose(self):
        # FUZZ's toolchain is nightly, and the tree passes.
        self.assertIn("toolchain: nightly", FUZZ)
        self.assertPasses()

    # ── pins ─────────────────────────────────────────────────────────────────

    def test_a_latest_tool_fails(self):
        mise = edit(MISE, 'uv = "0.12.13"', 'uv = "latest"')
        self.assertFails("mise.toml [tools] pins `uv` to latest", mise=mise)

    def test_a_latest_task_tool_fails(self):
        for pin in ('"latest"', '{ version = "latest" }'):
            with self.subTest(pin=pin):
                mise = edit(
                    MISE, '"cargo:thing" = "{{vars.tool}}"', f'"cargo:thing" = {pin}'
                )
                self.assertFails(
                    "mise.toml [tasks.gated] tools pins `cargo:thing` to latest",
                    mise=mise,
                )

    def test_a_task_tool_read_through_vars_is_held_too(self):
        mise = edit(MISE, 'tool = "1.0.0"', 'tool = "latest"')
        self.assertFails(
            "mise.toml [tasks.gated] tools pins `cargo:thing` to latest", mise=mise
        )
        mise = edit(MISE, 'tool = "1.0.0"', 'other = "1.0.0"')
        self.assertFails("a variable [vars] doesn't set", mise=mise)

    def with_call(self, call):
        """MISE with `test`'s command running `call`."""
        return {"mise": edit(MISE, 'run = "cargo test"', f"run = {call!r}")}

    def test_every_uvx_spelling_is_read(self):
        for call in (
            "uvx -q ruff@0.1.0 format --check .",
            "uvx --python 3.12 ruff==0.1.0 format --check .",
            "uvx --from ruff==0.1.0 ruff format --check .",
            "uvx --from=ruff@0.1.0 ruff format --check .",
            "uvx --from 'ruff[extra]==0.1.0' ruff format --check .",
            "uv tool run ruff@0.1.0 format --check .",
            "uv  tool  run --quiet ruff@0.1.0 format --check .",
            "echo x && uvx ruff@0.1.0 format --check .",
        ):
            with self.subTest(call=call):
                self.assertPasses(**self.with_call(call))

    def test_an_unpinned_uvx_call_fails(self):
        for call, shown in (
            ("uvx ruff check .", "uvx ruff check ."),
            ("uvx ruff@0.15 check .", "uvx ruff@0.15 check ."),
            ("uvx --from ruff ruff check .", "uvx --from ruff ruff check ."),
            (
                "uvx --from 'ruff>=0.1' ruff check .",
                "uvx --from 'ruff>=0.1' ruff check .",
            ),
            ("uv tool run ruff check .", "uv tool run ruff check ."),
        ):
            with self.subTest(call=call):
                self.assertFails(
                    f"mise.toml [tasks.test] runs `{shown}`, which names no exact version",
                    **self.with_call(call),
                )

    def test_a_uvx_call_that_cannot_be_read_fails(self):
        for call, why in (
            ("uvx 'ruff check .", "`uvx 'ruff check .`"),
            ("uvx --from", "`--from` has no value"),
            ("uvx -- ./x.py", "isn't a package name"),
        ):
            with self.subTest(call=call):
                self.assertFails(why, **self.with_call(call))

    def test_an_unpinned_uvx_in_a_workflow_step_fails(self):
        ci = edit(
            CI,
            "      - run: mise run ci:one\n",
            "      - run: mise run ci:one\n      - run: uvx ty check\n",
        )
        self.assertFails(
            "ci.yml job `one` step `uvx ty check` runs `uvx ty check`", ci=ci
        )

    def test_an_unpinned_uvx_in_a_registry_entry_fails(self):
        registry = edit(REGISTRY, '"maturin@1.14.1"', '"maturin"')
        self.assertFails(
            "verify/guards/faults/bindings.toml entry `py-entry` runs `uvx maturin develop",
            registry=registry,
        )

    def test_every_registry_file_is_read(self):
        other = edit(REGISTRY, '"py-entry"', '"js-entry"').replace(
            "maturin@1.14.1", "maturin"
        )
        self.assertFails(
            "verify/guards/faults/other.toml entry `js-entry`",
            extra={"verify/guards/faults/other.toml": other},
        )

    def test_the_former_single_registry_file_is_unreadable(self):
        self.assertFails(
            "verify/guards/faults.toml is the single file the registry was before it was split",
            extra={"verify/guards/faults.toml": REGISTRY},
        )

    # ── unreadable input ─────────────────────────────────────────────────────

    def test_a_missing_or_empty_file_fails(self):
        for files, needle in (
            ({"mise": None}, "mise.toml: can't read"),
            ({"mise": ""}, "mise.toml: "),
            ({"ci": None}, ".github/workflows/ci.yml: can't read"),
            ({"toolchain": "\n"}, "rust-toolchain.toml: "),
        ):
            with self.subTest(files=files):
                self.assertFails(needle, **files)

    def test_a_workflow_that_doesnt_parse_fails(self):
        self.assertFails("doesn't parse", ci="jobs: [\n")


# ── CI's `mise run` steps, through real mise over stubbed tasks ──────────────

# What a pull request against main gives the expressions a `mise run` step's `env` uses. One
# the table lacks fails the step's case by name rather than running with the text in.
EXPRESSIONS = {
    "github.base_ref": "main",
    "github.event.pull_request.user.login": "contributor",
    "github.event_name == 'pull_request' && format('origin/{0}', github.base_ref) || 'HEAD^'": "origin/main",
    "github.event_name == 'pull_request' && format('origin/{0}', github.base_ref) || ''": "origin/main",
    "matrix.shard": "0",
    "strategy.job-total": "1",
}
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def units(tasks: dict, name: str) -> dict[str, list]:
    """Each task's commands, by the label its stub records: `<task>#<n>` for the nth entry of a
    `run` list that isn't a task, `<task>` for a `run` that's one string."""
    run = tasks[name].get("run")
    if isinstance(run, str):
        return {name: []}
    return {
        f"{name}#{n}": [] for n, entry in enumerate(run or []) if isinstance(entry, str)
    }


def stub(label: str) -> str:
    """A command that records it ran and fails when `$FAIL` names it. It ends with `:`, which
    takes the arguments `mise run` puts after a task's last line."""
    return f"printf '%s\\n' '{label}' >> \"$RAN\"; if [ \"$FAIL\" = '{label}' ]; then exit 1; fi; :"


def stubbed_config(tasks: dict) -> str:
    """This repository's tasks with every command a stub, and nothing else: no tools to
    install, no sources to skip on, no directory or shell of their own."""
    lines = []
    for name, task in tasks.items():
        lines.append(f"[tasks.{toml_key(name)}]")
        for key in ("depends", "depends_post", "wait_for"):
            if key in task:
                lines.append(f"{key} = {toml_value(task[key])}")
        if isinstance(task.get("env"), dict):
            lines.append(f"env = {toml_value(task['env'])}")
        run = task.get("run")
        if isinstance(run, str):
            lines.append(f"run = {toml_value(stub(name))}")
        elif isinstance(run, list):
            entries = []
            for n, entry in enumerate(run):
                if isinstance(entry, str):
                    entries.append(toml_value(stub(f"{name}#{n}")))
                else:
                    entries.append(toml_value(entry))
            lines.append("run = [\n  " + ",\n  ".join(entries) + ",\n]")
        lines.append("")
    return "\n".join(lines)


def toml_key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else '"' + name + '"'


def toml_value(value) -> str:
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items())
            + " }"
        )
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def mise_env(fixture: Path, **extra: str) -> dict[str, str]:
    """The caller's environment without mise's own variables, and mise pointed at `fixture`
    alone: no parent directory's config (a home directory's `.config/mise/config.toml` among
    them), no global config, and the fixture trusted."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MISE_", "__MISE_")) and not k.startswith("GITHUB_")
    }
    env |= {
        "MISE_CEILING_PATHS": str(fixture.parent),
        "MISE_TRUSTED_CONFIG_PATHS": str(fixture),
        "MISE_GLOBAL_CONFIG_FILE": str(fixture / "no-global-config.toml"),
        "MISE_CONFIG_DIR": str(fixture / "no-config-dir"),
        "MISE_YES": "1",
    }
    return env | extra


def answer(text: str, where: str) -> str:
    def value(match: re.Match) -> str:
        expr = match.group(1)
        if expr not in EXPRESSIONS:
            raise AssertionError(
                f"{where} uses `${{{{ {expr} }}}}`, which EXPRESSIONS doesn't answer"
            )
        return EXPRESSIONS[expr]

    return EXPRESSION.sub(value, text)


def commands() -> list:
    problems: list[str] = []
    found = PARITY["ci_commands"](REPO, problems)
    if problems:
        raise AssertionError("; ".join(problems))
    return found


def case_name(command) -> str:
    leg = "_".join(v for v in command.leg.values())
    words = [command.job, leg, PARITY["step_label"](command.step)]
    return (
        "test_"
        + re.sub(r"\W+", "_", "_".join(w for w in words if w)).strip("_").lower()
    )


class CiCommands(unittest.TestCase):
    """Each workflow `mise run` step fails when any command its task reaches fails, and runs
    them all when none does. One case per step and matrix leg."""

    @classmethod
    def setUpClass(cls):
        cls.mise = shutil.which("mise")
        cls.tasks = PARITY["load_tasks"](REPO)
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.fixture = Path(tmp.name) / "tasks"
        cls.fixture.mkdir()
        (cls.fixture / "mise.toml").write_text(
            stubbed_config(cls.tasks), encoding="utf-8"
        )

    def reached_units(self, task: str) -> set[str]:
        return {
            label
            for name in PARITY["reached"](self.tasks, [task])
            for label in units(self.tasks, name)
        }

    def run_command(self, command, fail: str = "") -> tuple[bool, set[str], str]:
        """Whether the job fails, which stubs ran, and the output, with `fail` failing."""
        self.assertIsNotNone(self.mise, "mise isn't on PATH, and these cases run it")
        where = command.where()
        env = {}
        for scope in (
            PARITY["load_yaml"](REPO / command.workflow, command.workflow).get("env")
            or {},
            (
                PARITY["load_yaml"](REPO / command.workflow, command.workflow)["jobs"][
                    command.job
                ].get("env")
                or {}
            ),
            command.step.get("env") or {},
        ):
            env |= {
                str(k): answer(str(v), f"{where} env {k}") for k, v in scope.items()
            }
        ran = self.fixture / f"ran-{os.getpid()}-{id(command)}-{fail or 'none'}"
        ran.unlink(missing_ok=True)
        shell = (
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]
            if command.step.get("shell") == "bash"
            else ["bash", "-e", "-c"]
        )
        out = subprocess.run(
            [*shell, answer(command.run, where)],
            cwd=self.fixture,
            capture_output=True,
            text=True,
            env=mise_env(self.fixture, RAN=str(ran), FAIL=fail) | env,
        )
        job = PARITY["load_yaml"](REPO / command.workflow, command.workflow)["jobs"][
            command.job
        ]
        allowed = bool(command.step.get("continue-on-error")) or bool(
            job.get("continue-on-error")
        )
        stubs = set(ran.read_text().split()) if ran.exists() else set()
        ran.unlink(missing_ok=True)
        return out.returncode != 0 and not allowed, stubs, out.stdout + out.stderr

    def check_command(self, command):
        want = self.reached_units(command.task)
        self.assertTrue(want, f"{command.where()}: `{command.task}` reaches no command")
        failed, stubs, out = self.run_command(command)
        self.assertFalse(
            failed, f"{command.where()} fails with nothing failing:\n{out}"
        )
        self.assertEqual(
            stubs, want, f"{command.where()}: the commands that ran\n{out}"
        )
        for label in sorted(want):
            failed, stubs, out = self.run_command(command, fail=label)
            self.assertTrue(
                failed,
                f"{command.where()} passes when `{label}` fails, which its job runs:\n{out}",
            )

    def test_the_repository_has_steps_to_run(self):
        # The floor: every case below comes from this list.
        self.assertTrue(commands(), "ci.yml and fuzz.yml run no `mise run` step")

    def test_mise_puts_a_steps_arguments_after_the_tasks_last_line_and_its_env_wins(
        self,
    ):
        # What test_check_guards_fire.py's GuardsJob relies on when it runs the guards job's
        # steps through `ci:guards`'s command rather than through mise.
        self.assertIsNotNone(self.mise, "mise isn't on PATH, and these cases run it")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "args"
            fixture.mkdir()
            (fixture / "mise.toml").write_text(
                '[tasks.t]\nenv = { SEEN = "task" }\nrun = \'printf "%s|" "$SEEN"; printf "<%s>" \'\n'
            )
            out = subprocess.run(
                ["bash", "-e", "-c", 'mise run t --base "$BASE" "two words"'],
                cwd=fixture,
                capture_output=True,
                text=True,
                env=mise_env(fixture, SEEN="step", BASE="origin/x"),
            )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("task|<--base><origin/x><two words>", out.stdout)


def _add_cases() -> None:
    for command in commands():
        name = case_name(command)
        while hasattr(CiCommands, name):
            name += "_again"

        def case(self, command=command):
            self.check_command(command)

        case.__doc__ = f"{command.where()} runs `mise run {command.task}`{command.args}"
        setattr(CiCommands, name, case)


_add_cases()


if __name__ == "__main__":
    unittest.main()
