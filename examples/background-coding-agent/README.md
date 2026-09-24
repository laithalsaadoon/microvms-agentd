# Background coding agent

Point Claude Code or Codex at a GitHub issue or pull request, close your laptop,
and come back to a draft PR or a posted review. A Lambda durable function runs
the job in a MicroVM, and DynamoDB tracks each job.

```bash
uv run cli.py submit https://github.com/you/app/issues/42   # → draft PR
uv run cli.py submit you/app#57                             # PR → review
uv run cli.py list
```

## How it works

```text
cli.py submit ──► DynamoDB job (QUEUED) ──► durable Lambda (async invoke)
                                               │
   stage: GitHub issue/PR + tarball → S3       │  GitHub token stays here
   launch MicroVM → upload code and task       │
   start agent ──► poll ◄── durable wait (30s) │  Lambda is suspended between polls
   collect report, transcript, changes → S3    │
   terminate VM                                ▼
   publish: draft PR (issue) or COMMENT review (PR) → DynamoDB (SUCCEEDED + URL)
```

| File | Role |
| --- | --- |
| [handler.py](handler.py) | The durable workflow and every MicroVM operation |
| [github_ops.py](github_ops.py) | GitHub reads before the VM starts and writes after it stops (PyGithub) |
| [jobs.py](jobs.py) | The DynamoDB job record (PynamoDB) |
| [cli.py](cli.py) | `submit`, `list`, `show`, `fetch`, `cancel` (cyclopts) |
| [infra/main.tf](infra/main.tf) | Lambda, DynamoDB, S3, Secrets Manager, and IAM |

The workflow decides the mode itself: a plain issue is implemented, and an open
pull request is reviewed. Every step is recorded by Lambda durable execution, so
a retried or replayed invocation reuses finished steps. A stable client token,
exec ID, branch name, and review marker make retrying launch, agent start, and
publishing safe.

Each step runs in a fresh process, so the VM is managed through the `microvms`
SDK by its record: the launch step calls `Sandbox.run(client_token=…,
agent_token=…, wait=False)`, every later step calls `Sandbox.adopt(region,
microvm_id, endpoint, agent_token)`, and polling and cleanup use `ControlPlane`
(`get`, `terminate`, `wait_for_state`). No step calls the MicroVM API directly.

## Deploy once

You need the repository's normal AWS and MicroVM prerequisites, Bedrock model
access, Python 3.13, uv, and Terraform. The Lambda is arm64 and every dependency,
the `microvms` binding included, installs from published wheels, so `deploy.sh`
packages it on any OS. Building the agent image needs Cargo for the daemon. Build
it from the neighboring example (from the repository root):

```bash
microvm build target/aarch64-unknown-linux-musl/release/agentd \
  --reuse --name coding-agents \
  --dockerfile examples/coding-agents-on-bedrock/Dockerfile --json
```

Deploy with the returned image ARN and version:

```bash
cd examples/background-coding-agent
uv sync --frozen
export TF_VAR_image_arn='arn:aws:lambda:us-east-1:ACCOUNT:microvm-image:NAME'
export TF_VAR_image_version='1.0'
./deploy.sh
```

Deployment writes `.agent/config.json` and prints the command that stores your
GitHub token in Secrets Manager. Use a
[fine-grained token](https://github.com/settings/personal-access-tokens) limited
to the repositories you want the agent on, with **Contents** and **Pull
requests** read/write and **Issues** read. `gh auth token` works too, but it
grants everything your account can do. Changes under `.github/workflows/` also
need **Workflows** write access; without it, publishing those changes fails.

Set `TF_VAR_task_seconds` (600–3600, default 1800) to change the agent's time
budget. The VM's maximum lifetime is 15 minutes longer.

## Submit and check jobs

```bash
uv run cli.py submit you/app#42 --agent codex --note "Keep the public API stable."
uv run cli.py list
uv run cli.py show 3f2a9c          # any unique ID prefix
uv run cli.py fetch 3f2a9c         # → results/<id>/
uv run cli.py cancel 3f2a9c
```

`submit` returns as soon as the job is queued. `list` and `show` read DynamoDB;
`show` also checks the durable execution and marks a stopped or timed-out one
as failed, because those never reach the workflow's own failure step. `fetch`
downloads `REPORT.md`, the agent's stdout and stderr, and for issues
`patch.diff`. Failed jobs usually still have output to fetch.

For an issue, the agent works on the default branch's current commit. Its
changes become one commit on `agent/issue-<n>-<id>` and a draft PR whose body
is the agent's report plus `Closes #<n>`. Repositories whose plan does not allow
drafts get a regular PR. No changes means no PR, and the job ends as
`no-changes`.

For a pull request, the agent receives the PR head and its diff. It writes a
review and optional line comments. The review is always a `COMMENT`; the agent
never approves or requests changes. If GitHub rejects a line comment, the
comments are folded into the review body instead.

## Trust boundaries

- Issue and PR text is untrusted. The prompt labels it as such, but prompt
  injection is not solved; review every PR the agent opens before merging.
- The GitHub token is read only by Lambda. The guest gets a source tarball and
  the task text, and publishing happens after the VM is terminated.
- The guest has a short-lived Bedrock bearer token. Published text is scanned
  for Bedrock bearer tokens and redacted, and publishing refuses a change that
  contains one. The token is signed with the Lambda role's credentials, so it
  cannot outlive them.
- The guest role can only write MicroVM logs, and the agent runs as uid 1000.
  The guest has internet egress for installing dependencies. For network
  isolation, see the repository's egress guidance: omitting egress does not
  enforce it.
- The agentd token is stored in durable execution history, which is private
  account data retained for 14 days. The VM it opens is terminated by then.

## Failure and cleanup

Normal completion and caught errors terminate the VM before publishing. Brief
transport errors while polling are retried as long as the VM exists. `cancel`
stops the workflow and terminates the VM; a job cancelled in the seconds before
its VM ID is recorded relies on the VM's maximum lifetime instead. Terminate a
stray VM with `microvm terminate <id>`.

Job records, inputs, and outputs expire after 30 days. The bucket uses SSE-S3;
if your jobs carry private source, add a customer-managed KMS key. `terraform -chdir=infra
destroy` deletes the stack, including the job bucket, table, and token secret.
Images are managed separately.

## Checks

Run `mise run background:check` from the repository root. The tests drive the
real durable SDK through suspension and replay, the job table through moto, and
the real change-export script through Git. They check that failures record the
job and terminate the VM exactly once, that publishing retries do not duplicate
PRs or reviews, and that deletions, executable bits, symlinks, and leaked tokens
are handled. Live status is in [LIVE_TEST.md](LIVE_TEST.md).
