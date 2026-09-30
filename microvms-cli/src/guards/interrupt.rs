// SPDX-License-Identifier: Apache-2.0
//! CLI-6: the interrupt teardown.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    ScriptedSeam, ScriptedTransport, TempDir, full_infra, interrupt_run_args, microvm_body,
};
use crate::commands::Ctx;
use crate::envelope::{Format, Output};
use crate::exit::Exit;

/// **CLI-6, the guard proof.** An interrupt after the launch is accepted tears the VM down and
/// names every identifier the teardown could not remove.
///
/// The interrupt fires when the fake sees `RunMicrovm`, which is precisely the window that
/// matters: the VM exists and is billing, its id is recorded (`sandbox.rs:574` assigns it before
/// the RUNNING wait), and nothing has confirmed it is ready. `TerminateMicrovm` is scripted to
/// fail, so the id survives into `undeleted` — which is what makes "names what it could not
/// delete" observable rather than trivially true.
///
/// Four assertions, one per way this can be wrong: the exit code says interrupted rather than
/// timed out, the terminate really went to the wire, the leaked id is in the failure envelope's
/// `data`, and the ledger on disk carries it too — because the envelope is lost the moment the
/// terminal scrolls and the file is the operator's actual remedy.
///
/// **Falsification** — replace the `tokio::select!` with a bare `.await` on the launch body and
/// the interrupt is never observed: the run ends in `ERR_TIMEOUT` after the fake clock burns the
/// ready deadline, no `TerminateMicrovm` goes out under the interrupt condition, and all four
/// assertions go red. Verified; see the packet's guard proofs.
#[tokio::test]
async fn an_interrupt_after_launch_tears_down_and_names_every_leaked_identifier() {
    let dir = TempDir::new("interrupt");
    let transport = Arc::new(ScriptedTransport::new());
    let (fire, fired) = tokio::sync::oneshot::channel();
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        // Never reaches RUNNING, so the only way out of the wait is the interrupt.
        .answer("GetMicrovm", 200, &microvm_body("PENDING"))
        // The teardown's terminate fails, so the id has to be reported rather than assumed gone.
        //
        // 409 rather than 500, and the reason is a defect this test found in its own first
        // draft: core retries a 5xx through `send_with_retry`, so a 500 here produced six
        // `TerminateMicrovm` calls and the call-count assertion below read 6. A conflict is
        // both the realistic failure — the VM is in a state that forbids the call — and the one
        // core does not retry, so the count is the observable it is supposed to be.
        .answer(
            "TerminateMicrovm",
            409,
            r#"{"message": "ConflictException"}"#,
        )
        .fire_on("RunMicrovm", fire);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let args = interrupt_run_args(dir.0.clone());
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let env = |_: &str| None;
    let interrupt: crate::commands::lifecycle::Interrupt<'_> = Box::pin(async move {
        let _ = fired.await;
    });
    let result = {
        let mut ctx = Ctx {
            seam: &seam,
            out: &mut out,
            infra: full_infra(),
            env: &env,
            fetch: &crate::provision::PanickingFetch,
        };
        crate::commands::lifecycle::run(&mut ctx, &args, interrupt).await
    };

    let failure = result.expect_err("an interrupt is a failure");
    assert_eq!(
        failure.exit,
        Exit::Interrupted,
        "an interrupt must not read as a timeout: {}",
        failure.message
    );
    assert_eq!(failure.code(), "ERR_INTERRUPTED");
    assert_eq!(failure.finding(), "The build log group survives Terraform");

    assert_eq!(
        transport.called("TerminateMicrovm"),
        1,
        "the teardown must have run: {:?}",
        transport.calls()
    );

    let envelope = crate::envelope::error(&failure);
    assert_eq!(
        envelope["data"]["leaked"],
        serde_json::json!(["mvm-abc123"]),
        "the identifier the teardown could not delete has to be in the payload: {envelope}"
    );
    assert_eq!(envelope["data"]["microvmId"], "mvm-abc123");
    assert_eq!(envelope["data"]["terminateAccepted"], false);

    // And on disk, because the envelope is gone the moment the terminal scrolls.
    let ledgers = crate::ledger::read_all(&dir.0);
    assert_eq!(ledgers.len(), 1, "{ledgers:?}");
    assert_eq!(
        ledgers[0]["leaked"],
        serde_json::json!(["mvm-abc123"]),
        "{ledgers:?}"
    );

    // The human output warns about it too, and a warning is never suppressed.
    let stderr = String::from_utf8(out.into_streams().1).expect("utf8");
    assert!(
        stderr.contains("warning: could not delete mvm-abc123"),
        "{stderr}"
    );
    assert!(stderr.contains("still billing"), "{stderr}");
}

/// The same interrupt, with a teardown that **succeeds**: no leak reported, still exit 11.
///
/// The negative case, and it is what keeps the test above from passing vacuously. A CLI that
/// listed every identifier it had ever seen as leaked would satisfy "the leak is named" while
/// sending an operator to delete a VM that is already gone — and an operator who is sent on one
/// wild goose chase stops reading the list.
#[tokio::test]
async fn an_interrupt_whose_teardown_succeeds_reports_no_leak_and_still_exits_interrupted() {
    let dir = TempDir::new("interrupt-clean");
    let transport = Arc::new(ScriptedTransport::new());
    let (fire, fired) = tokio::sync::oneshot::channel();
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("PENDING"))
        .answer("TerminateMicrovm", 200, "{}")
        .fire_on("RunMicrovm", fire);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let args = interrupt_run_args(dir.0.clone());
    let mut out = Output::new(Format::Json, false, Vec::new(), Vec::new());
    let env = |_: &str| None;
    let interrupt: crate::commands::lifecycle::Interrupt<'_> = Box::pin(async move {
        let _ = fired.await;
    });
    let result = {
        let mut ctx = Ctx {
            seam: &seam,
            out: &mut out,
            infra: full_infra(),
            env: &env,
            fetch: &crate::provision::PanickingFetch,
        };
        crate::commands::lifecycle::run(&mut ctx, &args, interrupt).await
    };

    let failure = result.expect_err("an interrupt is still a failure");
    assert_eq!(failure.exit, Exit::Interrupted);
    let envelope = crate::envelope::error(&failure);
    assert_eq!(
        envelope["data"]["leaked"],
        serde_json::json!([]),
        "a VM that really was terminated must not be reported as leaked: {envelope}"
    );
    assert_eq!(envelope["data"]["terminateAccepted"], true);
    // A clean teardown clears its ledger, so `microvm ls` says nothing outstanding.
    assert!(
        crate::ledger::read_all(&dir.0).is_empty(),
        "a clean teardown leaves no ledger"
    );
    // The history is the opposite property, asserted side by side on purpose: the ledger is
    // gone because nothing leaked, and the record of what happened survives anyway — with
    // the values the platform reported (`RunMicrovm`'s own id and endpoint, the teardown's
    // acceptance), which is what `microvm history` exists to answer after the VM is gone.
    let events = crate::history::read_events(&dir.0, "mvm-abc123");
    assert_eq!(events.len(), 2, "launched, then terminated: {events:?}");
    assert_eq!(events[0]["event"], "launched");
    assert_eq!(
        events[0]["endpoint"],
        "https://mvm-abc123.microvm.us-east-1.amazonaws.com"
    );
    assert_eq!(events[0]["region"], "us-east-1");
    assert_eq!(events[1]["event"], "terminated");
    assert_eq!(events[1]["terminateAccepted"], true);
}
