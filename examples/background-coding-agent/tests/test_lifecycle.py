# SPDX-License-Identifier: Apache-2.0
"""The VM lifecycle goes through the SDK's awaitables: launch once, adopt in every later
step. The fakes define only the `_async` spellings, so a blocking call fails here."""

import asyncio
import threading
from types import SimpleNamespace as NS

import handler
import pytest

VM = {"microvmId": "vm-1", "endpoint": "https://vm-1.example", "launched_at": 0.0}


class Plane:
    def __init__(self, states):
        self.states, self.terminated, self.waits = list(states), [], []

    async def get_async(self, microvm_id):
        return NS(state=self.states.pop(0) if len(self.states) > 1 else self.states[0])

    async def terminate_async(self, microvm_id):
        self.terminated.append(microvm_id)

    async def wait_for_state_async(self, microvm_id, wanted, timeout):
        self.waits.append((microvm_id, tuple(wanted)))


def use_plane(monkeypatch, control):
    async def plane():
        return control

    monkeypatch.setattr(handler, "plane", plane)


class Session:
    def __init__(self, calls):
        self.calls = calls

    async def wait_until_ready_async(self, timeout):
        await self.calls["on_ready"]()

    async def upload_tar_async(self, remote, archive):
        self.calls["uploads"].append((remote, archive))

    async def run_sync_async(self, command, **kwargs):
        self.calls["commands"].append((command, kwargs))
        return self.calls["results"].pop(0)

    def exec(self, exec_id):
        async def poll_async():
            self.calls["polled"].append(exec_id)
            return NS(
                stdout="out", stderr="err", exit_code=0, timed_out=False, done=True
            )

        return NS(poll_async=poll_async)

    async def download_tar_async(self, remote):
        return b"artifacts"

    async def file_exists_async(self, path):
        return True


@pytest.fixture
def sdk(monkeypatch):
    async def ready():
        pass

    calls = {
        "run": [],
        "adopt": [],
        "waits": 0,
        "uploads": [],
        "commands": [],
        "results": [NS(ok=False, stderr="stop here")],
        "polled": [],
        "saved": {},
        "on_ready": ready,
    }

    class Sandbox:
        def __init__(self):
            self.microvm_id, self.endpoint = "vm-1", "https://vm-1.example"

        @staticmethod
        async def create_async(region):
            return Sandbox()

        async def run_async(self, **kwargs):
            calls["run"].append(kwargs)

        @staticmethod
        async def adopt_async(region, microvm_id, endpoint, token):
            calls["adopt"].append((microvm_id, endpoint, token))
            lifecycle = calls.get("lifecycle", "RUNNING")
            sandbox = NS(lifecycle=lifecycle, session=None)
            if lifecycle not in handler.GONE:
                sandbox.session = Session(calls)

            async def wait_until_running_async(timeout):
                calls["waits"] += 1
                sandbox.lifecycle = "RUNNING"

            sandbox.wait_until_running_async = wait_until_running_async
            return sandbox

    async def mint(region, ttl_seconds):
        return "bedrock-token"

    async def install(session, agents, token):
        calls["installed"] = token

    monkeypatch.setattr(handler.microvms, "Sandbox", Sandbox)
    monkeypatch.setattr(handler.microvms, "mint_bedrock_token_async", mint)
    monkeypatch.setattr(handler.microvms, "install_agent_access_async", install)
    monkeypatch.setattr(handler, "read_inputs", lambda _: (b"source", b"task"))
    monkeypatch.setattr(
        handler, "s3_put", lambda _, name, data: calls["saved"].update({name: data})
    )
    monkeypatch.setattr(handler.jobs, "mark", lambda *a, **k: None)
    for name, value in {
        "IMAGE_ARN": "image",
        "IMAGE_VERSION": "1.0",
        "GUEST_ROLE_ARN": "role",
    }.items():
        monkeypatch.setenv(name, value)
    return calls


def test_launch_is_retry_safe_and_leaves_the_wait_to_the_next_step(sdk):
    vm = asyncio.run(handler.launch("job-1", "token-1"))
    [run] = sdk["run"]
    assert run["client_token"] == "job-1" and run["agent_token"] == "token-1"
    assert run["wait"] is False and run["egress"] is True and run["auto_resume"]
    assert run["max_duration_sec"] == handler.TASK_SECONDS + 900
    assert (vm["microvmId"], vm["endpoint"]) == ("vm-1", "https://vm-1.example")


