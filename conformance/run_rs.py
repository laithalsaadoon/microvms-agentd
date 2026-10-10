#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.40", "httpx>=0.27"]
# ///
# SPDX-License-Identifier: Apache-2.0
"""The live conformance suite: the **Rust** client stack, driven through the `microvm` CLI.

This is the only live suite. It drives the real CLI against AWS and records each named check as
PASS or FAIL, with none recorded SKIP. A check's name is its identity: a report diffs line for
line against earlier runs, the last run of the deleted Python oracle (`conformance/run.py`)
among them, and every check that suite had still carries its name byte for byte.

This file is the entry point and holds only the command line. The suite is the three packages
beside it. They import as top-level packages because Python puts this file's directory first on
`sys.path` when it runs a script, whatever the working directory:

- `harness/`: the envelope and the exceptions it becomes, the CLI driver, `Results` and the
  section runner, what a log may print, the raw daemon client, the hostile archives, and the
  suite's own image;
- `lanes/`: the checks, one module per area, and `lanes/suite.py`, which runs them in order
  against one account;
- `selftest/`: the offline half, with the stub CLI and the helpers' negative twins;
- `drivers/`, beside them and imported by none: the scripts `lanes/bindings.py` runs under each
  binding's own runtime.

A hybrid driver, and every path is deliberate
---------------------------------------------

1. **The CLI, through `--json` envelopes.** The client under test, and the whole protocol
   surface: lifecycle, exec identity, file and tar transfer, streaming, stdin, health. Every
   invocation also verifies CLI-4 for free: `Cli.call` parses the whole of stdout as one JSON
   document, so a stray `println!` anywhere in the Rust crate turns this suite red rather than
   being noticed by nobody.

   `exec --stream` is the one documented exception and has its own reader, `Cli.call_stream`,
   which asserts the shape rather than tolerating it: every line but the last parses as an
   event, the last parses as the envelope, and its `type` is `microvm.exec.stream` rather than
   `microvm.exec`. A streaming invocation read with `Cli.call` would fail on the parse, which
   is correct: the two shapes are different contracts, and the driver shouldn't have one
   function that accepts either.

2. **Raw `httpx`, for the checks that test the DAEMON** (`lanes/bootstrap.py`). The raw
   run-hook POST and the raw status-code sends. The only callers of `/run` are the platform
   itself and an attacker inside the VM, and the rest assert on a status integer the daemon
   chose. They aren't about the client, so the client they go through doesn't matter, and
   adding a raw-request escape to the CLI so they could go through it would violate CLI-2 and
   CLI-5 to make a report look tidier.

   Raw rather than through a client library, and that's the *stronger* shape for what these
   checks mean. They assert on the status integer directly (409, 200, 401, 400), where the
   deleted Python suite asserted on the exception its own taxonomy mapped that integer to. One
   layer fewer between the daemon's decision and the assertion about it, and no way for a
   client's status table to be the thing that passes.

3. **The Python and Node bindings, through drivers** (`lanes/bindings.py`, `drivers/`). Each
   binding is built from the working tree and a small script of each language opens the
   binding's tunnel and port-forward handles and imports a name record against the kept VM,
   through the binding's public API alone. The request through each handle is the suite's own,
   with httpx, so the client on this side of a handle is never the code under test.

`Results.skipped` stays in the summary as a count that should read zero: a suite that removed
its own ability to report a skip is a suite whose next gap is silent.

Money
-----

This run creates real MicroVMs and is billable, about 20 minutes: about 15 for the main flow,
about five for `drive_idle_keepalive`, which launches a second VM from the image already built
and deliberately waits out a 60-second idle window twice, plus `drive_agent_vm`, which builds
the two-agent image on the arm64 builder, launches a VM with egress, and pays for two model
calls on Bedrock. Building the two bindings costs no AWS call and runs on a thread beside the
suite image's build: from an empty target directory on a 16-core host at 0552d8c the Python
build took 129 s and the Node build after it 92 s, and a smaller runner takes longer. It
belongs to `mise run live` and is never hooked. `--self-test` is the offline half: it drives the envelope-to-exception mapping, the NDJSON stream reader and the
lanes' own helpers against a stub `microvm` script and touches no account.

Usage:
    conformance/run_rs.py --self-test          # offline, free
    conformance/run_rs.py --binary target/aarch64-unknown-linux-musl/release/agentd \
        --microvm-binary target/release/microvm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lanes.suite import run_suite
from selftest.suite import self_test


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="drive the envelope→exception mapping against a stub. Offline and free",
    )
    parser.add_argument(
        "--binary", type=Path, help="the aarch64 agentd binary to bake in"
    )
    parser.add_argument(
        "--microvm-binary",
        type=Path,
        default=Path("target/release/microvm"),
        help="the `microvm` CLI under test",
    )
    parser.add_argument(
        "--keep", action="store_true", help="skip teardown (leaks resources)"
    )
    parser.add_argument(
        "--infra-dir",
        type=Path,
        help="the Terraform directory whose outputs name the stack (default: conformance/infra)",
    )
    parser.add_argument(
        "--only",
        choices=["ensure_image"],
        help="run one self-contained section instead of the suite: it builds its own image",
    )
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    return run_suite(args)


if __name__ == "__main__":
    sys.exit(main())
