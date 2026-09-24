# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for CLI-7, CLI-8, and CLI-9 in spec/core.symspec.json (issue #216).
# `model/src/output.rs` checks the same requirements as a Stateright model, and the
# bolero harness in `microvms-cli/src/closed_output_fuzz.rs` fuzzes the output layer
# against them. Each scenario's tags name the requirements it verifies.
#
# "Closed" means the reader end of the pipe is gone before or while the CLI writes, which
# is what `microvm ... | head -1` does. The step closes the pipe itself, so the result does
# not depend on timing.

Feature: A closed stdout or stderr never crashes the CLI or changes its outcome

  Rule: CLI-7 — the exit status is a row of the exit table, never a panic or a signal

    @CLI-7 @CLI-8
    Scenario: help with stdout closed before the first byte
      When I run "microvm --help" with stdout closed after 0 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: version with stdout closed before the first byte
      When I run "microvm --version" with stdout closed after 0 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: a subcommand's help with stdout closed before the first byte
      When I run "microvm keepalive --help" with stdout closed after 0 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: the bare constants document with stdout closed before the first byte
      When I run "microvm constants --emit-json" with stdout closed after 0 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

  Rule: CLI-8 — the command's own outcome decides the exit code

    @CLI-7 @CLI-8
    Scenario: the manifest with stdout closed before the first byte
      When I run "microvm manifest" with stdout closed after 0 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: the manifest with stdout closed partway through the document
      # The manifest is larger than a 64 KiB pipe buffer, so the CLI is still writing
      # when the reader leaves.
      When I run "microvm manifest" with stdout closed after 4096 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: a JSON listing with stdout closed before the first byte
      When I run "microvm --json ls" with stdout closed after 0 bytes
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: a refused argument keeps its failure code when stdout is closed
      When I run "microvm history" with stdout closed after 0 bytes
      Then the CLI exited with code 2
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: help with stderr closed
      When I run "microvm --help" with stderr closed
      Then the CLI exited with code 0
      And the CLI did not panic

    @CLI-7 @CLI-8
    Scenario: the manifest with both streams closed
      When I run "microvm manifest" with stdout and stderr closed
      Then the CLI exited with code 0

  Rule: CLI-9 — a stream whose reader leaves stops, detaches, and says how to reattach

    @CLI-9 @live
    Scenario: a streamed exec stops when its stdout reader closes
      # Needs a running VM, which the shipped binary reaches only through the AWS control
      # plane. `cargo test` leaves it out and says so; `mise run live` runs it against the
      # suite's kept VM by setting MICROVM_BDD_ATTACH (see conformance/run_rs.py,
      # `drive_closed_output_bdd`). In-crate, the guard
      # `a_stream_whose_reader_leaves_stops_detaches_and_exits_interrupted` covers it over a
      # scripted daemon.
      Given a VM attached through MICROVM_BDD_ATTACH
      When I stream a ticker exec and close stdout after the first chunk
      Then the CLI exited with code 11 within 30 seconds
      And the CLI did not panic
      And stderr names the exec id and how to reattach
      And the exec is still running on the daemon
