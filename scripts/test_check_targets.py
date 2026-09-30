# SPDX-License-Identifier: Apache-2.0
"""A build `check` runs with an environment of its own doesn't share `test`'s target.

`test` (`cargo test --all`) runs its test binaries and doc tests after cargo has released the
build directory's lock, and `rustdoc --test` loads each crate's dependencies from
`target/debug/deps` while it compiles that crate's doc tests. When a unit's build script declares
an environment variable as an input (`rerun-if-env-changed`), cargo keeps the variable in the
unit's fingerprint and out of its file name (cargo 1.98), so a build that sees another value
rebuilds the unit in place, under the name `test` compiled against. `check` runs its tasks in
parallel, and a build landing in that window failed `test`'s doc tests with `error[E0463]: can't
find crate for axum` (and `serde`, `tokio`, ...): the rustdoc JSON walk's `RUSTC_BOOTSTRAP`, an
input of proc-macro2's, thiserror's and anyhow's build scripts, until #361 gave the walk its own
target.

mise gives every task one environment, so a plain `cargo` command in a task's `run` builds with
`test`'s. These build with their own, and each names a target of its own:

- a `run` line that sets a variable for cargo (`RUSTC_BOOTSTRAP=1 cargo doc`), or runs a tool in
  `TOOLS`, names one with `--target-dir`, or its task sets `CARGO_TARGET_DIR` in its `env`, or
  `SHARED` says why the target it shares is safe;
- the rustdoc JSON walk (`scripts/rustdoc_walk.py`'s `build`), which sets `RUSTC_BOOTSTRAP` inside
  the script, builds into a directory under cargo's target rather than the target itself.

A build some other script runs inside itself isn't visible from `mise.toml`, and a scan of the
scripts' source can't tell a build from the cargo argv their fixtures and fakes hold, so a new one
is review's to catch. To measure one: `cargo test --all --no-run`, then the task, then that
command again, which must find nothing to rebuild (`-v` prints why a unit is dirty).
"""

import json
import os
import re
import stat
import sys
import tempfile
import textwrap
import tomllib
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rustdoc_walk  # noqa: E402

# The cargo subcommands that compile into the target. `publish` and `package` aren't here: their
# verify build goes to `target/package`, a directory cargo keeps for them.
BUILDS = ("bench", "build", "check", "clippy", "doc", "run", "rustc", "rustdoc", "test")

# The commands that run cargo with an environment of their own, and what each one changes.
TOOLS = {
    r"\bmaturin\b": "maturin sets PYO3_BUILD_EXTENSION_MODULE and PYO3_CONFIG_FILE",
    r"\bnapi\s+build\b": "napi sets NAPI_ variables, and npx changes PATH",
    r"\bgenerate-py-stubs\.py\b": (
        "the stub generator runs maturin, then cargo under uv's VIRTUAL_ENV and PATH, and"
        " builds `microvms-py`, a cdylib whose library file names carry no hash, with another"
        " feature set"
    ),
}

# The builds above that share `test`'s target, each with why that can't rebuild a unit `test`
# built. A reason is a measurement: the task, run after `cargo test --all --no-run`, left that
# command nothing to rebuild (2026-09-30, at be99c5d).
SHARED = {
    "dts:check": (
        "napi builds with `--target` set to the host, so microvms-js and everything it links build"
        " under target/<triple>, and only build scripts and proc macros share target/debug. The"
        " NAPI_ variables are read by microvms-js's own build script, which runs under the triple,"
        " and pyo3-ffi's, which reads PATH, isn't in microvms-js's closure."
    ),
}

CARGO = re.compile(
    r"(?P<env>(?:\b[A-Z_][A-Z0-9_]*=\S*\s+)*)\bcargo\s+(?:\+\S+\s+)?(?P<sub>[a-z][a-z-]*)"
)


@dataclass(frozen=True)
class Build:
    """One build a `run` line runs, and whether it names a target of its own."""

    task: str
    line: str
    # "cargo" for a plain cargo command, "env" when the line sets a variable, or a TOOLS key.
    through: str
    own_target: bool


