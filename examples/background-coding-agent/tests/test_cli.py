# SPDX-License-Identifier: Apache-2.0
import json

import cli
import pytest


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
