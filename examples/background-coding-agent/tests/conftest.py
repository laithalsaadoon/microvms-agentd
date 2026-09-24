# SPDX-License-Identifier: Apache-2.0
import jobs
import pytest
from moto import mock_aws


@pytest.fixture
def table(monkeypatch):
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        jobs.bind("jobs-test", "us-east-1")
        jobs.Job.create_table(billing_mode="PAY_PER_REQUEST", wait=True)
        yield jobs.Job
