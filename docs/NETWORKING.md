# Networking

**No internet egress requires a VPC without an internet gateway (IGW) or NAT
gateway.** Attach that VPC through a custom network connector. Audit the
selected subnet routes for other internet paths, including IPv6 gateways,
transit networks, peering, and proxies. Private services and VPC endpoints
remain reachable if your VPC configuration permits them.

Omitting the managed `INTERNET_EGRESS` connector does not disable internet
access on the default MicroVM network. `--deny-egress` only sets proxy variables;
a workload can ignore them. Neither is an isolation boundary.

## Create a VPC connector

The [Lambda core API](https://docs.aws.amazon.com/lambda/latest/lambda-core/Welcome.html)
manages connectors separately from the `lambda-microvms` API. Use a current
boto3 release to create and manage connectors. This package accepts existing
connector ARNs at launch; it does not manage the connectors' lifecycle.

Provision the VPC, subnets, security groups, and a Lambda connector operator
role first. The operator role needs permissions to manage the connector's
network interfaces; it is separate from the VM execution role. The following
example creates a real AWS resource:

```python
import os
import time
import uuid

import boto3

core = boto3.client("lambda-core", region_name="us-east-1")
connector = core.create_network_connector(
    Name="isolated-microvms",
    ClientToken=str(uuid.uuid4()),
    OperatorRole=os.environ["CONNECTOR_OPERATOR_ROLE_ARN"],
    Configuration={
        "VpcEgressConfiguration": {
            "SubnetIds": [os.environ["ISOLATED_SUBNET_ID"]],
            "SecurityGroupIds": [os.environ["CONNECTOR_SECURITY_GROUP_ID"]],
            "NetworkProtocol": "IPv4",
            "AssociatedComputeResourceTypes": ["MicroVm"],
        }
    },
)
arn = connector["Arn"]
deadline = time.monotonic() + 900
while True:
    status = core.get_network_connector(Identifier=arn)
    if status.get("State") == "ACTIVE":
        break
    if status.get("State") != "PENDING":
        raise RuntimeError(status.get("StateReason", status.get("State")))
    if time.monotonic() >= deadline:
        raise TimeoutError(f"Connector still pending: {arn}")
    time.sleep(5)
print(arn)
```

Creation is asynchronous and there is no built-in boto3 waiter. Preserve the
client token when retrying the same creation request. After updates, inspect
`LastUpdateStatus` as well as `State`; an active connector can have a failed
configuration update. Delete an unused connector explicitly with
`delete_network_connector(Identifier=arn)` and confirm completion.

## Attach it at launch

```bash
microvm run --image my-image \
  --egress-network-connector "$CONNECTOR_ARN" \
  --exec "echo hello"
```

Persist the list in `microvm.toml` as `egress-network-connectors = ["ARN"]`.
Explicit flags replace that configured list. The flag is repeatable up to
the API's connector limit. Do not combine it with
`--egress`, which selects the managed internet connector. Python launch
methods accept `egress_network_connectors=[arn]`; Node launch options use
`egressNetworkConnectors: [arn]`. The AWS wire field is
`egressNetworkConnectors` on `RunMicrovm`.

An attached connector ARN is not proof of isolation. The package does not
audit its VPC, route tables, or security groups. Verify public destinations
are unreachable from the launched VM and private destinations behave as
intended. Custom connectors and VPC resources are managed separately from VM
cleanup.

The VM execution role remains available through guest metadata. Restrict
that role even when the VPC has no internet route; see [Trust](TRUST.md).

## Evidence

Reviewed 2026-09-16 against boto3/botocore 1.43.95, Lambda core API version
`2026-04-30`, and MicroVM API version `2025-09-09`. Connector configuration and
state transitions above come from AWS documentation and SDK models. The
example passed boto3 parameter validation with stubbed responses; it was not
live-executed during this documentation refresh.
Earlier default-network measurements are retained in [Platform](PLATFORM.md).
