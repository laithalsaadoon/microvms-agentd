// SPDX-License-Identifier: Apache-2.0
//! File and stdin credential inputs follow the same command behavior as literal inputs.

#![expect(
    clippy::disallowed_types,
    reason = "an integration test reaches this binary-only crate by spawning it"
)]

mod support;

use std::io::Write;
use std::process::{Command, Stdio};

fn run(args: &[&str], input: &[u8]) -> support::Run {
    let mut child = Command::new(support::binary())
        .args(args)
        .env_clear()
        .env("HOME", "/nonexistent-microvm-test-home")
        .env("AWS_EC2_METADATA_DISABLED", "true")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    let output = child.wait_with_output().unwrap();
    support::Run {
        code: output.status.code(),
        stdout: String::from_utf8(output.stdout).unwrap(),
        stderr: String::from_utf8(output.stderr).unwrap(),
    }
}

fn secret(value: &[u8]) -> tempfile::NamedTempFile {
    let mut file = tempfile::NamedTempFile::new().unwrap();
    file.write_all(value).unwrap();
    file
}

const ADDRESS: &[&str] = &[
    "--endpoint",
    "https://mvm-test.example",
    "--microvm-id",
    "mvm-test",
    "--region",
    "us-east-1",
    "--json",
];

#[test]
fn token_file_and_stdin_follow_literal_input_behavior() {
    let file = secret(b"example-token\r\n");
    let mut args = vec!["health", "--port", "0"];
    args.extend_from_slice(ADDRESS);
    let mut literal = args.clone();
    literal.extend_from_slice(&["--agent-token", "example-token"]);
    let expected = support::run(&literal, &[]);
    assert!(expected.stderr.contains("--agent-token-file"));
    assert!(!expected.stderr.contains("example-token"));

    for source in [file.path().to_str().unwrap(), "-"] {
        let mut sourced = args.clone();
        sourced.extend_from_slice(&["--agent-token-file", source]);
        let actual = run(&sourced, b"example-token\n");
        assert_eq!(actual.envelope(), expected.envelope());
        assert_eq!(actual.exit_code(), expected.exit_code());
        assert!(!actual.stderr.contains("supplies a secret"));
    }
}

#[test]
fn identity_file_and_stdin_follow_literal_input_behavior() {
    let file = secret(b"example-seed\n");
    for command in ["attach", "tunnel"] {
        let mut args = if command == "attach" {
            vec![command, "--name", "example"]
        } else {
            vec![command, "5432", "--verify-identity"]
        };
        args.extend_from_slice(ADDRESS);
        args.extend_from_slice(&[
            "--agent-token",
            "example-token",
            "--identity-vm-public-key",
            "example-pin",
        ]);
        let mut literal = args.clone();
        literal.extend_from_slice(&["--identity-host-seed", "example-seed"]);
        let expected = support::run(&literal, &[]);
        assert!(expected.stderr.contains("--identity-host-seed-file"));
        assert!(!expected.stderr.contains("example-seed"));
        for source in [file.path().to_str().unwrap(), "-"] {
            let mut sourced = args.clone();
            sourced.extend_from_slice(&["--identity-host-seed-file", source]);
            let actual = run(&sourced, b"example-seed\r\n");
            assert_eq!(actual.envelope(), expected.envelope());
            assert_eq!(actual.exit_code(), expected.exit_code());
            assert!(!actual.stderr.contains("--identity-host-seed supplies"));
        }
    }
}

#[test]
fn secret_sources_refuse_empty_input_and_shared_stdin() {
    let empty = secret(b"\r\n");
    for source in [empty.path().to_str().unwrap(), "-"] {
        let mut args = vec!["health", "--agent-token-file", source];
        args.extend_from_slice(ADDRESS);
        let result = run(&args, b"\n");
        assert_eq!(result.exit_code(), 2);
        assert!(result.stdout.contains("input must not be empty"));
    }
    let directory = support::TempDir::new("secret-input");
    let mut args = vec!["health", "--agent-token-file", directory.path()];
    args.extend_from_slice(ADDRESS);
    let result = run(&args, b"");
    assert_eq!(result.exit_code(), 2);
    assert!(
        result.stdout.contains("must name a regular file")
            || result
                .stdout
                .contains("could not read --agent-token-file input")
    );
    for command in ["exec", "stdin", "tunnel"] {
        let mut args = match command {
            "exec" => vec!["exec", "cat", "--stdin"],
            "stdin" => vec!["stdin", "exec-example", "--data", "-"],
            _ => vec![
                "tunnel",
                "5432",
                "--verify-identity",
                "--identity-host-seed-file",
                "-",
                "--identity-vm-public-key",
                "example-pin",
            ],
        };
        args.extend_from_slice(ADDRESS);
        args.extend_from_slice(&["--agent-token-file", "-"]);
        let result = run(&args, b"");
        assert_eq!(result.exit_code(), 2);
        assert!(result.stdout.contains("stdin can supply only one input"));
    }
}

#[test]
fn secret_file_flags_require_one_source() {
    for extra in [
        vec![
            "--agent-token",
            "example-token",
            "--agent-token-file",
            "unused",
        ],
        vec!["--agent-token-file", "unused", "--name", "example"],
    ] {
        let mut args = vec!["health"];
        args.extend_from_slice(ADDRESS);
        args.extend(extra);
        let result = run(&args, b"");
        assert_eq!(result.exit_code(), 2);
        assert!(result.stdout.contains("cannot be used with"));
    }
}

#[cfg(unix)]
#[test]
fn secret_files_require_private_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let file = secret(b"example-token");
    file.as_file()
        .set_permissions(std::fs::Permissions::from_mode(0o640))
        .unwrap();
    let mut args = vec![
        "health",
        "--agent-token-file",
        file.path().to_str().unwrap(),
    ];
    args.extend_from_slice(ADDRESS);
    let result = run(&args, b"");
    assert_eq!(result.exit_code(), 2);
    assert!(
        result
            .stdout
            .contains("must grant no permissions to group or other users")
    );
    assert!(!result.stdout.contains("example-token"));
}
