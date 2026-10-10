# SPDX-License-Identifier: Apache-2.0
"""`self_test()`: every offline check, in order, and the summary."""

from __future__ import annotations

import hashlib
import stat
import tempfile
from pathlib import Path
from typing import Any

from harness.archives import build_hostile_archives
from harness.cli import Cli
from harness.envelope import EnvelopeError, KindError
from harness.results import Results
from lanes.agents import daemon_deadline_ok, prompt_metadata_ok
from lanes.posture import EXECUTION_ROLE_ACTION_PREFIX, execution_role_actions
from lanes.quickstart import digest_record_agrees

from selftest.bindings import check_binding_handles
from selftest.caller_artifact import check_caller_artifact_section
from selftest.closed_output import (
    check_bdd_outcome,
    check_cli8_attribution,
    check_closing_reader_helper,
)
from selftest.cost import check_cost_checks
from selftest.ensure_image import check_ensure_image_section
from selftest.harness import check_run_section
from selftest.image_versions import check_image_versions_section
from selftest.local import check_doctor_region_lines, check_preflight_lines
from selftest.names import check_terminate_fallback
from selftest.posture import check_posture_lines
from selftest.privacy import check_log_privacy
from selftest.quickstart import check_gh_logged_out
from selftest.skew import check_version_skew_helpers
from selftest.stub import STUB_SOURCE


