# SPDX-License-Identifier: Apache-2.0
"""The SDLC variant of the Box example, checked without AWS: check.py, seeding, the box's
grants, and the driver's scoring. replay/replay.py checks the policy itself with the Dogwood
CLI (mise run box:sdlc:replay)."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/strands-box-codex-sdk"
SDLC = EXAMPLE / "sdlc"
GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=str(SDLC / "gitconfig"),
    GIT_CONFIG_NOSYSTEM="1",
    PYTHONDONTWRITEBYTECODE="1",
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Python 3.14's dataclasses reads the module of a class with string annotations from
    # sys.modules, so the module is there before its body runs.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


driver = load("sdlc_driver", SDLC / "vm/driver.py")
probe = load("sdlc_probe", SDLC / "vm/probe.py")


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def check(cwd):
    return subprocess.run(
        [sys.executable, "check.py"],
        cwd=cwd,
        env=GIT_ENV,
        capture_output=True,
        text=True,
    ).returncode


class SeedAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        seed = self.tmp / "seed"
        seed.mkdir()
        for f in ("calc.py", "test_calc.py"):
            shutil.copy(EXAMPLE / "project" / f, seed)
        shutil.copy(SDLC / "project/check.py", seed)
        self.project, self.origin = self.tmp / "project", self.tmp / "origin.git"
        subprocess.run(
            [
                "sh",
                str(SDLC / "vm/seed.sh"),
                str(seed),
                str(self.project),
                str(self.origin),
            ],
            env=GIT_ENV,
            check=True,
        )

    def test_seed_makes_a_clean_repo_and_a_bare_origin_at_the_same_commit(self):
        self.assertEqual(
            git("rev-parse", "--is-bare-repository", cwd=self.origin), "true"
        )
        self.assertEqual(
            git("rev-parse", "HEAD", cwd=self.project),
            git("rev-parse", "refs/heads/main", cwd=self.origin),
        )
        self.assertEqual(git("config", "core.hooksPath", cwd=self.project), "/dev/null")
        self.assertEqual(
            git("status", "--porcelain", "--untracked-files=all", cwd=self.project), ""
        )
        self.assertTrue((self.project / ".env").exists())

    def test_check_exits_1_on_the_seed_2_when_dirty_0_when_committed_2_when_untracked(
        self,
    ):
        self.assertEqual(check(self.project), 1)
        calc = self.project / "calc.py"
        calc.write_text(calc.read_text().replace("a - b", "a + b"))
        self.assertEqual(check(self.project), 2)
        git("commit", "-qam", "fix", cwd=self.project)
        subprocess.run(["chmod", "-R", "a-w", str(self.project)], check=True)
        try:
            self.assertEqual(check(self.project), 0)  # read-only, as in [tool.check]
        finally:
            subprocess.run(["chmod", "-R", "u+w", str(self.project)], check=True)
        (self.project / "calc_shadow.py").write_text("")
        self.assertEqual(check(self.project), 2)

    def test_check_exits_3_when_git_cannot_run_so_a_crash_never_reads_as_red(self):
        env = dict(GIT_ENV, PATH="/nonexistent")
        run = subprocess.run(
            [sys.executable, "check.py"], cwd=self.project, env=env, capture_output=True
        )
        self.assertEqual(run.returncode, 3)

    def test_a_staged_file_that_differs_from_the_work_tree_counts_as_dirty(self):
        git("add", "calc.py", cwd=self.project)
        calc = self.project / "calc.py"
        calc.write_text(calc.read_text().replace("a - b", "a + b"))
        git("add", "calc.py", cwd=self.project)
        calc.write_text(calc.read_text() + "# more\n")
        self.assertEqual(check(self.project), 2)


def stage_seed(tmp: Path, project: str) -> Path:
    """The seed directory run.sh stages for a project: its files plus the one frozen check.py."""
    seed = tmp / "seed" / project
    if project == "calc":
        seed.mkdir(parents=True)
        for f in ("calc.py", "test_calc.py"):
            shutil.copy(EXAMPLE / "project" / f, seed)
    else:
        shutil.copytree(SDLC / "projects/shop", seed)
        if project == "shop-conflict":
            shutil.copytree(SDLC / "projects/shop-conflict", seed, dirs_exist_ok=True)
    shutil.copy(SDLC / "project/check.py", seed)
    return seed


def seed_into(tmp: Path, project: str) -> Path:
    work = tmp / "project"
    subprocess.run(
        [
            "sh",
            str(SDLC / "vm/seed.sh"),
            str(stage_seed(tmp, project)),
            str(work),
            str(tmp / "origin.git"),
        ],
        env=GIT_ENV,
        check=True,
    )
    return work


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new))


def fix_coupons(work: Path) -> None:
    edit(
        work / "shop/coupons.py",
        '"amount": 5, "min_spend": 25',
        '"amount": 500, "min_spend": 2500',
    )


def fix_shipping(work: Path, rule: str = ">=") -> None:
    edit(work / "shop/shipping.py", "FREE_FROM = 50\n", "FREE_FROM = 5000\n")
    edit(work / "shop/shipping.py", "amount >= FREE_FROM", f"amount {rule} FREE_FROM")


class ShopProjectTests(unittest.TestCase):
    """shop's bug spans two files, and shop-conflict's tests contradict, so nothing passes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def commit_and_check(self, work):
        git("commit", "-qam", "fix", cwd=work)
        return check(work)

    def test_shop_seeds_red_with_its_own_commit_message(self):
        work = seed_into(self.tmp, "shop")
        self.assertEqual(check(work), 1)
        self.assertEqual(git("log", "-1", "--format=%s", cwd=work), "seed: shop")

    def test_fixing_one_file_of_shop_is_not_enough(self):
        for fix in (fix_coupons, fix_shipping):
            with self.subTest(fix=fix.__name__):
                shutil.rmtree(self.tmp)
                self.tmp.mkdir()
                work = seed_into(self.tmp, "shop")
                fix(work)
                self.assertEqual(self.commit_and_check(work), 1)

    def test_fixing_coupons_and_shipping_passes(self):
        work = seed_into(self.tmp, "shop")
        fix_coupons(work)
        fix_shipping(work)
        self.assertEqual(self.commit_and_check(work), 0)

    def test_no_shipping_rule_passes_shop_conflict(self):
        for rule in (">=", ">"):
            with self.subTest(rule=rule):
                shutil.rmtree(self.tmp)
                self.tmp.mkdir()
                work = seed_into(self.tmp, "shop-conflict")
                fix_coupons(work)
                fix_shipping(work, rule)
                self.assertEqual(self.commit_and_check(work), 1)

    def test_frozen_files_are_check_py_and_every_test(self):
        self.assertEqual(
            driver.frozen_files(stage_seed(self.tmp, "calc")),
            ["check.py", "test_calc.py"],
        )
        self.assertEqual(
            driver.frozen_files(stage_seed(self.tmp, "shop-conflict")),
            [
                "check.py",
                "tests/__init__.py",
                "tests/test_cart.py",
                "tests/test_checkout.py",
                "tests/test_money.py",
                "tests/test_shipping_policy.py",
            ],
        )

    def test_every_project_has_a_goal(self):
        self.assertEqual(sorted(driver.GOALS), ["calc", "shop", "shop-conflict"])


