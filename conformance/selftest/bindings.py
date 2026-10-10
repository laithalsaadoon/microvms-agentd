# SPDX-License-Identifier: Apache-2.0
"""The binding-handles lane offline: its pins, its driver protocol, and its verdicts.

A stand-in driver speaks the protocol of `conformance/drivers/handles.py` over a real
subprocess and serves `/v1/schema` from a loopback HTTP server, so the section's own control
flow, the suite's httpx fetch included, runs end to end without a binding or an account.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

from harness.constants import REPO
from harness.results import Results
from lanes.bindings import (
    MATURIN,
    NAPI_CLI,
    Built,
    DriverError,
    ServeOutcome,
    binding_verdicts,
    build_bindings,
    build_verdicts,
    import_request,
    import_verdicts,
    parse_event,
    record_node,
    record_python,
    serve_through,
    serve_verdicts,
)

#: A driver that answers as the bindings' do. `FAKE_MODE` turns it into a broken one:
#: `garbage` prints a line that isn't an event, `exit` ends before it answers.
FAKE_DRIVER = r"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)

mode = os.environ.get("FAKE_MODE", "")
request = json.loads(sys.stdin.readline())
if mode == "garbage":
    print("Traceback (most recent call last):", flush=True)
    sys.exit(1)
if mode == "exit":
    sys.exit(3)
if request["op"] == "serve":
    class Schema(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"protocol_version": 3}' if self.path == "/v1/schema" else b"no"
            self.send_response(200 if self.path == "/v1/schema" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Schema)
    threading.Thread(target=server.handle_request, daemon=True).start()
    emit("listening", localAddress="127.0.0.1:%d" % server.server_address[1], running=True)
    sys.stdin.readline()
    emit("stopped", running=False, report={
        "served": 1, "refused": 0, "proxyTokenMints": 1, "stopped": "limit", "ended": []})
else:
    record = request["record"]
    if record["agentToken"].startswith("refused"):
        emit("imported", replaced=[], found=None, listed=[],
             error={"code": "ERR_CREDENTIALS", "message": "401"})
    else:
        emit("imported", replaced=[False, True][: request["times"]], error=None,
             listed=[record["name"]],
             found={"name": record["name"], "microvmId": record["microvmId"],
                    "endpoint": record["endpoint"], "region": record["region"],
                    "tokenMatches": True})
"""

TOKEN = "selftest-agent-token-canary"


def fake_driver() -> list[str]:
    return [sys.executable, "-I", "-c", FAKE_DRIVER]


def fake_env(mode: str = "") -> dict[str, str]:
    return {**os.environ, "FAKE_MODE": mode}


def sessions() -> tuple[dict, dict]:
    def attach(token: str) -> dict:
        return {
            "attach": {
                "region": "us-east-1",
                "microvmId": "microvm-selftest",
                "endpoint": "https://selftest.invalid",
                "agentToken": token,
            }
        }

    return attach(TOKEN), attach("refused-" + TOKEN)


def check_pins(results: Results) -> None:
    """The lane builds with the maturin CI's `bindings:py` installs and the napi CLI `dts`
    builds with, so its build is the one CI's `bindings` job tests offline."""
    tasks = REPO / ".config" / "mise" / "tasks"
    python = tomllib.loads((tasks / "test.toml").read_text())["bindings:py"]["run"]
    node = tomllib.loads((tasks / "contracts.toml").read_text())["dts"]["run"]
    results.eq(
        "the binding lane's maturin pin is the bindings:py task's",
        f"uvx {MATURIN} develop" in python,
        True,
    )
    results.eq(
        "the binding lane's napi CLI is the dts task's",
        f"npx -y -p {NAPI_CLI} napi build" in node,
        True,
    )


def check_parse_event(results: Results) -> None:
    """A driver line is an object with a string `event`, and anything else names the line."""
    results.eq(
        "a driver event parses",
        parse_event('{"event": "listening", "localAddress": "127.0.0.1:1"}').get(
            "localAddress"
        ),
        "127.0.0.1:1",
    )
    refused = []
    for line in ("Traceback (most recent call last):", '["event"]', '{"code": 1}'):
        try:
            parse_event(line)
        except DriverError as error:
            refused.append(line[:10] in str(error))
    results.eq(
        "a driver line that isn't an event is refused by name",
        refused,
        [True, True, True],
    )


