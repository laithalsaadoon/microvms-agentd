# SPDX-License-Identifier: Apache-2.0
"""Files: `microvm cp` and `cp --tar` against the kept VM, and `microvm.toml` with
`run <DIR>` sync mode on a VM launched through the config file.

Every hostile archive is a live check. The archives are built with `tarfile`
(`harness/archives.py`), written to a temp file, and handed to `microvm cp --tar`. The expected
outcome is the **daemon's** refusal surfacing as `data.kind: ProtocolError` with exit 5, not
this suite's opinion of the archive and not the CLI's. The CLI deliberately doesn't
pre-validate an archive (`microvms-cli/src/commands/attached.rs`, and the byte-scan guard in
`microvms-cli/src/guards.rs` that proves it), because a client-side check would make these
checks pass against the client's copy of the member rules while the extractor that runs in
production went untested.
"""

from __future__ import annotations

from pathlib import Path

from harness.archives import build_hostile_archives
from harness.cli import Cli, attach_args
from harness.envelope import Envelope, EnvelopeError, KindError
from harness.results import Results


def drive_file_transfer(
    cli: Cli, launched: Envelope, results: Results, workdir: Path
) -> None:
    """`microvm cp` and `cp --tar`: thirteen checks, including the four hostile archives.

    The symlink pair is the one worth naming: harnesses pack symlinks deliberately, and a
    daemon that refused links would break real uploads — so an in-tree link has to survive
    the round trip *as a link* and still resolve to its target's content. Both halves are
    asserted, because a round trip that dereferenced the link would satisfy the second on
    its own.

    `--tar` is asymmetric, and the asymmetry is the design rather than a rough edge. The
    **local** side is an archive file, because neither `microvms-core` nor the CLI carries a
    tar library — `session/files.rs:112` declines to add one, since Rust's standard library
    has no equivalent of tarfile's `data` filter and "an extraction that looked safe and was
    not is worse than none". The **`vm:`** side is a *directory*, because the daemon does
    carry the crate and both routes are about trees: `GET /v1/fs/tar` packs a directory and
    `PUT /v1/fs/tar` extracts into one, through the confined extractor that stays the only
    extractor in the system.

    So nothing outside the daemon ever packs or unpacks, which is also why this section no
    longer shells out to `tar` in the guest: al2023-minimal has no `tar` binary, and a step
    that needed one would be testing the base image's tooling rather than this client.
    """
    print("\n-- file transfer --")
    attach = attach_args(cli, launched)

    payload = workdir / "live.txt"
    payload.write_bytes(b"written through the endpoint")
    results.ok(
        "single file write accepted",
        lambda: cli.call(
            "cp", str(payload), "vm:/tmp/live.txt", "--mode", "644", *attach
        ),
    )

    read_back = workdir / "read-back.txt"
    cli.call("cp", "vm:/tmp/live.txt", str(read_back), *attach)
    results.eq(
        "single file read returns the bytes",
        read_back.read_bytes(),
        b"written through the endpoint",
    )
    results.raises(
        "read of an absent file is 404",
        "NotFound",
        lambda: cli.call("cp", "vm:/tmp/absent", str(workdir / "absent.txt"), *attach),
    )

    # The tree, built in the guest. A symlink packed deliberately, because that is the
    # member a harness really sends.
    tree = cli.call(
        "exec",
        "rm -rf /tmp/tree /tmp/dest && mkdir -p /tmp/tree/sub && "
        "echo payload > /tmp/tree/a.txt && ln -sf a.txt /tmp/tree/link && "
        "echo deep > /tmp/tree/sub/b.txt",
        *attach,
    )
    results.eq("tree created for the round trip", tree.data.get("exitCode"), 0)

    # `vm:` names the DIRECTORY, and the daemon packs it. That is the whole shape of these
    # two routes and the first live round is what taught it: `GET /v1/fs/tar` requires a
    # directory (`agentd/src/fs.rs:786` — a non-directory is an explicit 400 "use
    # /v1/fs/file") and packs it itself with `pack_tree`, which carries the `tar` crate so
    # that no client and no base image needs one.
    #
    # The first draft of this section ran `tar cf` in the guest and then pointed `--tar` at
    # the resulting file. It failed twice over: al2023-minimal ships no `tar` binary (exit
    # 127), and the file was the wrong thing to hand the route anyway. Both errors came from
    # the same wrong belief — that something other than the daemon had to do the packing.
    # The guest-tar step is gone rather than fixed: it tested the base image's tooling, not
    # this client.
    #
    # Members are `./`-relative (`append_dir_all(".", root)`), so they land *flattened*
    # under the destination — `/tmp/dest/link`, not `/tmp/dest/tree/link`. That is what the
    # verification below reads, and it is what makes a downloaded archive re-uploadable,
    # which `fs.rs:226` names as the one round trip a harness performs constantly.
    archive = workdir / "tree.tar"
    try:
        cli.call("cp", "vm:/tmp/tree", str(archive), "--tar", *attach)
        results.check(
            "tar download succeeded",
            archive.exists() and archive.stat().st_size > 0,
            f"{archive.stat().st_size if archive.exists() else 0} bytes",
        )
    except (KindError, EnvelopeError) as exc:
        results.check("tar download succeeded", False, repr(exc))

    if archive.exists() and archive.stat().st_size > 0:
        results.ok(
            "tar upload accepted",
            lambda: cli.call("cp", str(archive), "vm:/tmp/dest", "--tar", *attach),
        )
    else:
        results.check(
            "tar upload accepted", False, "no archive was downloaded to upload"
        )

    # `readlink` first, so a dereferenced round trip fails on the *link* assertion rather
    # than passing the content one and looking fine. Paths are flattened under the
    # destination, per the note above.
    verify = cli.call(
        "exec",
        "readlink /tmp/dest/link; cat /tmp/dest/link; cat /tmp/dest/sub/b.txt",
        *attach,
    )
    verified = verify.data.get("stdout") or ""
    results.check(
        "symlink survived the round trip as a symlink",
        verified.startswith("a.txt"),
        repr(verified[:120]),
    )
    results.check(
        "symlink still resolves to its target's content",
        "payload" in verified,
        repr(verified[:120]),
    )

    # -- the four hostile archives -------------------------------------------
    #
    # Handed to `microvm cp --tar` as pre-built files. The expected failure is the
    # DAEMON's, surfacing as `data.kind: ProtocolError` with exit 5 — the CLI does not
    # pre-validate an archive, and `microvms-cli/src/guards.rs`'s byte-scan proves it. A
    # client-side check would make these four pass against the client's copy of the member
    # rules while the extractor that runs in production went untested.
    print("\n-- hostile archives --")
    for name, archive_bytes in build_hostile_archives():
        path = workdir / f"hostile-{name.replace(' ', '-')}.tar"
        path.write_bytes(archive_bytes)
        results.raises(
            f"hostile archive refused: {name}",
            "ProtocolError",
            lambda p=path: cli.call("cp", str(p), "vm:/tmp/hostile", "--tar", *attach),
        )

    escaped = cli.call(
        "exec",
        "ls /escaped.txt /tmp/escaped.txt 2>&1 | head -3; echo done",
        *attach,
    )
    listing = escaped.data.get("stdout") or ""
    results.check(
        "nothing escaped the extraction root",
        "No such file" in listing or "cannot access" in listing,
        repr(listing[:160]),
    )