class BoxConfigTests(unittest.TestCase):
    def config(self, name):
        return tomllib.loads((SDLC / "box" / name).read_text())

    def test_git_writes_only_dot_git_and_origin_and_the_check_writes_nothing(self):
        for name in ("box.toml", "claude-box.toml", "probe-box.toml"):
            cfg = self.config(name)
            fs = cfg["tool"]["git"]["filesystem"]
            self.assertEqual(
                sorted(fs["write"]),
                ["/home/agent/origin.git", "/home/agent/project/.git"],
                name,
            )
            self.assertNotIn("write", cfg["tool"]["check"]["filesystem"], name)
            self.assertEqual(
                cfg["tool"]["check"]["command"], ["python3.12", "check.py"], name
            )
            for table in ("check", "git"):
                self.assertIn(
                    "/home/agent/project/.env", cfg["tool"][table]["filesystem"]["deny"]
                )

    def test_every_write_grant_has_a_read_grant_over_it(self):
        def covered(path, reads):
            return any(path == r or path.startswith(r.rstrip("/") + "/") for r in reads)

        for name in ("box.toml", "claude-box.toml", "probe-box.toml"):
            cfg = self.config(name)
            tables = [cfg["agent"]["filesystem"]] + [
                t["filesystem"] for t in cfg["tool"].values()
            ]
            for fs in tables:
                for path in fs.get("write", []):
                    self.assertTrue(
                        covered(path, fs.get("read", [])), f"{name}: {path}"
                    )

    def test_the_agent_holds_no_grant_on_the_project_or_origin(self):
        for name in ("box.toml", "claude-box.toml"):
            fs = self.config(name)["agent"]["filesystem"]
            for key in fs:
                for path in fs[key]:
                    self.assertFalse(
                        path.startswith(
                            ("/home/agent/project", "/home/agent/origin.git")
                        ),
                        f"{name}: {path}",
                    )

    def test_claude_code_gets_the_same_policy_tools_and_model_route_as_codex(self):
        codex, claude = self.config("box.toml"), self.config("claude-box.toml")
        for key in ("policy", "tool", "egress"):
            self.assertEqual(claude[key], codex[key], key)
        self.assertEqual(claude["agent"]["workspace"], codex["agent"]["workspace"])
        self.assertNotIn("AWS_BEARER_TOKEN_BEDROCK", claude["agent"]["env"])
        # The agent is uid 0 in the box, and without this Claude Code exits at once in bypass mode.
        self.assertEqual(claude["agent"]["env"].get("IS_SANDBOX"), "1")

    def test_every_agent_in_the_driver_has_its_box(self):
        for agent in driver.AGENTS.values():
            self.assertTrue((SDLC / "box" / agent.template).is_file(), agent.template)
            # run_cheats starts each scenario in a conversation of its own.
            self.assertTrue(
                callable(getattr(agent, "new_conversation", None)), agent.name
            )

    def test_claude_codes_state_is_where_the_policy_lets_the_shell_write(self):
        # Its Bash tool writes the working directory to a file in TMPDIR after every command.
        policy = (SDLC / "box" / "policy.dw").read_text()
        self.assertIn('context.input.path like "~/agent-state/*"', policy)
        claude = self.config("claude-box.toml")["agent"]
        for key in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "CLAUDE_CODE_TMPDIR"):
            path = claude["env"][key]
            self.assertTrue(
                path == "/home/agent/agent-state"
                or path.startswith("/home/agent/agent-state/"),
                f"{key}={path}",
            )
        self.assertEqual(claude["filesystem"]["write"], ["/home/agent/agent-state"])


