# SPDX-License-Identifier: Apache-2.0
"""The name registry the CLI shares, and records kept outside it — all without AWS.

A missing name and a foreign region are refused before any control-plane call; the live
half, a VM registered here and adopted by name in another process, is
`conformance/run_rs.py`'s `drive_find_by_name`.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import microvms

CANARY = "names-canary-token-7c1f"
ENDPOINT = "https://mvm-1.example.invalid"


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def record(name: str = "ci", microvm_id: str = "microvm-a") -> microvms.NameRecord:
    return microvms.NameRecord(
        name, microvm_id, ENDPOINT, CANARY, microvms.Region.us_east_1()
    )


def test_put_get_list_delete_and_release(tmp_path: Path) -> None:
    registry = microvms.NameRegistry(tmp_path)
    assert registry.get("ci") is None
    registry.put(record("ci"))
    registry.put(record("alias"))
    registry.put(record("other", "microvm-b"))
    found = registry.get("ci")
    assert found is not None and found.microvm_id == "microvm-a"
    assert found.agent_token == CANARY
    assert [r.name for r in registry.list()] == ["alias", "ci", "other"]
    assert registry.release_by_vm("microvm-a") == ["alias", "ci"]
    assert registry.delete("other") is True
    assert registry.delete("other") is False
    assert registry.list() == []
    assert registry.directory == str(tmp_path / "names")


def test_a_record_the_cli_wrote_resolves(tmp_path: Path) -> None:
    names = tmp_path / "names"
    names.mkdir()
    (names / "old.json").write_text(
        json.dumps(
            {
                "name": "old",
                "microvmId": "microvm-1",
                "endpoint": ENDPOINT,
                "agentToken": CANARY,
                "region": "us-east-1",
                "at": 1789000000,
                "egressPosture": "managed",
            },
            indent=2,
        )
    )
    found = microvms.NameRegistry(tmp_path).get("old")
    assert found is not None
    assert (found.microvm_id, found.at, found.egress_posture) == (
        "microvm-1",
        1789000000,
        "managed",
    )


def test_the_default_registry_is_the_clis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MICROVM_STATE_DIR", str(tmp_path))
    assert microvms.NameRegistry().directory == str(tmp_path / "names")


def test_repr_hides_the_token_and_to_dict_is_the_one_way_out(tmp_path: Path) -> None:
    original = record()
    assert CANARY not in repr(original)
    assert "<redacted>" in repr(original)
    assert CANARY not in repr(microvms.NameRegistry(tmp_path))
    as_dict = original.to_dict()
    assert as_dict["agentToken"] == CANARY
    assert as_dict["microvmId"] == "microvm-a"
    assert microvms.NameRecord.from_dict(as_dict) == original


def test_a_bad_record_is_refused_without_printing_the_token() -> None:
    with pytest.raises(microvms.InvalidArgError) as caught:
        microvms.NameRecord.from_dict(
            {
                "name": "ci",
                "microvmId": "microvm-a",
                "endpoint": ENDPOINT,
                "agentToken": CANARY,
                "region": "us-east-1",
                "at": "yesterday",
            }
        )
    assert CANARY not in str(caught.value)
    with pytest.raises(microvms.InvalidArgError, match="prefix"):
        microvms.NameRecord(
            "microvm-x", "id", ENDPOINT, CANARY, microvms.Region.us_east_1()
        )
    with pytest.raises(microvms.InvalidArgError, match="agentToken"):
        microvms.NameRecord("ci", "id", ENDPOINT, "", microvms.Region.us_east_1())


def test_a_torn_file_keeps_its_name_taken(tmp_path: Path) -> None:
    registry = microvms.NameRegistry(tmp_path)
    registry.put(record("good"))
    (tmp_path / "names" / "torn.json").write_text('{"name": "to')
    with pytest.raises(microvms.PreconditionError, match="torn.json"):
        registry.get("torn")
    assert [r.name for r in registry.list()] == ["good"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_record_file_is_owner_only(tmp_path: Path) -> None:
    registry = microvms.NameRegistry(tmp_path)
    registry.put(record())
    mode = stat.S_IMODE((tmp_path / "names" / "ci.json").stat().st_mode)
    assert mode == 0o600


def test_from_name_refuses_missing_and_foreign_names_before_any_call(
    tmp_path: Path,
) -> None:
    registry = microvms.NameRegistry(tmp_path)
    for from_name in (microvms.Sandbox.from_name, microvms.AgentVm.from_name):
        with pytest.raises(microvms.PreconditionError, match="no VM is named"):
            from_name(microvms.Region.us_east_1(), "ci", registry)
    registry.put(record())
    for from_name in (microvms.Sandbox.from_name, microvms.AgentVm.from_name):
        with pytest.raises(
            microvms.InvalidArgError, match="registered in us-east-1"
        ) as caught:
            from_name(microvms.Region.us_west_2(), "ci", registry)
        assert CANARY not in str(caught.value)


def test_an_unlaunched_sandbox_has_nothing_to_name(tmp_path: Path) -> None:
    sandbox = microvms.Sandbox(microvms.Region.us_east_1())
    with pytest.raises(microvms.PreconditionError, match="no VM to name"):
        microvms.NameRecord.for_sandbox("ci", sandbox)
    with pytest.raises(microvms.PreconditionError):
        microvms.NameRegistry(tmp_path).register("ci", sandbox)
