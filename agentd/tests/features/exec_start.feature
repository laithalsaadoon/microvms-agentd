# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for AGENTD-7 through AGENTD-16 in spec/agentd.symspec.json (issues #224,
# #225, #226). `model/src/exec_start.rs` checks the same requirements as a Stateright model,
# and the bolero harness in `agentd/src/exec_start_fuzz.rs` fuzzes the resolution against
# them. Each scenario's tags name the requirements it verifies.
#
# The daemon runs in-process behind its real router, with its passwd and group databases
# pointed at files the scenario writes. The guest user "tester" carries this test process's
# own uid and gid, which is what lets a demotion actually spawn without root: setuid to
# your own uid is always permitted.

Feature: A start request names its user, group and shell, and may inherit the image's ENV

  Background:
    Given a guest whose passwd lists "tester" with this process's uid and gid and home "/home/tester"
    And a guest whose group file lists "crew" with this process's gid
    And a daemon that inherited the environment:
      | PATH        | /image/bin:/usr/bin:/bin |
      | HOME        | /root                    |
      | JAVA_HOME   | /opt/java                |
      | AGENTD_PORT | 9000                     |
    And the run hook installed the token "tok-bdd-7f3a9c" with the launch environment:
      | FROM_LAUNCH | launch |

  Rule: AGENTD-7 — a name is resolved in the guest before the child is spawned

    @AGENTD-7 @AGENTD-9
    Scenario: a user named by string runs as its passwd row
      Given a start of "id -u; id -g; echo $HOME $USER $LOGNAME" under shell "true"
      And the user named "tester"
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And output line 1 is this process's uid
      And output line 2 is this process's gid
      And output line 3 is "/home/tester tester tester"

    @AGENTD-7
    Scenario: a group named by string runs as its group row
      Given a start of "id -g" under shell "true"
      And the user named "tester"
      And the group named "crew"
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And output line 1 is this process's gid

  Rule: AGENTD-8 — an unknown name is a 400 and nothing is spawned

    @AGENTD-8
    Scenario: an unknown user is refused with unknown_user and spawns nothing
      Given a start that would leave a marker file, under shell "true"
      And the user named "nobody-here"
      When the daemon answers the start
      Then the answer is 400 with error "unknown_user" naming "nobody-here"
      And no child was spawned

    @AGENTD-8
    Scenario: an unknown group is refused with unknown_group and spawns nothing
      Given a start that would leave a marker file, under shell "true"
      And the group named "no-such-crew"
      When the daemon answers the start
      Then the answer is 400 with error "unknown_group" naming "no-such-crew"
      And no child was spawned

  Rule: AGENTD-9 — a passwd row supplies HOME, USER and LOGNAME beneath the caller's own

    @AGENTD-9
    Scenario: the request's HOME overrides the passwd HOME
      Given a start of "echo $HOME $USER" under shell "true"
      And the user named "tester"
      And the request sets "HOME" to "/work"
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And output line 1 is "/work tester"

  Rule: AGENTD-10 — without inherit_image_env the environment is the launch map and the request's

    @AGENTD-10
    Scenario: a child that asks for nothing gets exactly the launch environment
      Given a start of the argv "/usr/bin/env"
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And the child's environment is exactly:
        | FROM_LAUNCH | launch |

  Rule: AGENTD-11 — with inherit_image_env the image ENV is the lowest layer

    @AGENTD-11 @AGENTD-12
    Scenario: the image ENV reaches a child that inherits it, under the launch environment
      Given a start of the argv "/usr/bin/env"
      And the request inherits the image environment
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And the child's environment is exactly:
        | PATH        | /image/bin:/usr/bin:/bin |
        | HOME        | /root                    |
        | JAVA_HOME   | /opt/java                |
        | FROM_LAUNCH | launch                   |

    @AGENTD-11 @AGENTD-9
    Scenario: a demoted user's passwd HOME overrides the image HOME, and the request's PATH the image PATH
      Given a start of the argv "/usr/bin/env"
      And the user named "tester"
      And the request inherits the image environment
      And the request sets "PATH" to "/request/bin"
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And the child's environment is exactly:
        | PATH        | /request/bin |
        | HOME        | /home/tester |
        | USER        | tester       |
        | LOGNAME     | tester       |
        | JAVA_HOME   | /opt/java    |
        | FROM_LAUNCH | launch       |

  Rule: AGENTD-12 — the token and the daemon's configuration never reach a child

    @AGENTD-12
    Scenario: a child that inherits the image ENV holds neither the token nor AGENTD_ configuration
      Given a start of the argv "/usr/bin/env"
      And the request inherits the image environment
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And the child's output does not contain the token
      And the child's output does not contain "AGENTD_"

    @AGENTD-12
    Scenario: the startup snapshot drops every AGENTD_ variable
      When the daemon snapshots a startup environment of:
        | PATH           | /usr/bin     |
        | AGENTD_PORT    | 9000         |
        | AGENTD_LOG     | info         |
        | AGENTD_ANYTHING | would-be-secret |
      Then the snapshot is exactly:
        | PATH | /usr/bin |

  Rule: AGENTD-13 — health reports the snapshot's size and never its values

    @AGENTD-13
    Scenario: health counts the image environment without echoing it
      When health is read
      Then health reports 3 image environment keys
      And the health body does not contain "/opt/java"

  Rule: AGENTD-14 — a named shell is resolved in the guest and runs the script

    @AGENTD-14
    Scenario: bash runs a pipefail pipeline
      Given a start of "set -o pipefail; false | true" under shell "bash"
      When the daemon answers the start
      Then the start was accepted and the child exited 1

    @AGENTD-14
    Scenario: the named shell is the program that runs
      Given a start of "echo $0" under shell "bash"
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And output line 1 ends with "bash"

  Rule: AGENTD-15 — a shell the guest does not have is a 400 and nothing is spawned

    @AGENTD-15
    Scenario: a missing shell name is refused with unknown_shell and spawns nothing
      Given a start that would leave a marker file, under shell "no-such-shell-9q"
      When the daemon answers the start
      Then the answer is 400 with error "unknown_shell" naming "no-such-shell-9q"
      And no child was spawned

    @AGENTD-15
    Scenario: a missing absolute shell path is refused with unknown_shell and spawns nothing
      Given a start that would leave a marker file, under shell "/nonexistent/bin/bash"
      When the daemon answers the start
      Then the answer is 400 with error "unknown_shell" naming "/nonexistent/bin/bash"
      And no child was spawned

  Rule: AGENTD-16 — integers and booleans keep their protocol-1 meaning

    @AGENTD-16
    Scenario: an integer user and a true shell run as that uid under /bin/sh
      Given a start of "id -u; echo $0" under shell "true"
      And the user numbered with this process's uid
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And output line 1 is this process's uid
      And output line 2 is "/bin/sh"

    @AGENTD-16 @AGENTD-9
    Scenario: an integer uid with no passwd row sets no identity variables
      Given the guest's passwd no longer lists this process's uid
      And a start of the argv "/usr/bin/env"
      And the user numbered with this process's uid
      When the daemon answers the start
      Then the start was accepted and the child exited 0
      And the child's environment is exactly:
        | FROM_LAUNCH | launch |
