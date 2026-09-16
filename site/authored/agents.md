---
title: For agents
description: Machine-readable contracts, automation rules, and documentation entry points.
editUrl: false
sidebar:
  order: 1
---

:::agent

**For an agent.** Start with `microvm manifest`. It returns commands, flags, response types, and
exit codes as JSON without credentials or network access. `microvm manifest
--dense` gives a compact command list. Prefer the installed binary's contract
over prose that may describe a different version.

:::

## 1. Automation

- Use `--json`. Commands write one envelope to stdout and progress to stderr.
  `exec --stream` emits NDJSON events and the final envelope.
- Branch on error `code` and `exitCode`, not message text. `ERR_EXEC_FAILED`
  means the command in the VM failed; it does not imply an AWS failure.
- Read `leaked` after cleanup. Teardown is attempted by default, but failures
  and process termination can leave resources behind. `microvm ls --remote`
  compares the local ledger with AWS.
- Keep per-VM secrets out of shared images. The guest can access its execution
  role; user demotion does not hide it.
- No internet egress requires a VPC without an IGW or NAT gateway. Neither
  omitting `--egress` nor setting `--deny-egress` enforces isolation.
- Keep `agentd` as the image's `CMD` and start workloads only after bootstrap.

## 2. Entry points

| Task | Contract or guide |
|---|---|
| CLI integration | `microvm manifest`, [Reference](/reference/) |
| Direct daemon integration | `GET /v1/schema`, [Protocol](/internals/protocol/) |
| Rust, Python, Node | [Libraries](/learn/tutorial/from-code/) |
| First AWS run | [First-run tutorial](/learn/tutorial/first-run/) |
| Coding agents inside a VM | [Agents on Bedrock](/learn/operations/run-coding-agents-on-bedrock/) |
| Network isolation | [Networking](/learn/operations/configure-networking/) |
| Image integration | [Embedding](/internals/embedding/) |

`microvm doctor` checks prerequisites before a build. `quickstart` creates
billable AWS resources, runs a hello-world, and attempts cleanup. Reuse images
with `--image` to avoid unnecessary builds and retention charges.

## 3. Repository work

Read `CONTRIBUTING.md`. Run `mise run check` for local verification and
`mise run docs:check` for documentation. Live AWS behavior needs a separate
exercise of the changed path; `mise run live` is billable. Report when it has
not been run, and verify cleanup independently afterward.

Edit `site/authored/` for user guides and top-level `docs/*.md` for contracts.
`site/src/content/docs/` is generated. References generated from source contain
commit-pinned citations that may be stale in the current checkout; check the
source instead of treating prose as proof. Record new AWS measurements with
date, region, and API version, retaining earlier observations.

## 4. Read next

<!--READ-NEXT-->

## 5. Machine-readable documentation

Append `.md` to a page path: `/reference/cli/` becomes `/reference/cli.md`.
Fetch the individual page when its location is known.
[llms.txt](/llms.txt) indexes the corpus; [llms-small.txt](/llms-small.txt) and
[llms-full.txt](/llms-full.txt) provide bundles. [schema.json](/schema.json)
is the generated daemon wire contract.
