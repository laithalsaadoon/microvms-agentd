# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-publishable.py`'s tag comparison, the gate `release:tag` and
release.yml's `guard` job run with `--tag` before a release exists.

`tag_version_skew` reads the crate versions it's handed and the two binding manifests in the
working directory, so the cases run from the repository root and hand it a workspace whose
versions they set. The last case reads this tree's own metadata, the way the gate does: every
manifest the tag compares carries the release number the Python manifest does, which v1.0.0's
daemon at `0.1.0` broke (#592). The daemon left out of the comparison is a seeded fault in
`verify/guards/faults/script-gates.toml`.
"""

import contextlib
import io
import runpy
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools/check-publishable.py"
PUBLISHABLE = runpy.run_path(str(SCRIPT), run_name="tools.check-publishable")
PUBLISHED = PUBLISHABLE["PUBLISHED"]
tag_version_skew = PUBLISHABLE["tag_version_skew"]


class TagVersionSkewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.chdir(ROOT))
        # The release number this tree carries, from the manifest the tag check reads it from.
        self.release = PUBLISHABLE["_toml_version"](
            "bindings/microvms-py/pyproject.toml"
        )
        self.assertRegex(self.release, r"^\d+\.\d+\.\d+")

    def workspace(self, versions=None):
        """Every crate the gate can be handed, at the release number unless `versions` says."""
        versions = versions or {}
        names = sorted(PUBLISHED | {"agentd", "agentd-model"})
        return [
            {
                "name": name,
                "version": versions.get(name, self.release),
                "manifest_path": f"{ROOT}/crates/{name}/Cargo.toml",
            }
            for name in names
        ]

    def test_a_workspace_at_the_tag_passes(self):
        self.assertEqual(tag_version_skew(f"v{self.release}", self.workspace()), [])

    def test_the_daemon_at_another_version_fails(self):
        failures = tag_version_skew(
            f"v{self.release}", self.workspace({"agentd": "0.1.0"})
        )
        self.assertEqual(
            failures,
            [
                f"agentd is version 0.1.0 and the tag says {self.release} "
                f"({ROOT}/crates/agentd/Cargo.toml)"
            ],
        )

    def test_the_proof_harness_is_free_to_differ(self):
        packages = self.workspace({"agentd-model": "0.1.0"})
        self.assertEqual(tag_version_skew(f"v{self.release}", packages), [])

    def test_each_published_crate_at_another_version_fails(self):
        for name in sorted(PUBLISHED):
            with self.subTest(crate=name):
                packages = self.workspace({name: "0.0.1"})
                failures = tag_version_skew(f"v{self.release}", packages)
                self.assertEqual(len(failures), 1, failures)
                self.assertTrue(
                    failures[0].startswith(
                        f"{name} is version 0.0.1 and the tag says {self.release} "
                    ),
                    failures,
                )

    def test_the_binding_manifests_are_compared_with_the_tag(self):
        other = "99.0.0"
        packages = [{**p, "version": other} for p in self.workspace()]
        self.assertEqual(
            tag_version_skew(f"v{other}", packages),
            [
                f"bindings/microvms-py/pyproject.toml is version {self.release} and the tag "
                f"says {other}",
                f"bindings/microvms-js/package.json is version {self.release} and the tag "
                f"says {other}",
            ],
        )

    def test_this_tree_agrees_with_a_tag_of_its_own_release_number(self):
        packages = PUBLISHABLE["metadata"]()["packages"]
        names = {p["name"] for p in packages}
        self.assertLessEqual(PUBLISHED | {"agentd"}, names)
        self.assertEqual(tag_version_skew(f"v{self.release}", packages), [])

    def test_the_command_names_the_daemon_in_its_verdict(self):
        for tag, code, stream, fragment in (
            (
                f"v{self.release}",
                0,
                "out",
                "every published manifest and the daemon's agree with tag",
            ),
            ("v0.0.1", 1, "err", "agentd is version"),
        ):
            with (
                self.subTest(tag=tag),
                mock.patch("sys.argv", ["check-publishable.py", f"--tag={tag}"]),
                contextlib.redirect_stdout(io.StringIO()) as out,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(PUBLISHABLE["main"](), code)
                written = (out if stream == "out" else err).getvalue()
                self.assertIn(fragment, written)


if __name__ == "__main__":
    unittest.main()
