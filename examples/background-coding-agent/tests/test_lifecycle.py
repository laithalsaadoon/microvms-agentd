# SPDX-License-Identifier: Apache-2.0
"""The VM lifecycle goes through the SDK: launch once, adopt in every later step."""

from types import SimpleNamespace as NS

import handler
import pytest

VM = {"microvmId": "vm-1", "endpoint": "https://vm-1.example", "launched_at": 0.0}


class Plane:
    def __init__(self, states):
        self.states, self.terminated, self.waits = list(states), [], []

    def get(self, microvm_id):
        return NS(state=self.states.pop(0) if len(self.states) > 1 else self.states[0])

    def terminate(self, microvm_id):
        self.terminated.append(microvm_id)

    def wait_for_state(self, microvm_id, wanted, timeout):
        self.waits.append((microvm_id, tuple(wanted)))


@pytest.fixture
def sdk(monkeypatch):
    calls = {"run": [], "adopt": [], "waits": 0}

    class Sandbox:
        def __init__(self, region):
            self.microvm_id, self.endpoint = "vm-1", "https://vm-1.example"

        def run(self, **kwargs):
            calls["run"].append(kwargs)

        @staticmethod
        def adopt(region, microvm_id, endpoint, token):
            calls["adopt"].append((microvm_id, endpoint, token))
            lifecycle = calls.get("lifecycle", "RUNNING")
            sandbox = NS(lifecycle=lifecycle, session=None)
            if lifecycle not in handler.GONE:
                sandbox.session = NS(wait_until_ready=lambda timeout: None)

            def wait_until_running(timeout):
                calls["waits"] += 1
                sandbox.lifecycle = "RUNNING"

            sandbox.wait_until_running = wait_until_running
            return sandbox

    monkeypatch.setattr(handler.microvms, "Sandbox", Sandbox)
    monkeypatch.setattr(handler.jobs, "mark", lambda *a, **k: None)
    for name, value in {
        "IMAGE_ARN": "image",
        "IMAGE_VERSION": "1.0",
        "GUEST_ROLE_ARN": "role",
    }.items():
        monkeypatch.setenv(name, value)
    return calls


def test_launch_is_retry_safe_and_leaves_the_wait_to_the_next_step(sdk):
    vm = handler.launch("job-1", "token-1")
    [run] = sdk["run"]
    assert run["client_token"] == "job-1" and run["agent_token"] == "token-1"
    assert run["wait"] is False and run["egress"] is True and run["auto_resume"]
    assert run["max_duration_sec"] == handler.TASK_SECONDS + 900
    assert (vm["microvmId"], vm["endpoint"]) == ("vm-1", "https://vm-1.example")


@pytest.mark.parametrize(("lifecycle", "waits"), [("PENDING", 1), ("RUNNING", 0)])
def test_prepare_adopts_and_finishes_a_pending_launch(
    sdk, monkeypatch, lifecycle, waits
):
    sdk["lifecycle"] = lifecycle
    monkeypatch.setattr(handler, "s3_get", lambda *_: b"")
    session = NS(
        wait_until_ready=lambda timeout: None,
        upload_tar=lambda *_: None,
        run_sync=lambda *a, **k: NS(ok=False, stderr="stop here"),
    )
    real_adopt = handler.microvms.Sandbox.adopt

    def adopt(*args):
        sandbox = real_adopt(*args)
        sandbox.session = session
        return sandbox

    monkeypatch.setattr(handler.microvms.Sandbox, "adopt", staticmethod(adopt))
    with pytest.raises(RuntimeError, match="stop here"):
        handler.prepare("job-1", "claude-code", VM, "token-1")
    assert sdk["adopt"] == [("vm-1", "https://vm-1.example", "token-1")]
    assert sdk["waits"] == waits


def test_connect_names_a_vm_that_is_gone(sdk):
    sdk["lifecycle"] = "TERMINATED"
    with pytest.raises(RuntimeError, match="vm-1 is TERMINATED"):
        handler.connect(VM, "token-1")


@pytest.mark.parametrize(
    ("state", "terminated"), [("RUNNING", ["vm-1"]), ("TERMINATED", [])]
)
def test_terminate_is_idempotent_and_waits_for_terminated(
    monkeypatch, state, terminated
):
    control = Plane([state])
    monkeypatch.setattr(handler, "plane", lambda: control)
    handler.terminate(VM)
    assert control.terminated == terminated
    assert control.waits == [("vm-1", ("TERMINATED",))]


@pytest.mark.parametrize(
    ("state", "gone"),
    [
        ("RUNNING", False),
        ("SUSPENDED", False),
        ("TERMINATING", True),
        ("TERMINATED", True),
    ],
)
def test_vm_gone_reads_the_control_plane(monkeypatch, state, gone):
    monkeypatch.setattr(handler, "plane", lambda: Plane([state]))
    assert handler.vm_gone(VM) is gone
