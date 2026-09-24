# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for IMAGE-6 through IMAGE-11 in spec/core.symspec.json (issue #221):
# `Sandbox::ensure_image`, content-addressed build-or-reuse with a build context and an S3
# upload. `model/src/image.rs` checks the same requirements as a Stateright model over two
# concurrent callers, and the bolero harness in `microvms-core/tests/context_fuzz.rs` fuzzes
# the context, the hash, and the key.
#
# Run by `microvms-core/tests/bdd_ensure.rs` against the real `Sandbox`, over a fake platform
# that keeps one image per name and moves it through its states as the service does: a build
# settles when the scenario says so, a deletion finishes a poll later, and a create against a
# name that exists is refused with 409. STS and S3 are fakes that record their calls.

Feature: One call from build inputs to a usable image

  Background:
    Given a task directory with a Dockerfile "FROM python:3.12-slim" and a file "app/main.py"

  Rule: IMAGE-6 — the name is content-addressed over every input

    @IMAGE-6
    Scenario: equal inputs name one image, a changed context file names another
      When the name for the task is derived
      And the task file "app/main.py" is changed
      And the name for the task is derived
      Then the two names differ
      And each name is the prefix and twelve hex characters

    @IMAGE-6
    Scenario: a different size class names a different image
      When the name for the task is derived
      And the name for the task at 4096 MiB is derived
      Then the two names differ

  Rule: IMAGE-7 — the context is read the way docker build reads it

    @IMAGE-7
    Scenario: .dockerignore excludes, and a symlink is skipped with a warning
      Given the task file ".dockerignore" holds "secret.txt"
      And the task file "secret.txt" holds "do not ship"
      And the task has a symlink "link.py" to "app/main.py"
      When the image is ensured
      Then the uploaded artifact holds "app/main.py"
      And the uploaded artifact does not hold "secret.txt"
      And the uploaded artifact does not hold "link.py"
      And the warnings name "link.py"

    @IMAGE-7
    Scenario: Dockerfile.dockerignore takes precedence over .dockerignore
      Given the task file ".dockerignore" holds "app"
      And the task file "Dockerfile.dockerignore" holds "notes.txt"
      And the task file "notes.txt" holds "scratch"
      When the image is ensured
      Then the uploaded artifact holds "app/main.py"
      And the uploaded artifact does not hold "notes.txt"

    @IMAGE-7
    Scenario: the artifact carries the wrapped Dockerfile, not the task's own
      When the image is ensured
      Then the uploaded artifact's Dockerfile ends with the agentd stanza

  Rule: IMAGE-8 — one account lookup per sandbox, one content-addressed key

    @IMAGE-8
    Scenario: a build uploads to the key under the prefix and names that URI
      When the image is ensured
      Then the artifact was uploaded to "harbor/<name>/artifact.zip"
      And the create named that artifact
      And the account was looked up 1 time

    @IMAGE-8
    Scenario: two ensures on one sandbox look the account up once
      When the image is ensured
      And the image is ensured again on the same sandbox
      Then the account was looked up 1 time

  Rule: IMAGE-9 — a ready or building image is reused, never rebuilt

    @IMAGE-9
    Scenario: the second call reuses the first call's image
      When the image is ensured
      And the image is ensured again on a new sandbox
      Then the first call built the image
      And the second call reused it with no upload and no create

    @IMAGE-9
    Scenario: a build already running is waited out
      Given the platform is building the task's image
      When the image is ensured
      Then the call reused the image
      And the platform saw no create from the call

  Rule: IMAGE-10 — failure and force delete before they rebuild

    @IMAGE-10
    Scenario: a failed image is deleted and rebuilt
      Given the platform holds the task's image as CREATE_FAILED
      When the image is ensured
      Then the call built the image
      And the platform deleted the image before the create

    @IMAGE-10
    Scenario: force rebuilds a ready image
      Given the platform holds the task's image as CREATED
      When the image is ensured with force
      Then the call built the image
      And the platform deleted the image before the create

    @IMAGE-10
    Scenario: without force a ready image is never deleted
      Given the platform holds the task's image as CREATED
      When the image is ensured
      Then the call reused the image
      And the platform deleted nothing

  Rule: IMAGE-11 — the loser of a create race joins the winner

    @IMAGE-11
    Scenario: two concurrent calls build one image
      When the image is ensured by two sandboxes at once
      Then exactly one call built the image and the other reused it
      And the platform accepted 1 create and refused 1
      And both calls returned the same ready image

    @IMAGE-11
    Scenario: the loser waits for the winner's build rather than returning early
      When the image is ensured by two sandboxes at once, the build taking 5 polls
      Then both calls returned the same ready image
