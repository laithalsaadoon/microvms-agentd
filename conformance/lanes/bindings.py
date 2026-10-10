# SPDX-License-Identifier: Apache-2.0
"""The Python and Node bindings' tunnel, port-forward and import handles on the kept VM (#263,
#270).

`drive_serve` proves core's loops through the real proxy; this proves the bindings' handles
over them, which their own suites drive only against local stand-ins. Each binding is built from
the working tree once, the way CI's `bindings` job builds it (maturin into a wheel installed in
a venv of its own, and napi into a scratch directory, so the tree's `index.d.ts` is left alone),
in a thread started before the suite's VM launches, so the build overlaps the image build.
Then a driver per binding (`conformance/drivers/`) attaches to the kept VM through the
binding's public API alone and opens each handle, the suite makes its own `GET /v1/schema`
through the handle's local address with httpx, and the handle's report is read after `stop()`.
The import is the CLI's `attach` rule through the binding: the kept VM's own record registers,
and a record whose token the daemon refuses writes nothing.

It launches nothing and terminates nothing. Each name registry is a temporary directory of the
suite's, and every driver process is stopped before the section returns.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from harness.cli import Cli
from harness.constants import AGENT_PORT, REPO
from harness.envelope import Envelope
from harness.redact import command_for_log
from harness.results import Results

#: The maturin CI's `bindings:py` task builds with, and the napi CLI the `dts` task builds with.
#: The self-test holds both to those tasks, so this build is the one CI tests offline.
MATURIN = "maturin@1.14.1"
NAPI_CLI = "@napi-rs/cli@3"

DRIVERS = REPO / "conformance" / "drivers"

#: From an empty target directory on a 16-core host at 0552d8c, the Python build took 129 s and
#: the Node build after it 92 s; a 4-core runner takes a few times that. This bounds a hung step.
BUILD_TIMEOUT_SEC = 30 * 60
#: Until the driver answers its request: an attach, a proxy-token mint and a bind.
EVENT_TIMEOUT_SEC = 180
#: The request through the handle, which crosses the real endpoint proxy.
FETCH_TIMEOUT_SEC = 60
#: `stop(timeout=...)`: connections still open after this are cut and listed as failed.
STOP_GRACE_SEC = 30

BINDINGS = ("python", "node")


class DriverError(Exception):
    """A driver that answered with something other than its protocol, or not at all."""


@dataclass
class Built:
    """What the build left: each binding's driver command, or why it has none."""

    commands: dict[str, list[str]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)


def build_steps(workdir: Path) -> dict[str, list[list[str]]]:
    """Each binding's build, in order, into `workdir`; the tree is only read."""
    wheels, venv, addon = workdir / "wheel", workdir / "venv", workdir / "addon"
    return {
        "python": [
            [
                "uvx",
                MATURIN,
                "build",
                "-m",
                "bindings/microvms-py/Cargo.toml",
                "-o",
                str(wheels),
            ],
            ["uv", "venv", "--clear", "-q", str(venv)],
            [
                "uv",
                "pip",
                "install",
                "-q",
                "--python",
                str(venv / "bin" / "python"),
                "--no-index",
                "--find-links",
                str(wheels),
                "microvms",
            ],
        ],
        "node": [
            [
                "npx",
                "-y",
                "-p",
                NAPI_CLI,
                "napi",
                "build",
                "--manifest-path",
                "Cargo.toml",
                "--package",
                "microvms-js",
                "--platform",
                "--output-dir",
                str(addon),
                "--cwd",
                "bindings/microvms-js",
            ]
        ],
    }


def driver_commands(workdir: Path) -> dict[str, list[str]]:
    """The command that runs each binding's driver once `build_steps` succeeded.

    `-I` keeps the venv's interpreter off the working directory and the `PYTHON*` variables, so
    the `microvms` it imports is the one the wheel installed.
    """
    return {
        "python": [
            str(workdir / "venv" / "bin" / "python"),
            "-I",
            str(DRIVERS / "handles.py"),
        ],
        "node": [
            "node",
            str(DRIVERS / "handles.mjs"),
            str(workdir / "addon" / "index.js"),
        ],
    }


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_step(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=BUILD_TIMEOUT_SEC,
        check=False,
    )


def build_bindings(workdir: Path, log: list[str], run: Runner = run_step) -> Built:
    """Builds each binding's steps in order; a failed step ends that binding's build only.

    A failure is the step and the tail of its output, since a compile error prints last.
    """
    built = Built()
    commands = driver_commands(workdir)
    for binding, steps in build_steps(workdir).items():
        started = time.monotonic()
        for step in steps:
            log.append(command_for_log(step))
            try:
                done = run(step)
            except (OSError, subprocess.TimeoutExpired) as error:
                built.failures[binding] = f"{step[0]} {step[1]}: {error!r}"[:600]
                break
            if done.returncode != 0:
                tail = (done.stderr or done.stdout or "").strip()[-400:]
                built.failures[binding] = (
                    f"{step[0]} {step[1]} exited {done.returncode}: {tail}"
                )
                break
        else:
            built.commands[binding] = commands[binding]
        built.seconds[binding] = round(time.monotonic() - started, 1)
    return built


