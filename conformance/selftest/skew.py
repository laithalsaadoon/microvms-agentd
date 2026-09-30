# SPDX-License-Identifier: Apache-2.0
"""The version-skew section's helpers, offline: which release it skews against, and how it
checks the assets it downloads. Each refusal is asserted to refuse."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

from harness.results import Results
from lanes.skew import (
    CLI_ASSET,
    DAEMON_ASSET,
    RELEASES,
    fetch_release,
    pick_previous,
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
        pick_previous([newest, older], head="face"),
        "v0.11.0",
    )
    results.eq(
        "skew: on a release's own commit the previous release is the one before",
        pick_previous([newest, older], head="c0ffee"),
        "v0.10.0",
    )
    ok, detail = refused(
        lambda: pick_previous([newest], head="c0ffee"), "git fetch --tags"
    )
    results.check("skew: no tag but HEAD's is refused, naming the fetch", ok, detail)

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
