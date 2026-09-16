# Strategy: the trust and preparation contract for coding agents on microVMs

Provide a reusable guest daemon, client libraries, and measured AWS behavior
for teams building on Lambda MicroVMs. Scheduling, pooling, and task routing
belong to consumer applications.

## Diagnosis

A workload and its control daemon share the guest. Bootstrap, credential
handling, filesystem operations, and lifecycle recovery therefore need
explicit contracts. Image preparation is also a recurring cost: dependencies
can be built once and reused across launches.

The platform changes independently of this package. SDK models describe
request shapes; only runtime measurements establish service behavior.
[Platform](PLATFORM.md) records those observations, with dates and corrections.

## Guiding policy

Keep shared behavior in core and use thin CLI/Python/Node adapters. Validate
requests before billable operations, preserve meaningful errors, and test
failure paths. State limits directly: guest root is not isolated from the
daemon, metadata exposes the execution role, and internet isolation requires
a correctly configured VPC connector.

## Coherent actions

1. Maintain the [wire protocol](PROTOCOL.md) and [trust contract](TRUST.md),
   with model, unit, property, and live conformance coverage appropriate to
   each claim.
2. Reuse images by build inputs and dependency lockfiles. Keep per-run code
   and credentials out of shared snapshots.
3. Maintain published CLI and language packages from one implementation.
   Refresh API references with boto3 and check serializer/SDK parity.
4. Keep documentation short: tutorials explain tasks, generated references
   describe contracts, and platform notes retain dated evidence.

The agent helpers are a deliberate convenience layer over core primitives;
[Agent VMs](AGENT-VMS.md) defines their bounded scope.

## What we are deliberately not doing

- A scheduler, pool manager, or general orchestrator.
- Guest-side process-tree cloning as a substitute for provider snapshots.
- A new turn-boundary suspend protocol: consumers can use existing lifecycle
  calls and endpoint health polling.
- Competing with AgentCore by duplicating its managed execution product.

## The AWS ask, and the honest bet

Reusable snapshots of running VM state require provider support. Existing
suspend/resume preserves a single VM, but does not establish a reusable clone
API. Prefer measured capabilities over predictions about future AWS features.

## How we would know this worked

Consumers can integrate the daemon without rediscovering bootstrap or network
assumptions, reuse environments across launches, and upgrade packages without
surprises between the CLI and language bindings. Live coverage remains scoped
to the regions and paths actually exercised.

## What the first draft got wrong

The earlier strategy prioritized a turn-boundary suspend convention even
though existing hooks and lifecycle APIs already covered it. It also mixed
human interactive idle time with headless batch workloads. The lesson is to
measure the target workload and check existing platform capabilities before
adding an abstraction. Full research notes remain in git history.
