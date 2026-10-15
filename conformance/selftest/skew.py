# SPDX-License-Identifier: Apache-2.0
"""The version-skew section's helpers, offline: which release it skews against, how it checks
the assets it downloads, and which daemon version each pairing expects health to report. Each
refusal is asserted to refuse."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from harness.results import Results
from lanes import skew as skew_lane
from lanes.skew import (
    CLI_ASSET,
    DAEMON_ASSET,
    RELEASE_METADATA,
    RELEASES,
    MetadataRedirect,
    daemon_version,
    fetch_release,
    metadata_download,
    pick_previous,
    published_release_tags,
    sha256_sums,
    verified,
)


def refused(call: Callable[[], object], needle: str) -> tuple[bool, str]:
    """Whether `call` raised a RuntimeError naming `needle`, and what it did instead."""
    try:
        got = call()
    except RuntimeError as error:
        return needle in str(error), str(error)
    return False, f"returned {got!r}"


def answered(call: Callable[[], object]) -> object:
    """What `call` returns, or None when it raises: `Results.eq` then fails the check by name
    instead of the self-test stopping on the error."""
    try:
        return call()
    except RuntimeError:
        return None


def release(tamper: str = "") -> dict[str, bytes]:
    """A release's three downloads, by URL. `tamper` names one to break after it's summed."""
    daemon = b"\x7fELF an aarch64 daemon"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        name = "not-microvm" if tamper == "no-cli" else "microvm"
        member = tarfile.TarInfo(name)
        body = b"#!/bin/sh\necho microvm 0.10.0\n"
        member.size = len(body)
        tar.addfile(member, io.BytesIO(body))
    files = {DAEMON_ASSET: daemon, CLI_ASSET: archive.getvalue()}
    sums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {asset}\n"
        for asset, data in files.items()
    )
    if tamper == "daemon":
        files[DAEMON_ASSET] = daemon + b" and a byte more"
    if tamper == "unlisted":
        sums = sums.splitlines(keepends=True)[1]
    base = f"{RELEASES}/v0.10.0"
    return {
        f"{base}/SHA256SUMS": sums.encode(),
        **{f"{base}/{asset}": data for asset, data in files.items()},
    }


def check_version_skew_helpers(results: Results) -> None:
    newest, older = ("v0.11.0", "c0ffee"), ("v0.10.0", "beef")
    results.eq(
        "skew: the previous release is the newest tag not on HEAD",
        pick_previous([newest, older], head="face", published={newest[0], older[0]}),
        "v0.11.0",
    )
    results.eq(
        "skew: on a release's own commit the previous release is the one before",
        pick_previous([newest, older], head="c0ffee", published={newest[0], older[0]}),
        "v0.10.0",
    )
    ok, detail = refused(
        lambda: pick_previous([newest], head="c0ffee", published={newest[0]}),
        "git fetch --tags",
    )
    results.check("skew: no tag but HEAD's is refused, naming the fetch", ok, detail)

    def metadata(tag: str, **changes: object) -> dict[str, object]:
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "published_at": "2026-10-15T00:00:00Z",
            **changes,
        }

    rows = [
        metadata("v1.1.1", draft=True, published_at=None),
        metadata("v1.1.0", draft=True, published_at=None),
        metadata("v1.0.1"),
        metadata("v1.2.0-rc.1", prerelease=True),
        metadata("v9.0.0", prerelease=True),
        metadata("nightly"),
    ]
    published = published_release_tags(lambda _: json.dumps(rows).encode())
    results.eq(
        "skew: publication metadata excludes drafts and prereleases",
        published,
        {"v1.0.1"},
    )
    lineage = [("v1.1.1", "new"), ("v1.1.0", "draft"), ("v1.0.1", "released")]
    results.eq(
        "skew: main after an unpublished tag keeps the published baseline",
        pick_previous(lineage, "main", published),
        "v1.0.1",
    )
    results.eq(
        "skew: a draft release tag keeps the published baseline",
        pick_previous(lineage, "new", published),
        "v1.0.1",
    )
    results.eq(
        "skew: a published tag never pairs with itself",
        pick_previous(lineage, "new", published | {"v1.1.1"}),
        "v1.0.1",
    )
    results.eq(
        "skew: a published release outside the ancestry cannot win",
        pick_previous(lineage, "main", published | {"v2.0.0"}),
        "v1.0.1",
    )
    results.eq(
        "skew: publication order does not override semantic tag order",
        pick_previous(
            [("v1.10.0", "ten"), ("v1.9.0", "nine")], "main", {"v1.9.0", "v1.10.0"}
        ),
        "v1.10.0",
    )
    ok, detail = refused(
        lambda: pick_previous(lineage, "main", set()), "no published stable release"
    )
    results.check("skew: no published baseline is an error", ok, detail)
    results.eq(
        "skew: empty metadata is empty eligibility, not a tag fallback",
        published_release_tags(lambda _: b"[]"),
        set(),
    )
    results.eq(
        "skew: a bare tag absent from metadata is not a release",
        pick_previous([("v8.0.0", "bare"), *lineage], "main", published),
        "v1.0.1",
    )

    # A full first page of prereleases must not hide the actual published baseline.
    pages = [
        json.dumps(
            [metadata(f"v2.0.0-rc.{i}", prerelease=True) for i in range(100)]
        ).encode(),
        json.dumps([metadata("v1.0.1")]).encode(),
    ]
    requested = []

    def page_fetch(url: str) -> bytes:
        requested.append(url)
        return pages[len(requested) - 1]

    results.eq(
        "skew: publication discovery reads later pages",
        published_release_tags(page_fetch),
        {"v1.0.1"},
    )
    results.eq(
        "skew: publication discovery requests consecutive pages",
        requested,
        [f"{RELEASE_METADATA}?per_page=100&page={i}" for i in (1, 2)],
    )

    invalid = {
        "not JSON": b"broken",
        "an API error object": b'{"message":"API rate limit exceeded"}',
        "a non-object entry": b"[null]",
        "a missing draft field": json.dumps([{"tag_name": "v1.0.1"}]).encode(),
        "a string draft field": json.dumps(
            [metadata("v1.0.1", draft="false")]
        ).encode(),
        "a missing publication time": json.dumps(
            [metadata("v1.0.1", published_at=None)]
        ).encode(),
        "a duplicate tag": json.dumps(
            [metadata("v1.0.1"), metadata("v1.0.1")]
        ).encode(),
    }
    for label, body in invalid.items():
        ok, detail = refused(
            lambda body=body: published_release_tags(lambda _: body), "release metadata"
        )
        results.check(f"skew: metadata with {label} is refused", ok, detail)

    def unavailable(_: str) -> bytes:
        raise ConnectionError("metadata unavailable")

    try:
        published_release_tags(unavailable)
    except ConnectionError:
        propagated = True
    else:
        propagated = False
    results.check(
        "skew: metadata transport failure propagates without fallback", propagated
    )

    def fake_git(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "main\n"
        if args[0] == "tag":
            return "v1.1.0\nv1.0.1\n"
        return {"v1.1.0^{commit}": "draft\n", "v1.0.1^{commit}": "released\n"}[args[1]]

    with (
        patch.object(skew_lane, "git", fake_git),
        patch.object(
            skew_lane, "published_release_tags", return_value={"v1.0.1"}
        ) as metadata_fetch,
    ):
        results.eq(
            "skew: the lane wires publication eligibility into git ancestry",
            skew_lane.previous_release(),
            "v1.0.1",
        )
        results.eq(
            "skew: the lane fetches publication metadata once",
            metadata_fetch.call_count,
            1,
        )

    selected_assets = []

    def broken_assets(tag: str, *_: object) -> object:
        selected_assets.append(tag)
        raise ConnectionError("published asset unavailable")

    with (
        patch.object(skew_lane, "previous_release", return_value="v1.0.1"),
        patch.object(skew_lane, "fetch_release", broken_assets),
    ):
        try:
            skew_lane.drive_version_skew(
                None, None, None, None, Path("unused"), None, results
            )
        except ConnectionError:
            failed_assets = True
        else:
            failed_assets = False
    results.check(
        "skew: a published asset failure cannot select an older release",
        failed_assets and selected_assets == ["v1.0.1"],
    )

    valid_time_rows = [
        metadata("v1.0.1", published_at="2026-10-15T00:00:00Z"),
        metadata("v1.0.0", published_at="2026-10-15T01:30:00.123+01:30"),
    ]
    results.eq(
        "skew: RFC3339 publication times with UTC and offsets are accepted",
        published_release_tags(lambda _: json.dumps(valid_time_rows).encode()),
        {"v1.0.1", "v1.0.0"},
    )
    for label, stamp in [
        ("arbitrary text", "not-a-timestamp"),
        ("whitespace", "   "),
        ("date only", "2026-10-15"),
        ("no zone", "2026-10-15T00:00:00"),
        ("impossible calendar date", "2026-02-30T00:00:00Z"),
        ("invalid time", "2026-10-15T24:00:00Z"),
        ("invalid zone", "2026-10-15T00:00:00+24:00"),
        ("invalid zone minutes", "2026-10-15T00:00:00+00:99"),
    ]:
        body = json.dumps([metadata("v1.0.1", published_at=stamp)]).encode()
        ok, detail = refused(
            lambda body=body: published_release_tags(lambda _: body),
            "publication fields",
        )
        results.check(f"skew: publication time with {label} is refused", ok, detail)
    # Python's calendar parser may normalize out-of-range time components. Refuse them
    # lexically first, independently of the interpreter's normalization behavior.
    with patch.object(skew_lane.datetime, "datetime") as calendar:
        invalid_times = [
            "2026-10-15T24:00:00Z",
            "2026-10-15T00:60:00Z",
            "2026-10-15T00:00:60Z",
            "2026-10-15T00:00:00+24:00",
            "2026-10-15T00:00:00+00:99",
        ]
        refused_times = [
            not skew_lane.valid_publication_time(value) for value in invalid_times
        ]
        results.check(
            "skew: RFC3339 time bounds refuse calendar normalization",
            all(refused_times) and not calendar.fromisoformat.called,
        )

    for key in ("draft", "prerelease", "published_at"):
        body = (
            json.dumps([metadata("v1.0.1")])
            .replace('"tag_name":', f'"{key}": true, "tag_name":')
            .encode()
        )
        ok, detail = refused(
            lambda body=body: published_release_tags(lambda _: body), "repeats JSON key"
        )
        results.check(f"skew: duplicate {key} JSON fields are refused", ok, detail)

    allowed = f"{RELEASE_METADATA}?per_page=100&page=1"
    target = f"{RELEASE_METADATA}?page=2"
    handler = MetadataRedirect()
    redirected = handler.redirect_request(
        urllib.request.Request(allowed), None, 302, "Found", {}, target
    )
    results.eq(
        "skew: metadata permits a same-origin HTTPS redirect",
        redirected.full_url,
        target,
    )
    for label, url in [
        ("HTTP downgrade", "http://api.github.com/releases"),
        ("another host", "https://unexpected.invalid/releases"),
        ("another port", "https://api.github.com:444/releases"),
        ("user info", "https://user@api.github.com/releases"),
    ]:
        ok, detail = refused(
            lambda url=url: handler.redirect_request(
                urllib.request.Request(allowed), None, 302, "Found", {}, url
            ),
            "outside https://api.github.com",
        )
        results.check(f"skew: metadata redirect to {label} is refused", ok, detail)
        with patch.object(urllib.request, "build_opener") as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.geturl.return_value = allowed
            response.read.return_value = b"[]"
            ok, detail = refused(
                lambda url=url: metadata_download(url), "outside https://api.github.com"
            )
            results.check(
                f"skew: metadata initial URL with {label} is refused before opening",
                ok and not factory.called,
                detail,
            )
            response.geturl.return_value = url
            ok, detail = refused(
                lambda: metadata_download(allowed), "outside https://api.github.com"
            )
            results.check(
                f"skew: metadata final URL with {label} is refused before reading",
                ok and not response.read.called,
                detail,
            )

    with patch.object(urllib.request, "build_opener") as factory:
        response = factory.return_value.open.return_value.__enter__.return_value
        response.geturl.return_value = target
        response.read.return_value = b"[]"
        results.eq(
            "skew: metadata reads a successful same-origin HTTPS response",
            metadata_download(allowed),
            b"[]",
        )
        results.check(
            "skew: metadata installs the constrained redirect handler",
            any(
                isinstance(value, MetadataRedirect) for value in factory.call_args.args
            ),
        )
        results.check(
            "skew: metadata keeps an explicit transport timeout",
            factory.return_value.open.call_args.kwargs.get("timeout") == 120,
        )

    with (
        patch.object(urllib.request, "build_opener") as factory,
        patch.object(urllib.request, "urlopen") as generic,
    ):
        response = factory.return_value.open.return_value.__enter__.return_value
        response.geturl.return_value = allowed
        response.read.return_value = b"[]"
        generic.return_value.__enter__.return_value.read.return_value = b"[]"
        results.eq(
            "skew: default publication fetch reads metadata successfully",
            published_release_tags(),
            set(),
        )
        results.check(
            "skew: default publication fetch uses the constrained transport",
            factory.called and not generic.called,
        )

    digest = "a" * 64
    results.eq(
        "skew: SHA256SUMS reads each line's file and digest",
        sha256_sums(f"{digest}  agentd\n{digest} *microvm.tar.gz\n\n"),
        {"agentd": digest, "microvm.tar.gz": digest},
    )
    ok, detail = refused(
        lambda: sha256_sums(f"{digest}  agentd\nnot a line\n"), "isn't"
    )
    results.check("skew: a SHA256SUMS line in another shape is refused", ok, detail)
    ok, detail = refused(lambda: sha256_sums("\n"), "lists no file")
    results.check("skew: an empty SHA256SUMS is refused", ok, detail)

    data = b"bytes"
    sums = {"agentd": hashlib.sha256(data).hexdigest()}
    results.eq(
        "skew: bytes that match their sum pass", verified("agentd", data, sums), data
    )
    ok, detail = refused(
        lambda: verified("agentd", data + b"!", sums), "SHA256SUMS records"
    )
    results.check("skew: bytes whose sha256 differs are refused", ok, detail)
    ok, detail = refused(
        lambda: verified("microvm", data, sums), "has no line for microvm"
    )
    results.check("skew: an asset SHA256SUMS doesn't list is refused", ok, detail)

    with tempfile.TemporaryDirectory() as tmp:
        files = release()
        daemon, cli = fetch_release("v0.10.0", Path(tmp) / "ok", files.__getitem__)
        results.check(
            "skew: a release whose assets match its sums is unpacked, executable",
            daemon.read_bytes().startswith(b"\x7fELF")
            and cli.read_bytes().startswith(b"#!/bin/sh")
            and os.access(daemon, os.X_OK)
            and os.access(cli, os.X_OK),
            f"daemon={daemon} cli={cli}",
        )
        for tamper, needle, name in (
            ("daemon", "agentd hashes to", "a daemon that isn't the one summed"),
            (
                "unlisted",
                "no line for agentd",
                "a release whose sums leave the daemon out",
            ),
            ("no-cli", "holds no `microvm` file", "a CLI archive without the binary"),
        ):
            broken = release(tamper)
            ok, detail = refused(
                lambda broken=broken, tamper=tamper: fetch_release(
                    "v0.10.0", Path(tmp) / tamper, broken.__getitem__
                ),
                needle,
            )
            results.check(f"skew: {name} is refused", ok, detail)

    # Health reports agentd's crate version, not the release tag's (wave 2's live run: v0.10.0's
    # daemon answered 0.1.0 where the section expected 0.10.0). The manifest moved in #374, so
    # a release before it has only the old path.
    manifest = '[package]\nname = "agentd"\nversion = "0.1.0"\n'
    moved = {"crates/agentd/Cargo.toml": manifest.replace("0.1.0", "0.2.0")}
    results.eq(
        "skew: the daemon's version is agentd's crate version, at the current path",
        answered(lambda: daemon_version(moved.get)),
        "0.2.0",
    )
    before = {"agentd/Cargo.toml": manifest}
    results.eq(
        "skew: a release from before the move reads agentd's old manifest",
        answered(lambda: daemon_version(before.get)),
        "0.1.0",
    )
    both = {**before, **moved}
    results.eq(
        "skew: the current path wins where both exist",
        answered(lambda: daemon_version(both.get)),
        "0.2.0",
    )
    ok, detail = refused(lambda: daemon_version({}.get), "is in that tree")
    results.check("skew: a tree with neither manifest is refused", ok, detail)
    unversioned = {"crates/agentd/Cargo.toml": '[package]\nname = "agentd"\n'}
    ok, detail = refused(
        lambda: daemon_version(unversioned.get), "has no [package] version"
    )
    results.check("skew: a manifest with no version is refused", ok, detail)
