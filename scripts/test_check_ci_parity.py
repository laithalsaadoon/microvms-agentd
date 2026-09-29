# SPDX-License-Identifier: Apache-2.0
"""Tests for `check-ci-parity.py` (#315), over fixture ci.yml and mise.toml pairs.

Each case starts from a pair that agrees and changes one thing, so a failure names the rule
that stopped holding. `edit` refuses an anchor the fixture doesn't contain: a case whose edit
matched nothing would pass while testing the unchanged, agreeing pair.
"""

import contextlib
import io
import runpy
import tempfile
import unittest
from pathlib import Path

PARITY = runpy.run_path(str(Path(__file__).with_name("check-ci-parity.py")))

CI = """\
name: ci
on:
  pull_request:
env:
  CARGO_TERM_COLOR: always
jobs:
  rust:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@4360b52568e2003a75bf9bc1d59f33a8e3fc893c # stable
        with:
          toolchain: stable
      - run: cargo test --all
  security:
    runs-on: ubuntu-latest
    steps:
      - name: semgrep
        run: uvx semgrep@1.176.1 scan --config p/rust
      - name: betterleaks
        run: |
          curl -sSfL -o bl.tgz https://github.com/betterleaks/betterleaks/releases/download/v1.7.3/betterleaks_1.7.3_linux_x64.tar.gz
          echo "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  bl.tgz" | sha256sum -c -
          ./betterleaks git .
      - name: cargo-deny
        uses: EmbarkStudios/cargo-deny-action@3c6349835b2b7b196a839186cb8b78e02f7b5f25 # v2.1.1
        with:
          rust-version: stable
      - name: actionlint
        uses: raven-actions/actionlint@3d39aea434753780c3b3d4a1a31c854b4dbf49d7 # v2.2.0
        with:
          version: 1.7.12
      # uvx ruff check . in a comment isn't a step
      - name: install ast-grep
        run: |
          curl -sSfL -o a.zip https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-x86_64-unknown-linux-gnu.zip
          echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  a.zip" | sha256sum -c -
  sbom:
    runs-on: ubuntu-latest
    steps:
      - name: install syft and grype
        run: |
          curl -sSfL -o syft.tgz https://github.com/anchore/syft/releases/download/v1.50.0/syft_1.50.0_linux_amd64.tar.gz
          echo "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  syft.tgz" | sha256sum -c -
          curl -sSfL -o grype.tgz https://github.com/anchore/grype/releases/download/v0.116.1/grype_0.116.1_linux_amd64.tar.gz
          echo "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  grype.tgz" | sha256sum -c -
      - name: osv-scanner
        run: |
          curl -sSfL -o osv-scanner https://github.com/google/osv-scanner/releases/download/v2.5.0/osv-scanner_linux_amd64
          echo "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee  osv-scanner" | sha256sum -c -
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4 # v10.1.0
        with:
          version: "0.12.13"
      - name: lint coverage
        env:
          RUFF: uvx ruff@0.15.22
        run: ./scripts/check-lint-coverage.py
      - run: uvx ruff@0.15.22 format --check .
  bindings:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7
        with:
          node-version: '22'
      - name: python binding tests
        run: uvx maturin@1.14.1 develop -m microvms-py/Cargo.toml
      - name: typed
        run: uvx ty@0.0.72 check x.py
"""

MISE = """\
[tools]
rust = "stable"
uv = "0.12.13"
ruff = "0.15.22"
semgrep = "1.176.1"
betterleaks = "1.7.3"
syft = "1.50.0"
grype = "0.116.1"
osv-scanner = "2.5.0"
node = "22"
"aqua:rhysd/actionlint" = "1.7.12"
"aqua:ast-grep/ast-grep" = "0.43.0"

[env]
AGENTD_TARGET = "aarch64-unknown-linux-musl"
CARGO_TERM_COLOR = "always"

[tasks."docs:links"]
tools = { "aqua:lycheeverse/lychee" = "0.24.2" }
run = "lychee"

[tasks.check]
depends = ["ci:parity"]

[tasks."ci:rust"]
run = "./scripts/ci-local.py rust"

[tasks."ci:security"]
run = "./scripts/ci-local.py security"

[tasks."ci:drift"]
run = "./scripts/ci-local.py drift"

[tasks."ci:bindings"]
run = ["./scripts/ci-local.py bindings"]

[tasks."ci:local"]
depends = ["ci:rust", "ci:security", "ci:drift", "ci:bindings"]
run = "./scripts/ci-local.py summary"
"""

LOCK = """\
# @generated - by `mise lock`

[[tools."aqua:ast-grep/ast-grep"]]
version = "0.43.0"

[tools."aqua:ast-grep/ast-grep"."platforms.linux-x64"]
checksum = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[tools."aqua:ast-grep/ast-grep"."platforms.macos-arm64"]
checksum = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

[[tools.betterleaks]]
version = "1.7.3"

[tools.betterleaks."platforms.linux-x64"]
checksum = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

[tools.betterleaks."platforms.macos-arm64"]
checksum = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

[[tools.syft]]
version = "1.50.0"

[tools.syft."platforms.linux-x64"]
checksum = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

[tools.syft."platforms.macos-arm64"]
checksum = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

[[tools.grype]]
version = "0.116.1"

[tools.grype."platforms.linux-x64"]
checksum = "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

[tools.grype."platforms.macos-arm64"]
checksum = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

[[tools.osv-scanner]]
version = "2.5.0"

[tools.osv-scanner."platforms.linux-x64"]
checksum = "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

[tools.osv-scanner."platforms.macos-arm64"]
checksum = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
"""