def self_test() -> int:
    """Drives the envelope-to-exception mapping against the stub. No AWS, no money.

    The point is not that the mapping works — it is that **`Results.raises`
    discriminates**. A `raises` that passed for any failure at all would make the five
    `ERR_PROTOCOL` checks vacuous, and vacuous is exactly how they would look green. So
    every positive case here has its negative twin, and the negatives are asserted to
    FAIL rather than described as failing.
    """
    print("== self-test: the envelope→exception mapping, offline ==")
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "microvm"
        stub.write_text(STUB_SOURCE)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        cli = Cli(binary=stub)
        results = Results()
        check_log_privacy(cli, results, Path(tmp))
        check_closing_reader_helper(results)
        check_cli8_attribution(results)
        check_run_section(results)
        check_terminate_fallback(results)
        check_bdd_outcome(results)
        check_posture_lines(results)
        check_preflight_lines(results)
        check_doctor_region_lines(results)
        check_ensure_image_section(results)
        check_caller_artifact_section(results)
        check_image_versions_section(results)
        check_gh_logged_out(results)
        check_version_skew_helpers(results)
        check_cost_checks(results)
        check_binding_handles(results)

        # -- the success side -------------------------------------------------
        ok = cli.call("ok")
        results.eq(
            "a success envelope parses its discriminant", ok.type, "microvm.state"
        )
        results.eq(
            "a success envelope carries its data", ok.data.get("state"), "SUSPENDED"
        )
        results.check("a success envelope has no kind", ok.kind is None, repr(ok.kind))

        # -- the load-bearing one --------------------------------------------
        # Three failures that are IDENTICAL in code and exit code and differ only in
        # `data.kind`. This is the whole reason this driver asserts on kinds: if the
        # coarse code were enough, these three would be one check.
        kinds = {}
        for case in ("conflict", "notfound", "protocol"):
            try:
                cli.call(case)
            except KindError as exc:
                kinds[case] = (exc.kind, exc.code, exc.exit_code)
        results.check(
            "three protocol failures share one code and one exit code",
            {v[1] for v in kinds.values()} == {"ERR_PROTOCOL"}
            and {v[2] for v in kinds.values()} == {5},
            repr(kinds),
        )
        results.check(
            "and are distinguishable only through data.kind",
            {v[0] for v in kinds.values()} == {"Conflict", "NotFound", "ProtocolError"},
            repr({k: v[0] for k, v in kinds.items()}),
        )

        # -- raises() asserts on the kind, positively and negatively ----------
        results.raises(
            "raises matches the expected kind", "Conflict", lambda: cli.call("conflict")
        )
        results.raises(
            "raises matches NotFound", "NotFound", lambda: cli.call("notfound")
        )

        # The negative twins, run through a throwaway Results so their failures are the
        # assertion rather than this run's verdict. A guard that cannot be made to fail
        # is not a guard, and these are the three ways it could be vacuous. Printed as
        # `PROBE` because a green run must not print a line that reads as broken.
        print(
            "  -- probing that raises() can fail (each PROBE line below is expected) --"
        )
        probe = Results(probe=True)
        probe.raises("wrong kind must fail", "Conflict", lambda: cli.call("notfound"))
        probe.raises("nothing raised must fail", "Conflict", lambda: cli.call("ok"))
        probe.raises(
            "a local reject has no kind", "Conflict", lambda: cli.call("localreject")
        )
        results.eq(
            "raises() fails on a wrong kind, no raise, and an absent kind",
            len(probe.failed),
            3,
        )
        results.eq("and passes nothing while doing it", len(probe.passed), 0)

        # -- eq() refuses an absent value ----------------------------------------
        # A missing key reads as None through `.get()`, so two lookups that both missed
        # used to compare equal and pass as agreement. The twin for each side, and both.
        print(
            "  -- probing that eq() refuses None (each PROBE line below is expected) --"
        )
        probe = Results(probe=True)
        probe.eq("absent", None, None)
        probe.eq("absent actual", None, "present")
        probe.eq("absent expected", "present", None)
        results.eq(
            "eq() fails when either side is None, both sides included",
            [name for name, _ in probe.failed],
            ["absent", "absent actual", "absent expected"],
        )
        results.eq("and eq() passes nothing while refusing None", len(probe.passed), 0)
        probe = Results(probe=True)
        probe.absent("a present value is not absent", "present")
        probe.absent("an empty string is present", "")
        results.eq(
            "absent() fails on any value that isn't None, an empty one included",
            len(probe.failed),
            2,
        )
        results.absent("absent() passes None", None)
        results.eq("eq() still compares empty strings", "", "")
        results.eq("and empty lists", [], [])
        # BIND-19 keeps one named check, so its copy of the rule gets its own twin.
        body = b"daemon"
        record = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "verification": "attested",
            "version": "1.0.0",
        }
        unproven = {
            key: value for key, value in record.items() if key != "verification"
        }
        results.check(
            "the digest record check refuses a proof absent from both sides (BIND-19)",
            not digest_record_agrees(unproven, None, body, "1.0.0"),
            repr(unproven),
        )
        results.check(
            "and passes a record that names the bytes, the proof and the version",
            digest_record_agrees(record, "attested", body, "1.0.0"),
            repr(record),
        )

        # -- the local reject carries no kind, which is information ------------
        try:
            cli.call("localreject")
        except KindError as exc:
            results.check(
                "a local reject reports no wire kind",
                exc.kind is None
                and exc.code == "ERR_INVALID_ARG"
                and exc.exit_code == 2,
                repr(exc),
            )

        # -- CLI-6's envelope half --------------------------------------------
        try:
            cli.call("leak")
        except KindError as exc:
            results.eq(
                "leaked identifiers and the wire kind coexist in data",
                (exc.envelope.data.get("leaked"), exc.kind),
                (["mvm-1", "arn:image"], "Conflict"),
            )
            results.check(
                "a platform-trap failure names its finding",
                exc.envelope.finding == "The build log group survives Terraform",
                repr(exc.envelope.finding),
            )

        # -- the success-envelope-with-non-zero-exit case ----------------------
        # `already_reported`: the payload is right and the exit code is not zero. It must
        # NOT raise, because the caller asked for the output and the output is there.
        results.ok("a failing workload does not raise", lambda: cli.call("execfailed"))

        timeout_envelope = cli.call("daemondeadline")
        deadline = timeout_envelope.data
        results.check(
            "a daemon timeout parses its raw outcome after the CLI parent exits",
            daemon_deadline_ok(deadline, timeout_envelope.process_exit_code),
        )
        for field, replacement in (
            ("phase", "running"),
            ("timedOut", False),
            ("timedOut", None),
            ("outcome", {"timed_out": False}),
            ("outcome", {}),
            ("outcome", None),
        ):
            changed = {**deadline, field: replacement}
            results.check(
                f"the deadline oracle refuses {field}={replacement!r}",
                not daemon_deadline_ok(changed, timeout_envelope.process_exit_code),
            )
        graceful = {
            **deadline,
            "exitCode": 0,
            "signal": None,
            "outcome": {"exit_code": 0, "signal": None, "timed_out": True},
        }
        results.check(
            "a child exiting cleanly after SIGTERM still records the authoritative deadline",
            daemon_deadline_ok(graceful, timeout_envelope.process_exit_code),
        )
        results.check(
            "a timeout with a successful CLI process exit is rejected",
            not daemon_deadline_ok(deadline, 0),
        )
        metadata = {
            "agent": "codex",
            "model": "global.openai.gpt-5.6-sol",
            "agentVersion": "1.2.3",
            "uid": 1000,
            "permissionMode": "unrestricted",
            "executionTimeoutSec": 120,
            "reapGroupOnExit": True,
        }
        results.check(
            "the background metadata oracle accepts all reported facts",
            prompt_metadata_ok(
                metadata, "codex", metadata["model"], "unrestricted", 120
            ),
        )
        for field in metadata:
            changed = {key: value for key, value in metadata.items() if key != field}
            results.check(
                f"the background metadata oracle refuses missing {field}",
                not prompt_metadata_ok(
                    changed, "codex", metadata["model"], "unrestricted", 120
                ),
            )
        default_metadata = {
            **metadata,
            "permissionMode": "agent-default",
            "executionTimeoutSec": None,
            "reapGroupOnExit": False,
        }
        results.check(
            "the background metadata oracle also checks the unchanged default policy",
            prompt_metadata_ok(
                default_metadata, "codex", metadata["model"], "agent-default", None
            )
            and not prompt_metadata_ok(
                metadata, "codex", metadata["model"], "agent-default", None
            ),
        )

        # -- CLI-4, three ways it can break ----------------------------------
        for case, why in (
            ("twoenvelopes", "two envelopes on stdout"),
            ("progress", "a progress line on stdout"),
            ("notjson", "human text on stdout"),
        ):
            try:
                cli.call(case)
            except EnvelopeError:
                results.check(f"CLI-4: {why} is caught", True)
            except KindError as exc:
                results.check(
                    f"CLI-4: {why} is caught",
                    False,
                    f"read as a protocol result: {exc!r}",
                )
            else:
                results.check(
                    f"CLI-4: {why} is caught", False, "parsed as one envelope"
                )

        # -- CLI-3: the two renderings of one decision must agree -------------
        try:
            cli.call("mismatch")
        except EnvelopeError as exc:
            results.check(
                "CLI-3: a $? that disagrees with exitCode is caught",
                True,
                str(exc)[:80],
            )
        except KindError:
            results.check(
                "CLI-3: a $? that disagrees with exitCode is caught",
                False,
                "the disagreement was accepted",
            )

        # -- the NDJSON stream reader -----------------------------------------
        #
        # `Cli.call_stream` is the one function in this file with a contract of its own, so
        # it gets the same treatment `Results.raises` got above: the happy path, then every
        # way it can be vacuous. A reader that accepted any multi-line stdout would make
        # all five streaming checks pass against a CLI that had stopped streaming.
        events, envelope = cli.call_stream("stream")
        results.eq("a stream yields its events and its envelope", len(events), 2)
        results.eq(
            "a stream's envelope carries the streaming discriminant",
            envelope.type,
            "microvm.exec.stream",
        )
        results.check(
            "a stream's events are events rather than envelopes",
            all("status" not in event for event in events)
            and events[0].get("event") == "output"
            and events[-1].get("event") == "exit",
            repr([event.get("event") for event in events]),
        )
        results.eq(
            "a stream's summary reports the event count",
            envelope.data.get("events"),
            2,
        )

        # A streamed exec whose workload failed keeps its success envelope and a non-zero
        # `$?` — the `already_reported` case on the streaming path, which `call_stream` must
        # not raise on for the same reason `call` must not.
        results.ok(
            "a failing streamed workload does not raise",
            lambda: cli.call_stream("streamfailed"),
        )

        print(
            "  -- probing that call_stream() can fail (each PROBE line below is expected) --"
        )
        stream_probe = Results(probe=True)
        for case, why in (
            ("streamenvelopefirst", "the envelope written first"),
            ("streampretty", "a pretty-printed envelope spanning lines"),
            ("streamnoenvelope", "no envelope at all"),
            ("streamwrongtype", "the non-streaming discriminant"),
        ):
            try:
                cli.call_stream(case)
            except EnvelopeError as exc:
                stream_probe.check(f"caught: {why}", False, str(exc)[:70])
            else:
                stream_probe.check(
                    f"NOT caught: {why}", True, "accepted a broken shape"
                )
        results.eq(
            "call_stream() rejects all four malformed stream shapes",
            len(stream_probe.failed),
            4,
        )
        results.eq("and accepts none of them", len(stream_probe.passed), 0)

        # A stream that failed mid-way raises with its kind, and the events already written
        # are not the driver's problem — the failure is.
        results.raises(
            "a mid-stream failure raises with the daemon's kind",
            "Transport",
            lambda: cli.call_stream("streamerror"),
        )

        # -- the five attached commands' argv round-trips ----------------------
        #
        # Cheap, and it covers the one thing the live tier discovers expensively: an argv
        # this suite builds that the CLI does not accept. The stub answers by its first
        # non-flag argument, so what is exercised here is `Cli.call`'s construction and the
        # envelope shape each section reads — not the CLI's parser, which `tests/manifest.rs`
        # covers at the process boundary.
        for case, kind, key in (
            ("health", "microvm.health", "bootstrapped"),
            ("ack", "microvm.exec", "phase"),
            ("poll", "microvm.exec", "phase"),
            ("stdinwrite", "microvm.stdin", "written"),
            ("cp", "microvm.copy", "direction"),
            ("build", "microvm.image", "reused"),
        ):
            got = cli.call(case)
            results.check(
                f"the {case} envelope parses with its own discriminant",
                got.type == kind and key in got.data,
                f"type={got.type!r} keys={sorted(got.data)}",
            )
        # A running exec's poll: `exitCode` is present and null, which is the shape
        # `--poll`'s "polling is not a failure" contract produces. Asserted because a
        # *missing* key and a null one read the same way in a permissive consumer, and this
        # suite's identity section branches on it.
        polled = cli.call("poll")
        results.check(
            "a running exec polls as a success with a present-but-null exit code",
            "exitCode" in polled.data and polled.data["exitCode"] is None,
            repr(polled.data),
        )

        # -- the start/poll/ack decomposition `--detach` restores ---------------
        #
        # The shape the first live round could not produce. `exec` without `--detach` acks
        # its own output, so `ack accepted` got a 409 and `polling reads an exec without
        # consuming it` read `''`. What is checked here is that the driver's *reading* of the
        # three-step sequence is right — a detached start reports `running` with no verdict,
        # a later poll finds the exec exited with its output still buffered, and only then is
        # there anything for an ack to release.
        detached = cli.call("detach")
        results.check(
            "a detached start reports running with no verdict yet",
            detached.data.get("phase") == "running"
            and detached.data.get("exitCode") is None
            and detached.data.get("execId") == "c1",
            repr(detached.data),
        )
        done = cli.call("polldone")
        results.check(
            "a detached exec's output is still readable when it exits",
            done.data.get("phase") == "exited"
            and "identity-live" in (done.data.get("stdout") or ""),
            repr(done.data),
        )
        # And the loop condition the identity section uses to wait for it: `phase != running`
        # is the exit test, so a `running` poll must not satisfy it and an `exited` one must.
        results.check(
            "the poll loop's exit condition distinguishes running from exited",
            cli.call("poll").data.get("phase") == "running"
            and done.data.get("phase") != "running",
            "running poll keeps looping, exited poll breaks",
        )

        # -- the four hostile archives really are hostile ----------------------
        #
        # Offline, and worth having offline: these are built with `tarfile` precisely
        # because GNU tar sanitizes them, and an archive that had been silently sanitized
        # would make four live checks pass against nothing. So the *bytes* are inspected
        # here — with `tarfile` reading them back, which is the only reader that can see a
        # member type — before any of them is ever handed to a real daemon.
        import io
        import tarfile

        archives = dict(build_hostile_archives())
        results.eq("all four hostile archives are built", len(archives), 4)

        def members(name: str) -> list[tarfile.TarInfo]:
            with tarfile.open(fileobj=io.BytesIO(archives[name]), mode="r") as tar:
                return list(tar.getmembers())

        traversal = members("parent traversal")
        results.check(
            "the traversal archive really escapes the root",
            any(".." in member.name for member in traversal),
            repr([member.name for member in traversal]),
        )
        absolute = members("absolute link target")
        results.check(
            "the absolute-link archive really names an absolute target",
            any(
                member.issym() and member.linkname.startswith("/")
                for member in absolute
            ),
            repr([(member.name, member.linkname) for member in absolute]),
        )
        redirect = members("symlink redirect")
        results.check(
            "the redirect archive is a link to .. plus a file through it",
            any(member.issym() and member.linkname == ".." for member in redirect)
            and any(member.isfile() for member in redirect),
            repr([(member.name, member.type) for member in redirect]),
        )
        device = members("character device")
        results.check(
            "the device archive really carries a character device",
            any(member.ischr() for member in device),
            repr([(member.name, member.type) for member in device]),
        )

        # -- the skip primitive still works, with no live caller ---------------
        #
        # `Results.skip` has no caller in the live path any more: the 34 entries it used to
        # print became real checks. Exercised here against a throwaway `Results` so it
        # cannot rot into a function that no longer runs — the next inexpressible check
        # needs somewhere to be recorded, and a primitive nothing ever calls is one nobody
        # notices has broken.
        skip_probe = Results()
        skip_probe.skip("a future gap", "recorded rather than silent")
        results.eq(
            "the skip primitive records rather than passing", len(skip_probe.skipped), 1
        )
        results.eq("and does not count as a pass", len(skip_probe.passed), 0)

        # -- the IAM guard reads NotAction as a grant, offline -------------------
        #
        # `execution_role_actions` feeds "the conformance execution role grants only logs
        # actions", and an `Allow` statement carrying `NotAction` grants everything
        # *except* what it names. A reader that collected `Action` alone reported such a
        # role as its `logs:` actions and stayed green (review of #163). Pinned here with a
        # fake IAM client so the shape is asserted without a role to create.
        class _FakeIam:
            def __init__(self, statements: list[dict[str, Any]]) -> None:
                self.statements = statements

            def list_role_policies(self, **_: Any) -> dict[str, Any]:
                return {"PolicyNames": ["inline"]}

            def get_role_policy(self, **_: Any) -> dict[str, Any]:
                return {"PolicyDocument": {"Statement": self.statements}}

            def list_attached_role_policies(self, **_: Any) -> dict[str, Any]:
                return {"AttachedPolicies": []}

        logs_only = {
            "Effect": "Allow",
            "Action": ["logs:PutLogEvents"],
            "Resource": "*",
        }
        fake_role = "arn:aws:iam::000000000000:role/self-test"
        widened, _ = execution_role_actions(
            _FakeIam(
                [logs_only, {"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"}]
            ),
            fake_role,
        )
        results.check(
            "an Allow statement with NotAction counts as a grant outside logs:",
            any(not a.startswith(EXECUTION_ROLE_ACTION_PREFIX) for a in widened),
            repr(widened),
        )
        denied, _ = execution_role_actions(
            _FakeIam(
                [logs_only, {"Effect": "Deny", "NotAction": "logs:*", "Resource": "*"}]
            ),
            fake_role,
        )
        results.check(
            "a Deny statement with NotAction grants nothing",
            all(a.startswith(EXECUTION_ROLE_ACTION_PREFIX) for a in denied),
            repr(denied),
        )

        print("\n== self-test summary ==")
        print(f"  passed: {len(results.passed)}")
        print(f"  failed: {len(results.failed)}")
        for name, detail in results.failed:
            print(f"    FAIL {name}: {detail}")
        return 0 if not results.failed else 1
