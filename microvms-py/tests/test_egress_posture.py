# SPDX-License-Identifier: Apache-2.0
"""The egress posture a harness reads before and after a launch (#227; BIND-11, BIND-12, BIND-13).

`egress_posture_for` is the request-side answer: what `Sandbox.run` with the same options will
report, or the `InvalidArgError` it will raise, with no AWS call. `Session.egress_posture` is
the launched session's value, the same string the CLI envelope's `egressPosture` carries. The
launch half is asserted in Rust (`microvms-cli/src/guards.rs` runs the CLI and the core over
one scripted launch; the live suite repeats it against AWS), because a unit run here has no
control plane to launch against.

`sealed` needs a VPC egress connector and separately verified VPC routing without an internet
gateway or NAT gateway. No launch option carries that audit, so nothing here answers `sealed`,
and a harness that must not start without network isolation rejects every launch it cannot
vouch for.
"""

from __future__ import annotations

import pytest

import microvms

VPC = "arn:aws:lambda:us-east-1:123456789012:network-connector:isolated-vpc"


@pytest.mark.parametrize(
    ("egress", "connectors", "deny_egress", "posture"),
    [
        (False, [], False, "unsealed"),
        (False, [], True, "best-effort"),
        (False, [VPC], False, "unsealed"),
        (False, [VPC], True, "best-effort"),
        (True, [], False, "open"),
    ],
)
def test_the_request_side_answer_is_the_decision_table(
    egress: bool, connectors: list[str], deny_egress: bool, posture: str
) -> None:
    """BIND-11 and BIND-13: the rows of the model's table, positionally and by keyword."""
    assert microvms.egress_posture_for(egress, connectors, deny_egress) == posture
    assert (
        microvms.egress_posture_for(
            egress=egress,
            connectors=connectors,
            deny_egress=deny_egress,
            region=microvms.Region.us_east_1(),
        )
        == posture
    )


def test_the_defaults_are_a_default_launch_and_it_is_not_sealed() -> None:
    """BIND-11: a harness gating `disable_internet` on this rejects the default launch."""
    assert microvms.egress_posture_for() == "unsealed"
    assert microvms.egress_posture_for() != "sealed"


@pytest.mark.parametrize(
    ("egress", "connectors", "deny_egress", "reason"),
    [
        (True, [], True, "opposite things"),
        (True, [VPC], False, "INTERNET_EGRESS cannot be combined"),
        (False, ["not-an-arn"], False, "network connector ARN"),
        (False, [VPC] * 11, False, "NetworkConnectorList ceiling"),
    ],
)
def test_options_the_launch_refuses_raise_its_refusal(
    egress: bool, connectors: list[str], deny_egress: bool, reason: str
) -> None:
    """BIND-13: the launch's own `InvalidArgError`, before any AWS call."""
    with pytest.raises(microvms.InvalidArgError, match=reason):
        microvms.egress_posture_for(egress, connectors, deny_egress)


def test_a_connector_is_checked_against_the_launch_region_when_one_is_given() -> None:
    """BIND-13: without a region an ARN is checked against the region it names."""
    elsewhere = "arn:aws:lambda:eu-west-1:123456789012:network-connector:vpc"
    assert microvms.egress_posture_for(False, [elsewhere], False) == "unsealed"
    with pytest.raises(microvms.InvalidArgError, match="in us-east-1"):
        microvms.egress_posture_for(
            False, [elsewhere], False, region=microvms.Region.us_east_1()
        )


def test_a_session_without_its_launch_options_reports_unsealed() -> None:
    """BIND-12: a direct session holds no launch options, so it claims nothing."""
    session = microvms.Session.direct("http://127.0.0.1:9", "agent-token")
    assert session.egress_posture == "unsealed"


def test_every_posture_is_one_of_the_four_wire_labels() -> None:
    """The same spelling the CLI envelope's `egressPosture` uses."""
    labels = {"open", "unsealed", "best-effort", "sealed"}
    for egress, deny in [(False, False), (False, True), (True, False)]:
        assert microvms.egress_posture_for(egress, [], deny) in labels
