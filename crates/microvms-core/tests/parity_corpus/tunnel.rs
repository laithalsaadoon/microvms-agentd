// SPDX-License-Identifier: Apache-2.0
//! Process plumbing shared by the Rust case runners. Each runner opens its own surface.

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdout};

use serde_json::Value;

pub fn fixture_path() -> PathBuf {
    let executable = std::env::current_exe().expect("the test executable has a path");
    executable
        .parent()
        .expect("deps directory")
        .parent()
        .expect("profile directory")
        .join("examples")
        .join(format!(
            "parity-tunnel-peer{}",
            std::env::consts::EXE_SUFFIX
        ))
}

pub struct Peer {
    child: Child,
    output: BufReader<ChildStdout>,
    pub ready: Value,
}

impl Peer {
    pub fn started(mut child: Child, input: &Value) -> Self {
        writeln!(child.stdin.take().expect("piped stdin"), "{input}").expect("case input writes");
        let mut output = BufReader::new(child.stdout.take().expect("piped stdout"));
        let mut line = String::new();
        output.read_line(&mut line).expect("peer ready line reads");
        let ready = serde_json::from_str(&line).expect("peer ready JSON");
        Self {
            child,
            output,
            ready,
        }
    }

    pub fn finish(&mut self) -> Value {
        let mut line = String::new();
        self.output
            .read_line(&mut line)
            .expect("peer verdict line reads");
        let verdict = serde_json::from_str(&line).expect("peer verdict JSON");
        assert!(self.child.wait().expect("peer exits").success());
        verdict
    }
}

impl Drop for Peer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
