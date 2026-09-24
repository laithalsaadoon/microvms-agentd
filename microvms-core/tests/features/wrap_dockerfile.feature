# SPDX-License-Identifier: Apache-2.0
#
# The behavior spec for IMAGE-1 through IMAGE-4 in spec/core.symspec.json (issue #220):
# wrapping a task Dockerfile with the agentd stanza, and deriving the base image that pairs
# with it. `model/src/wrap.rs` checks the same requirements as a Stateright model, and the
# bolero harness in `microvms-core/tests/wrap_fuzz.rs` fuzzes `wrap_dockerfile` over
# arbitrary Dockerfile text. Each scenario's tags name the requirements it verifies.
#
# Run by `microvms-core/tests/bdd_wrap.rs` against the real functions, with no AWS call:
# the create preflight is the one `ControlPlane::create_image` runs before the wire.

Feature: A task Dockerfile becomes a buildable one with one call

  Rule: IMAGE-1 — the wrapped stanza and the default Dockerfile's are one text

    @IMAGE-1
    Scenario: a bare FROM wraps to the default Dockerfile
      Given the task Dockerfile "FROM x"
      When the task Dockerfile is wrapped
      Then the result is the default Dockerfile for base "x" with no workdir

    @IMAGE-1
    Scenario: a workdir option writes the default generator's workdir lines
      Given the task Dockerfile "FROM x"
      And the wrap option workdir "/srv/task"
      When the task Dockerfile is wrapped
      Then the result is the default Dockerfile for base "x" with workdir "/srv/task"

  Rule: IMAGE-2 — the result ends with the bootstrap invariant, whatever the task set

    @IMAGE-2
    Scenario: the task's own entrypoint and command are overridden
      Given the task Dockerfile:
        """
        FROM python:3.12-slim
        WORKDIR /app
        ENV AGENTD_PORT=8080
        ENTRYPOINT ["/bin/sh", "-c"]
        CMD ["python", "serve.py"]
        """
      When the task Dockerfile is wrapped
      Then the wrap succeeds
      And the result ends with the lines:
        """
        ENV AGENTD_PORT=9000
        ENV AGENTD_LOG=info
        EXPOSE 9000
        ENTRYPOINT []
        CMD ["/agentd"]
        """
      And the create preflight accepts the result under the from-Dockerfile base

    @IMAGE-2
    Scenario: a task that ends on another user has USER root restored before the stanza
      Given the task Dockerfile:
        """
        FROM python:3.12-slim
        RUN useradd -m app
        USER app
        """
      When the task Dockerfile is wrapped
      Then the wrap succeeds
      And "USER root" is the first line after the task text

    @IMAGE-2
    Scenario: a task that ends on root gets no extra USER line
      Given the task Dockerfile:
        """
        FROM python:3.12-slim
        USER app
        USER root
        """
      When the task Dockerfile is wrapped
      Then the wrap succeeds
      And the result adds no USER line

    @IMAGE-2
    Scenario: a task with no trailing newline is normalized before the stanza
      Given the task Dockerfile "FROM x" with no trailing newline
      When the task Dockerfile is wrapped
      Then the wrap succeeds
      And the line after the task text is "COPY agentd /agentd"

  Rule: IMAGE-3 — a task the stanza cannot be appended to safely is refused

    @IMAGE-3
    Scenario: a Dockerfile with no FROM is refused
      Given the task Dockerfile "RUN echo hello"
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "no FROM"

    @IMAGE-3
    Scenario: a line continuation at the end would swallow the stanza's first line
      Given the task Dockerfile:
        """
        FROM x
        RUN apt-get update && \
        """
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "line continuation"

    @IMAGE-3
    Scenario: a continuation in a custom escape character is still a continuation
      Given the task Dockerfile:
        """
        # escape=`
        FROM x
        RUN echo one `
        """
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "line continuation"

    @IMAGE-3
    Scenario: an unterminated heredoc would swallow the whole stanza
      Given the task Dockerfile:
        """
        FROM x
        RUN <<EOF
        echo never closed
        """
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "heredoc"

    @IMAGE-3
    Scenario: a keepalive the client cannot tolerate is refused
      Given the task Dockerfile:
        """
        FROM x
        ENV AGENTD_SSE_KEEPALIVE_SECS=90
        """
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "AGENTD_SSE_KEEPALIVE_SECS=90"

    @IMAGE-3
    Scenario: a workdir option that is not one absolute path is refused
      Given the task Dockerfile "FROM x"
      And the wrap option workdir "relative/dir"
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "absolute path"

    @IMAGE-3
    Scenario: inheriting a workdir nothing declares is refused
      Given the task Dockerfile "FROM x"
      And the wrap option to inherit the workdir
      When the task Dockerfile is wrapped
      Then the wrap is refused naming "nothing to inherit"

    @IMAGE-3
    Scenario: inheriting the task's own WORKDIR is accepted
      Given the task Dockerfile:
        """
        FROM x
        WORKDIR /app
        """
      And the wrap option to inherit the workdir
      When the task Dockerfile is wrapped
      Then the wrap succeeds

  Rule: IMAGE-4 — the base image comes from the Dockerfile and satisfies the FROM guard

    @IMAGE-4
    Scenario: the managed base refuses a task on another base, the derived base accepts it
      Given the task Dockerfile "FROM python:3.12-slim"
      When the task Dockerfile is wrapped
      Then the create preflight refuses the result under the managed base naming "python:3.12-slim"
      And the create preflight accepts the result under the from-Dockerfile base

    @IMAGE-4
    Scenario: a derived base keeps the managed base name and takes the first FROM
      Given the task Dockerfile:
        """
        FROM --platform=linux/arm64 golang:1.23 AS build
        FROM python:3.12-slim
        """
      When a base image is derived from the task Dockerfile
      Then the derived base's docker_ref is "golang:1.23"
      And the derived base's name is the managed base's

    @IMAGE-4
    Scenario: a digest-pinned FROM keeps its digest and passes the guard
      Given the task Dockerfile "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de"
      When the task Dockerfile is wrapped
      Then the create preflight accepts the result under the from-Dockerfile base
      And the create preflight accepts the result under the managed base

    @IMAGE-4
    Scenario: a Dockerfile with no FROM has no base to derive
      Given the task Dockerfile "RUN echo hello"
      When a base image is derived from the task Dockerfile
      Then the derivation is refused naming "no FROM"
