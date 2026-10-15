# SPDX-License-Identifier: Apache-2.0
"""Offline verdict, dispatch, and cleanup controls for the actual capability policy."""

from __future__ import annotations

import ast
import copy
import errno
import io
import json
import os
import runpy
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.constants import REPO
from harness.results import Results
from lanes.capability_policy import (
    BLOCKING_BYTES,
    COMPAT_OUTPUT,
    DROP_MASK,
    FIFO_PATH,
    REQUEST_SECONDS,
    check_capability_policy,
    drive_capability_policy,
    policy_dockerfile,
)


def good_report(repair: bool) -> dict:
    mask = 0x1FFFFFFFFFF if repair else 0xA80425FB
    before = {
        "CapInh": "0000000000000000",
        "CapAmb": "0000000000000000",
        "CapPrm": f"{mask:016x}",
        "CapEff": f"{mask:016x}",
        "CapBnd": f"{mask:016x}",
        "NoNewPrivs": "1",
        "Seccomp": "0",
    }
    seeded = {**before, "CapInh": "0000000000000001", "CapAmb": "0000000000000001"}
    daemon = {**before, "CapBnd": f"{mask & ~DROP_MASK:016x}"}
    child = {
        **daemon,
        "CapPrm": daemon["CapBnd"],
        "CapEff": daemon["CapBnd"],
    }
    return {
        "baseline": {"before": before, "seeded": seeded},
        "daemonPid": 1,
        "readerConnected": True,
        "blockingTransfer": True,
        "daemon": daemon,
        "threads": [{"tid": tid, "status": daemon.copy()} for tid in (1, 2)],
        "rootExec": {"uid": 0, "status": child.copy()},
        "hook": {"uid": 0, "status": child.copy()},
        "initialSyncExit": 0,
        "compatExit": 0,
        "compatOutput": COMPAT_OUTPUT,
        "identitySteps": [
            {"name": name, "outcome": "repaired"}
            for name in ("machine-id", "hostname", "boot-id")
        ]
        if repair
        else [],
        "repair": repair,
    }


def check_policy_lane(results: Results) -> None:
    for repair in (False, True):
        label = "repair" if repair else "default"
        good = good_report(repair)
        positive = Results(probe=True)
        check_capability_policy(good, repair, positive)
        results.check(f"policy {label} accepts complete evidence", not positive.failed)
        floor = Results(probe=True)
        check_capability_policy({}, repair, floor)
        results.check(
            f"policy {label} rejects every verdict without evidence",
            {name for name, _ in floor.failed} == set(positive.passed),
        )
        for key in good:
            broken = copy.deepcopy(good)
            del broken[key]
            failed = Results(probe=True)
            check_capability_policy(broken, repair, failed)
            results.check(f"policy {label} rejects missing {key}", bool(failed.failed))
        cases = [
            (("baseline", "seeded", "CapInh"), "0000000000000000"),
            (("baseline", "seeded", "CapAmb"), "0000000000000000"),
            (("baseline", "seeded", "CapEff"), "0000000000000000"),
            (("baseline", "seeded", "CapBnd"), "0000000000000000"),
            (("daemon", "CapInh"), "0000000000000001"),
            (("daemon", "CapAmb"), "0000000000000001"),
            (("daemon", "CapBnd"), "0000000000000000"),
            (("daemon", "CapEff"), "0000000000000000"),
            (("daemon", "CapPrm"), "bad"),
            (("daemon", "Seccomp"), "2"),
            (("daemon", "NoNewPrivs"), "0"),
            (("threads",), []),
            (("threads",), [{"tid": 1, "status": good["daemon"]}]),
            (("threads",), [None]),
            (("threads",), [{"tid": {}, "status": good["daemon"]}]),
            (("threads", 1, "tid"), True),
            (("threads", 1, "tid"), 1),
            (("threads", 1, "status", "CapInh"), "0000000000000001"),
            (("threads", 1, "status", "CapEff"), "0000000000000000"),
            (("daemonPid",), True),
            (("readerConnected",), False),
            (("blockingTransfer",), False),
            (("rootExec", "uid"), 4242),
            (("rootExec", "uid"), False),
            (("rootExec", "status", "CapAmb"), "0000000000000001"),
            (("rootExec", "status", "Seccomp"), "2"),
            (("hook", "uid"), 4242),
            (("hook", "status", "CapPrm"), "0000000000000000"),
            (("initialSyncExit",), False),
            (("initialSyncExit",), 1),
            (("compatExit",), False),
            (("compatExit",), 1),
            (("compatOutput",), COMPAT_OUTPUT.replace("4242:4242", "0:0")),
            (("repair",), not repair),
            (("identitySteps",), None),
        ]
        if repair:
            cases += [
                (("baseline", "before", "CapBnd"), "0000000000000000"),
                (("daemon", "CapBnd"), good["baseline"]["before"]["CapBnd"]),
                (
                    ("threads", 1, "status", "CapBnd"),
                    good["baseline"]["before"]["CapBnd"],
                ),
                (
                    ("rootExec", "status", "CapEff"),
                    good["baseline"]["before"]["CapEff"],
                ),
                (("hook", "status", "CapEff"), good["baseline"]["before"]["CapEff"]),
                (("identitySteps", 2, "outcome"), "failed"),
                (("identitySteps",), []),
            ]
        for index, (path, value) in enumerate(cases):
            broken = copy.deepcopy(good)
            node = broken
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            failed = Results(probe=True)
            check_capability_policy(broken, repair, failed)
            results.check(
                f"policy {label} rejects mutation {index}", bool(failed.failed)
            )
    check_policy_dispatch(results)
    check_policy_wiring(results)
    check_probe_seed(results)
    check_probe_reader(results)


