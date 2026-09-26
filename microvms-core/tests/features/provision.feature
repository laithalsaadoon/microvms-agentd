# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for BIND-17, BIND-18, BIND-19, and BIND-20 in spec/core.symspec.json
# (issue #219). `model/src/provision.rs` checks the same requirements as a Stateright
# model, and the bolero harness in `microvms-edges/src/provision_fuzz.rs` fuzzes the
# parsers they rest on. Each scenario's tags name the requirements it verifies.
#
# "The release" is a fake at the release seam: it answers the three operations
# `microvms_core::provision::fetch_release` asks of a release (the asset, an attestation
# bundle for its digest, `SHA256SUMS`) and plays the attestation check, so every scenario
# exercises the real verification policy and nothing opens a socket to GitHub.

Feature: The agentd daemon binary is provisioned by microvms-core, verified or refused

  Rule: BIND-17 — a caller's binary, then the version's cache entry, then the release

    @BIND-17
    Scenario: the first request fetches the core's own version and the second reads the cache
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      When I provision agentd with no version
      Then the binary came from "fetched", verified by "attestation"
      And the binary is the core's own version
      And the release was downloaded 1 time
      When I provision agentd with no version
      Then the binary came from "cache", verified by "attestation"
      And the release was downloaded 1 time

    @BIND-17 @BIND-20
    Scenario: a caller-supplied aarch64 binary outranks a populated cache and never fetches
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And the cache already holds the core's own version
      And the caller supplies an aarch64 ELF binary
      When I provision agentd with the caller's binary
      Then the binary came from "caller-supplied", with no verification
      And the binary is the caller's bytes
      And the release was downloaded 1 time

    @BIND-17
    Scenario: MICROVM_AGENTD supplies the binary when the call does not
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And MICROVM_AGENTD names an aarch64 ELF binary
      When I provision agentd with no version
      Then the binary came from "caller-supplied", with no verification
      And the release was downloaded 0 times

    @BIND-17
    Scenario: another version's cache entry is never served
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And the cache already holds the core's own version
      When I provision agentd version "9.9.9"
      Then the binary came from "fetched", verified by "attestation"
      And the release was asked for tag "v9.9.9"

    @BIND-17
    Scenario Outline: a version that is not a release tag is refused before anything runs
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      When I provision agentd version "<version>"
      Then provisioning failed with "ERR_INVALID_ARG"
      And the release was downloaded 0 times

      Examples:
        | version    |
        | ../../etc  |
        | 1.0/../x   |
        | v          |
        | .hidden    |

  Rule: BIND-18 — a fetch that cannot be verified is an error, never a warning

    @BIND-18
    Scenario: no attestation can be fetched, and the SHA256SUMS entry verifies the download
      Given a release whose agentd is an aarch64 ELF with no attestation to fetch, and a matching SHA256SUMS
      When I provision agentd with no version
      Then the binary came from "fetched", verified by "checksum"

    @BIND-18
    Scenario Outline: an unverifiable fetch fails and caches nothing
      Given a release where <what goes wrong>
      When I provision agentd with no version
      Then provisioning failed with "ERR_PRECONDITION"
      And the failure mentions "<detail>"
      And nothing is cached

      Examples:
        | what goes wrong                                               | detail                   |
        | the attestation refuses the downloaded bytes                  | did not verify           |
        | the release publishes no attestation for the downloaded bytes | publishes no attestation |
        | no attestation can be fetched and SHA256SUMS is gone          | SHA256SUMS               |
        | no attestation can be fetched and SHA256SUMS differs          | SHA256 mismatch          |
        | no attestation can be fetched and SHA256SUMS omits it         | no entry for agentd      |
        | the asset cannot be downloaded                                | could not download       |

    @BIND-18
    Scenario: a refused attestation never falls through to SHA256SUMS
      Given a release where the attestation refuses the downloaded bytes
      When I provision agentd with no version
      Then SHA256SUMS was never fetched

    # What a replaced asset looks like: its bundle deleted and SHA256SUMS rewritten to match.
    # Every release that ships SHA256SUMS also ships its bundle, so "none" is a finding.
    @BIND-18
    Scenario: a release that says it has no attestation never falls through to SHA256SUMS
      Given a release where the release publishes no attestation for the downloaded bytes
      When I provision agentd with no version
      Then provisioning failed with "ERR_PRECONDITION"
      And SHA256SUMS was never fetched
      And nothing is cached

  Rule: BIND-19 — a cache entry that no longer matches its digest record is fetched again

    @BIND-19
    Scenario: a cache entry changed after it was verified is discarded and fetched again
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And the cache already holds the core's own version
      And the cached binary is overwritten with other bytes
      When I provision agentd with no version
      Then the binary came from "fetched", verified by "attestation"
      And the binary is the release's bytes
      And the release was downloaded 2 times

    @BIND-19
    Scenario: a cache entry with no digest record is fetched again
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And the cache holds a binary for the core's own version with no digest record
      When I provision agentd with no version
      Then the binary came from "fetched", verified by "attestation"
      And the release was downloaded 1 time

  Rule: BIND-20 — nothing that is not an aarch64 ELF is served

    @BIND-20
    Scenario Outline: a caller-supplied binary that is not an aarch64 ELF is refused
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And the caller supplies <binary>
      When I provision agentd with the caller's binary
      Then provisioning failed with "ERR_PRECONDITION"
      And the failure mentions "<detail>"
      And the release was downloaded 0 times

      Examples:
        | binary              | detail              |
        | an x86_64 ELF binary | ELF machine 0x3e   |
        | a shell script      | not an ELF          |
        | a path that is gone | does not exist      |

    @BIND-20
    Scenario: a MICROVM_AGENTD binary that is not an aarch64 ELF is refused
      Given a release whose agentd is an aarch64 ELF with a verifying attestation
      And MICROVM_AGENTD names an x86_64 ELF binary
      When I provision agentd with no version
      Then provisioning failed with "ERR_PRECONDITION"
      And the failure mentions "MICROVM_AGENTD"
      And the release was downloaded 0 times

    @BIND-20
    Scenario: a fetched asset that is not an aarch64 ELF is refused and not cached
      Given a release whose agentd is an x86_64 ELF with a verifying attestation
      When I provision agentd with no version
      Then provisioning failed with "ERR_PRECONDITION"
      And the failure mentions "ELF machine 0x3e"
      And nothing is cached