TOOLCHAIN = '[toolchain]\nchannel = "stable"\n'
STUBS = 'MATURIN = "maturin@1.14.1"\n'
REGISTRY = """\
[[fault]]
id = "py-entry"
run = [
  ["uvx", "maturin@1.14.1", "develop", "-q", "-m", "microvms-py/Cargo.toml"],
  ["python", "-m", "pytest", "-q"],
]
"""

LOCAL = """\
workflows = ["ci.yml"]

[expressions]
"matrix.os" = "ubuntu-latest"

[job."ci.yml".rust]
task = "rust"
steps = ["cargo test --all"]

[job."ci.yml".security]
task = "security"
steps = ["semgrep", "betterleaks", "cargo-deny", "actionlint", "install ast-grep"]

[job."ci.yml".security.local.cargo-deny]
run = "cargo deny check"
reason = "the command the action runs"

[job."ci.yml".security.local.actionlint]
run = "actionlint"
reason = "the binary the action downloads"

[job."ci.yml".sbom]
skip = "sudo"
steps = ["install syft and grype", "osv-scanner"]

[job."ci.yml".drift]
task = "drift"
steps = ["lint coverage", "uvx ruff@0.15.22 format --check ."]

[job."ci.yml".bindings]
task = "bindings"
steps = ["python binding tests", "typed"]

[job."ci.yml".bindings.local.typed]
skip = "CI only"

[actions]
"actions/checkout" = "the runner clones the snapshot itself"
"dtolnay/rust-toolchain" = "rust-toolchain.toml picks the channel"
"astral-sh/setup-uv" = "mise installs uv"
"actions/setup-node" = "mise installs Node"
"""

# Every fixture job opens with a checkout, as the real ones do.
CI = CI.replace(
    "    steps:\n",
    "    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7\n",
)


def edit(text, old, new):
    """`text` with `old` replaced once; fails when `old` isn't there."""
    if text.count(old) != 1:
        raise AssertionError(f"fixture anchor not found exactly once: {old!r}")
    return text.replace(old, new)