def check_probe_seed(results: Results) -> None:
    directory = REPO / "conformance/drivers"
    with patch.object(sys, "path", [str(directory), *sys.path]):
        seed = runpy.run_path(str(directory / "capability_policy_probe.py"))[
            "seed_inheritance"
        ]
    for failure in (None, "get", "set", "ambient"):
        writes = []

        class Libc:
            def capget(self, _, data):
                data._obj[0].effective = 0x1234
                data._obj[0].permitted = 0x5678
                data._obj[0].inheritable = 2
                data._obj[1].effective = 0xABCD
                data._obj[1].permitted = 0xEF00
                data._obj[1].inheritable = 4
                return -1 if failure == "get" else 0

            def capset(self, _, data):
                writes.append(
                    [
                        (row.effective, row.permitted, row.inheritable)
                        for row in data._obj
                    ]
                )
                return -1 if failure == "set" else 0

            def prctl(self, *args):
                writes.append(args)
                return -1 if failure == "ambient" else 0

        raised = False
        with (
            patch("ctypes.CDLL", return_value=Libc()),
            patch("ctypes.get_errno", return_value=1),
        ):
            try:
                seed()
            except OSError:
                raised = True
        if failure is None:
            results.check(
                "policy probe seeds only inheritable and ambient CHOWN",
                not raised
                and writes
                == [[(0x1234, 0x5678, 3), (0xABCD, 0xEF00, 4)], (47, 2, 0, 0, 0)],
            )
        else:
            results.check(f"policy probe fails closed on {failure} refusal", raised)


def check_probe_reader(results: Results) -> None:
    directory = REPO / "conformance/drivers"
    with patch.object(sys, "path", [str(directory), *sys.path]):
        observe = runpy.run_path(str(directory / "capability_policy_probe.py"))[
            "observe_with_reader"
        ]
    namespace = observe.__globals__
    for fail_observation in (False, True):
        seen = []
        with (
            patch("os.open", side_effect=[OSError(errno.ENXIO, "no reader"), 7]),
            patch("time.sleep"),
            patch("os.write", side_effect=lambda fd, data: seen.append((fd, data))),
            patch("os.close", side_effect=lambda fd: seen.append(fd)),
            patch.dict(
                namespace,
                {
                    "observe": lambda: (
                        (_ for _ in ()).throw(ValueError("failed"))
                        if fail_observation
                        else {}
                    )
                },
            ),
        ):
            raised = False
            report = {}
            try:
                report = observe(Path("/test-fifo"))
            except ValueError:
                raised = True
        results.check(
            f"policy reader stays connected through {'failed' if fail_observation else 'successful'} observation",
            raised == fail_observation
            and (fail_observation or report.get("readerConnected") is True)
            and seen == [(7, BLOCKING_BYTES), 7],
        )
    with (
        patch("os.open", side_effect=OSError(errno.ENXIO, "no reader")),
        patch("time.monotonic", side_effect=[0, 31]),
    ):
        timed_out = False
        try:
            observe(Path("/test-fifo"))
        except OSError:
            timed_out = True
    results.check("policy reader bounds an absent blocking request", timed_out)


