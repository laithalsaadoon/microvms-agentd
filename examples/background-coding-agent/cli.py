# SPDX-License-Identifier: Apache-2.0
"""Submit GitHub issues and pull requests, then check on them from anywhere."""

import io
import json
import re
import tarfile
import uuid
from pathlib import Path
from typing import Annotated, Literal

import boto3
import jobs
from cyclopts import App, Parameter
from jobs import Job
from rich.console import Console
from rich.table import Table

REF = re.compile(
    r"^(?:https://github\.com/)?(?P<repo>[\w.-]+/[\w.-]+)"
    r"(?:#|/issues/|/pull/)(?P<number>\d+)/?$"
)
CONFIG = Path(__file__).parent / ".agent" / "config.json"

app = App(help=__doc__)
console = Console()


def parse_ref(ref: str) -> tuple[str, int]:
    match = REF.match(ref.strip())
    if not match:
        raise ValueError(
            f"expected owner/repo#N or a GitHub issue or PR URL, got {ref!r}"
        )
    return match["repo"], int(match["number"])


def config() -> dict:
    settings = json.loads(CONFIG.read_text())
    jobs.bind(settings["table"], settings["region"])
    return settings


def aws(service: str, settings: dict):
    return boto3.client(service, region_name=settings["region"])


def find(prefix: str) -> Job:
    matches = list(Job.scan(Job.id.startswith(prefix)))
    if len(matches) != 1:
        raise SystemExit(f"{len(matches)} jobs match {prefix!r}")
    return matches[0]


@app.command
def submit(
    ref: str,
    *,
    agent: Literal["claude-code", "codex"] = "claude-code",
    note: Annotated[str | None, Parameter(help="Extra guidance for the agent.")] = None,
):
    """Start a job. An issue gets a draft PR; a pull request gets a review."""
    settings = config()
    repo, number = parse_ref(ref)
    job = Job(id=uuid.uuid4().hex, repo=repo, number=number, agent=agent, note=note)
    job.save(condition=Job.id.does_not_exist())
    try:
        response = aws("lambda", settings).invoke(
            FunctionName=settings["function_arn"],
            InvocationType="Event",
            DurableExecutionName=job.id,
            Payload=json.dumps({"id": job.id}).encode(),
        )
    except Exception as error:
        jobs.mark(job.id, status="FAILED", error=f"invoke failed: {error}"[:1000])
        raise
    jobs.mark(job.id, execution_arn=response["DurableExecutionArn"])
    console.print(
        f"Submitted [bold]{job.id}[/] for {repo}#{number}. You can disconnect."
    )


@app.command(name="list")
def list_jobs(*, limit: int = 20):
    """Show recent jobs."""
    config()
    table = Table("id", "target", "kind", "status", "phase", "result")
    for job in sorted(Job.scan(), key=lambda j: j.created_at, reverse=True)[:limit]:
        table.add_row(
            job.id[:12],
            f"{job.repo}#{job.number}",
            job.kind or "",
            job.status,
            job.phase,
            job.url or job.error or "",
        )
    console.print(table)


@app.command
def show(job_id: str):
    """Show one job, reconciling it with its durable execution."""
    settings = config()
    job = find(job_id)
    execution = None
    if job.execution_arn:
        execution = aws("lambda", settings).get_durable_execution(
            DurableExecutionArn=job.execution_arn
        )
        # A stopped or timed-out execution never reaches its own failure step.
        if job.status in jobs.ACTIVE and execution["Status"] not in (
            "RUNNING",
            "SUCCEEDED",
        ):
            jobs.mark(job.id, status="FAILED", error=f"execution {execution['Status']}")
            job.refresh()
    record = {k: v for k, v in job.attribute_values.items() if k != "expires_at"}
    if execution:
        record["execution_status"] = execution["Status"]
    console.print_json(json.dumps(record, default=str))


@app.command
def fetch(job_id: str, *, output: Path | None = None):
    """Download the agent's report, transcript, and patch."""
    settings = config()
    job = find(job_id)
    output = output or Path("results") / job.id
    output.mkdir(parents=True, exist_ok=True)
    s3 = aws("s3", settings)
    for name in ("stdout.txt", "stderr.txt"):
        s3.download_file(
            settings["bucket"], f"{job.id}/results/{name}", str(output / name)
        )
    body = s3.get_object(
        Bucket=settings["bucket"], Key=f"{job.id}/results/artifacts.tar"
    )
    with tarfile.open(fileobj=io.BytesIO(body["Body"].read())) as archive:
        archive.extractall(output, filter="data")
    console.print(f"Saved to {output}")


@app.command
def cancel(job_id: str):
    """Stop the workflow and terminate its VM."""
    settings = config()
    job = find(job_id)
    if job.status not in jobs.ACTIVE:
        raise SystemExit(f"{job.id} is already {job.status}")
    if job.execution_arn:
        client = aws("lambda", settings)
        execution = client.get_durable_execution(DurableExecutionArn=job.execution_arn)
        if execution["Status"] == "RUNNING":
            client.stop_durable_execution(DurableExecutionArn=job.execution_arn)
    # A stopped workflow runs no cleanup step, so terminate its VM here.
    if job.microvm_id:
        microvms = aws("lambda-microvms", settings)
        try:
            microvms.terminate_microvm(microvmIdentifier=job.microvm_id)
        except microvms.exceptions.ResourceNotFoundException:
            pass
    jobs.mark(job.id, status="CANCELLED")
    console.print(f"Cancelled {job.id}")


if __name__ == "__main__":
    app()
