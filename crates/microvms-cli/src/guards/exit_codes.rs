// SPDX-License-Identifier: Apache-2.0
//! CLI-3's classification half.

#![cfg(test)]

use microvms_core::{Error, ErrorKind};

use crate::commands::Rendered;
use crate::envelope::{Format, Output};
use crate::exit::Exit;

/// **CLI-3, table-driven over every non-zero row that a core failure can produce.**
///
/// Each row induces the failure at the seam and asserts the integer, the `ERR_*` code, and the
/// `docs/PLATFORM.md` finding — the same three `test_cli.py:484` asserts, and for the same reason:
/// a CLI that mapped every failure to one code satisfies "it exited non-zero" and fails here.
///
/// This is the *classification* half. The half that asserts the process really exits with these
/// numbers is `tests/exit_codes.rs`, because `ExitCode` deliberately hides its value in-process.
///
/// **Falsification** — map `ErrorKind::BuildWedged` and `ErrorKind::LaunchDied` to one `Exit` in
/// `Exit::for_kind` and two rows go red on both the code and the finding. Verified; see the
/// packet's guard proofs.
#[tokio::test]
async fn each_induced_failure_class_earns_its_own_code_and_finding() {
    let rows: [(&str, Error, Exit, &str); 11] = [
        (
            "wedged build",
            Error::new(
                ErrorKind::BuildWedged,
                "build never scheduled after 240s: all builds still PENDING — the clientToken \
                 replay signature",
            ),
            Exit::BuildWedged,
            "`clientToken` is a permanent idempotency key",
        ),
        (
            "terminal state before RUNNING",
            Error::new(
                ErrorKind::LaunchDied,
                "microvm mvm-1 reached TERMINATED before RUNNING: run hook returned 500",
            ),
            Exit::LaunchDied,
            "`runHookPayload` arrives wrapped, not as the body",
        ),
        (
            "expired suspended window",
            Error::new(
                ErrorKind::WindowClosed,
                "suspended 301s, past the 300s suspendedDurationSeconds window",
            ),
            Exit::WindowClosed,
            "`idlePolicy`",
        ),
        (
            "mint failure",
            Error::wire(
                microvms_core::WireKind::AuthTokenMint,
                "could not mint a proxy auth token",
            ),
            Exit::Retryable,
            "Endpoint authentication",
        ),
        (
            "wrong agent token",
            Error::wire(
                microvms_core::WireKind::Unauthorized,
                "GET /v1/exec/x -> 401",
            ),
            Exit::Credentials,
            "",
        ),
        (
            "daemon refused the request",
            Error::wire(microvms_core::WireKind::Conflict, "409 wrong state"),
            Exit::Protocol,
            "",
        ),
        (
            "off-table size class",
            Error::invalid_arg("minimumMemoryInMiB=1500 is not a documented size class baseline"),
            Exit::InvalidArg,
            "",
        ),
        (
            "control-plane failure",
            Error::new(ErrorKind::Platform, "ValidationException"),
            Exit::Platform,
            "",
        ),
        (
            "client-side deadline",
            Error::new(
                ErrorKind::Timeout,
                "the image did not become usable within 2700s",
            ),
            Exit::Timeout,
            "",
        ),
        (
            "missing prerequisite",
            Error::new(ErrorKind::Precondition, "no image to launch"),
            Exit::Precondition,
            "",
        ),
        (
            "a bug in this client",
            Error::new(ErrorKind::Unexpected, "no handler claimed this"),
            Exit::Unexpected,
            "",
        ),
    ];

    for (label, error, expected, finding) in rows {
        let failure = crate::exit::classify(&error);
        assert_eq!(failure.exit, expected, "{label}");
        assert_eq!(
            failure.code(),
            expected.code().expect("a non-zero row"),
            "{label}"
        );
        assert_eq!(failure.finding(), finding, "{label}");

        // And the envelope carries all three, since that is what a consumer actually reads.
        let envelope = crate::envelope::error(&failure);
        assert_eq!(envelope["exitCode"], expected.as_u8(), "{label}");
        assert_eq!(envelope["code"], failure.code(), "{label}");
        assert_eq!(envelope["finding"], finding, "{label}");
    }
}

/// The two rows no core error can produce, produced the way the CLI produces them.
///
/// `ERR_EXEC_FAILED` and `ERR_INTERRUPTED` complete the catalogue's coverage: the first is
/// `AlreadyReported` beside a *success* envelope, and the second is the interrupt. Without this
/// the table above would cover eleven of thirteen and the two most CLI-specific rows would be
/// untested.
#[tokio::test]
async fn the_two_cli_only_rows_are_reachable_and_distinct() {
    // ERR_EXEC_FAILED: the sandbox worked and the command in it did not.
    let outcome = crate::render::RunOutcome {
        exec_exit_code: Some(7),
        ..crate::render::RunOutcome::default()
    };
    let rendered = Rendered::ok(
        "microvm.run",
        outcome.to_data(),
        String::new(),
        String::new(),
    )
    .reporting(Exit::ExecFailed);
    assert_eq!(rendered.already_reported, Some(Exit::ExecFailed));
    assert_eq!(Exit::ExecFailed.as_u8(), 13);
    assert_eq!(Exit::ExecFailed.code(), Some("ERR_EXEC_FAILED"));
    // The workload's own code is in the payload and is *not* the process's exit code: a workload
    // exiting 4 must not be indistinguishable from a credential failure.
    assert_eq!(rendered.data["execExitCode"], 7);

    // ERR_INTERRUPTED, from core's own kind.
    let interrupted = crate::exit::classify(&Error::new(ErrorKind::Interrupted, "interrupted"));
    assert_eq!(interrupted.exit, Exit::Interrupted);
    assert_eq!(interrupted.exit.as_u8(), 11);
    assert_ne!(Exit::Interrupted, Exit::ExecFailed);
}

/// A success envelope precedes a non-zero exit, and there is exactly one of them.
///
/// The `AlreadyReported` property: `run`'s workload failed, the output and cost the caller asked
/// for are in `data`, and the code is 13. A `CliError` there would print a second envelope and
/// break the one-document rule — which is why this is a field on the returned value rather than
/// an error the dispatcher raises.
#[test]
fn an_already_reported_exit_writes_one_success_envelope_and_no_failure_one() {
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let rendered = Rendered::ok(
        "microvm.run",
        crate::render::RunOutcome {
            exec_exit_code: Some(7),
            ..crate::render::RunOutcome::default()
        }
        .to_data(),
        "exit code: 7".into(),
        String::new(),
    )
    .reporting(Exit::ExecFailed);

    out.emit(
        &crate::envelope::ok(rendered.kind, rendered.data.clone()),
        &rendered.text,
    );
    let exit = rendered.already_reported.expect("reports a code");
    assert_eq!(exit, Exit::ExecFailed);

    let stdout = String::from_utf8(out.into_streams().0).expect("utf8");
    let parsed: serde_json::Value = serde_json::from_str(&stdout).expect("exactly one document");
    assert_eq!(
        parsed["status"], "ok",
        "the envelope is a success: {stdout}"
    );
    assert_eq!(parsed["data"]["execExitCode"], 7);
}
