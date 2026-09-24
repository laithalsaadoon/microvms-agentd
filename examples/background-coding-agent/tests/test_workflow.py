# SPDX-License-Identifier: Apache-2.0
"""Real durable suspension and replay, with GitHub and VM operations replaced."""

import json
from collections import Counter
from functools import partial

import handler
import pytest
from aws_durable_execution_sdk_python_testing import DurableFunctionTestRunner

import microvms

URL = "https://github.com/octo/app/pull/9"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "stage",
        "prepare",
        "collect",
        "agent",
        "publish",
        "flaky-poll",
        "dead-poll",
    ],
)
def test_job_record_follows_the_workflow(monkeypatch, table, failure):
    calls = Counter()
    fast = handler.Duration.from_seconds(1)
    monkeypatch.setattr(handler.Duration, "from_seconds", lambda _: fast)

    def operation(name):
        def run(*_):
            calls[name] += 1
            if name == failure:
                raise ValueError("injected operation failure")
            if name == "stage":
                return {"kind": "implement", "base_sha": "abc", "base_ref": "main"}
            if name == "launch":
                return {"microvmId": "vm-1", "endpoint": "unused", "launched_at": 0.0}
            if name == "agent_done":
                # Cleanup on suspension would terminate the VM before a later poll.
                assert calls["terminate"] == 0
                if failure == "dead-poll" or (
                    failure == "flaky-poll" and calls[name] <= 2
                ):
                    raise microvms.ProtocolError("injected transport failure")
                return calls[name] >= 2
            if name == "collect":
                return {"exit_code": 1 if failure == "agent" else 0, "report": True}
            if name == "vm_gone":
                return failure == "dead-poll"
            if name == "terminate":
                return 900.0
            if name == "publish":
                return URL
            return None

        return run

    for name in (
        "stage",
        "launch",
        "prepare",
        "start",
        "agent_done",
        "vm_gone",
        "collect",
        "terminate",
        "publish",
    ):
        monkeypatch.setattr(handler, name, operation(name))
    table(id="job-1", repo="octo/app", number=9, agent="claude-code").save()

    with DurableFunctionTestRunner(handler.handler, poll_interval=0.05) as runner:
        result = runner.run(input=json.dumps({"id": "job-1"}), timeout=30)

    job = table.get("job-1")
    if failure in (None, "flaky-poll"):
        assert result.status.value == "SUCCEEDED"
        assert (job.status, job.phase, job.url) == ("SUCCEEDED", "published", URL)
        assert job.cost.startswith("~$")
    else:
        assert result.status.value == "FAILED"
        assert job.status == "FAILED" and job.error
    assert calls["launch"] == (0 if failure == "stage" else 1)
    assert calls["terminate"] == calls["launch"]
    assert calls["start"] <= 1
    if failure not in (None, "flaky-poll", "publish"):
        assert calls["publish"] == 0
    if failure == "dead-poll":
        assert calls["agent_done"] == calls["vm_gone"] == 1
    if failure == "flaky-poll":
        assert calls["vm_gone"] == 2


def test_poll_fails_only_when_the_vm_is_gone(monkeypatch):
    def broken(*_):
        raise microvms.ProtocolError("injected")

    monkeypatch.setattr(handler, "agent_done", broken)
    poll = partial(handler.poll, None, None, job_id="j", vm={}, token="t")
    monkeypatch.setattr(handler, "vm_gone", lambda _: False)
    assert poll(deadline=1e12) == {"done": False, "expired": False}
    assert handler.keep_polling(poll(deadline=0), 1).should_continue is False
    monkeypatch.setattr(handler, "vm_gone", lambda _: True)
    with pytest.raises(microvms.ProtocolError):
        poll(deadline=1e12)