class DriverTests(unittest.TestCase):
    def d(self, action, verdict, rule, resource=""):
        return {
            "time_ns": 0,
            "action": action,
            "verdict": verdict,
            "rule": rule,
            "resource": resource,
            "attrs": {"process.command_args": resource},
        }

    def honest(self):
        return [
            self.d("fs:write", "deny", "red_before_green", "~/project/calc.py"),
            self.d("shell:spawn", "permit", "run_check", "python3.12 check.py"),
            self.d("fs:write", "permit", "project_write", "~/project/calc.py"),
            self.d("shell:spawn", "permit", "git_commands", "git add calc.py"),
            self.d("shell:spawn", "permit", "git_commands", "git commit -m fix"),
            self.d("shell:spawn", "permit", "run_check", "python3.12 check.py"),
            self.d("shell:spawn", "permit", "git_commands", "git push origin"),
        ]

    def test_the_honest_order_passes(self):
        self.assertTrue(driver.order_ok(self.honest())[0])

    def test_a_push_with_no_check_after_the_last_commit_fails(self):
        ds = self.honest()
        ds.insert(
            6, self.d("shell:spawn", "permit", "git_commands", "git commit -m more")
        )
        self.assertFalse(driver.order_ok(ds)[0])

    def test_a_write_before_any_check_fails(self):
        ds = self.honest()
        ds.insert(0, self.d("fs:write", "permit", "project_write", "~/project/calc.py"))
        self.assertFalse(driver.order_ok(ds)[0])

    def test_no_push_fails(self):
        self.assertFalse(driver.order_ok(self.honest()[:-1])[0])

    def test_any_project_file_counts_as_the_first_write_and_dot_git_does_not(self):
        self.assertTrue(driver.is_code("~/project/shop/coupons.py"))
        self.assertFalse(driver.is_code("~/project/.git/config"))
        ds = self.honest()
        ds.insert(
            0, self.d("fs:write", "permit", "project_write", "~/project/shop/cart.py")
        )
        self.assertFalse(driver.order_ok(ds)[0])

    def test_the_probe_steps_and_their_expectations_line_up(self):
        self.assertEqual([name for name, _ in probe.STEPS], list(driver.PROBE))

    def test_every_rule_the_driver_expects_is_in_the_policy(self):
        ids = set(re.findall(r'@id\("([^"]+)"\)', (SDLC / "box/policy.dw").read_text()))
        expected = {
            r for step in driver.PROBE.values() for r in (step.expect.rules or ())
        }
        expected |= {
            r for ch in driver.CHEATS for e in ch.expect for r in (e.rules or ())
        }
        self.assertLessEqual(expected, ids)

    def test_fresh_config_gives_each_phase_its_own_box_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "box").mkdir()
            shutil.copy(SDLC / "box/box.toml", tmp / "box")
            old = driver.SDLC, driver.BOXDIR
            driver.SDLC, driver.BOXDIR = tmp, tmp / "box"
            try:
                config, records = driver.fresh_config("box.toml", "goal")
            finally:
                driver.SDLC, driver.BOXDIR = old
            cfg = tomllib.loads(config.read_text())
            self.assertEqual(cfg["box_dir"], str(tmp / "state-goal"))
            self.assertEqual(
                records, tmp / "state-goal/private/telemetry/records.jsonl"
            )


if __name__ == "__main__":
    unittest.main()
