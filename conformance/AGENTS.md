# conformance

The live suite: `run_rs.py` drives the real `microvm` CLI against AWS and records each named
check as PASS or FAIL. `infra/` is the Terraform stack it runs in. Everything here except the
self-test is billable.

`run_rs.py` is the entry point and holds only the command line. The suite is the three
packages beside it, which import as top-level packages because Python puts a script's own
directory first on its module search path, whatever the working directory:

- `harness/`: what every section drives the CLI and the daemon through, `Results` among it
  (`harness/results.py`). It imports nothing from the lanes.
- `lanes/`: the checks, one module per area. A section is a `drive_*` function in its area's
  module, and `lanes/suite.py` runs the sections in the order they need, with the reasons
  beside them. A new check goes in its section; a new section also gets its line there.
- `selftest/`: the offline half. `selftest/suite.py` runs every twin; a lane's twins, where it
  has them, are in the `selftest/` module of the same name.

`drivers/` holds what the suite runs outside its own interpreter: `drivers/handles.py` and
`drivers/handles.mjs` drive the Python and Node bindings' tunnel, port-forward and import handles
through each binding's public API alone, over one JSON-lines protocol (the Python file's
docstring has it). `lanes/bindings.py` builds both bindings from the working tree on a thread
started before the suite's VM launches (uv and maturin, npx and the napi CLI, at the pins CI's
`bindings:py` and `dts` tasks use, which its self-test twin holds), then runs each driver
against the kept VM and makes the request through each handle itself, with httpx. A live run
therefore needs Node on PATH as well as cargo and uv; `mise run live` gets it from `mise.toml`,
and `live-conformance.yml` installs it.

- Offline first: `./conformance/run_rs.py --self-test` exercises the suite's own helpers and
  their negative twins, and `mise run live:check` checks the live tier's wiring in
  `.config/mise/tasks/live.toml`. Both are free, and `mise run check` runs both (the first as
  `conformance:self-test`).
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
- A new live task needs `tools/check-live-wiring.py` to accept it, and a new AWS behavior
  needs a check here or an explicit statement that it's unverified against AWS.
