# SPDX-License-Identifier: Apache-2.0
"""Negative twins for the delayed-ready experiment; no AWS requests or real sleeps."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.constants import AGENT_PORT, REPO
from harness.results import Results
from lanes.delayed_readiness import delayed_recipe, measurement_outcome, timing_records


def check_delayed_readiness(results: Results) -> None:
    success = {
        "requestedTimeout": 600,
        "echoedTimeouts": [600],
        "state": "CREATED",
        "records": [{"event": "begin"}, {"event": "end", "elapsed": 310.5}],
    }
    capped = {
        "requestedTimeout": 600,
        "echoedTimeouts": [600],
        "state": "FAILED",
        "records": [{"event": "begin"}],
        "reasons": ["Ready hook invocation timed out after PT5M"],
    }
    results.eq(
        "delayed readiness recognizes a completed hook beyond five minutes",
        measurement_outcome(success),
        "completed beyond five minutes",
    )
    results.eq(
        "delayed readiness recognizes a measured service cap",
        measurement_outcome(capped),
        "service capped the ready hook at five minutes",
    )
    results.eq(
        "delayed readiness recognizes a failed image creation",
        measurement_outcome({**capped, "state": "CREATE_FAILED"}),
        "service capped the ready hook at five minutes",
    )
    results.eq(
        "delayed readiness recognizes a service cap with a late end marker",
        measurement_outcome({**capped, "records": success["records"]}),
        "service capped the ready hook at five minutes",
    )
    for original in (success, capped):
        for field in original:
            broken = copy.deepcopy(original)
            del broken[field]
            results.absent(
                f"delayed readiness refuses absent {field} from {original['state']}",
                measurement_outcome(broken),
            )
    for elapsed in (0, 300, 309.99, 600, True, "310", None):
        for state in ("CREATED", "FAILED", "CREATE_FAILED"):
            broken = copy.deepcopy({**success, **capped, "records": success["records"]})
            broken["state"] = state
            broken["records"][1]["elapsed"] = elapsed
            results.absent(
                f"delayed readiness refuses invalid elapsed {elapsed!r} from {state}",
                measurement_outcome(broken),
            )
    for field, value in (
        ("state", "CREATING"),
        ("requestedTimeout", 300),
        ("reasons", ["unrelated failure"]),
        ("records", [{"event": "end", "elapsed": 310}]),
    ):
        broken = {**capped, field: value}
        results.absent(
            f"delayed readiness refuses inconclusive {field}",
            measurement_outcome(broken),
        )
    results.eq(
        "delayed readiness log parser ignores unrelated and malformed records",
        timing_records(
            [
                'private unrelated line\nREADY_MEASUREMENT {"event":"begin"}',
                "READY_MEASUREMENT malformed",
                "READY_MEASUREMENT []",
            ]
        ),
        [{"event": "begin"}],
    )
    recipe = delayed_recipe()
    results.check(
        "delayed readiness image runs the supplied daemon behind the relay",
        "COPY agentd /agentd" in recipe
        and f"ENV AGENTD_PORT={AGENT_PORT}" in recipe
        and "ENTRYPOINT []" in recipe
        and 'CMD ["/agentd"]' in recipe
        and "mv /agentd /real-agentd" in recipe
        and "export AGENTD_PORT=9001\\nexec /real-agentd" in recipe,
    )
    check_launcher_ports(results, recipe)
    check_relay(results)


def check_launcher_ports(results: Results, recipe: str) -> None:
    line = next(
        line for line in recipe.splitlines() if line.startswith("RUN mv /agentd ")
    )
    format_string = line.split("printf '", 1)[1].split("' > /agentd", 1)[0]
    rendered = subprocess.run(
        ["/bin/sh", "-c", 'printf "$1"', "launcher", format_string],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    with tempfile.TemporaryDirectory() as directory:
        relay = Path(directory) / "relay.py"
        relay.write_text('import os; print("relay=" + os.environ["AGENTD_PORT"])')
        daemon = Path(directory) / "daemon.sh"
        daemon.write_text('#!/bin/sh\nprintf "daemon=%s\\n" "$AGENTD_PORT"\n')
        daemon.chmod(0o700)
        launcher = rendered.replace(
            "python3 /delayed_ready.py",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(relay))}",
        ).replace("/real-agentd", shlex.quote(str(daemon)))
        process = subprocess.run(
            ["/bin/sh", "-c", launcher],
            env={**os.environ, "AGENTD_PORT": "19000"},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    results.check(
        "delayed readiness relay inherits the front port before the daemon override",
        process.returncode == 0
        and sorted(process.stdout.splitlines()) == ["daemon=9001", "relay=19000"],
        process.stderr,
    )


def check_relay(results: Results) -> None:
    spec = importlib.util.spec_from_file_location(
        "delayed_ready_fixture", REPO / "conformance/fixtures/delayed_ready.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sleeps = []
    forwarded = []
    response = SimpleNamespace(status=204, read=lambda: b"", getheaders=lambda: [])
    connection = SimpleNamespace(
        request=lambda *args: forwarded.append(args),
        getresponse=lambda: response,
        close=lambda: None,
    )
    statuses = []
    with (
        patch.object(
            module,
            "time",
            SimpleNamespace(monotonic=iter([1.0, 311.5]).__next__, sleep=sleeps.append),
        ),
        patch.object(module.http.client, "HTTPConnection", return_value=connection),
        contextlib.redirect_stdout(io.StringIO()) as output,
    ):
        relay = object.__new__(module.Relay)
        relay.command = "POST"
        relay.path = module.READY_PATH
        relay.headers = {"Content-Length": "2", "Authorization": "private"}
        relay.rfile = io.BytesIO(b"{}")
        relay.wfile = io.BytesIO()
        relay.send_response = statuses.append
        relay.send_header = lambda *_: None
        relay.end_headers = lambda: None
        relay.forward()
        relay.path = "/v1/health"
        relay.command = "GET"
        relay.headers = {}
        relay.forward()
    results.check(
        "delayed readiness fixture sleeps only for ready and preserves the response",
        sleeps == [310]
        and statuses == [204, 204]
        and forwarded[0][0:3] == ("POST", module.READY_PATH, b"{}")
        and forwarded[1][0:3] == ("GET", "/v1/health", b"")
        and timing_records([output.getvalue()])
        == [{"event": "begin"}, {"event": "end", "elapsed": 310.5}]
        and "private" not in output.getvalue(),
    )
