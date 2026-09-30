# SPDX-License-Identifier: Apache-2.0
"""One `--json` invocation's stdout, parsed, and the exceptions a failure becomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Envelope:
    """One `--json` invocation's whole stdout, parsed.

    Every field unconditional on the failure side, which is the CLI's own contract
    (`crates/microvms-cli/src/envelope.rs:24`: "A key that appears conditionally is a key
    every consumer has to guard"). This dataclass takes it at its word and reads them
    directly, so a field that goes missing is a `KeyError` here rather than a `None`
    that flows into an assertion and passes.
    """

    status: str
    api_version: str
    #: The success discriminant (`microvm.run`, `microvm.state`, ...). Empty on failure.
    type: str
    data: dict[str, Any]
    #: `ERR_*`, empty on success.
    code: str = ""
    exit_code: int = 0
    error: str = ""
    finding: str = ""
    suggestions: tuple[str, ...] = ()
    #: Observed subprocess status, separate from data.exitCode (the guest workload).
    process_exit_code: int | None = None

    @property
    def kind(self) -> str | None:
        """The daemon's own status name, or `None` when nothing reached the daemon.

        `data.kind` is a `microvms_core::WireKind` — `Conflict`, `NotFound`,
        `ProtocolError`. Absent for a local rejection, and that absence is
        information: it says the CLI refused before any call.
        """
        found = self.data.get("kind")
        return str(found) if found is not None else None

    @classmethod
    def parse(cls, document: dict[str, Any]) -> Envelope:
        status = str(document["status"])
        data = dict(document.get("data") or {})
        if status == "ok":
            return cls(
                status=status,
                api_version=str(document["apiVersion"]),
                type=str(document["type"]),
                data=data,
            )
        return cls(
            status=status,
            api_version=str(document["apiVersion"]),
            type="",
            data=data,
            code=str(document["code"]),
            exit_code=int(document["exitCode"]),
            error=str(document["error"]),
            finding=str(document["finding"]),
            suggestions=tuple(document["suggestions"]),
        )


class KindError(Exception):
    """A failure envelope, as something `Results.raises` can assert on.

    **Why the kind and not the code.** `Results.raises` in the deleted oracle asserted the client
    exception *type* — `Conflict` versus `NotFound` — because "a 404 arriving where a
    400 belongs fails here as loudly as it should". The CLI's exit code cannot carry
    that: `crates/microvms-cli/src/exit.rs:40` collapses five `WireKind`s onto one
    `ERR_PROTOCOL` deliberately, since "a shell branching on `$?` cannot act
    differently on a 400 than on a 409".

    So the code is the wrong granularity for this suite by construction, not by
    accident, and `data.kind` is the field the CLI added for exactly this consumer
    (`envelope.rs:28` names `conformance/run_rs.py` in as many words). This exception
    carries all three — kind, code, exit code — so a check can assert at whichever
    granularity it means, and the summary can print the coarse one beside the fine.
    """

    def __init__(self, envelope: Envelope) -> None:
        super().__init__(
            f"{envelope.code} (kind={envelope.kind!r}, exit={envelope.exit_code})"
        )
        self.envelope = envelope
        self.kind = envelope.kind
        self.code = envelope.code
        self.exit_code = envelope.exit_code

    def __repr__(self) -> str:
        return (
            f"KindError(kind={self.kind!r}, code={self.code!r}, exit={self.exit_code})"
        )


class EnvelopeError(Exception):
    """Stdout was not exactly one JSON envelope. A CLI-4 violation, not a check failure.

    Its own type so it is never mistaken for a protocol result: a second document on
    stdout means the *binary* is wrong, and reporting that as "the daemon answered
    oddly" would send the reader to the wrong crate.
    """
