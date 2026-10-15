# SPDX-License-Identifier: Apache-2.0
"""Version skew (#298): the previous release's daemon under this tree's CLI, and this tree's
daemon under the previous release's CLI.

An upgrade replaces one side at a time. A user with a kept VM upgrades the CLI and attaches to
a daemon built from the release before, and a harness pinned to the last CLI release runs it
against an image this tree built. `docs/schema.json` and `schema:compat` hold the routes and
fields to the previous release offline; this is the same claim run end to end: each pairing
launches, runs an exec, copies a file up and back, reads health and tears down.

The previous release is the highest stable `vX.Y.Z` tag reachable from HEAD that has a
published, non-prerelease GitHub release and does not point at HEAD. A draft or bare tag is
not a release a user can upgrade from. Publication metadata errors fail the section rather
than silently choosing an older baseline. Its daemon and its x86_64 Linux CLI come from
that release's assets, each checked against the release's `SHA256SUMS`. That's integrity, not
provenance: both come from the same release, and core's provisioning is where a Sigstore bundle
is verified (BIND-18).

Health's `version` is the daemon's own crate version (`agentd`'s `CARGO_PKG_VERSION`). Through
v1.0.0 it stayed at 0.1.0 while the tag moved, so v0.10.0's and v1.0.0's daemons report 0.1.0;
since #592 the release pull request sets it to the release number. So each pairing expects
the version in `agentd`'s `Cargo.toml` at the tree its daemon was built from, read with git for
the release (the manifest moved from `agentd/` to `crates/agentd/` in #374, so both paths are
tried). While the two trees carry the same crate version, that check can't tell the daemons apart;
the digest of `/agentd` read back from the VM does.

The first pairing builds an image around the old daemon, so it's one of the sections with its
own build. The second launches the suite's image (this tree's daemon) through the old CLI, so it
builds nothing. Each pairing keeps its record in a state directory of its own.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import secrets
import subprocess
import tarfile
import tomllib
import urllib.parse
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
RELEASE_METADATA = (
    "https://api.github.com/repos/laithalsaadoon/microvms-agentd/releases"
)
PUBLICATION_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
STABLE_TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
DAEMON_ASSET = "agentd"
CLI_ASSET = "microvm-x86_64-unknown-linux-gnu.tar.gz"
SUMS_LINE = re.compile(r"([0-9a-f]{64}) [ *]?(\S+)")
#: Where `agentd`'s manifest is at a commit, newest layout first: `crates/` since #374.
DAEMON_MANIFESTS = ("crates/agentd/Cargo.toml", "agentd/Cargo.toml")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def pick_previous(tags: list[tuple[str, str]], head: str, published: set[str]) -> str:
    """The newest reachable published stable tag whose commit differs from HEAD."""
    for tag, commit in tags:
        if commit != head and STABLE_TAG.fullmatch(tag) and tag in published:
            return tag
    raise RuntimeError(
        "no published stable release other than HEAD is reachable from HEAD; "
        "git fetch --tags and check the repository's published releases"
    )


def crate_version(text: str, where: str) -> str:
    """`[package] version` in a Cargo manifest's text. A manifest without one is an error."""
    try:
        version = tomllib.loads(text).get("package", {}).get("version")
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"{where}: {error}") from None
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"{where} has no [package] version")
    return version


def daemon_version(show: Callable[[str], str | None]) -> str:
    """The `agentd` crate version at a tree, which is what its daemon's health reports.

    `show` answers a path's text in that tree, or None when the tree has no such file.
    """
    for path in DAEMON_MANIFESTS:
        text = show(path)
        if text is not None:
            return crate_version(text, path)
    raise RuntimeError(f"none of {', '.join(DAEMON_MANIFESTS)} is in that tree")


def shown_at(ref: str) -> Callable[[str], str | None]:
    """A `show` for `daemon_version` over the tree at `ref`."""

    def show(path: str) -> str | None:
        done = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return done.stdout if done.returncode == 0 else None

    return show


