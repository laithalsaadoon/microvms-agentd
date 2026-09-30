# SPDX-License-Identifier: Apache-2.0
"""The `microvm` binary as a callable that returns envelopes, and the flags every attached
command takes."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from harness.constants import REGION
from harness.envelope import Envelope, EnvelopeError, KindError
from harness.redact import command_for_log, redacted_argv


@dataclass
class Cli:
    """The `microvm` binary, as a callable that returns envelopes.

    Every call passes `--json` and `--quiet`. `--quiet` because progress on stderr is
    noise in a suite this long, and it is safe: `envelope.rs:12` guarantees `--quiet`
    cannot suppress a leak warning, which is the one line here worth reading.
    """

    binary: Path
    #: Prepended to every invocation's argv. Region only — the three infra values go
    #: through the environment, matching how a human runs it.
    region: str = REGION
    #: Every invocation, for the report. A suite that cannot say what it ran is a
    #: suite whose failures cannot be reproduced by hand.
    log: list[str] = field(default_factory=list)

    def argv(self, *args: str) -> list[str]:
        return [str(self.binary), "--json", "--quiet", *args]

    def version(self) -> str:
        """The version `microvm --version` prints (`microvm 0.9.0` reads as `0.9.0`)."""
        out = self.run_process([str(self.binary), "--version"], timeout=30)
        return out.stdout.split()[-1] if out.stdout.split() else ""

    @staticmethod
    def run_process(
        argv: list[str], timeout: float, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            # TimeoutExpired repr includes cmd, and captured streams may be agent output.
            raise subprocess.TimeoutExpired(
                redacted_argv(argv), error.timeout
            ) from None

    def call(
        self, *args: str, timeout: float = 900.0, env: dict[str, str] | None = None
    ) -> Envelope:
        """One invocation. Raises `KindError` on a failure envelope. `env` replaces the
        process environment for this invocation only.

        The exit code is cross-checked against the envelope's own `exitCode` rather
        than trusted from either side alone. They are two independent renderings of
        one decision — `exit.rs`'s table and `main`'s exit — and CLI-3 is the claim
        that they agree, so a suite that read only one would not be checking it.
        """
        argv = self.argv(*args)
        self.log.append(command_for_log(argv))
        proc = self.run_process(argv, timeout, env)
        envelope = replace(
            self.parse_stdout(proc.stdout, argv), process_exit_code=proc.returncode
        )
        if envelope.status == "error":
            if proc.returncode != envelope.exit_code:
                raise EnvelopeError(
                    f"{command_for_log(argv)} exited {proc.returncode} but its envelope says "
                    f"exitCode {envelope.exit_code}. CLI-3 is the claim that those agree."
                )
            raise KindError(envelope)
        # A success envelope with a non-zero exit is legal and is the `already_reported`
        # case — a workload that exited 4, a suspend that reached TERMINATED. Recorded on
        # the envelope's data by the caller rather than raised, because the payload really
        # is the right answer.
        return envelope

    def call_stream(
        self, *args: str, timeout: float = 900.0
    ) -> tuple[list[dict[str, Any]], Envelope]:
        """One `exec --stream` invocation, as (events, final envelope).

        **The one invocation with a different stdout contract**, and this function asserts
        that contract rather than tolerating it. `microvm manifest` publishes it as
        `exec`'s `alternateResponse`: NDJSON, one event object per line, the envelope last,
        with `type: microvm.exec.stream` rather than `microvm.exec`.

        Three things are checked here, and each is a way the shape can be wrong while the
        command still looks like it worked:

        * every line parses as JSON on its own — a partial or multi-line record would
          make a line-reading consumer lose an event;
        * the **last** line is the envelope and the ones before it are not — an envelope
          written first (or pretty-printed, which makes it several lines) would have a
          consumer hit the terminator before any output;
        * the discriminant is the streaming one, so a consumer branching on `type` learns
          which parse applied from the field it reads first.

        A separate function from `call` rather than a flag on it, deliberately: `call`'s
        whole assertion is that stdout is *one* document, and a function that accepted
        either shape would weaken that for the sixty invocations that are not streams.
        """
        argv = self.argv(*args)
        self.log.append(command_for_log(argv))
        proc = self.run_process(argv, timeout)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            raise EnvelopeError(
                f"{command_for_log(argv)} wrote nothing to stdout. A stream emits one event "
                f"per line and the envelope last. stderrChars={len(proc.stderr)}"
            )

        documents: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EnvelopeError(
                    f"{command_for_log(argv)} line {index} is not one JSON document ({exc}). "
                    f"A streamed exec writes NDJSON — one object per line — so a record "
                    f"spanning lines makes a line-reading consumer lose it. lineChars={len(line)}"
                ) from None
            if not isinstance(parsed, dict):
                raise EnvelopeError(
                    f"{command_for_log(argv)} line {index} is a {type(parsed).__name__}"
                )
            documents.append(parsed)

        *events, final = documents
        if "status" not in final:
            raise EnvelopeError(
                f"{command_for_log(argv)}'s last line is not the envelope. "
                "The envelope goes last precisely so a consumer reading line by line "
                "receives every event before the terminator."
            )
        for index, event in enumerate(events):
            if "status" in event:
                raise EnvelopeError(
                    f"{command_for_log(argv)} line {index} looks like an envelope rather than "
                    f"an event. Exactly one envelope per invocation, and it is the last "
                    "line."
                )
        envelope = replace(Envelope.parse(final), process_exit_code=proc.returncode)
        if envelope.status == "error":
            if proc.returncode != envelope.exit_code:
                raise EnvelopeError(
                    f"{command_for_log(argv)} exited {proc.returncode} but its envelope says "
                    f"exitCode {envelope.exit_code}. CLI-3 holds on the streaming path too."
                )
            raise KindError(envelope)
        if envelope.type != "microvm.exec.stream":
            raise EnvelopeError(
                f"{command_for_log(argv)} streamed but announced {envelope.type!r}. The "
                "streaming shape must carry its own discriminant, or a consumer "
                "branching on `type` cannot tell which parse to use."
            )
        return events, envelope

    @staticmethod
    def parse_stdout(stdout: str, argv: list[str]) -> Envelope:
        """The whole of stdout as one document. This *is* CLI-4's assertion.

        `json.loads` over the entire stream rather than the first line, so a second
        envelope, a progress line, or a stray `print` all fail here. That is the same
        assertion `tests/exit_codes.rs` makes in-crate; making it again on every one of
        this suite's invocations is cheap and covers the paths a unit test cannot reach.
        """
        try:
            document = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise EnvelopeError(
                f"{command_for_log(argv)} did not write exactly one JSON document to stdout "
                f"({exc}). Progress belongs on stderr (CLI-4). stdoutChars={len(stdout)}"
            ) from None
        if not isinstance(document, dict):
            raise EnvelopeError(
                f"{command_for_log(argv)} wrote a {type(document).__name__}"
            )
        return Envelope.parse(document)


def attach_args(cli: Cli, launched: Envelope, endpoint: str | None = None) -> list[str]:
    """The identifier triple every attached command takes, plus the region.

    One helper rather than the same six-element list written out in each section, which is
    the same argument `microvms-cli`'s own `AttachFlags` makes: three of the four are
    opaque strings of the same shape, so writing them out repeatedly is repeated chances to
    put an endpoint where a token belongs.

    `endpoint` overrides the launch's, for the post-resume sections: `resume` hands back the
    endpoint it read, and following that rather than the launch's is what makes a changed
    endpoint a followed change instead of a silent failure.
    """
    return [
        "--endpoint",
        endpoint or str(launched.data["endpoint"]),
        "--agent-token",
        str(launched.data["agentToken"]),
        "--microvm-id",
        str(launched.data["microvmId"]),
        "--region",
        cli.region,
    ]