def check_policy_dispatch(results: Results) -> None:
    calls, variants, cleaned = [], [], []

    class Cli:
        region = "us-east-1"

        def call(self, *args, **kwargs):
            calls.append((args, kwargs))
            data = {}
            if args[0] == "cp" and args[1] == f"vm:{FIFO_PATH}":
                Path(args[2]).write_bytes(BLOCKING_BYTES)
            if args[0] == "exec":
                if "/capability-policy-probe.py observe" in args[1]:
                    data = {
                        "exitCode": 0,
                        "stdout": json.dumps(good_report(variants[-1])),
                    }
                else:
                    data = {"exitCode": 0, "stdout": COMPAT_OUTPUT}
            elif args[0] == "health":
                data = {"identitySteps": good_report(variants[-1])["identitySteps"]}
            return SimpleNamespace(data=data)

    @contextmanager
    def image(*args, **kwargs):
        variants.append(kwargs["repair"])
        try:
            yield SimpleNamespace(), {}
        finally:
            cleaned.append(kwargs["repair"])

    launched = SimpleNamespace(
        data={
            "microvmId": "vm-test",
            "endpoint": "https://example.invalid",
            "agentToken": "not-a-token",
        }
    )
    observed = Results(probe=True)
    with (
        patch("lanes.capability_policy.isolated_image", image),
        patch("lanes.capability_policy.launch_image", return_value=launched),
        redirect_stdout(io.StringIO()),
    ):
        drive_capability_policy(Cli(), Path(__file__), None, observed)
    results.check(
        "policy driver exercises default and repair images",
        variants == [False, True] and not observed.failed,
    )
    results.check("policy driver cleans both owned images", cleaned == [False, True])
    results.check(
        "policy driver uses bounded real root probes and default-user transfers",
        all(kwargs.get("timeout") == REQUEST_SECONDS for _, kwargs in calls)
        and sum(args[0] == "sync" for args, _ in calls) == 4
        and sum(args[0] == "cp" and "--tar" in args for args, _ in calls) == 2
        and all("--user" not in args for args, _ in calls if args[0] in {"sync", "cp"})
        and sum(
            "--user" in args and args[args.index("--user") + 1] == "0"
            for args, _ in calls
        )
        == 4,
    )
    recipe = policy_dockerfile()
    results.check(
        "policy image enables the real startup environment and hook",
        "ENV AGENTD_DROP_EXEC_CAPABILITIES=true" in recipe
        and 'CMD ["python3", "/capability-policy-probe.py", "bootstrap"]' in recipe
        and "/etc/agentd/hooks.d/run" in recipe,
    )
    cleaned.clear()
    try:
        with (
            patch("lanes.capability_policy.isolated_image", image),
            patch(
                "lanes.capability_policy.launch_image",
                side_effect=ValueError("launch failed"),
            ),
            redirect_stdout(io.StringIO()),
        ):
            drive_capability_policy(Cli(), Path(__file__), None, Results(probe=True))
    except ValueError:
        pass
    results.check(
        "policy driver cleans its image on launch failure", cleaned == [False]
    )


def check_policy_wiring(results: Results) -> None:
    from lanes.suite import run_suite
    from run_rs import parse_args

    selected = parse_args(
        [
            "--only",
            "capability_policy",
            "--binary",
            __file__,
            "--microvm-binary",
            __file__,
        ]
    )
    called = []
    outputs = {
        key: {"value": "test"}
        for key in ("s3_bucket", "build_role_arn", "execution_role_arn")
    }
    with (
        patch("lanes.suite.sh", return_value=json.dumps(outputs)),
        patch("lanes.suite.boto3.Session", return_value=None),
        patch(
            "lanes.suite.drive_capability_policy",
            side_effect=lambda *args: called.append(args),
        ),
        patch.dict(os.environ),
        redirect_stdout(io.StringIO()),
    ):
        code = run_suite(selected)
    results.check(
        "policy selector dispatches the actual lane", code == 0 and len(called) == 1
    )
    tree = ast.parse((REPO / "conformance/lanes/suite.py").read_text())
    results.check(
        "policy lane is included in the full live suite",
        any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_section"
            and any(
                isinstance(arg, ast.Name) and arg.id == "drive_capability_policy"
                for arg in node.args
            )
            for node in ast.walk(tree)
        ),
    )
