# Security

Report suspected vulnerabilities privately through
[GitHub Security Advisories](https://github.com/laithalsaadoon/microvms-agentd/security/advisories/new).
Include the version or commit, a reproduction, and the region/API version for
AWS behavior. There is no guaranteed response time. Use public issues for
non-sensitive bugs and design proposals.

## Threat model

[Trust](docs/TRUST.md) defines the boundaries; [Protocol](docs/PROTOCOL.md)
defines enforced behavior. The workload may be hostile, but a root workload
shares the guest with `agentd` and is not isolated from it.

The image must start `agentd` as `CMD`, with workloads started only after
bootstrap. The platform's lifecycle hook arrives over loopback without a
credential. Bootstrap therefore installs a token exactly once; identical
replays succeed and conflicting replays fail. An image that runs untrusted
processes before bootstrap violates the deployment requirement.

## Reportable findings

- Bypassing agent-token authentication on a protected control route.
- Replacing the installed bootstrap token or leaking it into child environments.
- Escaping a tar extraction root through archive paths or links.
- Crashing the daemon through unauthenticated requests, or bypassing
  authorization-before-body-processing.
- Violating another enforced protocol guarantee under the supported deployment.

`/v1/health` and `/v1/schema` are intentionally unauthenticated. An authorized
caller can execute commands as root and read or change guest files. User
demotion is a convenience, not a separate security boundary. Resource
exhaustion by an authorized workload is constrained by VM limits.

## AWS credentials and networking

The guest can retrieve the VM execution role's credentials through metadata,
including from a non-root workload. Grant only permissions every workload may
use. VPC isolation does not remove metadata credentials.

No internet egress requires a VPC without an internet gateway or NAT gateway,
attached through a VPC network connector. Audit routes for alternative internet
paths. Omitting the managed internet connector does not seal the default
network, and `--deny-egress` only sets proxy variables that workloads can bypass.
See [Trust](docs/TRUST.md) for measured behavior and limitations.

## Supply chain

The project distributes crates, Python wheels, a Node package, and release
binaries. Release workflows use OIDC publishing and attestations where
supported. Automatic daemon provisioning verifies provenance through `gh`
when available and release checksums otherwise; these provide different
assurance levels.

`mise run security` checks shipped source, secrets, license headers,
dependencies, and workflows. CI also produces SBOMs and runs vulnerability
scanners. Accepted findings and reasons live in `.vex/`, `.trivyignore.yaml`,
and `osv-scanner.toml`.
