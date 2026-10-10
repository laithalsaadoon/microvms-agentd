# SPDX-License-Identifier: Apache-2.0
"""The Python binding's tunnel, port-forward and import handles, driven for the live suite.

`conformance/lanes/bindings.py` runs this file with the interpreter of a venv the binding was
installed into, and the two speak JSON lines: the first line on stdin is the request, each line
on stdout is one event, and `conformance/drivers/handles.mjs` speaks the same protocol for the
Node binding. Everything here goes through the binding's public API alone, so a check that
passes says the binding's handles work, not that a helper of this file does.

- `{"op": "serve", "kind": "tunnel" | "forward", "session", "guestPort", "maxConnections",
  "stopTimeout"}` opens the handle and prints `listening` with its `localAddress`; the suite
  makes its own request through that address, then sends a `stop` line, and this prints
  `stopped` with the report.
- `{"op": "import", "session", "record", "stateDir", "times"}` imports the record into a
  registry at `stateDir` `times` times, then prints `imported` with what each import returned,
  the error that ended them (if one did), and what `get` and `list` read afterward.

`session` is `{"attach": {"region", "microvmId", "endpoint", "agentToken"}}` for a VM through
the endpoint proxy, or `{"direct": {"endpoint", "agentToken"}}` for a daemon reached directly.
The agent token arrives on stdin rather than in argv, and nothing printed carries it: `get`'s
record is reported as whether its token matches the request's.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import microvms


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def session_from(spec: dict[str, Any]) -> microvms.Session:
    if "attach" in spec:
        attach = spec["attach"]
        return microvms.Session.attach(
            microvms.Region.parse(attach["region"]),
            attach["microvmId"],
            attach["endpoint"],
            attach["agentToken"],
        )
    direct = spec["direct"]
    return microvms.Session.direct(direct["endpoint"], direct["agentToken"])


def report_of(kind: str, report: Any) -> dict[str, Any]:
    """The report's fields under the Node binding's names, so the suite reads one shape."""
    fields = {
        "served": report.served,
        "refused": report.refused,
        "proxyTokenMints": report.proxy_token_mints,
        "stopped": report.stopped,
        "ended": [
            {"peer": end.peer, "kind": end.kind, "code": end.code, "detail": end.detail}
            for end in report.ended
        ],
    }
    if kind == "tunnel":
        fields.update(truncated=report.truncated, unproven=report.unproven)
    else:
        fields.update(upgrades=report.upgrades)
    return fields


def serve(request: dict[str, Any]) -> None:
    session = session_from(request["session"])
    open_handle = (
        session.tunnel if request["kind"] == "tunnel" else session.port_forward
    )
    handle = open_handle(
        request["guestPort"], max_connections=request["maxConnections"]
    )
    emit("listening", localAddress=handle.local_address, running=handle.running)
    # The suite's request goes through the handle while this waits; its `stop` line, or the
    # pipe closing, ends the wait.
    sys.stdin.readline()
    report = handle.stop(timeout=request["stopTimeout"])
    emit("stopped", report=report_of(request["kind"], report), running=handle.running)


def record_from(record: dict[str, Any]) -> microvms.NameRecord:
    return microvms.NameRecord(
        record["name"],
        record["microvmId"],
        record["endpoint"],
        record["agentToken"],
        microvms.Region.parse(record["region"]),
    )


def import_record(request: dict[str, Any]) -> None:
    session = session_from(request["session"])
    wanted = request["record"]
    record = record_from(wanted)
    registry = microvms.NameRegistry(request["stateDir"])
    replaced: list[bool] = []
    error = None
    try:
        for _ in range(request["times"]):
            replaced.append(registry.import_record(record, session))
    except microvms.MicrovmError as failure:
        error = {"code": failure.code, "message": str(failure)[:300]}
    found = registry.get(wanted["name"])
    emit(
        "imported",
        replaced=replaced,
        error=error,
        found=None
        if found is None
        else {
            "name": found.name,
            "microvmId": found.microvm_id,
            "endpoint": found.endpoint,
            "region": found.region,
            "tokenMatches": found.agent_token == wanted["agentToken"],
        },
        listed=[listed.name for listed in registry.list()],
    )


def main() -> int:
    request = json.loads(sys.stdin.readline())
    try:
        if request["op"] == "serve":
            serve(request)
        elif request["op"] == "import":
            import_record(request)
        else:
            emit("error", code=None, message=f"unknown op {request['op']!r}")
            return 2
    except microvms.MicrovmError as failure:
        emit("error", code=failure.code, message=str(failure)[:300])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