class BindingBuild:
    """`build_bindings` on a thread of its own, started early and joined where it's needed."""

    def __init__(self, workdir: Path, log: list[str]) -> None:
        self._workdir = workdir
        self._log = log
        self._built: Built | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self._built = build_bindings(self._workdir, self._log)
        except Exception as error:  # noqa: BLE001 - a raise here is the build's failure
            self._built = Built(failures=dict.fromkeys(BINDINGS, repr(error)[:600]))

    def start(self) -> BindingBuild:
        self._thread.start()
        return self

    def wait(self, timeout: float) -> Built:
        self._thread.join(timeout)
        if self._built is None:
            return Built(
                failures=dict.fromkeys(BINDINGS, f"still building after {timeout:.0f}s")
            )
        return self._built


def parse_event(line: str) -> dict[str, Any]:
    """One line of a driver's stdout as its event, or a `DriverError` naming the line."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        event = None
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        raise DriverError(f"not a driver event: {line[:200]!r}")
    return event


class Driver:
    """One driver process: requests on its stdin, events off its stdout, both as lines."""

    def __init__(
        self, command: Sequence[str], env: dict[str, str] | None = None
    ) -> None:
        self._process = subprocess.Popen(
            list(command),
            cwd=REPO,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _pump(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _drain(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line)

    def send(self, line: str) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(line + "\n")
        self._process.stdin.flush()

    def event(self, timeout: float) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise DriverError(f"no event within {timeout:.0f}s") from None
        if line is None:
            raise DriverError("the driver ended before it answered")
        return parse_event(line.strip())

    def close(self, timeout: float = 30) -> int:
        """Closes stdin and waits for the process, killing it past `timeout`."""
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.kill()
            return self._process.wait()

    def stderr_tail(self) -> str:
        return "".join(self._stderr).strip()[-300:]


def refused_event(event: dict[str, Any]) -> str:
    return f"driver {event.get('event')}: {event.get('code')} {event.get('message')}"


Fetch = Callable[[str], "tuple[int, str]"]


def fetch_schema(url: str) -> tuple[int, str]:
    """`GET` with httpx, so the client on this side of the handle is not the code under test."""
    answer = httpx.get(url, timeout=FETCH_TIMEOUT_SEC, headers={"Connection": "close"})
    return answer.status_code, answer.text


@dataclass
class ServeOutcome:
    """A handle's run: what the suite's request read through it, and its report."""

    status: int | None = None
    body: str | None = None
    report: dict[str, Any] | None = None
    running: bool | None = None
    failure: str = ""


def serve_through(
    command: Sequence[str],
    request: dict[str, Any],
    env: dict[str, str] | None = None,
    fetch: Fetch = fetch_schema,
) -> ServeOutcome:
    """Opens a handle through the driver, fetches `/v1/schema` through it, and stops it."""
    outcome = ServeOutcome()
    driver = Driver(command, env)
    try:
        driver.send(json.dumps(request))
        listening = driver.event(EVENT_TIMEOUT_SEC)
        if listening["event"] != "listening":
            outcome.failure = refused_event(listening)
            return outcome
        try:
            outcome.status, outcome.body = fetch(
                f"http://{listening.get('localAddress')}/v1/schema"
            )
        except Exception as error:  # noqa: BLE001 - a failed fetch is the finding
            outcome.failure = f"the fetch through the handle failed: {error!r}"[:300]
        driver.send("stop")
        stopped = driver.event(STOP_GRACE_SEC + EVENT_TIMEOUT_SEC)
        if stopped["event"] != "stopped":
            outcome.failure = refused_event(stopped)
            return outcome
        outcome.report = stopped.get("report")
        outcome.running = stopped.get("running")
    except (DriverError, OSError) as error:
        outcome.failure = str(error)
    finally:
        code = driver.close()
        if outcome.failure:
            outcome.failure += f" (exit={code}; stderr: {driver.stderr_tail()})"
    return outcome


def import_through(
    command: Sequence[str],
    request: dict[str, Any],
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """The driver's `imported` event, or `None` and why there is none."""
    driver = Driver(command, env)
    failure = ""
    event = None
    try:
        driver.send(json.dumps(request))
        answer = driver.event(EVENT_TIMEOUT_SEC)
        if answer["event"] == "imported":
            event = answer
        else:
            failure = refused_event(answer)
    except (DriverError, OSError) as error:
        failure = str(error)
    finally:
        code = driver.close()
        if failure:
            failure += f" (exit={code}; stderr: {driver.stderr_tail()})"
    return event, failure