def closure(tasks: dict, root: str = "check") -> list[str]:
    """`root` and every task it depends on, in the order they're found."""
    order, todo = [], [root]
    while todo:
        name = todo.pop(0)
        if name in order or name not in tasks:
            continue
        order.append(name)
        todo += [d.split()[0] for d in tasks[name].get("depends", []) if d.strip()]
    return order


def builds(name: str, task: dict) -> list[Build]:
    """The builds in a task's `run` lines: cargo commands, and the tools that run cargo."""
    env = task.get("env")
    task_own = isinstance(env, dict) and "CARGO_TARGET_DIR" in env
    run = task.get("run", [])
    found = []
    for command in [run] if isinstance(run, str) else run:
        for line in command.splitlines() if isinstance(command, str) else []:
            own = task_own or "--target-dir" in line
            for match in CARGO.finditer(line):
                if match["sub"] in BUILDS:
                    through = "env" if match["env"] else "cargo"
                    found.append(
                        Build(
                            name,
                            line.strip(),
                            through,
                            own or "CARGO_TARGET_DIR=" in match["env"],
                        )
                    )
            for tool in TOOLS:
                if re.search(tool, line):
                    found.append(Build(name, line.strip(), tool, own))
    return found


def scan(root: Path) -> tuple[list[str], list[Build]]:
    """`check`'s closure in `root`'s mise.toml, and every build its `run` lines run."""
    tasks = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8")).get(
        "tasks", {}
    )
    names = closure(tasks)
    return names, [b for name in names for b in builds(name, tasks[name])]


def unowned(found: list[Build]) -> dict[str, list[Build]]:
    """The builds that run with an environment of their own in the shared target."""
    out: dict[str, list[Build]] = {}
    for build in found:
        if build.through != "cargo" and not build.own_target:
            out.setdefault(build.task, []).append(build)
    return out