def drive_config_and_sync(
    cli: Cli, launched: Envelope, project: Path, results: Results
) -> None:
    """microvm.toml (issue #73) and `run <DIR>` sync mode (issue #72), live.

    Eight checks against one VM this section launches through the *config file* and
    terminates itself, from the image the suite already built (`image` pinned in the
    file, so no second build — and pinning it there is itself the check that a config
    value reaches a real launch). Live rather than only scripted for the named-VMs
    reason: the round trip crosses the daemon's real tar routes and the service's real
    launch, and a fixture convention is not a service fact.

    The project directory is this run's own temp dir: a `microvm.toml` pinning the
    suite's image and one artifact glob, a source file to prove upload, and a `.git`
    with a loose object to prove it stays home.
    """
    print("\n-- microvm.toml + run <DIR> (config to wire, sync round trip) --")
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "loose-object").write_text("never uploaded")
    (project / "hello.txt").write_text("from the host\n")
    (project / "microvm.toml").write_text(
        "\n".join(
            [
                f'image = "{launched.data["imageIdentifier"]}"',
                'exec = "pwd; cat hello.txt; ls .git 2>&1; mkdir -p dist; '
                'echo made-in-the-vm > dist/report.txt; echo secret > not-asked-for.txt"',
                "max-idle-sec = 480",
                'artifacts = ["dist/**"]',
                "",
            ]
        )
    )

    # doctor validates the same file run reads, through the same loader.
    checked = cli.call("doctor", "--config", str(project / "microvm.toml"))
    config_check = next(
        (row for row in checked.data.get("checks", []) if row.get("name") == "config"),
        {},
    )
    results.check(
        "doctor validated the config file and named what it pins",
        config_check.get("ok") is True
        and "pins" in str(config_check.get("detail", "")),
        f"{config_check}",
    )

    synced = cli.call(
        "run",
        str(project),
        "--config",
        str(project / "microvm.toml"),
        "--region",
        cli.region,
        "--max-duration-sec",
        "1800",
        timeout=15 * 60,
    )
    results.eq(
        "a config-only launch ran the file's exec", synced.data.get("execExitCode"), 0
    )
    # This run tore its VM down, so its token has no consumer; a live credential printed to
    # stdout for a VM that is about to be terminated is what issue #161 measured. The key
    # stays (a consumer never guards against a missing key) and the value is null.
    results.check(
        "a run without --keep nulls agentToken in its envelope (issue #161)",
        "agentToken" in synced.data and synced.data.get("agentToken") is None,
        f"key present={'agentToken' in synced.data} null={synced.data.get('agentToken') is None}",
    )
    stdout = synced.data.get("stdout") or ""
    results.check(
        "the exec ran in /workspace, not the image WORKDIR",
        stdout.startswith("/workspace"),
        repr(stdout[:60]),
    )
    results.check(
        "the uploaded tree reached the VM",
        "from the host" in stdout,
        repr(stdout[:120]),
    )
    results.check(
        ".git stayed home",
        "No such file" in stdout or "cannot access" in stdout,
        repr(stdout[:200]),
    )
    resolved = synced.data.get("resolvedConfig") or {}
    results.check(
        "resolvedConfig reports the file as each knob's source",
        resolved.get("image", {}).get("source") == "config"
        and resolved.get("maxIdleSec", {}).get("value") == 480,
        f"image={resolved.get('image')} maxIdleSec={resolved.get('maxIdleSec')}",
    )
    results.check(
        "the glob-matched artifact came back",
        (project / "dist" / "report.txt").exists()
        and "made-in-the-vm" in (project / "dist" / "report.txt").read_text(),
        f"dist exists: {(project / 'dist').exists()}",
    )
    results.check(
        "an unmatched member stayed in the VM",
        not (project / "not-asked-for.txt").exists(),
        f"landed: {sorted(p.name for p in project.iterdir())}",
    )
    # No terminate here and no --keep above: sync mode tears down by default, which is
    # itself part of what this section exercises — the teardown path after a download.
