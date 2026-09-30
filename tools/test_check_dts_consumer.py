# SPDX-License-Identifier: Apache-2.0
"""Tests for `tools/check-dts-consumer.py`'s tsc locate: the npx lock and where tsc came from (#348).

The gate itself runs over the real declarations in `dts:check` and CI's bindings job, and its
floors are seeded faults. These cases cover the locate that installs TypeScript with npx: it
runs under the lock `tools/npx_cache.py` holds beside npm's cache, and a `tsc` that npx didn't
install is refused by name. A fake npx stands in, and the real npm answers
`npm config get cache` from `npm_config_cache`, so neither touches the caller's `~/.npm`. They
need npm on PATH, so run this under `mise x` or a mise task, on a POSIX host (the lock uses
fcntl).
"""

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("check-dts-consumer.py")
DTS = runpy.run_path(str(SCRIPT), run_name="tools.check-dts-consumer")
VERSIONS = {"typescript": "5.9.3", "@types/node": "26.4.0"}

# Stands in for npx. It prints FAKE_TSC only while someone else holds the lock beside the npm
# cache exclusively, so a locate that runs unlocked, or under a shared lock, fails. The probe is
# shared because an exclusive probe is refused by a held shared lock too.
FAKE_NPX = """#!{python}
import fcntl, os, sys
if sys.argv[-2:] != ["-c", "command -v tsc"]:
    print("npx called as", sys.argv[1:])
    sys.exit(2)
with open(os.environ["FAKE_NPX_LOCK"], "a") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        print(os.environ["FAKE_TSC"])
        sys.exit(0)
print("npx ran with the lock free")
sys.exit(3)
"""


# Stands in for tsc: it lists the files its tsconfig names, as `--listFiles` does, and reports
# one diagnostic.
FAKE_TSC = """#!{python}
import json, sys
options = json.load(open(sys.argv[2]))
for paths in options["compilerOptions"]["paths"].values():
    print(*paths, sep="\\n")
print(*options["files"], sep="\\n")
print("probe.ts(1,1): error TS2322: seeded")
sys.exit(2)
"""


class ProbeShapeTests(unittest.TestCase):
    """A problem with the probes' shape is reported without keeping them from compiling."""

    def directory(self, files):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        return root

    def test_the_strays_and_each_probes_problems_are_reported_and_the_probes_kept(self):
        root = self.directory(
            {"a.ts": "// @ts-nocheck\n// @ts-expect-error\n", "b.ts": "", "c.mts": ""}
        )
        found, problems = DTS["probes"](root)
        a, b, c = (str((root / name).resolve()) for name in ("a.ts", "b.ts", "c.mts"))
        self.assertEqual([str(path) for path in found], [a, b])
        self.assertEqual(
            problems,
            [
                f"{c} isn't compiled: a probe is a *.ts file directly under {root}",
                f"{a} turns type-checking off (@ts-nocheck); {b} has no @ts-expect-error control",
            ],
        )

    def test_a_directory_with_no_probe_is_refused(self):
        with self.assertRaisesRegex(DTS["ConsumerError"], "no type probes under"):
            DTS["probes"](self.directory({}))

    def test_tsc_still_checks_probes_whose_shape_is_refused(self):
        root = self.directory({"a.ts": "export {}\n"})
        tsc = self.directory({}) / "tsc"
        tsc.write_text(FAKE_TSC.format(python=sys.executable), encoding="utf-8")
        tsc.chmod(0o755)
        dts = tsc.parent / "index.d.ts"
        dts.write_text("export {}\n", encoding="utf-8")
        with mock.patch.dict(DTS["check"].__globals__, {"locate_tsc": lambda v: tsc}):
            with self.assertRaises(DTS["ConsumerError"]) as raised:
                DTS["check"](dts, root)
        lines = str(raised.exception).splitlines()
        self.assertEqual(
            lines[0], f"{(root / 'a.ts').resolve()} has no @ts-expect-error control"
        )
        self.assertIn("probe.ts(1,1): error TS2322: seeded", lines)
        self.assertTrue(lines[-1].startswith("tsc 5.9.3 exited 2"), lines)


class LocateTscTests(unittest.TestCase):
    def locate(self, tsc):
        """`locate_tsc` with the fake npx first on PATH; `tsc` builds the path npx prints."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            npx = bin_dir / "npx"
            npx.write_text(FAKE_NPX.format(python=sys.executable), encoding="utf-8")
            npx.chmod(0o755)
            cache = root / "cache"
            # a used cache that lacks this package set, which is what the race needs
            (cache / "_npx" / "other").mkdir(parents=True)
            env = {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "npm_config_cache": str(cache),
                # spelled here, not asked of the helper, so a lock on any other file fails,
                # and it's the file check-parity.py's TypeDoc install locks
                "FAKE_NPX_LOCK": str(cache / "microvms-agentd-npx.lock"),
                "FAKE_TSC": str(tsc(root, cache)),
            }
            with mock.patch.dict(os.environ, env):
                return DTS["locate_tsc"](VERSIONS), tsc(root, cache)

    def test_tsc_is_located_with_the_npx_lock_held(self):
        try:
            found, printed = self.locate(
                lambda _, cache: cache / "_npx" / "0" / "node_modules" / ".bin" / "tsc"
            )
        except DTS["ConsumerError"] as error:
            self.fail(str(error))
        self.assertEqual(found, printed)

    def test_a_tsc_outside_the_npx_cache_is_refused(self):
        # What `command -v tsc` prints when npx's tree has no .bin/tsc: the next one on PATH,
        # such as mise's global typescript. It's refused before it compiles anything.
        with self.assertRaisesRegex(
            DTS["ConsumerError"],
            r"npx located tsc at .*/global/bin/tsc, outside npm's npx",
        ):
            self.locate(lambda root, _: root / "global" / "bin" / "tsc")


if __name__ == "__main__":
    unittest.main()
