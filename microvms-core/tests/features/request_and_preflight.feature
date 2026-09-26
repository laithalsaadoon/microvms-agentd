# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for BIND-14, BIND-15, and BIND-16 in spec/core.symspec.json (issue #223).
# `model/src/preflight.rs` checks the preflight's aggregation and calls as a Stateright model;
# `microvms-domain/src/sizing_fuzz.rs` fuzzes the size-class selection for minimality and
# coverage. Run by `microvms-core/tests/bdd_preflight.rs`; the preflights go through a scripted
# control plane, so no scenario makes an AWS call.

Feature: What a harness checks before it queues work

  Rule: BIND-14 — a resource request selects the smallest class whose baseline covers it

    @BIND-14
    Scenario Outline: a request selects a class
      When a task requests <cpus> vCPU and <memory> MiB
      Then the size class has a <baseline> MiB baseline

      Examples:
        | cpus  | memory | baseline |
        | unset | unset  | 2048     |
        | 0.25  | 512    | 512      |
        | 1     | 2048   | 2048     |
        | 2     | 1024   | 4096     |
        | 0.5   | 3072   | 4096     |
        | unset | 8192   | 8192     |
        | 4     | unset  | 8192     |
        | 0.25  | 513    | 1024     |

    @BIND-14
    Scenario Outline: a request no class covers
      When a task requests <cpus> vCPU and <memory> MiB
      Then the request is refused naming the largest class

      Examples:
        | cpus  | memory |
        | 4.5   | unset  |
        | unset | 8193   |
        | 16    | 32768  |

  Rule: BIND-15 and BIND-16 — one report, true only when a launch could proceed, and no billable call

    @BIND-15 @BIND-16
    Scenario: every check passes in a supported region
      Given the region us-east-1
      And credentials that resolve
      And a service that answers the listing
      When the harness runs a preflight
      Then the preflight is ok
      And the checks are region, credentials, and service, all passing
      And the only AWS call was ListManagedMicrovmImages

    @BIND-15 @BIND-16
    Scenario: an unlisted region is advisory, and the listing decides
      Given the unlisted region ap-south-1
      And credentials that resolve
      And a service that answers the listing
      When the harness runs a preflight
      Then the preflight is ok
      And the region check failed as advisory
      And the only AWS call was ListManagedMicrovmImages

    @BIND-15 @BIND-16
    Scenario: a service that denies the listing fails the preflight
      Given the unlisted region ap-south-1
      And credentials that resolve
      And a service that denies the listing
      When the harness runs a preflight
      Then the preflight is not ok
      And the service check failed as fatal
      And the only AWS call was ListManagedMicrovmImages

    @BIND-15 @BIND-16
    Scenario: credentials that do not resolve stop the preflight before any AWS call
      Given the region us-east-1
      And credentials that do not resolve
      And a service that answers the listing
      When the harness runs a preflight
      Then the preflight is not ok
      And the credentials check failed as fatal
      And the service check was not run
      And no AWS call was made

    @BIND-15 @BIND-16
    Scenario: an environment region the client refuses stops the preflight before any call
      Given the environment names the region eu-central-1
      When the harness runs a preflight with no region
      Then the preflight is not ok
      And the region check failed as fatal
      And the credentials check was not run
      And the service check was not run
      And no AWS call was made
