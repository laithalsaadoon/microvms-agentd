# SPDX-License-Identifier: Apache-2.0
"""Python's answers to the shared case corpus (`parity/cases/`, #272).

Each case is one test, named by its file. A case the capability table exempts Python from, or
one whose `skip` names Python, is skipped with its reason. The rules for reading and judging a
case are `parity/cases/README.md`'s, restated here the way the Rust runners' shared
`microvms-core/tests/parity_corpus/mod.rs` states them, so no runner relies on another.

Everything is offline. The image, launch and posture cases are refused or answered by core
before any AWS call (credentials come from the environment, which the default chain reads
without a network call), and the error cases talk to `SseServer` on loopback. HTTPS goes to a
proxy on a loopback port nothing listens on, so a refusal that regresses fails on a connection
error instead of sending a signed request to AWS.

The table is read with `tomllib`, so this file needs Python 3.11, the floor the repository's
scripts already declare; the wheel's own 3.9 floor is for consumers, not for this suite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import tomllib

import microvms
from conftest import SseServer

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "parity" / "cases"
TABLE = ROOT / "parity" / "capabilities.toml"
SURFACE = "py"
SURFACES = ("core", "cli", "py", "ts")
SENTINEL = "wrap-dockerfile/sentinel"
CASE_KEYS = {"capability", "input", "expect", "ignore", "known_drift", "skip"}
ISSUE = re.compile(r"#[1-9][0-9]*")
# A skip ends by naming the issue or trace id that holds the gap, such as `(IMAGE-12)`.
SKIP_REFERENCE = re.compile(r".*\((#[1-9][0-9]*|[A-Z]+-[1-9][0-9]*)\)", re.DOTALL)
BUILD_ROLE = "arn:aws:iam::123456789012:role/build"
IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:img"
MISSING = object()


@dataclass
class Case:
    id: str
    area: str
    capability: str
    input: dict[str, Any]
    expect: dict[str, Any]
    ignore: list[str] = field(default_factory=list)
    known_drift: dict[str, dict[str, Any]] = field(default_factory=dict)
    skip: dict[str, str] = field(default_factory=dict)

    def binary(self) -> bytes:
        return bytes.fromhex(self.input["binary_hex"])

    def size(self) -> microvms.SizeClass:
        return microvms.SizeClass.from_baseline_mib(self.input["size_mib"])


def read_table(path: Path) -> dict[str, dict[str, Any]]:
    """Each row's cells by surface: a name (string or list) or `{exempt, issue}`."""
    rows = tomllib.loads(path.read_bytes().decode("utf-8"))["capability"]
    return {row["id"]: {surface: row[surface] for surface in SURFACES} for row in rows}