def in_worktree(path: str) -> str | None:
    """A `show` for `daemon_version` over this checkout."""
    try:
        return (REPO / path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def previous_release() -> str:
    """The release a user of this tree upgrades from (the module docstring has the rule)."""
    head = git("rev-parse", "HEAD").strip()
    names = git("tag", "--merged", "HEAD", "--list", "v*", "--sort=-v:refname").split()
    return pick_previous(
        [(tag, git("rev-parse", f"{tag}^{{commit}}").strip()) for tag in names],
        head,
        published_release_tags(),
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


def require_metadata_url(url: str) -> None:
    """Publication authority stays on the exact GitHub API HTTPS origin."""
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "api.github.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("release metadata URL is outside https://api.github.com")


class MetadataRedirect(urllib.request.HTTPRedirectHandler):
    """Reject a changed origin before urllib follows it, including intermediate hops."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_metadata_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def metadata_download(url: str) -> bytes:
    """Metadata has a stricter origin boundary than the existing asset CDN transport."""
    require_metadata_url(url)
    opener = urllib.request.build_opener(MetadataRedirect())
    with opener.open(url, timeout=120) as response:
        require_metadata_url(response.geturl())
        return response.read()


def unique_metadata_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Ambiguous JSON cannot attest that a release is published and stable."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"release metadata repeats JSON key {key!r}")
        result[key] = value
    return result


def valid_publication_time(value: Any) -> bool:
    """Require a complete RFC 3339 timestamp and a real calendar date/time."""
    if not isinstance(value, str) or PUBLICATION_TIME.fullmatch(value) is None:
        return False
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def published_release_tags(
    fetch: Callable[[str], bytes] = metadata_download,
) -> set[str]:
    """Read all public release pages; missing or malformed metadata is not eligibility.

    List metadata, rather than probing asset URLs: a 404 for a broken published release
    must fail its asset check, not silently move compatibility testing to an older tag.
    """
    published: set[str] = set()
    seen: set[str] = set()
    page = 1
    while True:
        payload = fetch(f"{RELEASE_METADATA}?per_page=100&page={page}")
        try:
            records = json.loads(payload, object_pairs_hook=unique_metadata_object)
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError(f"release metadata page {page} is not JSON") from error
        if not isinstance(records, list) or len(records) > 100:
            raise RuntimeError(f"release metadata page {page} is not a release list")
        for record in records:
            if not isinstance(record, dict):
                raise RuntimeError(
                    f"release metadata page {page} has a non-object entry"
                )
            tag = record.get("tag_name")
            draft = record.get("draft")
            prerelease = record.get("prerelease")
            published_at = record.get("published_at")
            if (
                not isinstance(tag, str)
                or not tag
                or type(draft) is not bool
                or type(prerelease) is not bool
                or (
                    published_at is not None
                    and not valid_publication_time(published_at)
                )
                or (not draft and not published_at)
            ):
                raise RuntimeError(
                    f"release metadata page {page} has invalid publication fields"
                )
            if tag in seen:
                raise RuntimeError(f"release metadata repeats tag {tag!r}")
            seen.add(tag)
            if not draft and not prerelease and STABLE_TAG.fullmatch(tag):
                published.add(tag)
        if len(records) < 100:
            return published
        page += 1


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

    `daemon` is the binary the VM should be running, and `daemon_version` the `agentd` crate
    version it was built at, which health reports. Its digest is compared with `/agentd`'s, the
    file the image's `CMD` starts, read back through the pairing's own `cp`: the version can't
    tell two daemons apart while both trees carry the same crate version.
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
        daemon_version(shown_at(tag)),
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
        daemon_version(in_worktree),
        second / "state",
        second,
        logs,
        # The suite's image: its own teardown deletes it.
        delete_image=False,
    )
