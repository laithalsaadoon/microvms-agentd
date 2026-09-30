# SPDX-License-Identifier: Apache-2.0
"""The daemon lane: the run hook and the status codes, asserted on raw HTTP.

These test the daemon rather than the client under test, which is why they go around the CLI
(`run_rs.py` gives the reasoning).
"""

from __future__ import annotations

from harness.daemon import Daemon, post_run_hook
from harness.results import Results


def drive_daemon_lane(daemon: Daemon, results: Results) -> None:
    """The six checks that test the daemon rather than the client under test.

    Same six names the oracle used, asserting on the status integer the daemon chose.
    Routing them through a client library would test that library twice and the daemon
    no better; asserting on the integer is what they always meant.
    """
    print("\n-- bootstrap and authorization (daemon lane) --")
    results.eq(
        "post-bootstrap hijack refused with 409",
        post_run_hook(daemon, "attacker-token"),
        409,
    )
    results.eq(
        "identical bootstrap replay accepted",
        post_run_hook(daemon, daemon.agent_token),
        200,
    )

    for name, token in (
        ("wrong token refused with 401", "wrong-token"),
        # The daemon must *answer* a token it cannot decode rather than drop the
        # connection, which is why this asserts a status at all: a `TransportError`
        # out of `Daemon.status` is the failure, and `results.eq` reports it as the
        # exception it is rather than as a wrong status.
        ("non-ASCII token header answered, not a dropped connection", "tökén"),
    ):
        results.eq(name, daemon.status("GET", "/v1/exec/nope", token=token), 401)

    for name, method, path, body in (
        ("malformed body is 400, not 404", "POST", "/v1/exec/start", {"bogus": True}),
        ("missing path key is 400", "GET", "/v1/fs/file", None),
    ):
        # 400 rather than 404 is the whole assertion, and it is why this compares the
        # integer instead of catching a class: the deleted client mapped both onto
        # separate exceptions, but the defect being guarded against — a phantom
        # missing file where the request was simply malformed — is a 404 arriving
        # where a 400 belongs, and only the integer says which came back.
        results.eq(
            name,
            daemon.status(method, path, token=daemon.agent_token, json_body=body),
            400,
        )
