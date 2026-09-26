# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for BIND-11, BIND-12, and BIND-13 in spec/core.symspec.json (issue #227).
# `model/src/posture.rs` checks the same requirements as a Stateright model, and the bolero
# harness in `microvms-app/src/control/posture_fuzz.rs` fuzzes the decision against the launch.
# Run by `microvms-core/tests/bdd_posture.rs` against the core the bindings wrap; the launches
# go through a scripted control plane, so no scenario makes an AWS call.
#
# A posture is a claim about outbound network. `sealed` needs a VPC egress connector and
# separately verified VPC routing without an internet gateway or NAT gateway
# (docs/NETWORKING.md). No launch option carries that audit, so no row below is `sealed`.

Feature: The egress posture a harness can read before and after a launch

  Rule: BIND-11 and BIND-13 — the request-side answer is the decision table, and never sealed

    @BIND-11 @BIND-13
    Scenario Outline: the posture of a set of launch options
      Given launch options with egress <egress>, <connectors> VPC connectors, and deny <deny>
      When the harness asks for their egress posture
      Then the answer is "<posture>"
      And no AWS call was made

      Examples:
        | egress | connectors | deny  | posture     |
        | false  | 0          | false | unsealed    |
        | false  | 0          | true  | best-effort |
        | false  | 1          | false | unsealed    |
        | false  | 1          | true  | best-effort |
        | false  | 10         | false | unsealed    |
        | true   | 0          | false | open        |

    @BIND-13
    Scenario Outline: launch options the launch would refuse
      Given launch options with egress <egress>, <connectors> VPC connectors, and deny <deny>
      When the harness asks for their egress posture
      Then the answer is an invalid-argument refusal mentioning "<reason>"
      And the launch refuses the same options with the same message
      And no AWS call was made

      Examples:
        | egress | connectors | deny  | reason                       |
        | true   | 0          | true  | opposite things              |
        | true   | 1          | false | INTERNET_EGRESS cannot be    |
        | false  | 11         | false | NetworkConnectorList ceiling |

    @BIND-13
    Scenario: a connector that is not a connector ARN in the launch region
      Given launch options with one VPC connector "arn:aws:lambda:us-west-2:123456789012:network-connector:private"
      When the harness asks for their egress posture in us-east-1
      Then the answer is an invalid-argument refusal mentioning "customer-managed Lambda network connector ARN"
      And the launch refuses the same options with the same message
      And no AWS call was made

    @BIND-11
    Scenario: a harness that must not start without network isolation rejects the default launch
      Given launch options with egress false, 0 VPC connectors, and deny false
      When the harness asks for their egress posture
      Then the answer is not "sealed"

  Rule: BIND-12 — a session reports what the envelope reports for its launch options

    @BIND-12
    Scenario Outline: a launched session carries its launch's posture
      Given launch options with egress <egress>, <connectors> VPC connectors, and deny <deny>
      When the harness asks for their egress posture
      And the sandbox launches them
      Then the session reports "<posture>"
      And the session's posture equals the answer

      Examples:
        | egress | connectors | deny  | posture     |
        | false  | 0          | false | unsealed    |
        | false  | 0          | true  | best-effort |
        | false  | 1          | false | unsealed    |
        | true   | 0          | false | open        |

    @BIND-12
    Scenario: a session attached directly does not know its launch options
      When the harness attaches a session directly
      Then the session reports "unsealed"

    @BIND-12
    Scenario: a VM adopted by another process does not carry its launch's posture
      Given launch options with egress true, 0 VPC connectors, and deny false
      When the sandbox launches them
      And another process adopts the VM
      Then the session reports "open"
      And the adopted session reports "unsealed"