class CheckTargets(unittest.TestCase):
    """This tree's `check`."""

    @classmethod
    def setUpClass(cls):
        cls.names, cls.builds = scan(ROOT)

    def test_check_runs_a_build(self):
        """The floor: a scan that finds nothing would pass over anything."""
        self.assertTrue(
            self.builds,
            f"found no build in `check`'s tasks ({', '.join(self.names) or 'none'}): the scan"
            " reads nothing, or `check` builds nothing",
        )

    def test_the_scan_sees_each_kind_of_build(self):
        """The sentinels: a build of each kind this tree has, in the task that runs it today."""
        seen = {(b.task, b.through, b.own_target) for b in self.builds}
        for task, through, own, what in (
            ("test", "cargo", False, "`cargo test --all`"),
            ("dts:check", r"\bnapi\s+build\b", False, "napi's build"),
            ("stubs:check", r"\bgenerate-py-stubs\.py\b", True, "the stub generator"),
        ):
            self.assertIn(
                (task, through, own),
                seen,
                f"the scan no longer sees {what} in `{task}`"
                f"{', with its own target,' if own else ''} as it runs today",
            )

    def test_a_build_with_its_own_environment_names_its_own_target(self):
        """A `run` line that builds with an environment of its own names its own target."""
        bad = {t: bs for t, bs in unowned(self.builds).items() if t not in SHARED}
        self.assertFalse(
            bad,
            "these `check` tasks build in the shared target with an environment of their own,"
            " which can rebuild a unit in place while `test`'s doc tests read it (E0463). Set"
            " `CARGO_TARGET_DIR` in the task's `env` or pass `--target-dir`, or say in SHARED"
            " why sharing is safe:\n"
            + "\n".join(
                f"  {t}: {b.line} ({TOOLS.get(b.through, 'sets a variable for cargo')})"
                for t, bs in bad.items()
                for b in bs
            ),
        )

    def test_each_shared_entry_is_a_build_that_needs_one(self):
        """A SHARED entry for a task that no longer shares is a reason nobody reads."""
        stale = sorted(set(SHARED) - set(unowned(self.builds)))
        self.assertFalse(
            stale,
            f"SHARED names {stale}, which no longer run a build with an environment of their"
            " own in the shared target (or aren't in `check`); delete the entries",
        )

    def test_the_rustdoc_walk_builds_outside_cargos_target(self):
        """The walk sets RUSTC_BOOTSTRAP, so its default target is a directory of its own."""
        with tempfile.TemporaryDirectory() as scratch:
            scratch = Path(scratch)
            target = scratch / "target"
            log = scratch / "argv.jsonl"
            fake = scratch / "bin" / "cargo"
            fake.parent.mkdir()
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json, sys
                    with open({str(log)!r}, "a") as f:
                        f.write(json.dumps(sys.argv[1:]) + "\\n")
                    if sys.argv[1:2] == ["metadata"]:
                        print(json.dumps({{"target_directory": {str(target)!r}}}))
                    """
                )
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            path = f"{fake.parent}{os.pathsep}{os.environ['PATH']}"
            with mock.patch.dict(os.environ, {"PATH": path}):
                os.environ.pop("CARGO_TARGET_DIR", None)
                rustdoc_walk.build(scratch, ["some-crate"])
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        docs = [argv for argv in calls if argv[:1] == ["doc"]]
        self.assertEqual(len(docs), 1, f"expected one `cargo doc`, got {calls}")
        argv = docs[0]
        self.assertIn("--target-dir", argv, f"`cargo doc` names no target: {argv}")
        chosen = Path(argv[argv.index("--target-dir") + 1])
        self.assertNotEqual(
            chosen.resolve(),
            target.resolve(),
            "the rustdoc walk builds in cargo's own target, where its RUSTC_BOOTSTRAP reruns"
            " proc-macro2's build script and rebuilds what `test` reads",
        )


class RunLineRules(unittest.TestCase):
    """The scan's rules, over throwaway `mise.toml` files."""

    def unowned_tasks(self, mise: str) -> list[str]:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "mise.toml").write_text(textwrap.dedent(mise))
            return sorted(unowned(scan(Path(root))[1]))

    def test_an_empty_check_has_no_build(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "mise.toml").write_text("[tasks.check]\ndepends = []\n")
            self.assertEqual(scan(Path(root)), (["check"], []))

    def test_a_plain_cargo_line_shares_and_one_that_sets_a_variable_does_not(self):
        mise = """
            [tasks.check]
            depends = ["a", "b", "c", "d"]
            [tasks.a]
            run = ["cargo fmt --all -- --check", "cargo test --all"]
            [tasks.b]
            run = "RUSTC_BOOTSTRAP=1 cargo doc --no-deps"
            [tasks.c]
            run = "RUSTC_BOOTSTRAP=1 cargo doc --no-deps --target-dir target/own"
            [tasks.d]
            run = '''
            set -eu
            RUSTFLAGS=-Cinstrument-coverage cargo test --all
            '''
            """
        self.assertEqual(self.unowned_tasks(mise), ["b", "d"])

    def test_a_tool_needs_its_task_or_its_line_to_name_a_target(self):
        mise = """
            [tasks.check]
            depends = ["a", "b", "c", "d"]
            [tasks.a]
            run = "npx -y -p @napi-rs/cli@3 napi build --platform"
            [tasks.b]
            env = { CARGO_TARGET_DIR = "{{config_root}}/target/b" }
            run = "uvx maturin@1.14.1 build"
            [tasks.c]
            run = "./scripts/generate-py-stubs.py --check"
            [tasks.d]
            run = "uvx maturin@1.14.1 develop --target-dir target/d"
            """
        self.assertEqual(self.unowned_tasks(mise), ["a", "c"])

    def test_a_task_outside_checks_closure_is_not_read(self):
        mise = """
            [tasks.check]
            depends = ["a"]
            [tasks.a]
            depends = ["b"]
            run = "cargo test"
            [tasks.b]
            run = "true"
            [tasks.elsewhere]
            run = "RUSTC_BOOTSTRAP=1 cargo doc"
            """
        self.assertEqual(self.unowned_tasks(mise), [])


if __name__ == "__main__":
    unittest.main()
