// SPDX-License-Identifier: Apache-2.0
//! Fresh-process production credential failures, shared by the Rust surface runners.

use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};

use serde_json::{Value, json};

pub fn fixture_path() -> PathBuf {
    super::tunnel::fixture_path()
}

pub fn run(surface: &str, program: PathBuf, args: &[&str], input: &Value) -> Value {
    let mut child = Command::new(fixture_path())
        .args(["credentials", "launch"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("build the credential fixture with mise run parity:fixtures");
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
