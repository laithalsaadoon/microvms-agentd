# SPDX-License-Identifier: Apache-2.0
"""The `live_posture` reader, offline."""

from __future__ import annotations

from typing import Any

from harness.results import Results
from lanes.posture import check_execution_role, posture_lines


def check_posture_lines(results: "Results") -> None:
    """The `live_posture` reader keeps each launch's label and ignores every other line."""
    stderr = (
        "running 1 test\n"
        "POSTURE default=unsealed microvmId=mvm-1\n"
        "POSTURE egress=open microvmId=mvm-2\n"
        "POSTURE egress-adopted=unsealed microvmId=mvm-2\n"
        "cleanup microvmId=mvm-1 state=TERMINATED\n"
    )
    results.eq(
        "the posture-line reader maps each launch to its label",
        posture_lines(stderr),
        {"default": "unsealed", "egress": "open", "egress-adopted": "unsealed"},
    )


class FakeIam:
    """Inline and attached policy pages returned without making AWS requests."""

    def __init__(
        self, statements: Any, *, attached: bool = False, second_page: bool = False
    ) -> None:
        self.statements = statements
        self.attached = attached
        self.second_page = second_page

    def get_paginator(self, operation: str) -> Any:
        if operation == "list_role_policies":
            page = {"PolicyNames": ["inline"]}
        else:
            assert operation == "list_attached_role_policies"
            page = {"AttachedPolicies": [{"PolicyArn": "example-managed-policy"}]}
            if not self.attached:
                page = {"AttachedPolicies": []}
        pages = [page]
        if self.second_page:
            pages.insert(0, {key: [] for key in page})

        class Pages:
            def paginate(self, **_: Any) -> Any:
                return iter(pages)

        return Pages()

    def get_role_policy(self, **_: Any) -> dict[str, Any]:
        return {"PolicyDocument": {"Statement": self.statements}}


def check_execution_role_policy(results: Results) -> None:
    """The deployed policy's action and resource verdicts, including their negative twins."""
    role = "arn:aws:iam::000000000000:role/self-test"
    group = "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda-microvms/*"
    stream = group + ":log-stream:*"
    scoped = {
        "Effect": "Allow",
        "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": [group, stream],
    }

    def verdict(statements: Any, **kwargs: Any) -> Results:
        probe = Results(probe=True)
        check_execution_role(FakeIam(statements, **kwargs), role, "us-east-1", probe)
        return probe

    for label, statements in [
        ("group and stream lists", [scoped]),
        (
            "single statement and resource",
            dict(scoped, Action="logs:PutLogEvents", Resource=stream),
        ),
        ("explicit named group", dict(scoped, Resource=group[:-1] + "named")),
        (
            "denied unrelated resource",
            [scoped, dict(scoped, Effect="Deny", Resource="*")],
        ),
    ]:
        probe = verdict(statements)
        results.eq(f"execution role accepts {label}", len(probe.failed), 0)

    for label, resource in [
        ("all resources", "*"),
        ("regional groups", group.replace("/aws/lambda-microvms/*", "*")),
        ("another namespace", group.replace("/lambda-microvms/", "/lambda/")),
        (
            "namespace without a delimiter",
            group.replace("/lambda-microvms/", "/lambda-microvms"),
        ),
        ("another account", group.replace("000000000000", "111111111111")),
        ("another region", group.replace("us-east-1", "us-west-2")),
        ("another partition", group.replace("arn:aws:", "arn:aws-us-gov:")),
        ("mixed resource list", [group, "*"]),
        ("empty resources", []),
    ]:
        probe = verdict(dict(scoped, Resource=resource))
        results.eq(f"execution role rejects {label}", len(probe.failed), 1)

    for label, statement in [
        (
            "absent resources",
            {key: value for key, value in scoped.items() if key != "Resource"},
        ),
        ("NotResource", dict(scoped, NotResource=group)),
        ("NotAction", dict(scoped, NotAction="iam:*")),
        ("another action family", dict(scoped, Action="s3:GetObject")),
    ]:
        probe = verdict(statement)
        results.eq(f"execution role rejects {label}", len(probe.failed), 1)

    probe = verdict([])
    results.eq("execution role requires grants", len(probe.failed), 2)
    probe = verdict(scoped, attached=True, second_page=True)
    results.eq("execution role checks every attached-policy page", len(probe.failed), 2)
    probe = verdict(dict(scoped, Resource="*"), second_page=True)
    results.eq("execution role checks every inline-policy page", len(probe.failed), 1)