def check_build(results: Results) -> None:
    """A failed step ends its binding's build with that step named, and the other builds."""
    ran: list[list[str]] = []

    def run(step: list[str]) -> subprocess.CompletedProcess[str]:
        ran.append(step)
        failed = step[:2] == ["uvx", MATURIN]
        return subprocess.CompletedProcess(
            step, 1 if failed else 0, "", "error[E0425]" if failed else ""
        )

    log: list[str] = []
    built = build_bindings(Path("work"), log, run)
    results.eq(
        "a failed maturin build ends the Python build and leaves the Node build",
        (sorted(built.commands), [step[0] for step in ran]),
        (["node"], ["uvx", "npx"]),
    )
    results.eq(
        "a failed build names its step and keeps the compiler's tail",
        "uvx maturin@" in built.failures.get("python", "")
        and "E0425" in built.failures.get("python", ""),
        True,
    )


def check_serve_through(results: Results) -> None:
    """The protocol over a real subprocess: the suite's own GET through the address the driver
    names, then `stop`, then the report; a broken driver is a failure that says how."""
    request = {"op": "serve", "kind": "tunnel", "session": sessions()[0]}
    outcome = serve_through(fake_driver(), request, fake_env())
    results.eq(
        "the binding lane fetches /v1/schema through the address the driver names",
        (outcome.status, outcome.body),
        (200, '{"protocol_version": 3}'),
    )
    results.eq(
        "the binding lane reads the report the driver prints after stop",
        (outcome.report or {}).get("served"),
        1,
    )
    garbage = serve_through(fake_driver(), request, fake_env("garbage"))
    ended = serve_through(fake_driver(), request, fake_env("exit"))
    results.eq(
        "a driver that prints no event or ends early fails the run and says which",
        (
            "not a driver event" in garbage.failure,
            garbage.report,
            "ended before it answered" in ended.failure and "exit=3" in ended.failure,
        ),
        (True, None, True),
    )


def check_verdicts(results: Results) -> None:
    """The section end to end against the stand-in passes every check under its key, and
    each wrong or missing value fails."""
    log: list[str] = []
    with tempfile.TemporaryDirectory() as names:
        verdicts = binding_verdicts(
            fake_driver(), sessions(), Path(names), log, fake_env()
        )
    probe = Results(probe=True)
    # The probes' own lines would read like live checks in this report, so they stay quiet.
    with redirect_stdout(io.StringIO()):
        record_python(probe, verdicts)
        record_node(probe, verdicts)
    results.eq(
        "every binding-handle check passes against a driver that answers as the bindings do",
        probe.failed,
        [],
    )
    results.eq(
        "the binding-handle checks carry BIND-3 for Python and BIND-4 for Node",
        sorted({name.split(" the ")[0] for name in probe.passed}),
        ["BIND-3", "BIND-4"],
    )
    results.eq(
        "the binding lane's log never carries an agent token",
        any(TOKEN in line for line in log),
        False,
    )

    request = import_request(sessions()[1], "ci", Path("names"), 1)
    results.eq(
        "an import's record is the session's own endpoint and token",
        (request["record"]["endpoint"], request["record"]["agentToken"]),
        ("https://selftest.invalid", "refused-" + TOKEN),
    )

    wrong = Results(probe=True)
    with redirect_stdout(io.StringIO()):
        record_python(wrong, serve_verdicts("tunnel handle", ServeOutcome()))
        record_python(
            wrong,
            serve_verdicts(
                "tunnel handle",
                ServeOutcome(
                    status=502,
                    body="bad gateway",
                    running=True,
                    report={
                        "served": 2,
                        "refused": 1,
                        "proxyTokenMints": 0,
                        "stopped": "stopped",
                        "ended": [{"kind": "failed"}],
                    },
                ),
            ),
        )
        request = import_request(sessions()[0], "ci", Path("names"), 2)
        wrote = {
            "replaced": [True, False],
            "error": {"code": "ERR_PROTOCOL", "message": "404"},
            "found": {"name": "ci", "microvmId": "other", "endpoint": "x"},
            "listed": ["other"],
        }
        record_python(wrong, build_verdicts(Built(), "python"))
        record_python(wrong, import_verdicts(None, None, request))
        record_python(wrong, import_verdicts(wrote, wrote, request))
    results.eq(
        "every binding-handle check fails on an absent value and on a wrong one",
        (len(wrong.failed), len(wrong.passed)),
        (1 + 2 * (8 + 9), 0),
    )


def check_binding_handles(results: Results) -> None:
    check_pins(results)
    check_parse_event(results)
    check_build(results)
    check_serve_through(results)
    check_verdicts(results)
