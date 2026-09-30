# SPDX-License-Identifier: Apache-2.0
"""Every check's outcome, and the runner that turns a section's raise into a named FAIL."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.envelope import KindError


@dataclass
class Results:
    """Every check's outcome, so the summary reports facts rather than a feeling.

    The four primitives are the oracle's, with the same names and the same semantics,
    except that `eq` refuses `None` (its docstring says why). `absent()` is this suite's
    own, for the checks that expect nothing on purpose.
    `skipped` is a *third* list rather than a pass with a note, because a skip folded into
    `passed` is how a suite that covers half of what it claims looks identical to one that
    covers all of it.

    It is now always empty, and it stays here anyway. The `unsupported()` primitive that
    filled it was the honest record of 34 checks this client could not express; those
    surfaces landed and the entries became real check bodies. Deleting the list along with
    them would remove the suite's ability to *say* a gap exists — so the count is still
    printed, and it should read zero. The next gap gets a line rather than a silence.
    """

    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: Set on the throwaway `Results` the self-test's negative twins run against, where a
    #: FAIL is the *expected* outcome. Only changes the printed marker — `PROBE` rather
    #: than `FAIL` — because a suite whose green run prints three FAIL lines is a suite
    #: whose real failures get skimmed past, which is the same reasoning `mise.toml`
    #: gives for keeping a RuntimeWarning out of `live:rates`.
    probe: bool = False

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed.append(name)
            print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
        else:
            self.failed.append((name, detail))
            marker = "PROBE" if self.probe else "FAIL "
            print(f"  {marker} {name} — {detail}")
        return ok

    def eq(self, name: str, actual: Any, expected: Any) -> bool:
        """Passes when both sides are present and equal.

        `None` on either side fails, because it's what a missing key reads as through
        `.get()`: two envelopes that both lack a field would otherwise pass as agreeing
        about it. Empty strings and empty lists still compare, since some checks assert
        them on purpose. Deliberate absence goes through `absent()`.
        """
        if actual is None or expected is None:
            return self.check(
                name,
                False,
                "absent value; use results.absent() to assert absence"
                f" (expected {expected!r}, got {actual!r})",
            )
        return self.check(
            name, actual == expected, f"expected {expected!r}, got {actual!r}"
        )

    def absent(self, name: str, value: Any) -> bool:
        """Passes only when `value` is None: the check expects nothing on purpose."""
        return self.check(name, value is None, f"expected nothing, got {value!r}")

    def raises(self, name: str, expected_kind: str, call: Callable[[], Any]) -> bool:
        """Asserts a call fails with exactly the `WireKind` named by `expected_kind`.

        The kind rather than the `ERR_*` code, for the reason `KindError`'s docstring
        gives: five kinds share `ERR_PROTOCOL`, so a code comparison would pass for a
        404 where a 400 belongs — which is the precise confusion the daemon's status
        discipline exists to prevent, and the one this primitive is here to catch.

        `EnvelopeError` is deliberately not caught. A malformed stdout is a defect in
        the binary, and reporting it as "the wrong kind was raised" would name the
        daemon for the CLI's mistake.
        """
        try:
            call()
        except KindError as exc:
            if exc.kind == expected_kind:
                return self.check(
                    name, True, f"{exc.kind} ({exc.code}, exit {exc.exit_code})"
                )
            return self.check(
                name,
                False,
                f"expected kind {expected_kind!r}, got {exc.kind!r} "
                f"({exc.code}, exit {exc.exit_code})",
            )
        return self.check(
            name, False, f"expected kind {expected_kind!r}, nothing raised"
        )

    def ok(self, name: str, call: Callable[[], Any]) -> bool:
        """Asserts a call succeeds, which for this driver means "no failure envelope"."""
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - any error is the finding
            return self.check(name, False, repr(exc))
        return self.check(name, True)

    def skip(self, name: str, reason: str) -> None:
        """Records a check this client has no way to express.

        Printed as `SKIP` and counted apart from both passes and failures. It does not
        fail the run — a suite that is permanently red is a suite people stop reading —
        but it is never silent, which is the whole difference between a coverage statement
        and a gap.

        **No caller, on purpose.** This was `unsupported()` and it had 34; the CLI grew the
        surfaces and every one became a real check. It is kept as the shape the next gap
        takes, because the alternative is that the next gap has nowhere to be recorded and
        gets a comment instead. `--self-test` calls it once against a throwaway `Results`
        so it cannot rot into something that no longer runs.
        """
        self.skipped.append((name, reason))
        print(f"  SKIP  {name} — {reason}")


def run_section(
    results: "Results",
    name: str,
    section: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Runs one suite section so a raise inside it is a named FAIL, not the end of the run.

    A section's CLI call that fails with an envelope raises `KindError`; before this
    wrapper that exception left `main`, skipped every later section, and printed only the
    code. Measured 2026-09-24: Codex's version probe timing out in `drive_agent_vm`
    aborted the suite 200 checks in, so the three keepalive sections after it never ran.
    The FAIL carries the envelope's message, so the finding names its cause.
    """
    try:
        return section(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a section's raise is a finding, not an abort
        results.check(
            f"section {name} ran to completion", False, section_failure_detail(exc)
        )
        return None


def section_failure_detail(error: Exception) -> str:
    """The code and message of a section's raise; secrets never enter envelopes."""
    if isinstance(error, KindError):
        envelope = error.envelope
        return f"{envelope.code} (exit={envelope.exit_code}): {envelope.error[:300]}"
    return f"{type(error).__name__}: {str(error)[:300]}"
