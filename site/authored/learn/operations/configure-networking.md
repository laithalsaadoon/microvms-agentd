---
title: Configure networking
description: Attach a VPC connector and understand which settings enforce internet isolation.
---

No internet egress requires a VPC without an internet gateway or NAT gateway,
with no alternative internet route. Create a VPC network connector, wait for
it to become active, and attach its ARN at launch:

```bash
microvm run --image my-image \
  --egress-network-connector "$CONNECTOR_ARN" \
  --exec "echo hello"
```

[Networking](/internals/networking/) gives the boto3 setup and validation
steps. The package does not audit the VPC's routes. Omitting `--egress` only
omits the managed internet connector; `--deny-egress` only sets proxy variables
that a workload can ignore. Keep the execution role minimal because its
credentials remain available inside the guest.
