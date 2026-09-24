# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for BIND-6 through BIND-10 in spec/core.symspec.json (issue #222).
# `model/src/run.rs` checks the same requirements as a Stateright model, and the bolero
# harness in `microvms-core/tests/run_to_completion_fuzz.rs` fuzzes the composition against
# them. Each scenario's tags name the requirements it verifies.
#
# The daemon here is the scripted one in `tests/sim_daemon/mod.rs`, on tokio's paused clock:
# every fate, fault, and deadline below is declared, so a scenario's ordering is caused rather
# than timed. The three fallbacks issue #222 names are the cut stream, the client timeout
# with a successful kill, and the client timeout with a failed kill.

Feature: run_to_completion drives one exec to exactly one result

  Rule: BIND-8 — a stream that ends without its exit event falls back to wait and ack

    @BIND-8 @BIND-6
    Scenario: a streamed command that exits is acked once, with every chunk delivered
      Given a command that prints "hello" and exits with code 0 after 2 seconds
      When I run it to completion with an output callback
      Then the callback received "hello"
      And the result's stdout is "hello"
      And the result's POSIX exit code is 0
      And the result has no notes
      And the daemon saw no poll
      And the exec was acked exactly once

    @BIND-8
    Scenario: a cut stream falls back to wait and ack
      Given a command that prints "hello" and exits with code 0 after 5 seconds
      And the stream is cut after its output on every attach
      When I run it to completion with an output callback
      Then the callback received "hello"
      And the result's stdout is "hello"
      And the result's POSIX exit code is 0
      And the daemon saw a poll
      And the exec was acked exactly once

    @BIND-8
    Scenario: a failed ack after the exit event falls back to wait and ack
      Given a command that prints "hello" and exits with code 3 after 1 seconds
      And the first ack fails
      When I run it to completion with an output callback
      Then the result's POSIX exit code is 3
      And the daemon saw a poll
      And the exec was acked exactly once

  Rule: BIND-9 — the client deadline kills the process group before it acks

    @BIND-9 @BIND-6 @BIND-7
    Scenario: a client timeout with a successful kill returns the killed exec's result
      Given a command that prints "tick" and never finishes by itself
      And a kill ends it after 1 seconds
      And a timeout of 2 seconds and a client grace of 3 seconds
      When I run it to completion with an output callback
      Then the kill was sent before the last ack
      And the result is not synthesized
      And the result's stdout is "tick"
      And the result's POSIX exit code is 124
      And the result has a note containing "client deadline of 5s"

    @BIND-9 @BIND-6
    Scenario: a failed kill of a command that finishes during the grace keeps its own status
      Given a command that prints "late" and exits with code 0 after 6 seconds
      And the kill request fails
      And a timeout of 2 seconds and a client grace of 3 seconds
      When I run it to completion
      Then the kill was sent before the last ack
      And the result is not synthesized
      And the result's POSIX exit code is 0

  Rule: BIND-10 — 124 is synthesized only when the post-kill ack fails

    @BIND-10 @BIND-6 @BIND-7
    Scenario: a client timeout with a failed kill synthesizes 124
      Given a command that prints "tick" and never finishes by itself
      And the kill request fails
      And a timeout of 2 seconds and a client grace of 3 seconds
      When I run it to completion
      Then the daemon saw a kill
      And the result is synthesized
      And the result's POSIX exit code is 124
      And the result has a note containing "synthesized"
      And the result has a note containing "kill reset"

  Rule: BIND-6 and BIND-7 — the POSIX exit code and the notes say what ended the command

    @BIND-6 @BIND-7
    Scenario: the daemon's own deadline reports 124 with a note and needs no kill
      Given a command that the daemon's deadline ends with signal 15 after 2 seconds
      And a timeout of 2 seconds and a client grace of 60 seconds
      When I run it to completion with an output callback
      Then the result's POSIX exit code is 124
      And the result has a note containing "timeout_sec"
      And the daemon saw no kill

    @BIND-6
    Scenario: a signal death with no deadline reports 128 plus the signal
      Given a command that dies to signal 9 after 1 seconds
      When I run it to completion
      Then the result's POSIX exit code is 137
      And the result has no notes

    @BIND-7
    Scenario: truncated output carries a note
      Given a command that prints "x" and exits with code 0 after 1 seconds
      And its output was truncated at the cap
      When I run it to completion
      Then the result's POSIX exit code is 0
      And the result has a note containing "output cap"
