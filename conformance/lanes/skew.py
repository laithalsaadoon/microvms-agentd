# SPDX-License-Identifier: Apache-2.0
"""Version skew (#298): the previous release's daemon under this tree's CLI, and this tree's
daemon under the previous release's CLI.

An upgrade replaces one side at a time. A user with a kept VM upgrades the CLI and attaches to
a daemon built from the release before, and a harness pinned to the last CLI release runs it
against an image this tree built. `docs/schema.json` and `schema:compat` hold the routes and
fields to the previous release offline; this is the same claim run end to end: each pairing
launches, runs an exec, copies a file up and back, reads health and tears down.

The previous release is the highest `v*` tag reachable from HEAD that doesn't point at HEAD, so
on a release's own commit it's the release before. Its daemon and its x86_64 Linux CLI come from
that release's assets, each checked against the release's `SHA256SUMS`. That's integrity, not
provenance: both come from the same release, and core's provisioning is where a Sigstore bundle
is verified (BIND-18).

The first pairing builds an image around the old daemon, so it's one of the sections with its
own build. The second launches the suite's image (this tree's daemon) through the old CLI, so it
builds nothing. Each pairing keeps its record in a state directory of its own.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
import tarfile
import urllib.request
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, REPO
from harness.envelope import Envelope, EnvelopeError, KindError
from harness.results import Results

RELEASES = "https://github.com/laithalsaadoon/microvms-agentd/releases/download"
DAEMON_ASSET = "agentd"
CLI_ASSET = "microvm-x86_64-unknown-linux-gnu.tar.gz"
SUMS_LINE = re.compile(r"([0-9a-f]{64}) [ *]?(\S+)")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def pick_previous(tags: list[tuple[str, str]], head: str) -> str:
    """The first of `tags` (`(tag, commit)`, newest first) whose commit isn't `head`."""
    for tag, commit in tags:
        if commit != head:
            return tag
    raise RuntimeError(
        "no release tag other than HEAD's is reachable from HEAD, so there's no previous "
        "release to skew against. Fetch the tags: git fetch --tags"
    )


def previous_release() -> str:
    """The release a user of this tree upgrades from (the module docstring has the rule)."""
    head = git("rev-parse", "HEAD").strip()
    names = git("tag", "--merged", "HEAD", "--list", "v*", "--sort=-v:refname").split()
    return pick_previous(
        [(tag, git("rev-parse", f"{tag}^{{commit}}").strip()) for tag in names], head
    )


def sha256_sums(text: str) -> dict[str, str]:
    """`SHA256SUMS` as `{asset: hex}`. A line in any other shape is an error, not skipped."""
    sums = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = SUMS_LINE.fullmatch(line.strip())
        if match is None:
            raise RuntimeError(
                f"SHA256SUMS has a line that isn't `<sha256>  <file>`: {line!r}"
            )
        sums[match.group(2)] = match.group(1)
    if not sums:
        raise RuntimeError("SHA256SUMS lists no file")
    return sums


def verified(asset: str, data: bytes, sums: dict[str, str]) -> bytes:
    """`data` when its sha256 is the one `SHA256SUMS` records for `asset`."""
    want = sums.get(asset)
    if want is None:
        raise RuntimeError(f"the release's SHA256SUMS has no line for {asset}")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError(f"{asset} hashes to {got}, and SHA256SUMS records {want}")
    return data


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def fetch_release(
    tag: str, dest: Path, fetch: Callable[[str], bytes] = download
) -> tuple[Path, Path]:
    """The release's daemon and its Linux CLI under `dest`, each checked against its sums."""
    base = f"{RELEASES}/{tag}"
    sums = sha256_sums(fetch(f"{base}/SHA256SUMS").decode())
    dest.mkdir(parents=True, exist_ok=True)
    daemon = dest / "agentd"
    daemon.write_bytes(verified(DAEMON_ASSET, fetch(f"{base}/{DAEMON_ASSET}"), sums))
    daemon.chmod(0o755)
    archive = verified(CLI_ASSET, fetch(f"{base}/{CLI_ASSET}"), sums)
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        member = next((m for m in tar.getmembers() if m.name == "microvm"), None)
        if member is None or not member.isfile():
            raise RuntimeError(f"{CLI_ASSET} holds no `microvm` file")
        tar.extract(member, dest, filter="data")
    cli = dest / "microvm"
    cli.chmod(0o755)
    return daemon, cli


