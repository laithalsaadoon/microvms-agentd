# SPDX-License-Identifier: Apache-2.0
"""Daemon provisioning through the binding (BIND-17 through BIND-20), without GitHub.

Every case here is answered before a download: a caller-supplied binary, the cache, a
refusal, or a fetch that can't reach GitHub. The fake-release scenarios that drive
the verification policy are `microvms-core/tests/features/provision.feature`; the real
fetch from the published release is recorded in the PR that added this, and the live
suite's `drive_provisioned_quickstart` runs it through the CLI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import microvms


def elf(machine: int, tail: bytes = b"") -> bytes:
    """A little-endian ELF header for `machine` (0xB7 is aarch64, 0x3E is x86_64)."""
    header = bytearray(20)
    header[:4] = b"\x7fELF"
    header[5] = 1
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + tail


@pytest.fixture(autouse=True)
def no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MICROVM_AGENTD", raising=False)


def unreachable_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send the in-process fetch through a proxy on a closed local port, so it can't
    reach GitHub. reqwest reads the proxy variables when the fetch builds its client."""
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")


def test_bind17_a_caller_supplied_binary_is_returned_without_a_fetch(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "agentd"
    data = elf(0xB7, b"caller")
    binary.write_bytes(data)
    assert microvms.provision_agentd(state_dir=tmp_path, binary=binary) == data

    report = microvms.provision_agentd_report(state_dir=tmp_path, binary=str(binary))
    assert report.source == "caller-supplied"
    assert report.supplied_by == "argument"
    assert report.verification is None
    assert report.data == data
    assert report.path == str(binary)
    assert report.version == microvms.core_version()
    assert report.sha256 == hashlib.sha256(data).hexdigest()
    assert "size=26" in repr(report)
    assert "verification=None" in repr(report)
    assert "ELF" not in repr(report), "the bytes stay out of repr"


def test_bind17_microvm_agentd_supplies_the_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "env-agentd"
    binary.write_bytes(elf(0xB7))
    monkeypatch.setenv("MICROVM_AGENTD", str(binary))
    report = microvms.provision_agentd_report(version="v9.9.9", state_dir=tmp_path)
    assert report.source == "caller-supplied"
    assert report.supplied_by == "env"
    assert report.version == "9.9.9"


def test_bind19_a_recorded_cache_entry_is_served_and_a_changed_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The layout and record core writes after a verified fetch.
    version = "9.9.9"
    entry = tmp_path / "agentd" / f"v{version}"
    entry.mkdir(parents=True)
    data = elf(0xB7, b"cached")
    (entry / "agentd").write_bytes(data)
    (entry / "agentd.verified.json").write_text(
        json.dumps(
            {
                "version": version,
                "sha256": hashlib.sha256(data).hexdigest(),
                "verification": "checksum",
            }
        )
    )
    report = microvms.provision_agentd_report(version=version, state_dir=tmp_path)
    assert report.source == "cache"
    assert report.verification == "checksum"
    assert report.data == data

    # Changed after it was recorded: discarded and fetched again, and with GitHub out of
    # reach the fetch fails closed rather than serving the changed bytes.
    (entry / "agentd").write_bytes(elf(0xB7, b"changed"))
    unreachable_release(monkeypatch)
    with pytest.raises(microvms.PreconditionError, match="could not download agentd"):
        microvms.provision_agentd(version=version, state_dir=tmp_path)
    assert not (entry / "agentd").exists()


def test_bind18_a_fetch_that_cannot_run_is_an_error_naming_every_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreachable_release(monkeypatch)
    with pytest.raises(microvms.PreconditionError) as caught:
        microvms.provision_agentd(state_dir=tmp_path)
    message = str(caught.value)
    assert f"v{microvms.core_version()}" in message
    assert "gh release download" in message
    assert "MICROVM_AGENTD" in message
    assert caught.value.code == "ERR_PRECONDITION"
    assert not list((tmp_path / "agentd").rglob("agentd"))


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (elf(0x3E), "ELF machine 0x3e"),
        (b"#!/bin/sh\nexec agentd\n", "not an ELF"),
    ],
)
def test_bind20_a_caller_binary_that_is_not_aarch64_is_refused(
    tmp_path: Path, content: bytes, detail: str
) -> None:
    binary = tmp_path / "agentd"
    binary.write_bytes(content)
    with pytest.raises(microvms.PreconditionError, match=detail):
        microvms.provision_agentd(state_dir=tmp_path, binary=binary)


def test_bind20_a_missing_caller_binary_is_refused(tmp_path: Path) -> None:
    with pytest.raises(microvms.PreconditionError, match="does not exist"):
        microvms.provision_agentd(state_dir=tmp_path, binary=tmp_path / "gone")


@pytest.mark.parametrize("version", ["../../etc", "v", "1.0/../x"])
def test_bind17_a_version_that_is_not_a_tag_is_an_invalid_argument(
    tmp_path: Path, version: str
) -> None:
    with pytest.raises(microvms.InvalidArgError):
        microvms.provision_agentd(version=version, state_dir=tmp_path)
