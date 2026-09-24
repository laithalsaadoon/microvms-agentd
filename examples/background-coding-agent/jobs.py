# SPDX-License-Identifier: Apache-2.0
"""The job record: one DynamoDB item per submitted issue or pull request."""

import os
from datetime import UTC, datetime, timedelta

from pynamodb.attributes import (
    NumberAttribute,
    TTLAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.models import Model

RETENTION = timedelta(days=30)
ACTIVE = ("QUEUED", "RUNNING")


class Job(Model):
    class Meta:
        table_name = os.environ.get("JOBS_TABLE")
        region = os.environ.get("AWS_REGION")

    id = UnicodeAttribute(hash_key=True)
    repo = UnicodeAttribute()
    number = NumberAttribute()
    agent = UnicodeAttribute()
    note = UnicodeAttribute(null=True)
    # Filled in by the workflow: "implement" for an issue, "review" for a PR.
    kind = UnicodeAttribute(null=True)
    status = UnicodeAttribute(default="QUEUED")
    phase = UnicodeAttribute(default="queued")
    execution_arn = UnicodeAttribute(null=True)
    microvm_id = UnicodeAttribute(null=True)
    url = UnicodeAttribute(null=True)
    error = UnicodeAttribute(null=True)
    exit_code = NumberAttribute(null=True)
    cost = UnicodeAttribute(null=True)
    created_at = UTCDateTimeAttribute(default_for_new=lambda: datetime.now(UTC))
    updated_at = UTCDateTimeAttribute(default=lambda: datetime.now(UTC))
    expires_at = TTLAttribute(default_for_new=RETENTION)


def bind(table: str, region: str) -> None:
    """Point the model at a deployed table; PynamoDB reconnects on a name change."""
    Job.Meta.table_name = table
    Job.Meta.region = region
    Job._connection = None


def mark(job_id: str, **fields) -> None:
    actions = [Job.updated_at.set(datetime.now(UTC))]
    actions += [
        getattr(Job, name).remove() if value is None else getattr(Job, name).set(value)
        for name, value in fields.items()
    ]
    Job(job_id).update(actions=actions, condition=Job.id.exists())
