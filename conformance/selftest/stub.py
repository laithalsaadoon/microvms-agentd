# SPDX-License-Identifier: Apache-2.0
"""The stub `microvm` the self-test runs as a real subprocess."""

from __future__ import annotations

#: A stub `microvm` that emits a canned envelope chosen by its first non-flag argument.
#:
#: A real subprocess rather than a mocked `subprocess.run`, because the thing under test
#: is the *whole* path — argv construction, stdout capture, the exit-code cross-check,
#: `json.loads` over the entire stream. A mock at the `run` boundary would skip the two
#: that have actually been wrong.
#:
#: The `stream*` cases below are the additions the flip needed, and they are why the stub
#: exists at all rather than the reader being tested against a string: `Cli.call_stream`'s
#: whole job is asserting a *multi-line stdout shape*, and the four ways that shape can be
#: wrong — envelope first, envelope pretty-printed across lines, a non-envelope last line,
#: the wrong discriminant — are only reachable by a process that really writes them.
STUB_SOURCE = '''#!/usr/bin/env python3
"""A fake `microvm` for conformance/run_rs.py --self-test. Emits canned envelopes."""
import json
import sys

STREAM_ENVELOPE = {
    "status": "ok", "apiVersion": "1", "type": "microvm.exec.stream",
    "data": {"execId": "x-1", "events": 2, "bytes": 8, "nextOffset": 8,
             "exitCode": 0, "truncated": False, "gaps": 0},
}
STREAM_EVENTS = [
    {"event": "output", "stream": "stdout", "offset": 0, "bytes": 8,
     "text": "chunk-1\\n", "lossy": False},
    {"event": "exit", "exitCode": 0, "signal": None, "truncated": False,
     "writersMayBeAlive": False, "offset": 8},
]

CASES = {
    "ok": ({"status": "ok", "apiVersion": "1", "type": "microvm.state",
            "data": {"microvmId": "mvm-1", "state": "SUSPENDED"}}, 0),
    "conflict": ({"status": "error", "apiVersion": "1", "error": "409",
                  "code": "ERR_PROTOCOL", "exitCode": 5, "finding": "",
                  "suggestions": [], "data": {"kind": "Conflict"}}, 5),
    "notfound": ({"status": "error", "apiVersion": "1", "error": "404",
                  "code": "ERR_PROTOCOL", "exitCode": 5, "finding": "",
                  "suggestions": [], "data": {"kind": "NotFound"}}, 5),
    "protocol": ({"status": "error", "apiVersion": "1", "error": "400",
                  "code": "ERR_PROTOCOL", "exitCode": 5, "finding": "",
                  "suggestions": [], "data": {"kind": "ProtocolError"}}, 5),
    "localreject": ({"status": "error", "apiVersion": "1",
                     "error": "off-table size class", "code": "ERR_INVALID_ARG",
                     "exitCode": 2, "finding": "", "suggestions": [], "data": {}}, 2),
    "leak": ({"status": "error", "apiVersion": "1", "error": "interrupted",
              "code": "ERR_INTERRUPTED", "exitCode": 11,
              "finding": "The build log group survives Terraform", "suggestions": [],
              "data": {"kind": "Conflict", "leaked": ["mvm-1", "arn:image"]}}, 11),
    "execfailed": ({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
                    "data": {"execId": "x-1", "exitCode": 4}}, 13),
    # The exit code and the envelope disagree: CLI-3's claim is that they never do.
    "mismatch": ({"status": "error", "apiVersion": "1", "error": "boom",
                  "code": "ERR_PLATFORM", "exitCode": 9, "finding": "",
                  "suggestions": [], "data": {}}, 7),
    # The five attached commands, so the self-test drives the argv this suite now builds
    # for each of them rather than only the lifecycle ones.
    "health": ({"status": "ok", "apiVersion": "1", "type": "microvm.health",
                "data": {"version": "0.1.0", "bootstrapped": True,
                         "identityDegraded": False, "identityRepaired": True,
                         "diskAvailableBytes": 1024, "diskUnderPressure": False}}, 0),
    "ack": ({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
             "data": {"execId": "x-1", "phase": "acked", "exitCode": 0,
                      "stdout": "released", "stderr": "", "truncated": False}}, 0),
    "poll": ({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
              "data": {"execId": "x-1", "phase": "running", "exitCode": None,
                       "stdout": "", "stderr": "", "truncated": False}}, 0),
    # `exec --detach`: started, nothing waited on, nothing acked. Same envelope shape as
    # a poll of a running exec, which is the point — one `render_exec` serves both, so a
    # consumer needs one parser rather than two.
    "detach": ({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
                "data": {"execId": "c1", "phase": "running", "exitCode": None,
                         "stdout": "", "stderr": "", "truncated": False}}, 0),
    # A poll of a detached exec that has since exited, with its output still buffered
    # because nothing acked it. This is the shape `polling reads an exec without
    # consuming it` reads, and the shape the first live round could not produce.
    "polldone": ({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
                  "data": {"execId": "c1", "phase": "exited", "exitCode": 0,
                           "stdout": "identity-live\\n", "stderr": "",
                           "truncated": False}}, 0),
    "daemondeadline": ({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
                        "data": {"execId": "deadline-1", "phase": "exited", "exitCode": None,
                                 "signal": 15, "timedOut": True,
                                 "outcome": {"exit_code": None, "signal": 15,
                                             "timed_out": True}}}, 10),
    "stdinwrite": ({"status": "ok", "apiVersion": "1", "type": "microvm.stdin",
                    "data": {"execId": "x-1", "written": 5, "eof": True}}, 0),
    "cp": ({"status": "ok", "apiVersion": "1", "type": "microvm.copy",
            "data": {"direction": "upload", "bytes": 28, "local": "./f",
                     "remote": "/tmp/f", "tar": False}}, 0),
    # `build --project --reuse`: the image envelope the #74 section reads its name and
    # `reused` verdict from.
    "build": ({"status": "ok", "apiVersion": "1", "type": "microvm.image",
               "data": {"imageIdentifier": "arn:image",
                        "imageName": "microvm-cli-project-0a1b-0123456789ab",
                        "buildLogGroup": "/aws/lambda-microvms/x", "logStream": None,
                        "size": "1024", "reused": False, "agentd": None}}, 0),
}

args = [a for a in sys.argv[1:] if not a.startswith("--")]
case = args[0] if args else "ok"

if case in {"echoargs", "agent-prompt"}:
    print(json.dumps({"status": "ok", "apiVersion": "1", "type": "microvm.exec",
                      "data": {"argv": sys.argv[1:]}}))
    raise SystemExit(0)
if case.startswith("private"):
    marker = "private-transcript-canary"
    if case == "privatemalformed":
        print(marker)
    elif case == "privateempty":
        print(marker, file=sys.stderr)
    elif case == "privatelast":
        print(json.dumps({"event": "output", "text": marker}))
    elif case == "privatefirst":
        first = dict(STREAM_ENVELOPE, data={"stdout": marker})
        print(json.dumps(first))
        print(json.dumps(STREAM_ENVELOPE))
    elif case == "privatetimeout":
        import time
        print(marker, flush=True)
        time.sleep(2)
    else:
        print(json.dumps({"status": "error", "apiVersion": "1", "error": marker,
                          "code": "ERR_PROTOCOL", "exitCode": 5, "finding": marker,
                          "suggestions": [marker], "data": {"kind": "Conflict"}}))
        raise SystemExit(7 if case == "privatemismatch" else 5)
    raise SystemExit(0)

if case == "twoenvelopes":
    print(json.dumps(CASES["ok"][0]))
    print(json.dumps(CASES["ok"][0]))
    raise SystemExit(0)
if case == "progress":
    print("building image")
    print(json.dumps(CASES["ok"][0]))
    raise SystemExit(0)
if case == "notjson":
    print("error ERR_PROTOCOL: 409")
    raise SystemExit(5)

# -- the NDJSON stream cases, and its four malformations ---------------------
if case == "stream":
    for event in STREAM_EVENTS:
        print(json.dumps(event))
    print(json.dumps(STREAM_ENVELOPE))
    raise SystemExit(0)
if case == "streamfailed":
    # A streamed exec whose workload exited non-zero: a SUCCESS envelope with a
    # non-zero code, exactly as the non-streaming case does.
    envelope = json.loads(json.dumps(STREAM_ENVELOPE))
    envelope["data"]["exitCode"] = 4
    for event in STREAM_EVENTS[:1]:
        print(json.dumps(event))
    print(json.dumps({"event": "exit", "exitCode": 4, "signal": None,
                      "truncated": False, "writersMayBeAlive": False, "offset": 8}))
    print(json.dumps(envelope))
    raise SystemExit(13)
if case == "streamenvelopefirst":
    # The envelope leading: a consumer reading line by line hits the terminator
    # before any output and concludes the command produced none.
    print(json.dumps(STREAM_ENVELOPE))
    for event in STREAM_EVENTS:
        print(json.dumps(event))
    raise SystemExit(0)
if case == "streampretty":
    # The envelope pretty-printed, so the terminating record is nine broken lines.
    for event in STREAM_EVENTS:
        print(json.dumps(event))
    print(json.dumps(STREAM_ENVELOPE, indent=2))
    raise SystemExit(0)
if case == "streamnoenvelope":
    # Events and no terminator at all.
    for event in STREAM_EVENTS:
        print(json.dumps(event))
    raise SystemExit(0)
if case == "streamwrongtype":
    # NDJSON with the NON-streaming discriminant, which is the subtle one: the shape
    # is right and a consumer branching on `type` would pick the wrong parser.
    envelope = json.loads(json.dumps(STREAM_ENVELOPE))
    envelope["type"] = "microvm.exec"
    for event in STREAM_EVENTS:
        print(json.dumps(event))
    print(json.dumps(envelope))
    raise SystemExit(0)
if case == "streamerror":
    # A stream that failed part-way: the events written stay written, and the failure
    # envelope is the last line.
    print(json.dumps(STREAM_EVENTS[0]))
    print(json.dumps({"status": "error", "apiVersion": "1", "error": "cut",
                      "code": "ERR_RETRYABLE", "exitCode": 3, "finding": "",
                      "suggestions": [], "data": {"kind": "Transport"}}))
    raise SystemExit(3)

document, code = CASES[case]
print(json.dumps(document))
sys.exit(code)
'''
