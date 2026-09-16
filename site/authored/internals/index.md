---
title: Internals
description: Protocol, trust boundaries, measured AWS behavior, and source analyses.
---

Use [Learn](/learn/) for walkthroughs and [Reference](/reference/) for command
syntax. These pages explain contracts and design decisions.

| Document | Purpose |
|---|---|
| [Platform](/internals/platform/) | Dated AWS measurements and corrections |
| [Networking](/internals/networking/) | VPC connectors and internet isolation |
| [Protocol](/internals/protocol/) | Authentication, execution, files, and streaming |
| [Trust](/internals/trust/) | Bootstrap, workload boundaries, credentials, and egress |
| [Embedding](/internals/embedding/) | Image contract and custom harness integration |
| [Agent VMs](/internals/agent-vms/) | Agent lifecycle and Bedrock integration |
| [Strategy](/internals/strategy/) | Scope and priorities |
| [Harness capabilities](/internals/harness-capabilities/) | Integration requirements and gaps |

Generated source analyses cover [architecture](/internals/architecture/system-overview/),
[behavior](/internals/behavior/processes/), [risks](/internals/analysis/risk-hotspots/),
[diagrams](/internals/diagrams/architecture/components/), and
[maintenance](/internals/insights/impact-analysis/). The
[CLI coverage plan](/internals/cli-coverage-plan/) is retained as history.

Source citations link to a commit, but the cited line may describe older code.
Prefer the current executable contract and implementation when a page
conflicts with them. AWS runtime observations have a date, region, and API
version; later corrections retain the original measurement.