class ParityTests(unittest.TestCase):
    def run_pair(
        self,
        ci=CI,
        mise=MISE,
        lock=LOCK,
        toolchain=TOOLCHAIN,
        stubs=STUBS,
        registry=REGISTRY,
        local=LOCAL,
        extra=None,
    ):
        """The exit code and output of the checker over one fixture tree."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        files = {
            ".github/workflows/ci.yml": ci,
            "mise.toml": mise,
            "mise.lock": lock,
            "rust-toolchain.toml": toolchain,
            "scripts/generate-py-stubs.py": stubs,
            "guards/faults.toml": registry,
            "ci/local.toml": local,
            **(extra or {}),
        }
        for name, text in files.items():
            if text is None:
                continue
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = PARITY["main"](["--root", str(root)])
        return code, out.getvalue()

    def assertFails(self, needle, **files):
        code, out = self.run_pair(**files)
        self.assertEqual(code, 1, out)
        self.assertIn("ci parity: FAILED", out)
        self.assertIn(needle, out)
        return out

    # ── the agreeing pair ────────────────────────────────────────────────────

    def test_an_agreeing_pair_passes_and_names_every_tool(self):
        code, out = self.run_pair()
        self.assertEqual(code, 0, out)
        for tool in PARITY["TOOLS"]:
            self.assertIn(f" {tool} ", out)
        self.assertIn("env CARGO_TERM_COLOR", out)
        self.assertIn("rust stable", out)

    # ── env ──────────────────────────────────────────────────────────────────

    def test_a_ci_env_key_missing_from_mise_fails(self):
        mise = edit(MISE, 'CARGO_TERM_COLOR = "always"\n', "")
        self.assertFails("mise.toml [env] has no CARGO_TERM_COLOR", mise=mise)

    def test_a_differing_env_value_fails(self):
        mise = edit(MISE, 'CARGO_TERM_COLOR = "always"', 'CARGO_TERM_COLOR = "never"')
        self.assertFails("mise.toml [env] sets CARGO_TERM_COLOR=never", mise=mise)

    def test_an_env_key_only_mise_sets_passes(self):
        code, out = self.run_pair(mise=edit(MISE, "[env]\n", '[env]\nEXTRA = "1"\n'))
        self.assertEqual(code, 0, out)

    def test_an_empty_ci_env_block_fails(self):
        ci = edit(CI, "env:\n  CARGO_TERM_COLOR: always\n", "env: {}\n")
        self.assertFails("ci.yml has no top-level env block", ci=ci)

    def test_a_missing_ci_env_block_fails(self):
        ci = edit(CI, "env:\n  CARGO_TERM_COLOR: always\n", "")
        self.assertFails("ci.yml has no top-level env block", ci=ci)

    def test_a_missing_mise_env_table_fails(self):
        mise = edit(
            MISE,
            '[env]\nAGENTD_TARGET = "aarch64-unknown-linux-musl"\nCARGO_TERM_COLOR = "always"\n',
            "",
        )
        self.assertFails("mise.toml has no [env] table", mise=mise)

    def test_a_yaml_boolean_env_value_compares_as_the_string_a_process_sees(self):
        ci = edit(
            CI, "CARGO_TERM_COLOR: always\n", "CARGO_TERM_COLOR: always\n  FLAG: true\n"
        )
        mise = edit(MISE, "[env]\n", '[env]\nFLAG = "true"\n')
        code, out = self.run_pair(ci=ci, mise=mise)
        self.assertEqual(code, 0, out)

    # ── tool versions, one side bumped ───────────────────────────────────────

    def test_ruff_bumped_in_ci_only_fails(self):
        ci = edit(CI, "uvx ruff@0.15.22 format", "uvx ruff@0.15.23 format")
        self.assertFails(
            "ruff: ci.yml job `drift` step `uvx ruff@0.15.23 format --check .` "
            "runs 0.15.23, and mise.toml pins 0.15.22",
            ci=ci,
        )

    def test_ruff_in_a_step_env_value_is_compared(self):
        ci = edit(CI, "RUFF: uvx ruff@0.15.22", "RUFF: uvx ruff@0.15.21")
        self.assertFails("step `lint coverage` runs 0.15.21", ci=ci)

    def test_ruff_bumped_in_mise_only_fails(self):
        mise = edit(MISE, 'ruff = "0.15.22"', 'ruff = "0.15.23"')
        self.assertFails("runs 0.15.22, and mise.toml pins 0.15.23", mise=mise)

    def test_semgrep_bumped_on_one_side_fails(self):
        mise = edit(MISE, 'semgrep = "1.176.1"', 'semgrep = "1.177.0"')
        self.assertFails(
            "semgrep: ci.yml job `security` step `semgrep` runs 1.176.1", mise=mise
        )

    def test_maturin_differing_from_the_stub_generator_fails(self):
        stubs = edit(STUBS, "maturin@1.14.1", "maturin@1.14.2")
        self.assertFails(
            "runs 1.14.1, and scripts/generate-py-stubs.py pins 1.14.2", stubs=stubs
        )

    def test_maturin_in_a_registry_run_is_compared(self):
        registry = edit(REGISTRY, "maturin@1.14.1", "maturin@1.14.0")
        self.assertFails(
            "guards/faults.toml entry `py-entry` runs 1.14.0", registry=registry
        )

    def test_a_stub_generator_without_a_maturin_pin_fails(self):
        self.assertFails("has no MATURIN assignment", stubs="OTHER = 1\n")

    def test_node_major_differing_fails(self):
        mise = edit(MISE, 'node = "22"', 'node = "24"')
        self.assertFails(
            "runs Node 22, and mise.toml pins 24 (majors compared)", mise=mise
        )

    def test_node_compares_majors_not_minors(self):
        ci = edit(CI, "node-version: '22'", "node-version: '22.18'")
        mise = edit(MISE, 'node = "22"', 'node = "22.13.0"')
        code, out = self.run_pair(ci=ci, mise=mise)
        self.assertEqual(code, 0, out)

    def test_actionlint_with_no_version_input_fails(self):
        ci = edit(CI, "        with:\n          version: 1.7.12\n", "")
        out = self.assertFails("has no `version` input", ci=ci)
        self.assertNotIn("actionlint: found nowhere", out)

    def test_actionlint_bumped_on_one_side_fails(self):
        ci = edit(CI, "version: 1.7.12", "version: 1.7.13")
        self.assertFails(
            "actionlint: ci.yml job `security` step `actionlint` runs 1.7.13", ci=ci
        )

    def test_each_checksummed_download_bumped_on_one_side_fails(self):
        for tool, key, old, new in (
            ("ast-grep", '"aqua:ast-grep/ast-grep" = "0.43.0"', "0.43.0", "0.44.0"),
            ("betterleaks", 'betterleaks = "1.7.3"', "1.7.3", "1.7.4"),
            ("syft", 'syft = "1.50.0"', "1.50.0", "1.51.1"),
            ("grype", 'grype = "0.116.1"', "0.116.1", "0.118.0"),
            ("osv-scanner", 'osv-scanner = "2.5.0"', "2.5.0", "2.5.1"),
        ):
            with self.subTest(tool=tool):
                mise = edit(MISE, key, key.replace(old, new))
                self.assertFails(f"{tool}: ci.yml job", mise=mise)
                self.assertFails(f"runs {old}, and mise.toml pins {new}", mise=mise)

    def test_a_ci_hash_differing_from_mise_lock_fails(self):
        ci = edit(
            CI,
            '"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  syft.tgz"',
            '"9999999999999999999999999999999999999999999999999999999999999999  syft.tgz"',
        )
        self.assertFails(
            "syft: ci.yml job `sbom` step `install syft and grype` checks sha256 9999999999999999999999999999999999999999999999999999999999999999, "
            "and mise.lock records cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc for 1.50.0 on linux-x64",
            ci=ci,
        )

    def test_a_checksummed_download_with_no_sha256_check_fails(self):
        ci = edit(
            CI,
            '          echo "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  grype.tgz" | sha256sum -c -\n',
            "",
        )
        self.assertFails(
            "grype: ci.yml job `sbom` step `install syft and grype` downloads 0.116.1 with no "
            "sha256 check",
            ci=ci,
        )

    def test_a_download_bumped_without_mise_lock_fails(self):
        # Both sides bumped together, and the lock left behind.
        ci = edit(
            CI,
            "osv-scanner/releases/download/v2.5.0/",
            "osv-scanner/releases/download/v2.5.1/",
        )
        mise = edit(MISE, 'osv-scanner = "2.5.0"', 'osv-scanner = "2.5.1"')
        self.assertFails(
            "osv-scanner: mise.lock has no `osv-scanner` 2.5.1", ci=ci, mise=mise
        )

    def test_a_lock_entry_with_no_linux_checksum_fails(self):
        lock = edit(
            LOCK,
            '[tools.betterleaks."platforms.linux-x64"]\nchecksum = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\n',
            "",
        )
        self.assertFails(
            "betterleaks: mise.lock records no linux-x64 checksum for 1.7.3", lock=lock
        )

    def test_rust_channel_differing_from_the_toolchain_file_fails(self):
        ci = edit(CI, "toolchain: stable", "toolchain: nightly")
        self.assertFails(
            "installs 'nightly', and rust-toolchain.toml says stable", ci=ci
        )
        mise = edit(MISE, 'rust = "stable"', 'rust = "1.99.0"')
        self.assertFails("mise.toml pins rust = '1.99.0'", mise=mise)

    # ── pins ─────────────────────────────────────────────────────────────────

    def test_uv_with_no_version_input_fails(self):
        ci = edit(CI, '        with:\n          version: "0.12.13"\n', "")
        self.assertFails(
            "uv: ci.yml job `drift` step `astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4` "
            "has no `version` input",
            ci=ci,
        )

    def test_uv_bumped_on_one_side_fails(self):
        ci = edit(CI, 'version: "0.12.13"', 'version: "0.12.14"')
        self.assertFails("runs 0.12.14, and mise.toml pins 0.12.13", ci=ci)
        mise = edit(MISE, 'uv = "0.12.13"', 'uv = "0.12.12"')
        self.assertFails("runs 0.12.13, and mise.toml pins 0.12.12", mise=mise)

    def with_call(self, call):
        """CI and LOCAL with the drift job's format step running `call`, named `fmt`."""
        body = "\n".join("          " + line for line in call.splitlines())
        ci = edit(
            CI,
            "      - run: uvx ruff@0.15.22 format --check .\n",
            f"      - name: fmt\n        run: |\n{body}\n",
        )
        local = edit(
            LOCAL,
            '"lint coverage", "uvx ruff@0.15.22 format --check ."',
            '"lint coverage", "fmt"',
        )
        return {"ci": ci, "local": local}

    def test_every_uvx_spelling_is_compared(self):
        # The tool after options, `--from` with `==` or `@`, `uv tool run`, a call after
        # another command, a call split over lines: each runs ruff 0.1.0 on CI.
        for call in (
            "uvx -q ruff@0.1.0 format --check .",
            "uvx --python 3.12 ruff@0.1.0 format --check .",
            "uvx --from ruff==0.1.0 ruff format --check .",
            "uvx --from=ruff@0.1.0 ruff format --check .",
            "uvx --from 'ruff[extra]==0.1.0' ruff format --check .",
            "uv tool run ruff@0.1.0 format --check .",
            "uv  tool  run --quiet ruff@0.1.0 format --check .",
            "echo x && uvx ruff@0.1.0 format --check .",
            "uvx \\\n  ruff@0.1.0 format --check .",
        ):
            with self.subTest(call=call):
                self.assertFails(
                    "ruff: ci.yml job `drift` step `fmt` runs 0.1.0, and mise.toml pins "
                    "0.15.22",
                    **self.with_call(call),
                )

    def test_a_pinned_uvx_spelling_passes(self):
        for call in (
            "uvx --from ruff==0.15.22 ruff format --check .",
            "uv tool run ruff@0.15.22 format --check .",
        ):
            with self.subTest(call=call):
                code, out = self.run_pair(**self.with_call(call))
                self.assertEqual(code, 0, out)

    def test_an_unpinned_uvx_spelling_fails(self):
        for call, shown in (
            ("uvx --from ruff ruff check .", "uvx --from ruff ruff"),
            ("uvx --from 'ruff>=0.1' ruff check .", "uvx --from ruff>=0.1 ruff"),
            ("uv tool run ruff check .", "uv tool run ruff"),
        ):
            with self.subTest(call=call):
                self.assertFails(
                    f"runs `{shown}`, which names no exact version",
                    **self.with_call(call),
                )

    def test_a_uvx_call_that_cannot_be_read_fails(self):
        for call in ("uvx 'ruff check .", "uvx --from", "uvx -- ./x.py"):
            with self.subTest(call=call):
                self.assertFails(
                    "can't be read; write it as `uvx <tool>@X.Y.Z`",
                    **self.with_call(call),
                )

    def test_a_task_pinning_a_compared_tool_to_another_version_fails(self):
        mise = edit(
            MISE,
            "[tasks.check]\n",
            '[tasks.lint]\ntools = { ruff = "0.1.0" }\nrun = "ruff check"\n\n[tasks.check]\n',
        )
        self.assertFails(
            "ruff: mise.toml [tasks.'lint'] tools pins `ruff` = '0.1.0', and [tools] pins "
            "0.15.22",
            mise=mise,
        )
        same = mise.replace('ruff = "0.1.0"', 'ruff = "0.15.22"')
        code, out = self.run_pair(mise=same)
        self.assertEqual(code, 0, out)

    def test_an_unpinned_uvx_call_fails(self):
        ci = edit(CI, "uvx ruff@0.15.22 format", "uvx ruff format")
        self.assertFails("runs `uvx ruff`, which names no exact version", ci=ci)

    def test_a_minor_only_uvx_pin_fails(self):
        ci = edit(CI, "uvx maturin@1.14.1 develop", "uvx maturin@1.14 develop")
        self.assertFails("runs `uvx maturin@1.14`, which names no exact version", ci=ci)

    def test_an_uncompared_uvx_tool_still_needs_a_pin(self):
        ci = edit(CI, "uvx ty@0.0.72 check", "uvx ty check")
        self.assertFails("ty: ci.yml job `bindings` step `typed` runs `uvx ty`", ci=ci)

    def test_a_latest_tool_in_mise_fails(self):
        mise = edit(MISE, 'uv = "0.12.13"', 'uv = "latest"')
        self.assertFails("mise.toml [tools] pins `uv` to latest", mise=mise)

    def test_a_latest_tool_in_a_task_fails(self):
        mise = edit(
            MISE,
            '"aqua:lycheeverse/lychee" = "0.24.2"',
            '"aqua:lycheeverse/lychee" = "latest"',
        )
        self.assertFails(
            "[tasks.'docs:links'] tools pins `aqua:lycheeverse/lychee` to latest",
            mise=mise,
        )

    def test_a_table_form_pin_is_read(self):
        mise = edit(MISE, 'ruff = "0.15.22"', 'ruff = { version = "0.15.23" }')
        self.assertFails("mise.toml pins 0.15.23", mise=mise)

    def test_a_compared_tool_missing_from_mise_fails(self):
        mise = edit(MISE, 'grype = "0.116.1"\n', "")
        self.assertFails("grype: mise.toml [tools] has no `grype`", mise=mise)

    # ── a parser that returns nothing ────────────────────────────────────────

    def test_a_tool_found_nowhere_in_ci_fails(self):
        start = CI.index("      - name: osv-scanner\n")
        ci = CI[:start] + CI[CI.index("  drift:\n") :]
        self.assertFails("osv-scanner: found nowhere in ci.yml", ci=ci)

    def test_a_tool_named_only_in_a_yaml_comment_is_not_found(self):
        # The fixture's security job carries `uvx ruff check .` in a comment. Drop the real
        # ruff steps, and ruff has to be missing rather than read out of that comment.
        ci = edit(CI, "        env:\n          RUFF: uvx ruff@0.15.22\n", "")
        ci = edit(ci, "      - run: uvx ruff@0.15.22 format --check .\n", "")
        self.assertFails("ruff: found nowhere in ci.yml", ci=ci)

    def test_ci_with_no_jobs_fails(self):
        self.assertFails("ci.yml has no jobs", ci="env:\n  CARGO_TERM_COLOR: always\n")

    def test_ci_with_no_rust_toolchain_step_fails(self):
        ci = edit(
            CI,
            "      - uses: dtolnay/rust-toolchain@4360b52568e2003a75bf9bc1d59f33a8e3fc893c # stable\n        with:\n          toolchain: stable\n",
            "",
        )
        ci = edit(ci, "        with:\n          rust-version: stable\n", "")
        self.assertFails("rust: ci.yml has no dtolnay/rust-toolchain step", ci=ci)

    # ── step coverage: every run step has an entry in ci/local.toml ─────────

    def test_the_agreeing_pair_covers_every_step(self):
        code, out = self.run_pair()
        self.assertEqual(code, 0, out)
        self.assertIn("steps of ci.yml covered", out)
        self.assertIn("ci:local runs ci:bindings, ci:drift, ci:rust, ci:security", out)

    def test_a_run_step_with_no_entry_fails(self):
        ci = edit(
            CI,
            "      - run: uvx ruff@0.15.22 format --check .\n",
            "      - run: uvx ruff@0.15.22 format --check .\n      - run: echo uncovered\n",
        )
        self.assertFails(
            "ci.yml job `drift` step `echo uncovered` has no entry in ci/local.toml",
            ci=ci,
        )

    def test_a_named_run_step_with_no_entry_fails(self):
        ci = edit(
            CI,
            "      - name: typed\n",
            "      - name: new gate\n        run: ./gate\n      - name: typed\n",
        )
        self.assertFails("step `new gate` has no entry in ci/local.toml", ci=ci)

    def test_a_uses_step_whose_action_has_a_reason_needs_no_entry(self):
        ci = edit(
            CI,
            "      - name: typed\n",
            "      - name: a second node\n"
            "        uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020\n"
            "        with:\n          node-version: '22'\n"
            "      - name: typed\n",
        )
        code, out = self.run_pair(ci=ci)
        self.assertEqual(code, 0, out)

    def test_a_uses_step_with_no_entry_and_no_reason_fails(self):
        # A lint shipped as an action: CI runs it, and ci:local would run nothing.
        ci = edit(
            CI,
            "      - name: lint coverage\n",
            "      - name: typos\n        uses: crate-ci/typos@0000000000000000000000000000000000000000\n"
            "      - name: lint coverage\n",
        )
        self.assertFails(
            "ci.yml job `drift` step `typos` runs the action `crate-ci/typos`, which "
            "ci/local.toml neither lists with a local `run` nor names in [actions]",
            ci=ci,
        )

    def test_an_action_with_an_empty_reason_fails(self):
        local = edit(LOCAL, '"mise installs Node"', '" "')
        self.assertFails(
            "step `actions/setup-node` runs the action `actions/setup-node`",
            local=local,
        )

    def test_an_actions_entry_no_step_uses_fails(self):
        local = LOCAL + '"actions/cache" = "a cache"\n'
        self.assertFails(
            "[actions] names `actions/cache`, which no step ci:local runs uses",
            local=local,
        )

    def test_a_skipped_jobs_actions_need_no_reason(self):
        ci = edit(
            CI,
            "      - name: osv-scanner\n",
            "      - uses: aquasecurity/trivy-action@0000000000000000000000000000000000000000\n"
            "      - name: osv-scanner\n",
        )
        code, out = self.run_pair(ci=ci)
        self.assertEqual(code, 0, out)

    # ── keys the runner doesn't model ────────────────────────────────────────

    def test_a_job_key_the_runner_does_not_model_fails(self):
        for key, value in (
            ("if", "github.event_name == 'push'"),
            ("container", "node:22"),
            ("continue-on-error", "true"),
            ("needs", "rust"),
            ("services", "{}"),
        ):
            with self.subTest(key=key):
                ci = edit(
                    CI,
                    "  drift:\n    runs-on: ubuntu-latest\n",
                    f"  drift:\n    runs-on: ubuntu-latest\n    {key}: {value}\n",
                )
                self.assertFails(
                    f"ci.yml job `drift` sets `{key}`, which ci:local doesn't model",
                    ci=ci,
                )

    def test_a_skipped_job_may_set_any_key(self):
        ci = edit(
            CI,
            "  sbom:\n    runs-on: ubuntu-latest\n",
            "  sbom:\n    runs-on: ubuntu-latest\n    container: node:22\n",
        )
        code, out = self.run_pair(ci=ci)
        self.assertEqual(code, 0, out)

    def test_a_step_key_the_runner_does_not_model_fails(self):
        for key in ("continue-on-error: true", "timeout-minutes: 5"):
            with self.subTest(key=key):
                ci = edit(
                    CI,
                    "      - run: cargo test --all\n",
                    f"      - run: cargo test --all\n        {key}\n",
                )
                self.assertFails(
                    f"step `cargo test --all` sets `{key.split(':')[0]}`, which ci:local "
                    "doesn't model",
                    ci=ci,
                )

    def test_top_level_defaults_fail(self):
        ci = edit(CI, "jobs:\n", "defaults:\n  run:\n    shell: sh\njobs:\n")
        self.assertFails(
            "ci.yml sets top-level `defaults`, which ci:local doesn't model", ci=ci
        )

    def test_a_job_that_runs_on_another_os_fails(self):
        ci = edit(
            CI,
            "  drift:\n    runs-on: ubuntu-latest\n",
            "  drift:\n    runs-on: macos-latest\n",
        )
        self.assertFails(
            "ci.yml job `drift` runs on `macos-latest`, and ci:local runs Linux jobs only",
            ci=ci,
        )

    def test_a_job_with_no_entry_fails(self):
        ci = (
            CI + "  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n"
        )
        self.assertFails("ci.yml job `extra` has no entry in ci/local.toml", ci=ci)

    def test_an_entry_for_a_job_the_workflow_lacks_fails(self):
        local = LOCAL + '\n[job."ci.yml".gone]\ntask = "rust"\nsteps = []\n'
        self.assertFails(
            "entry for ci.yml job `gone`, which ci.yml doesn't have", local=local
        )

    def test_a_label_that_names_no_step_fails(self):
        local = edit(LOCAL, '"cargo test --all"', '"cargo test --all", "cargo bench"')
        self.assertFails("lists `cargo bench`, which names no step there", local=local)

    def test_steps_out_of_order_fail(self):
        local = edit(
            LOCAL,
            '"lint coverage", "uvx ruff@0.15.22 format --check ."',
            '"uvx ruff@0.15.22 format --check .", "lint coverage"',
        )
        self.assertFails("lists its steps in another order", local=local)

    def test_a_skipped_job_with_no_reason_fails(self):
        local = edit(LOCAL, 'skip = "sudo"', 'skip = " "')
        self.assertFails("job `sbom` is skipped and gives no reason", local=local)

    def test_a_skipped_job_still_lists_its_steps(self):
        local = edit(
            LOCAL,
            'steps = ["install syft and grype", "osv-scanner"]',
            'steps = ["osv-scanner"]',
        )
        self.assertFails("step `install syft and grype` has no entry", local=local)

    def test_a_skipped_step_with_no_reason_fails(self):
        local = edit(LOCAL, 'skip = "CI only"', 'skip = ""')
        self.assertFails("step `typed` is skipped and gives no reason", local=local)

    def test_a_local_change_with_no_reason_fails(self):
        local = edit(LOCAL, 'reason = "the binary the action downloads"\n', "")
        self.assertFails(
            "step `actionlint` runs differently here and gives no reason", local=local
        )

    def test_a_uses_step_listed_with_no_local_command_fails(self):
        local = edit(
            LOCAL,
            '[job."ci.yml".security.local.cargo-deny]\nrun = "cargo deny check"\n'
            'reason = "the command the action runs"\n',
            "",
        )
        self.assertFails(
            "step `cargo-deny` is a `uses:` step, and ci/local.toml gives no local `run`",
            local=local,
        )

    def test_a_change_to_an_unlisted_step_fails(self):
        local = LOCAL + '\n[job."ci.yml".rust.local."cargo fmt"]\nskip = "x"\n'
        self.assertFails(
            "changes `cargo fmt`, which its `steps` don't list", local=local
        )

    def test_an_unknown_change_key_fails(self):
        local = edit(LOCAL, 'skip = "CI only"', 'skip = "CI only"\ncommand = "x"')
        self.assertFails("step `typed`: unknown key `command`", local=local)

    def test_duplicate_labels_in_a_job_fail(self):
        ci = edit(
            CI, "      - run: cargo test --all\n", "      - run: cargo test --all\n" * 2
        )
        self.assertFails("more than one step labelled `cargo test --all`", ci=ci)

    def test_an_expression_with_no_value_fails(self):
        ci = edit(
            CI, "scan --config p/rust\n", "scan --config p/rust ${{ github.sha }}\n"
        )
        self.assertFails(
            "step `semgrep` uses `${{ github.sha }}`, which has no value in ci/local.toml "
            "[expressions]",
            ci=ci,
        )

    def test_a_known_expression_passes(self):
        ci = edit(
            CI, "scan --config p/rust\n", "scan --config p/rust ${{ matrix.os }}\n"
        )
        code, out = self.run_pair(ci=ci)
        self.assertEqual(code, 0, out)

    def test_an_if_with_no_true_or_false_value_fails(self):
        ci = edit(
            CI,
            "      - run: cargo test --all\n",
            "      - run: cargo test --all\n        if: matrix.os\n",
        )
        self.assertFails(
            "runs `if: matrix.os`, and ci/local.toml [expressions] gives it no true",
            ci=ci,
        )

    def test_a_skipped_steps_expressions_need_no_value(self):
        ci = edit(
            CI,
            "run: uvx ty@0.0.72 check x.py",
            "run: uvx ty@0.0.72 check ${{ github.sha }}",
        )
        code, out = self.run_pair(ci=ci)
        self.assertEqual(code, 0, out)

    def test_a_shell_other_than_bash_fails(self):
        ci = edit(
            CI,
            "      - run: cargo test --all\n",
            "      - run: cargo test --all\n        shell: pwsh\n",
        )
        self.assertFails("uses shell `pwsh`; ci:local runs bash only", ci=ci)

    def test_a_job_with_no_task_fails(self):
        local = edit(LOCAL, 'task = "rust"\n', "")
        self.assertFails("job `rust` names no `task` and isn't skipped", local=local)

    def test_a_job_with_no_checkout_fails(self):
        ci = edit(
            CI,
            "  rust:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7\n",
            "  rust:\n    runs-on: ubuntu-latest\n    steps:\n",
        )
        self.assertFails("ci.yml job `rust` has no actions/checkout step", ci=ci)

    def test_no_workflows_fails(self):
        local = edit(LOCAL, 'workflows = ["ci.yml"]\n', "")
        self.assertFails("ci/local.toml names no `workflows`", local=local)

    def test_a_plan_that_leaves_out_ci_yml_fails(self):
        local = edit(LOCAL, 'workflows = ["ci.yml"]', 'workflows = ["other.yml"]')
        other = "jobs:\n  a:\n    steps:\n      - run: make\n"
        out = self.assertFails(
            "doesn't name ci.yml in `workflows`",
            local=local,
            extra={".github/workflows/other.yml": other},
        )
        self.assertIn("has jobs for `ci.yml`, which `workflows` doesn't name", out)

    def test_a_named_workflow_that_is_missing_fails(self):
        local = edit(
            LOCAL, 'workflows = ["ci.yml"]', 'workflows = ["ci.yml", "fuzz.yml"]'
        )
        self.assertFails("fuzz.yml: can't read", local=local)

    def test_a_workflow_with_no_run_steps_fails(self):
        # The floor for a reader that returns nothing: a workflow in the plan has to have at
        # least one `run:` step, or every job in it would pass as covered.
        local = edit(
            LOCAL, 'workflows = ["ci.yml"]', 'workflows = ["ci.yml", "other.yml"]'
        )
        local += '\n[job."other.yml".a]\ntask = "rust"\nsteps = []\n'
        other = "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v5\n"
        self.assertFails(
            "other.yml: found no `run:` steps, so there's nothing to cover",
            local=local,
            extra={".github/workflows/other.yml": other},
        )

    def test_a_missing_ci_task_fails(self):
        mise = edit(
            MISE, '[tasks."ci:drift"]\nrun = "./scripts/ci-local.py drift"\n', ""
        )
        self.assertFails(
            "mise.toml has no `ci:drift` task running `./scripts/ci-local.py drift`",
            mise=mise,
        )

    def test_a_ci_task_that_runs_something_else_fails(self):
        mise = edit(MISE, 'run = "./scripts/ci-local.py rust"', 'run = "cargo test"')
        self.assertFails(
            "no `ci:rust` task running `./scripts/ci-local.py rust`", mise=mise
        )

    def test_ci_local_missing_a_dependency_fails(self):
        mise = edit(MISE, '"ci:rust", "ci:security"', '"ci:security"')
        self.assertFails("`ci:local` doesn't depend on `ci:rust`", mise=mise)

    def test_check_depending_on_ci_local_fails(self):
        mise = edit(
            MISE, 'depends = ["ci:parity"]', 'depends = ["ci:parity", "ci:local"]'
        )
        self.assertFails("`check` depends on `ci:local`", mise=mise)
        mise = edit(
            MISE, 'depends = ["ci:parity"]', 'depends = ["ci:parity", "ci:guards"]'
        )
        code, out = self.run_pair(mise=mise)
        self.assertEqual(code, 0, out)  # no job runs as ci:guards in this fixture
        mise = edit(
            MISE, 'depends = ["ci:parity"]', 'depends = ["ci:parity", "ci:drift"]'
        )
        self.assertFails("`check` depends on `ci:drift`", mise=mise)

    def test_an_empty_or_unparseable_plan_fails(self):
        self.assertFails("ci/local.toml: ", local="")
        self.assertFails("is empty", local="\n")
        self.assertFails(
            "doesn't parse as TOML", local=edit(LOCAL, "[expressions]", "[expressions")
        )

    def test_plan_gives_the_runner_each_step_as_written(self):
        problems = []
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/ci.yml").write_text(CI)
        import tomllib

        jobs = PARITY["plan"](root, tomllib.loads(LOCAL), problems)
        self.assertEqual(problems, [])
        by_name = {job.name: job for job in jobs}
        self.assertEqual(sorted(by_name), ["bindings", "drift", "rust", "security"])
        drift = by_name["drift"]
        self.assertEqual(drift.env, {"CARGO_TERM_COLOR": "always"})
        self.assertFalse(drift.full_history)
        self.assertEqual(drift.steps[0].env, {"RUFF": "uvx ruff@0.15.22"})
        self.assertEqual(drift.steps[0].label, "lint coverage")
        self.assertEqual(drift.steps[1].run, "uvx ruff@0.15.22 format --check .")
        security = by_name["security"]
        self.assertEqual([s.label for s in security.steps][3], "actionlint")
        self.assertEqual(security.steps[3].run, "actionlint")
        typed = by_name["bindings"].steps[1]
        self.assertIsNone(typed.run)
        self.assertEqual(typed.note, "CI only")

    def test_a_timeout_expression_takes_its_value_from_expressions(self):
        # The guards job gives main's push its own budget (#323). ci:local answers for a pull
        # request, as it does every expression, and a budget it can't read is a problem
        # rather than a run with no timeout.
        expr = "github.event_name == 'pull_request' && 30 || 60"
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root)
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/ci.yml").write_text(
            edit(
                CI,
                "  drift:\n    runs-on: ubuntu-latest\n",
                f"  drift:\n    runs-on: ubuntu-latest\n    timeout-minutes: ${{{{ {expr} }}}}\n",
            )
        )
        import tomllib

        def plan(answer: str | None) -> tuple[list, list[str]]:
            local = LOCAL
            if answer is not None:
                local = edit(
                    LOCAL, "[expressions]\n", f'[expressions]\n"{expr}" = "{answer}"\n'
                )
            problems: list[str] = []
            return PARITY["plan"](root, tomllib.loads(local), problems), problems

        jobs, problems = plan("30")
        self.assertEqual(problems, [])
        self.assertEqual({j.name: j for j in jobs}["drift"].timeout_minutes, 30)
        _, problems = plan(None)
        self.assertIn(
            f"ci.yml job `drift` timeout-minutes uses `${{{{ {expr} }}}}`, which has no "
            "value in ci/local.toml [expressions]",
            problems,
        )
        # `inf`, `nan`, `0` and `-5` parse as floats, and none is a deadline ci-local.py keeps.
        for answer in ("soon", "", "inf", "nan", "0", "-5"):
            with self.subTest(answer=answer):
                jobs, problems = plan(answer)
                self.assertIn(
                    f"ci.yml job `drift` timeout-minutes resolves to `{answer}`, not a "
                    "positive number",
                    problems,
                )
                self.assertIsNone({j.name: j for j in jobs}["drift"].timeout_minutes)

    # ── empty, missing and unparseable files ─────────────────────────────────

    def test_an_empty_ci_yml_fails(self):
        for text in ("", "\n  \n"):
            with self.subTest(text=text):
                self.assertFails("is empty", ci=text)

    def test_an_unparseable_ci_yml_fails(self):
        ci = edit(CI, "CARGO_TERM_COLOR: always", "CARGO_TERM_COLOR: [always")
        self.assertFails("doesn't parse as YAML", ci=ci)

    def test_a_ci_yml_that_isnt_a_mapping_fails(self):
        self.assertFails("isn't a YAML mapping", ci="- one\n- two\n")

    def test_a_missing_ci_yml_fails(self):
        self.assertFails("ci.yml: can't read", ci=None)

    def test_an_empty_mise_toml_fails(self):
        self.assertFails("mise.toml: ", mise="")
        self.assertFails("is empty", mise="")

    def test_an_unparseable_mise_toml_fails(self):
        self.assertFails("doesn't parse as TOML", mise=edit(MISE, "[tools]", "[tools"))

    def test_the_other_inputs_missing_fail(self):
        for name in ("lock", "toolchain", "stubs", "registry", "local"):
            with self.subTest(name=name):
                self.assertFails("can't read", **{name: None})

    def test_an_explicit_empty_ci_path_fails(self):
        """`--ci /dev/null` is how guards/faults.toml seeds an empty ci.yml."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty = Path(tmp.name) / "ci.yml"
        empty.write_text("", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = PARITY["main"](["--ci", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("is empty", out.getvalue())


if __name__ == "__main__":
    unittest.main()