@pytest.mark.parametrize(("lifecycle", "waits"), [("PENDING", 1), ("RUNNING", 0)])
def test_prepare_adopts_and_finishes_a_pending_launch(sdk, lifecycle, waits):
    sdk["lifecycle"] = lifecycle
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(handler.prepare("job-1", "claude-code", VM, "token-1"))
    assert sdk["adopt"] == [("vm-1", "https://vm-1.example", "token-1")]
    assert sdk["waits"] == waits
    assert "installed" not in sdk


def test_prepare_boots_the_vm_while_a_thread_reads_the_inputs(sdk, monkeypatch):
    # The boot and the read each wait for the other to have started, which only a
    # concurrent gather satisfies: in series, the first waits out its timeout and fails.
    # The fake boot yields before it starts, as the real one does, so a read run on the
    # loop's own thread blocks it and fails too.
    booting, reading = threading.Event(), threading.Event()

    async def ready():
        await asyncio.sleep(0)
        booting.set()
        assert await asyncio.to_thread(reading.wait, 2), "the boot waited for the read"

    def read(job_id):
        reading.set()
        assert booting.wait(2), "the read waited for the boot"
        return b"source", b"task"

    async def mint(region, ttl_seconds):
        sdk["commands_before_mint"] = len(sdk["commands"])
        return "bedrock-token"

    sdk["on_ready"] = ready
    sdk["results"] = [NS(ok=True, stderr="")]
    monkeypatch.setattr(handler, "read_inputs", read)
    monkeypatch.setattr(handler.microvms, "mint_bedrock_token_async", mint)
    asyncio.run(handler.prepare("job-1", "claude-code", VM, "token-1"))
    assert sdk["uploads"] == [
        ("/workspace/project", b"source"),
        ("/workspace/task", b"task"),
    ]
    # The token's lifetime starts after the setup command, as late as it can.
    assert sdk["commands_before_mint"] == 1
    assert sdk["installed"] == "bedrock-token"


def test_start_reattaches_by_the_job_id(sdk, monkeypatch):
    started = {}

    async def prompt(session, agent, task, **kwargs):
        started.update(kwargs, agent=agent.agent, task=task)

    monkeypatch.setattr(handler.microvms, "prompt_agent_async", prompt)
    asyncio.run(handler.start("job-1", "review", "codex", VM, "token-1"))
    assert started["exec_id"] == "job-1" and started["reap_group_on_exit"]
    assert started["agent"] == "codex" and "Review the pull request" in started["task"]


def test_collect_keeps_the_transcript_when_the_export_fails(sdk):
    sdk["results"] = [NS(ok=False, stderr="export broke")]
    with pytest.raises(RuntimeError, match="export broke"):
        asyncio.run(handler.collect("job-1", "implement", VM, "token-1"))
    assert sdk["polled"] == ["job-1"]
    assert sdk["saved"] == {"results/stdout.txt": b"out", "results/stderr.txt": b"err"}


def test_collect_saves_the_results_and_reports_the_exit(sdk):
    result = asyncio.run(handler.collect("job-1", "review", VM, "token-1"))
    assert result == {"exit_code": 0, "timed_out": False, "report": True}
    assert sdk["commands"] == []
    assert sdk["saved"]["results/artifacts.tar"] == b"artifacts"


def test_connect_names_a_vm_that_is_gone(sdk):
    sdk["lifecycle"] = "TERMINATED"
    with pytest.raises(RuntimeError, match="vm-1 is TERMINATED"):
        asyncio.run(handler.connect(VM, "token-1"))


@pytest.mark.parametrize(
    ("state", "terminated"), [("RUNNING", ["vm-1"]), ("TERMINATED", [])]
)
def test_terminate_is_idempotent_and_waits_for_terminated(
    monkeypatch, state, terminated
):
    control = Plane([state])
    use_plane(monkeypatch, control)
    asyncio.run(handler.terminate(VM))
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
    use_plane(monkeypatch, Plane([state]))
    assert asyncio.run(handler.vm_gone(VM)) is gone