def read_case(path: Path, area: str) -> Case:
    case_id = f"{area}/{path.stem}"
    raw = json.loads(path.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError(f"{case_id}: a case is a JSON object")
    unknown = set(raw) - CASE_KEYS
    if unknown:
        raise ValueError(f"{case_id}: unknown keys {sorted(unknown)}")
    if not isinstance(raw.get("capability"), str):
        raise ValueError(f"{case_id}: capability must be a string")
    for key in ("input", "expect"):
        if not isinstance(raw.get(key), dict):
            raise ValueError(f"{case_id}: {key} must be an object")
    ignore = raw.get("ignore", [])
    if not isinstance(ignore, list) or not all(
        isinstance(p, str) and p for p in ignore
    ):
        raise ValueError(f"{case_id}: ignore must be an array of paths")
    drifts = raw.get("known_drift", {})
    if not isinstance(drifts, dict):
        raise ValueError(f"{case_id}: known_drift must be an object")
    for surface, drift in drifts.items():
        if surface not in SURFACES:
            raise ValueError(f"{case_id}: known_drift names no surface {surface!r}")
        if not isinstance(drift, dict) or set(drift) != {"issue", "keys"}:
            raise ValueError(
                f"{case_id}: known_drift.{surface} has exactly issue and keys"
            )
        if not isinstance(drift["issue"], str) or not ISSUE.fullmatch(drift["issue"]):
            raise ValueError(f'{case_id}: known_drift.{surface}.issue must be "#N"')
        keys = drift["keys"]
        if (
            not isinstance(keys, list)
            or not keys
            or not all(isinstance(k, str) and k for k in keys)
        ):
            raise ValueError(
                f"{case_id}: known_drift.{surface}.keys must be a non-empty array"
            )
    skips = raw.get("skip", {})
    if not isinstance(skips, dict):
        raise ValueError(f"{case_id}: skip must be an object")
    for surface, reason in skips.items():
        if surface not in SURFACES:
            raise ValueError(f"{case_id}: skip names no surface {surface!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{case_id}: skip.{surface} needs a reason")
        if not SKIP_REFERENCE.fullmatch(reason):
            raise ValueError(
                f"{case_id}: skip.{surface} ends by naming its issue or trace id, as `(#N)` "
                "or `(IMAGE-12)`"
            )
        if surface in drifts:
            raise ValueError(f"{case_id}: {surface} is both skipped and known to drift")
    return Case(
        case_id,
        area,
        raw["capability"],
        raw["input"],
        raw["expect"],
        ignore,
        drifts,
        skips,
    )


def read_cases(directory: Path) -> tuple[list[Case], list[str]]:
    """Every `<area>/<case>.json` under `directory`, sorted, and a problem per stray file."""
    cases: list[Case] = []
    problems: list[str] = []
    if not directory.is_dir():
        return cases, [f"the corpus directory {directory} can't be read"]
    for area in sorted(directory.iterdir()):
        if area.is_file():
            if area.name != "README.md":
                problems.append(
                    f"{area}: only README.md may sit beside the area directories"
                )
            continue
        for path in sorted(area.iterdir()):
            if not (path.is_file() and path.suffix == ".json" and path.stem):
                problems.append(
                    f"{path}: a case is a .json file directly under its area"
                )
                continue
            try:
                cases.append(read_case(path, area.name))
            except ValueError as problem:
                problems.append(str(problem))
    return cases, problems


def decide(case: Case, row: dict[str, Any]) -> str | None:
    """`None` to run the case, or the reason to skip it. Raises on an inconsistent case."""
    cell = row[SURFACE]
    drift = case.known_drift.get(SURFACE)
    if isinstance(cell, dict):
        if drift is not None:
            # The table records the gap and the case measures it: run, and expect the drift.
            if cell.get("issue") == drift["issue"]:
                return None
            raise ValueError(
                f"{case.id}: known_drift.{SURFACE} names {drift['issue']}, but the table exempts "
                f"{SURFACE} from {case.capability!r} with issue {cell.get('issue')!r}; a drift "
                "on an exempt surface names the issue that closes the gap"
            )
        if SURFACE in case.skip:
            raise ValueError(
                f"{case.id}: skip.{SURFACE} repeats what the table already says"
            )
        return f"the table exempts {SURFACE} from {case.capability!r}: {cell['exempt']}"
    return case.skip.get(SURFACE)


# ── judging ──────────────────────────────────────────────────────────────────


def lookup(value: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def remove(value: Any, path: str) -> None:
    *parents, last = path.split(".")
    for key in parents:
        if not isinstance(value, dict) or key not in value:
            return
        value = value[key]
    if isinstance(value, dict):
        value.pop(last, None)


def same(left: Any, right: Any) -> bool:
    """JSON equality with numbers compared by value, and a boolean never equal to a number."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return float(left) == float(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same(left[k], right[k]) for k in left
        )
    return type(left) is type(right) and left == right


# The ids `judge` has seen, so the last test can fail on a planned case nothing compared.
JUDGED: set[str] = set()


def judge(case: Case, answer: dict[str, Any]) -> None:
    JUDGED.add(case.id)
    expect = json.loads(json.dumps(case.expect))
    actual = json.loads(json.dumps(answer))
    if isinstance(expect.get("error"), dict) and isinstance(actual.get("error"), dict):
        actual["error"] = {
            k: v for k, v in actual["error"].items() if k in expect["error"]
        }
    for path in case.ignore:
        remove(expect, path)
        remove(actual, path)
    problems = []
    drift = case.known_drift.get(SURFACE)
    if drift is not None:
        for key in drift["keys"]:
            got, want = lookup(actual, key), lookup(expect, key)
            both_missing = got is MISSING and want is MISSING
            if both_missing or (
                got is not MISSING and want is not MISSING and same(got, want)
            ):
                problems.append(
                    f"{case.id}: {SURFACE} now agrees at {key}; remove known_drift "
                    f"({SURFACE}, {key}, {drift['issue']})"
                )
        for key in drift["keys"]:
            remove(expect, key)
            remove(actual, key)
    if not same(actual, expect):
        problems.append(
            f"{case.id}: {SURFACE} answered\n  {json.dumps(actual, sort_keys=True)}\n"
            f"expected\n  {json.dumps(expect, sort_keys=True)}"
        )
    assert not problems, "\n".join(problems)


# ── Python's entry points, one handler per area ──────────────────────────────


def refusal(error: microvms.MicrovmError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "wire_kind": error.wire_kind,
            "retryable": error.retryable,
        }
    }


def answered(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return call()
    except microvms.MicrovmError as error:
        return refusal(error)


def image_name(case: Case, _servers: Callable[..., SseServer]) -> dict[str, Any]:
    if case.capability == "ensure-image":
        default = microvms.wrap_dockerfile(
            f"FROM {microvms.BaseImage.al2023().docker_ref}\n"
        )

        def ensure() -> dict[str, Any]:
            image = microvms.Sandbox(microvms.Region.us_east_1()).ensure_image(
                name_prefix=case.input["name_prefix"],
                binary=case.binary(),
                dockerfile=default,
                s3_bucket="parity-cases-bucket",
                build_role_arn=BUILD_ROLE,
                size=case.size(),
            )
            return {"built": repr(image)}

        return answered(ensure)
    if case.capability == "agent-image-name":
        return answered(
            lambda: {
                "name": microvms.AgentVm(
                    microvms.Region.us_east_1(),
                    [microvms.AgentSpec(agent) for agent in case.input["agents"]],
                ).image_name(
                    binary=case.binary(), build_role_arn=BUILD_ROLE, size=case.size()
                )
            }
        )
    raise AssertionError(
        f"{case.id}: no Python handler for {case.capability!r} in image-name"
    )


def cost(case: Case, _servers: Callable[..., SseServer]) -> dict[str, Any]:
    assert case.capability == "estimate", case.id
    # `input.defaults` stays out: the binding's own defaults for `launched` and the label are
    # what the case asks about.
    return answered(
        lambda: microvms.estimate_run(
            case.size(),
            running_seconds=case.input["running_seconds"],
            suspended_seconds=case.input["suspended_seconds"],
            suspend_resume_cycles=case.input["suspend_resume_cycles"],
        ).to_dict()
    )


def daemon_status(case: Case, servers: Callable[..., SseServer]) -> dict[str, Any]:
    server = servers([[case.input["body"].encode()]], case.input["status"])
    session = microvms.Session.direct(server.endpoint, "agent-token")

    def call() -> dict[str, Any]:
        if case.capability == "health":
            session.health()
        elif case.capability == "upload-file":
            session.upload_file(case.input["path"], b"parity")
        else:
            raise AssertionError(
                f"{case.id}: no Python handler for {case.capability!r} in error"
            )
        return {"ok": True}

    return answered(call)


def egress(case: Case, _servers: Callable[..., SseServer]) -> dict[str, Any]:
    options = case.input
    if case.capability == "launch":
        return answered(
            lambda: {
                "launched": repr(
                    microvms.Sandbox(microvms.Region.us_east_1()).run(
                        image_identifier=IMAGE_ARN,
                        egress=options["egress"],
                        egress_network_connectors=options["egress_network_connectors"],
                        deny_egress=options["deny_egress"],
                    )
                )
            }
        )
    if case.capability == "egress-posture-for":
        return answered(
            lambda: {
                "posture": microvms.egress_posture_for(
                    options["egress"],
                    options["egress_network_connectors"],
                    options["deny_egress"],
                )
            }
        )
    raise AssertionError(
        f"{case.id}: no Python handler for {case.capability!r} in egress"
    )


def size_class(case: Case, _servers: Callable[..., SseServer]) -> dict[str, Any]:
    return answered(
        lambda: {
            "baseline_mib": microvms.SizeClass.from_request(
                cpus=case.input["cpus"], memory_mib=case.input["memory_mib"]
            ).baseline_mib
        }
    )


def wrap(case: Case, _servers: Callable[..., SseServer]) -> dict[str, Any]:
    return answered(
        lambda: {"dockerfile": microvms.wrap_dockerfile(case.input["task"])}
    )


HANDLERS: dict[str, Callable[[Case, Callable[..., SseServer]], dict[str, Any]]] = {
    "image-name": image_name,
    "cost": cost,
    "error": daemon_status,
    "egress": egress,
    "size-class": size_class,
    "wrap-dockerfile": wrap,
}


# ── the plan, made at collection ─────────────────────────────────────────────


@dataclass
class Plan:
    run: list[Case] = field(default_factory=list)
    skipped: list[tuple[Case, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    loaded: set[str] = field(default_factory=set)


def plan(directory: Path = CASES, table_path: Path = TABLE) -> Plan:
    table = read_table(table_path)
    cases, problems = read_cases(directory)
    result = Plan(problems=problems)
    for case in cases:
        result.loaded.add(case.id)
        row = table.get(case.capability)
        if row is None:
            result.problems.append(
                f"{case.id}: capability {case.capability!r} names no row in "
                "parity/capabilities.toml"
            )
            continue
        try:
            reason = decide(case, row)
        except ValueError as problem:
            result.problems.append(str(problem))
            continue
        if reason is not None:
            result.skipped.append((case, reason))
        elif case.area in HANDLERS:
            result.run.append(case)
        else:
            result.problems.append(
                f"{case.id}: the {SURFACE} runner has no handler for area {case.area!r}, and "
                f"the table doesn't exempt {SURFACE} from {case.capability!r}"
            )
    return result


PLAN = plan()


def floor_problems(result: Plan) -> list[str]:
    """An owned area with nothing to run, or a sentinel that isn't there or won't run."""
    problems = [
        f"no case runs in area {area!r} on {SURFACE}: the corpus is empty or unread there"
        for area in HANDLERS
        if not any(case.area == area for case in result.run)
    ]
    if SENTINEL not in result.loaded:
        problems.append(f"the sentinel {SENTINEL}.json wasn't loaded")
    elif not any(case.id == SENTINEL for case in result.run):
        problems.append(f"the sentinel {SENTINEL}.json doesn't run on {SURFACE}")
    return problems


@pytest.fixture(autouse=True)
def offline_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials the default chain reads from the environment, with no network call."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    for name in (
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def test_the_corpus_plans_cleanly() -> None:
    """Every case file reads, names a row, and lands on a handler or a stated skip."""
    assert not PLAN.problems, "\n".join(PLAN.problems)


def test_every_area_and_the_sentinel_run() -> None:
    """A wrong path or a loader that finds nothing fails here, not as zero tests."""
    problems = floor_problems(PLAN)
    assert not problems, "\n".join(problems)


def _params() -> Iterator[Any]:
    for case in PLAN.run:
        yield pytest.param(case, id=case.id)
    for case, reason in PLAN.skipped:
        yield pytest.param(case, id=case.id, marks=pytest.mark.skip(reason=reason))


@pytest.mark.parametrize("case", list(_params()))
def test_a_case(case: Case, sse_server: Callable[..., SseServer]) -> None:
    judge(case, HANDLERS[case.area](case, sse_server))


def test_every_planned_case_was_judged(request: pytest.FixtureRequest) -> None:
    """The floors above count what the plan holds; this counts what was compared.

    A skip mark or a handler that returns before `judge` would otherwise leave every case
    skipped and the file green. Only the cases this session selected are held, so a run
    filtered to a few cases still passes. It's the file's last test, so it runs after them.
    """
    planned = {case.id for case in PLAN.run}
    selected = {
        item.callspec.params["case"].id
        for item in request.session.items
        if getattr(item, "originalname", None) == "test_a_case"
        and item.module is request.module
        and item.callspec.params["case"].id in planned
    }
    unjudged = sorted(selected - JUDGED)
    assert not unjudged, "\n".join(
        f"{case_id}: planned for {SURFACE} but never judged" for case_id in unjudged
    )
