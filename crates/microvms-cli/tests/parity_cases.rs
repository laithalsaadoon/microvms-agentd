// SPDX-License-Identifier: Apache-2.0
//! The CLI's answers to the shared case corpus (`verify/parity/cases/`, #272), from a spawned
//! `microvm`.
//!
//! This tier takes the areas a process can answer with no AWS call and no fake: `cost --json`
//! and the launch refusals `run` makes before any call. The areas that need a scripted control
//! plane or daemon are `src/guards/parity.rs`, which reads the same corpus by
//! the same rules (`crates/microvms-core/tests/parity_corpus/mod.rs`).

#[allow(dead_code)]
mod support;

#[path = "../../microvms-core/tests/parity_corpus/mod.rs"]
mod parity_corpus;

use parity_corpus::{CLI_FAKE_AREAS, CLI_PROCESS_AREAS, Case, Run};
use serde_json::{Value, json};
use support::run;

const IMAGE_ARN: &str = "arn:aws:lambda:us-east-1:123456789012:microvm-image:img";

#[test]
fn the_cli_process_answers_the_shared_case_corpus() {
    let mut run = Run::plan("cli", &CLI_PROCESS_AREAS, &CLI_FAKE_AREAS);
    for case in run.cases() {
        let answer = match case.area.as_str() {
            "cost" => cost(&case),
            "egress" => egress(&case),
            other => panic!(
                "{}: parity_corpus::CLI_PROCESS_AREAS gives the process tier area {other:?}, \
                 which it has no handler for",
                case.id
            ),
        };
        run.judge(&case, answer);
    }
    run.finish();
}

/// The envelope's answer: its `data` on success, or the refusal's facets. `retryable` is the
/// exit code's row, which is where the CLI says it (the table's `retryable` row). Each code is
/// one row of `EXIT_TABLE` (`src/exit.rs`), and the process exits with the envelope's code, so
/// `ERR_RETRYABLE` here is the same test as `Exit::Retryable` in `src/guards/parity.rs`.
fn answer_of(args: &[&str]) -> (Value, Value) {
    let outcome = run(args, &[]);
    let envelope = outcome.envelope();
    if envelope["status"] == "ok" {
        assert_eq!(outcome.exit_code(), 0, "a success exits 0: {envelope}");
        return (envelope["data"].clone(), envelope);
    }
    assert_eq!(
        envelope["exitCode"],
        outcome.exit_code(),
        "the process exits with the envelope's code: {envelope}"
    );
    let code = envelope["code"].clone();
    let refusal = json!({"error": {
        "code": code,
        "wire_kind": envelope["data"].get("kind").cloned().unwrap_or(Value::Null),
        "retryable": code == "ERR_RETRYABLE",
    }});
    (refusal, envelope)
}

fn number(case: &Case, key: &str) -> String {
    let value = case.input(key);
    assert!(value.is_number(), "{}: input.{key} is a number", case.id);
    value.to_string()
}

fn cost(case: &Case) -> Value {
    assert_eq!(case.capability, "estimate", "{}: an estimate case", case.id);
    // `input.defaults` stays out: the CLI's own defaults for `launched` and the label are
    // what the case asks about.
    let args = [
        "--json".to_string(),
        "cost".to_string(),
        "--estimate".to_string(),
        "--memory".to_string(),
        number(case, "size_mib"),
        "--running-sec".to_string(),
        number(case, "running_seconds"),
        "--suspended-sec".to_string(),
        number(case, "suspended_seconds"),
        "--cycles".to_string(),
        number(case, "suspend_resume_cycles"),
    ];
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let (answer, envelope) = answer_of(&args);
    if answer.get("error").is_some() {
        return answer;
    }
    answer
        .get("report")
        .cloned()
        .unwrap_or_else(|| panic!("{}: no data.report in {envelope}", case.id))
}

fn egress(case: &Case) -> Value {
    assert_eq!(
        case.capability, "launch",
        "{}: the CLI's egress cases are launches",
        case.id
    );
    let mut args = vec!["--json", "run", "--image", IMAGE_ARN];
    if case.input_bool("egress") {
        args.push("--egress");
    }
    let connectors = case.input_strings("egress_network_connectors");
    for connector in &connectors {
        args.extend(["--egress-network-connector", connector.as_str()]);
    }
    if case.input_bool("deny_egress") {
        args.push("--deny-egress");
    }
    answer_of(&args).0
}