def attach_spec(launched: Envelope, region: str, token: str | None = None) -> dict:
    """The driver's `session`: the kept VM, attached with its token or `token`."""
    return {
        "attach": {
            "region": region,
            "microvmId": str(launched.data["microvmId"]),
            "endpoint": str(launched.data["endpoint"]),
            "agentToken": token or str(launched.data["agentToken"]),
        }
    }


def import_request(
    session: dict[str, Any], name: str, state_dir: Path, times: int
) -> dict[str, Any]:
    """An import of the record `session` was attached from, under `name`.

    The record's endpoint and token are the session's own, which is what `names::import`
    requires: a probe proves the token it was sent with.
    """
    attach = session["attach"] if "attach" in session else session["direct"]
    return {
        "op": "import",
        "session": session,
        "record": {
            "name": name,
            "microvmId": attach.get("microvmId", "microvm-direct"),
            "endpoint": attach["endpoint"],
            "agentToken": attach["agentToken"],
            "region": attach.get("region", "us-east-1"),
        },
        "stateDir": str(state_dir),
        "times": times,
    }


def serve_request(
    kind: str, session: dict[str, Any], guest_port: int = AGENT_PORT
) -> dict[str, Any]:
    """A handle to the daemon's own port, whose `GET /v1/schema` every image answers."""
    return {
        "op": "serve",
        "kind": kind,
        "session": session,
        "guestPort": guest_port,
        "maxConnections": 1,
        "stopTimeout": STOP_GRACE_SEC,
    }


@dataclass
class Verdict:
    """One check before it is named for its binding: `absent` checks `actual` is None."""

    name: str
    actual: Any
    expected: Any = None
    absent: bool = False
    detail: str = ""


def _when(value: Any, test: Callable[[Any], bool]) -> bool | None:
    """`test(value)`, or None when the value is absent, so `Results.eq` fails on it."""
    return None if value is None else test(value)


def build_verdicts(built: Built, binding: str) -> list[Verdict]:
    return [
        Verdict(
            "build from the working tree succeeds",
            binding in built.commands,
            True,
            detail=built.failures.get(binding, f"{built.seconds.get(binding)}s"),
        )
    ]


def serve_verdicts(handle: str, outcome: ServeOutcome) -> list[Verdict]:
    """A handle with a limit of one that served the suite's one request and stopped there."""
    report = outcome.report or {}
    failure = outcome.failure
    return [
        Verdict(
            f"{handle} answers GET /v1/schema with 200 through the real proxy",
            outcome.status,
            200,
            detail=failure,
        ),
        Verdict(
            f"{handle} relays the daemon's schema",
            _when(outcome.body, lambda body: "protocol_version" in body),
            True,
            detail=failure,
        ),
        Verdict(
            f"{handle}'s report counts the one connection served",
            report.get("served"),
            1,
        ),
        Verdict(f"{handle}'s report counts no refusal", report.get("refused"), 0),
        Verdict(
            f"{handle} stopped at its limit of one connection",
            report.get("stopped"),
            "limit",
        ),
        Verdict(
            f"{handle} minted its proxy token through the control plane",
            _when(report.get("proxyTokenMints"), lambda mints: mints >= 1),
            True,
        ),
        Verdict(
            f"{handle} lists no connection that ended unclean",
            report.get("ended"),
            [],
        ),
        Verdict(f"{handle} is not running after stop()", outcome.running, False),
    ]


def import_verdicts(
    imported: dict[str, Any] | None,
    refused: dict[str, Any] | None,
    request: dict[str, Any],
    failures: tuple[str, str] = ("", ""),
) -> list[Verdict]:
    """The kept VM's record imported twice, then a refused token's record, each in a registry
    of its own."""
    imported, refused = imported or {}, refused or {}
    record = request["record"]
    replaced = imported.get("replaced") or []
    found = imported.get("found") or {}
    error = refused.get("error") or {}
    return [
        Verdict(
            "import of the kept VM's record reports a name it didn't hold (replaced false)",
            replaced[0] if replaced else None,
            False,
            detail=failures[0],
        ),
        Verdict(
            "import of the same record again reports a refresh (replaced true)",
            replaced[1] if len(replaced) > 1 else None,
            True,
            detail=failures[0],
        ),
        Verdict(
            "import of the kept VM's record raises nothing",
            imported.get("error") if imported else "no import ran",
            absent=True,
        ),
        Verdict(
            "get finds the imported record under the VM's id",
            found.get("microvmId"),
            record["microvmId"],
        ),
        Verdict(
            "get's imported record carries the VM's endpoint and the record's token",
            _when(
                found.get("endpoint"),
                lambda endpoint: (
                    endpoint == record["endpoint"] and found.get("tokenMatches") is True
                ),
            ),
            True,
        ),
        Verdict(
            "list shows the imported name",
            _when(imported.get("listed"), lambda listed: record["name"] in listed),
            True,
        ),
        Verdict(
            "import of a record whose token the daemon refuses raises ERR_CREDENTIALS",
            error.get("code"),
            "ERR_CREDENTIALS",
            detail=failures[1] or str(error.get("message") or ""),
        ),
        Verdict(
            "an import the daemon refused writes no record under its name",
            refused.get("found") if refused else "no import ran",
            absent=True,
        ),
        Verdict(
            "an import the daemon refused leaves the registry empty",
            refused.get("listed"),
            [],
        ),
    ]


