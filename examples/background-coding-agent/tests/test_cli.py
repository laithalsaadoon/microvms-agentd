# SPDX-License-Identifier: Apache-2.0
import io
import json
from types import SimpleNamespace as NS

import cli
import pytest
from rich.console import Console


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("octo/app#12", ("octo/app", 12)),
        ("https://github.com/octo/app/issues/12", ("octo/app", 12)),
        ("https://github.com/octo/my.app/pull/7/", ("octo/my.app", 7)),
    ],
)
def test_parse_ref(ref, expected):
    assert cli.parse_ref(ref) == expected


def test_parse_ref_rejects_other_urls():
    with pytest.raises(ValueError):
        cli.parse_ref("https://github.com/octo/app/tree/main")


class Lambda:
    status = "RUNNING"

    def invoke(self, **kwargs):
        assert kwargs["InvocationType"] == "Event"
        self.payload = json.loads(kwargs["Payload"])
        return {"DurableExecutionArn": "arn:execution"}

    def get_durable_execution(self, DurableExecutionArn):
        return {"Status": self.status}


def test_submit_records_the_job_and_show_reconciles_a_stopped_run(
    table, tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"region": "us-east-1", "table": "jobs-test", "function_arn": "fn:1"}
        )
    )
    monkeypatch.setattr(cli, "CONFIG", config)
    client = Lambda()
    monkeypatch.setattr(cli, "aws", lambda *_: client)
    cli.submit("https://github.com/octo/app/issues/12", agent="codex")
    [job] = table.scan()
    assert client.payload == {"id": job.id}
    assert (job.repo, job.number, job.agent, job.status) == (
        "octo/app",
        12,
        "codex",
        "QUEUED",
    )
    assert job.execution_arn == "arn:execution"
    assert job.expires_at > job.created_at
    client.status = "STOPPED"
    cli.show(job.id[:8])
    job.refresh()
    assert (job.status, job.error) == ("FAILED", "execution STOPPED")


def test_job_list_renders_stored_text_literally(monkeypatch):
    monkeypatch.setenv("COLUMNS", "240")
    output = io.StringIO()
    console = Console(
        file=output, force_terminal=True, color_system="truecolor", width=240, height=25
    )
    message = (
        "file\x1b[2J\x1b]52;c;value\x07[link=https://example.invalid]text[/link]\x85"
    )
    job = NS(
        created_at=0,
        id="0123456789ab",
        repo="octo/app",
        number=7,
        kind="implement",
        status="FAILED",
        phase="publish",
        url=None,
        error=message,
    )
    monkeypatch.setattr(cli, "config", lambda: None)
    monkeypatch.setattr(cli, "Job", NS(scan=lambda: [job]))
    monkeypatch.setattr(cli, "console", console)
    cli.list_jobs()
    rendered = output.getvalue()
    assert "\x1b]52;" not in rendered and "\x1b[2J" not in rendered
    assert "\x1b]8;" not in rendered and "\x85" not in rendered
    assert r"\x1b[2J" in rendered and r"\x07" in rendered and r"\x85" in rendered
    assert "[link=https://example.invalid]text[/link]" in rendered


def test_every_job_cell_uses_literal_text(monkeypatch):
    monkeypatch.setenv("COLUMNS", "240")
    output = io.StringIO()
    job = NS(
        created_at=0,
        id="[bold]id[/]",
        repo="[bold]repo[/]",
        number=7,
        kind="[bold]kind[/]",
        status="[bold]status[/]",
        phase="[bold]phase[/]",
        url="[bold]url[/]",
        error=None,
    )
    monkeypatch.setattr(cli, "config", lambda: None)
    monkeypatch.setattr(cli, "Job", NS(scan=lambda: [job]))
    monkeypatch.setattr(cli, "console", Console(file=output, width=240, height=25))
    cli.list_jobs()
    rendered = output.getvalue()
    for field in ["id", "repo", "kind", "status", "phase", "url"]:
        assert f"[bold]{field}[/]" in rendered
