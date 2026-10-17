// SPDX-License-Identifier: Apache-2.0
//! A failing-first proof must run its selected case, even when the rest would pass.

mod parity_corpus;

use parity_corpus::{CLI_FAKE_AREAS, CLI_PROCESS_AREAS, Run};

fn plan() -> Run {
    Run::plan_in(
        &parity_corpus::cases_dir(),
        &parity_corpus::table_path(),
        "core",
        &[CLI_PROCESS_AREAS.as_slice(), CLI_FAKE_AREAS.as_slice()].concat(),
        &[],
    )
}

#[test]
fn a_proof_selects_only_its_case_and_compares_the_shared_expectation() {
    let mut run = plan().select("control-plane/window-closed");
    let cases = run.cases();
    assert_eq!(cases.len(), 1);
    assert_eq!(cases[0].id, "control-plane/window-closed");
    run.judge(&cases[0], cases[0].expect.clone());
    run.finish();

    let mut run = plan().select("control-plane/window-closed");
    run.judge(
        &run.cases()[0],
        serde_json::json!({"error":{"code":"ERR_PLATFORM"}}),
    );
    assert!(std::panic::catch_unwind(|| run.finish()).is_err());
}

#[test]
fn a_proof_refuses_an_absent_case_instead_of_passing_empty() {
    let run = plan().select("control-plane/absent");
    assert!(std::panic::catch_unwind(|| run.finish()).is_err());
}

#[test]
fn a_selected_proof_still_requires_the_corpus_sentinel() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("control-plane")).unwrap();
    std::fs::copy(
        parity_corpus::cases_dir().join("control-plane/window-closed.json"),
        dir.path().join("control-plane/window-closed.json"),
    )
    .unwrap();
    let mut run = Run::plan_in(
        dir.path(),
        &parity_corpus::table_path(),
        "core",
        &["control-plane"],
        &[],
    )
    .select("control-plane/window-closed");
    let case = run.cases().remove(0);
    run.judge(&case, case.expect.clone());
    assert!(std::panic::catch_unwind(|| run.finish()).is_err());
}
