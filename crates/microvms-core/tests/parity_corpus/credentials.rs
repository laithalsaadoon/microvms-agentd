// SPDX-License-Identifier: Apache-2.0
//! Fresh-process production credential failures, shared by the Rust surface runners.
//! Each surface starts its own child so the CLI's process creation stays in its test harness.

use std::io::Write;
use std::path::PathBuf;
use std::process::Child;

use serde_json::{Value, json};

pub fn fixture_path() -> PathBuf {
    super::tunnel::fixture_path()
}

pub fn run(
    mut child: Child,
    surface: &str,
    program: PathBuf,
    args: &[&str],
    input: &Value,
) -> Value {
    write!(
        child.stdin.take().unwrap(),
        "{}",
        json!({
            "surface": surface, "program": program, "args": args, "input": input,
        })
    )
    .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success(), "credential fixture failed");
    serde_json::from_slice(&output.stdout).expect("one credential verdict")
}