def explain(verdict: Verdict, ok: bool) -> None:
    """A failed check's cause, under its line: the driver's error or the build's tail."""
    if not ok and verdict.detail:
        print(f"        why: {verdict.detail[:300]}")


def record_python(results: Results, verdicts: list[Verdict]) -> None:
    """The Python binding's checks: PyO3's wrapper driving core (BIND-3)."""
    for verdict in verdicts:
        if verdict.absent:
            ok = results.absent(
                f"BIND-3 the Python binding's {verdict.name}", verdict.actual
            )
        else:
            ok = results.eq(
                f"BIND-3 the Python binding's {verdict.name}",
                verdict.actual,
                verdict.expected,
            )
        explain(verdict, ok)


def record_node(results: Results, verdicts: list[Verdict]) -> None:
    """The Node binding's checks: napi-rs's wrapper driving core (BIND-4)."""
    for verdict in verdicts:
        if verdict.absent:
            ok = results.absent(
                f"BIND-4 the Node binding's {verdict.name}", verdict.actual
            )
        else:
            ok = results.eq(
                f"BIND-4 the Node binding's {verdict.name}",
                verdict.actual,
                verdict.expected,
            )
        explain(verdict, ok)


RECORDERS = {"python": record_python, "node": record_node}


def binding_verdicts(
    command: list[str] | None,
    sessions: tuple[dict[str, Any], dict[str, Any]],
    names_dir: Path,
    log: list[str],
    env: dict[str, str] | None = None,
    guest_port: int = AGENT_PORT,
) -> list[Verdict]:
    """One binding's handles and imports through its driver, as verdicts.

    `sessions` is the VM's own session and one carrying a token its daemon refuses. With no
    `command` (the build failed) nothing runs and every verdict reads absent, so the report
    keeps the same check names and each one fails.
    """
    session, refusing = sessions
    name = f"conformance-bind-{secrets.token_hex(4)}"
    imported_request = import_request(session, name, names_dir / "imported", 2)
    refused_request = import_request(refusing, name, names_dir / "refused", 1)
    verdicts: list[Verdict] = []
    for kind, handle in (
        ("tunnel", "tunnel handle"),
        ("forward", "port-forward handle"),
    ):
        outcome = ServeOutcome(failure="not built")
        if command is not None:
            log.append(command_for_log(command) + f"  # {kind} request on stdin")
            outcome = serve_through(
                command, serve_request(kind, session, guest_port), env
            )
        verdicts += serve_verdicts(handle, outcome)
    imported = refused = None
    failures = ("not built", "not built")
    if command is not None:
        log.append(command_for_log(command) + "  # import requests on stdin")
        imported, imported_failure = import_through(command, imported_request, env)
        refused, refused_failure = import_through(command, refused_request, env)
        failures = (imported_failure, refused_failure)
    verdicts += import_verdicts(imported, refused, imported_request, failures)
    return verdicts


def drive_binding_handles(
    cli: Cli,
    launched: Envelope,
    build: BindingBuild,
    names_dir: Path,
    results: Results,
) -> None:
    """Both bindings' tunnel, port-forward and import handles against the kept VM (#263, #270)."""
    print("\n-- binding handles (#263, #270: tunnel, port-forward and import) --")
    built = build.wait(BUILD_TIMEOUT_SEC)
    for binding in BINDINGS:
        took = built.seconds.get(binding)
        state = "built" if binding in built.commands else "not built"
        print(f"  {binding} binding: {state}" + (f" in {took}s" if took else ""))
    env = os.environ.copy()
    env["AWS_REGION"] = cli.region
    session = attach_spec(launched, cli.region)
    refusing = attach_spec(
        launched, cli.region, f"conformance-refused-{secrets.token_hex(16)}"
    )
    for binding in BINDINGS:
        verdicts = build_verdicts(built, binding)
        verdicts += binding_verdicts(
            built.commands.get(binding),
            (session, refusing),
            names_dir / binding,
            cli.log,
            env,
        )
        RECORDERS[binding](results, verdicts)
