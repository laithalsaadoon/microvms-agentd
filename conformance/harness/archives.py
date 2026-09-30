# SPDX-License-Identifier: Apache-2.0
"""The hostile archives the file-transfer section hands the daemon, built by hand."""

from __future__ import annotations


def build_hostile_archives() -> list[tuple[str, bytes]]:
    """The four malicious archives, hand-built, exactly as the deleted oracle built them.

    `tarfile` rather than `tar(1)`, and that is not a convenience: GNU tar **sanitizes**
    several of these — it strips a leading `../`, refuses to store an absolute link target
    — so shelling out would produce four harmless archives and four checks that passed
    against nothing. Each `TarInfo` is constructed field by field here so the hostile
    member really is in the bytes.

    Every one of these is a refused *member*, which the daemon answers 400 for and the
    client maps to `ProtocolError`. A 413 would be a cap violation instead, and the
    distinction matters: one means "this archive is hostile", the other means "this archive
    is merely too big".

    The names are the oracle's, so the four `hostile archive refused: <name>` lines in this
    report diff against the four `SKIP` lines in the last one.
    """
    import io
    import tarfile

    def make(members: list[tuple[str, str, str | None, bytes]]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, kind, target, data in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                elif kind == "sym":
                    info.type = tarfile.SYMTYPE
                    info.linkname = target or ""
                    tar.addfile(info)
                elif kind == "dev":
                    info.type = tarfile.CHRTYPE
                    info.devmajor, info.devminor = 1, 3
                    tar.addfile(info)
        return buffer.getvalue()

    return [
        # Writes outside the extraction root by walking up out of it.
        ("parent traversal", make([("../../escaped.txt", "file", None, b"pwned")])),
        # A symlink pointing at an absolute path in the guest, which would let a later
        # write land on /etc/passwd.
        ("absolute link target", make([("link", "sym", "/etc/passwd", b"")])),
        # The two-member version, which defeats a naive per-member path check: `s` is an
        # in-tree symlink to `..`, and `s/escaped.txt` then resolves outside the root
        # without any member's own name containing `..`.
        (
            "symlink redirect",
            make([("s", "sym", "..", b""), ("s/escaped.txt", "file", None, b"pwned")]),
        ),
        # A character device. Extracting one means the archive can create a node that
        # reads host memory or produces attacker-chosen bytes.
        ("character device", make([("dev", "dev", None, b"")])),
    ]
