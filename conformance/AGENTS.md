# conformance

The live suite: `run_rs.py` drives the real `microvm` CLI against AWS and records each named
check as PASS or FAIL. `infra/` is the Terraform stack it runs in. Everything here except the
self-test is billable.

- Offline first: `./conformance/run_rs.py --self-test` exercises the suite's own helpers and
  their negative twins, and `mise run live:check` checks the live tier's wiring in `mise.toml`.
  Both are free, and `mise run check` runs both (the first as `conformance:self-test`).
- `results.eq` fails when either side is `None`, because that's what a missing key reads as
  through `.get()`. A check that expects nothing on purpose uses `results.absent`.
- Live runs: `mise run live` for everything, or `mise run live:conformance-rs` for this suite.
  Both build the release CLI and the daemon from the working tree and apply `infra/` first.
  Afterward, run `mise run live:verify-clean` to confirm no VMs, images or log groups leaked.
- The Terraform state is local and gitignored (`infra/terraform.tfstate`), and resource names
  carry a random suffix. Never apply on an empty state: it creates a second stack. Copy
  `conformance/infra/terraform.tfstate` in from the checkout that last ran live, then run
  `terraform -chdir=conformance/infra init -input=false` and
  `terraform -chdir=conformance/infra plan -detailed-exitcode`, and read the plan before any
  live task applies it. Copy the state back once the run has exited.
- A check's name is its identity: reports diff line for line against earlier runs, so rename
  one only on purpose. Prefix it with the requirement it proves (`BIND-18 ...`).
- A new live task needs `scripts/check-live-wiring.py` to accept it, and a new AWS behavior
  needs a check here or an explicit statement that it's unverified against AWS.