def pairing(
    results: Results,
    label: str,
    cli: Cli,
    run_args: list[str],
    daemon: Path,
    daemon_version: str,
    state_dir: Path,
    workdir: Path,
    logs: Any,
    delete_image: bool,
) -> None:
    """One pairing: launch, exec, a file up and back, health, and the teardown.

    `daemon` is the binary the VM should be running. Its digest is compared with `/agentd`'s,
    the file the image's `CMD` starts, read back through the pairing's own `cp`: the version
    health reports can't tell the two apart while this tree still carries the last release's
    version number.
    """
    try:
        launched: Envelope = cli.call(
            "run",
            *run_args,
            "--memory",
            str(BASELINE_MEMORY_MIB),
            "--keep",
            "--state-dir",
            str(state_dir),
            "--region",
            cli.region,
            "--exec",
            "echo skew",
            "--max-idle-sec",
            "600",
            "--suspended-sec",
            "600",
            "--max-duration-sec",
            "1800",
            timeout=50 * 60,
        )
    except (KindError, EnvelopeError, subprocess.TimeoutExpired) as error:
        results.check(
            f"skew: {label}: run launches and runs its exec", False, repr(error)
        )
        return
    try:
        results.eq(
            f"skew: {label}: run launches and runs its exec",
            launched.data.get("execExitCode"),
            0,
        )
        attach = [*attach_args(cli, launched), "--state-dir", str(state_dir)]
        health = cli.call("health", *attach)
        results.eq(
            f"skew: {label}: health reports the daemon's version",
            health.data.get("version"),
            daemon_version,
        )
        baked = workdir / "agentd-in-the-vm"
        cli.call("cp", "vm:/agentd", str(baked), *attach, timeout=10 * 60)
        results.eq(
            f"skew: {label}: the VM runs the daemon it was given",
            hashlib.sha256(baked.read_bytes()).hexdigest() if baked.exists() else None,
            hashlib.sha256(daemon.read_bytes()).hexdigest(),
        )
        ran = cli.call("exec", "echo across versions", *attach)
        results.check(
            f"skew: {label}: exec exits 0 with its stdout",
            ran.data.get("exitCode") == 0
            and "across versions" in (ran.data.get("stdout") or ""),
            f"exit={ran.data.get('exitCode')!r} stdout={ran.data.get('stdout')!r}",
        )
        up = workdir / "up.txt"
        up.write_bytes(b"copied across versions")
        down = workdir / "down.txt"
        cli.call("cp", str(up), "vm:/tmp/skew.txt", *attach)
        cli.call("cp", "vm:/tmp/skew.txt", str(down), *attach)
        results.eq(
            f"skew: {label}: cp brings back the bytes it sent",
            down.read_bytes() if down.exists() else None,
            up.read_bytes(),
        )
    except (KindError, EnvelopeError, subprocess.TimeoutExpired) as error:
        results.check(f"skew: {label}: every step answers", False, repr(error))
    finally:
        teardown(results, label, cli, launched, state_dir, logs, delete_image)


def teardown(
    results: Results,
    label: str,
    cli: Cli,
    launched: Envelope,
    state_dir: Path,
    logs: Any,
    delete_image: bool,
) -> None:
    """The pairing's VM (and, for its own build, its image) terminated, and the log groups the
    service created deleted, for the reason `drive_teardown` gives."""
    args = [
        "terminate",
        str(launched.data["microvmId"]),
        *(["--delete-image"] if delete_image else []),
        "--wait",
        "--state-dir",
        str(state_dir),
        "--region",
        cli.region,
    ]
    try:
        torn = cli.call(*args, timeout=15 * 60)
    except (KindError, EnvelopeError, subprocess.TimeoutExpired) as error:
        results.check(
            f"skew: {label}: terminate leaves nothing behind", False, repr(error)
        )
        return
    groups = [str(group) for group in torn.data.get("undeletedLogGroups") or []]
    failures = []
    for group in groups:
        try:
            logs.delete_log_group(logGroupName=group)
        except Exception as exc:  # noqa: BLE001 - the reason is the finding
            if type(exc).__name__ != "ResourceNotFoundException":
                failures.append(f"{group}: {type(exc).__name__}: {exc}")
    results.check(
        f"skew: {label}: terminate leaves nothing behind",
        not torn.data.get("leaked") and not failures,
        f"leaked={torn.data.get('leaked')!r} log groups={groups!r} failures={failures!r}",
    )


def drive_version_skew(
    cli: Cli,
    launched: Envelope,
    binary: Path,
    dockerfile: Path,
    workdir: Path,
    logs: Any,
    results: Results,
) -> None:
    """Both pairings against the previous release (the module docstring has the design)."""
    tag = previous_release()
    print(f"\n-- version skew against {tag} --")
    old_daemon, old_cli_path = fetch_release(tag, workdir / "release")
    old_cli = Cli(binary=old_cli_path, region=cli.region)
    results.eq(
        f"skew: the {tag} CLI is the release's",
        old_cli.version(),
        tag.removeprefix("v"),
    )

    first = workdir / "old-daemon"
    first.mkdir(parents=True, exist_ok=True)
    pairing(
        results,
        f"this CLI drives the {tag} daemon",
        cli,
        [
            str(old_daemon),
            "--name",
            f"microvm-cli-conformance-skew-{secrets.token_hex(4)}",
            "--dockerfile",
            str(dockerfile),
        ],
        old_daemon,
        tag.removeprefix("v"),
        first / "state",
        first,
        logs,
        delete_image=True,
    )

    second = workdir / "old-cli"
    second.mkdir(parents=True, exist_ok=True)
    pairing(
        results,
        f"the {tag} CLI drives this daemon",
        old_cli,
        ["--image", str(launched.data["imageIdentifier"])],
        binary,
        cli.version(),
        second / "state",
        second,
        logs,
        # The suite's image: its own teardown deletes it.
        delete_image=False,
    )
