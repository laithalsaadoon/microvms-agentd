# Live validation

## Current design: unverified against AWS and GitHub

The GitHub-driven workflow (DynamoDB job records, Secrets Manager token,
draft PR and review publishing, `wait_for_condition` polling) has passed only
local checks. It has not yet run on AWS or against a real GitHub repository.

## Earlier design, 2026-09-18, us-east-1

The previous version took a local Git checkout and returned a patch. It shared
this version's VM launch, preparation, agent start, polling keepalive, and
collection code paths.

- An implementation task (add optional priority sorting to a small Python
  module) succeeded in about 2 minutes 16 seconds, with durable waits between
  polling invocations.
- The agent exited 0; the returned patch applied to a fresh clone, and its 10
  tests passed.
- A separate AWS API read confirmed the VM was `TERMINATED`.

Private receipts from that run are under `.agent/simple-live/`, which is not
committed.
