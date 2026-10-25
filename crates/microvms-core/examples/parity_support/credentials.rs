// SPDX-License-Identifier: Apache-2.0
//! Real credential-provider failures for the shared corpus. Each surface runs in a
//! fresh process so an earlier test's process-wide provider cannot satisfy the call.

use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use microvms_core::prelude::ControlPlaneExt as _;
use microvms_core::{Error, Region, control::ControlPlane};
use serde_json::{Value, json};

fn input() -> Value {
    let mut text = String::new();
    std::io::stdin().read_to_string(&mut text).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn mentions(input: &Value, text: &str) -> Value {
    input["message_mentions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| {
            let needle = value.as_str().unwrap();
            (needle.to_owned(), Value::Bool(text.contains(needle)))
        })
        .collect::<serde_json::Map<_, _>>()
        .into()
}

fn core_error(input: &Value, error: &Error) -> Value {
    let mut diagnostics = format!("{error} {error:?}");
    let mut source = std::error::Error::source(error);
    while let Some(inner) = source {
        diagnostics.push_str(&format!(" {inner} {inner:?}"));
        source = inner.source();
    }
    json!({
        "error": {"code": error.code(), "wire_kind": error.wire_kind().map(|k| k.as_str()),
            "retryable": error.retryable()},
        "message_mentions": mentions(input, &diagnostics),
        "diagnostics_secret_free": !diagnostics.contains(input["provider_stderr"].as_str().unwrap()),
    })
}

fn launch(request: Value) -> Value {
    let input = &request["input"];
    let directory = tempfile::tempdir().unwrap();
    let marker = directory.path().join("provider-called");
    let config = directory.path().join("config");
    let executable = std::env::current_exe().unwrap();
    let executable = executable.to_str().unwrap();
    // AWS invokes credential_process through the platform shell. The command contains
    // only this executable and a fixed mode; case text is passed through the child env.
    let provider = if cfg!(windows) {
        format!("\"{executable}\" credentials provider")
    } else {
        format!(
            "'{}' credentials provider",
            executable.replace('\'', "'\"'\"'")
        )
    };
    std::fs::write(
        &config,
        format!("[default]\ncredential_process = {provider}\n"),
    )
    .unwrap();
    let mut command = Command::new(request["program"].as_str().unwrap());
    command.args(
        request["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_str().unwrap()),
    );
    command.env_clear();
    // These are runtime locations, never AWS settings or credentials. Windows needs
    // SystemRoot to start cmd.exe; Python and Node may need the dynamic-loader path.
    for name in [
        "PATH",
        "SystemRoot",
        "WINDIR",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
    ] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
        .env("HOME", directory.path())
        .env("USERPROFILE", directory.path())
        .env("AWS_CONFIG_FILE", &config)
        .env(
            "AWS_SHARED_CREDENTIALS_FILE",
            directory.path().join("absent-credentials"),
        )
        .env("AWS_REGION", "us-east-1")
        .env("AWS_EC2_METADATA_DISABLED", "true")
        .env("HTTPS_PROXY", "http://127.0.0.1:9")
        .env("HTTP_PROXY", "http://127.0.0.1:9")
        .env("NO_PROXY", "")
        .env("MICROVMS_PARITY_CREDENTIAL_MARKER", &marker)
        .env(
            "MICROVMS_PARITY_CREDENTIAL_SENTINEL",
            input["provider_stderr"].as_str().unwrap(),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().expect("the actual surface starts");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(input.to_string().as_bytes())
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("credential case exceeded its deadline");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let output = child.wait_with_output().unwrap();
    let stdout = String::from_utf8(output.stdout).unwrap();
    let stderr = String::from_utf8(output.stderr).unwrap();
    let secret_free = !stdout.contains(input["provider_stderr"].as_str().unwrap())
        && !stderr.contains(input["provider_stderr"].as_str().unwrap());
    let mut answer: Value = if request["surface"] == "cli" {
        let envelope: Value = serde_json::from_str(&stdout).expect("one CLI envelope");
        assert_eq!(envelope["exitCode"], output.status.code().unwrap());
        json!({
            "error": {"code": envelope["code"], "wire_kind": envelope["data"].get("kind"),
                "retryable": envelope["code"] == "ERR_RETRYABLE"},
            "message_mentions": mentions(input, &format!("{stdout} {stderr}")),
            "diagnostics_secret_free": secret_free,
        })
    } else {
        assert!(output.status.success(), "the surface worker failed");
        let mut answer: Value = serde_json::from_str(&stdout).expect("one worker verdict");
        answer["diagnostics_secret_free"] =
            Value::Bool(secret_free && answer["diagnostics_secret_free"] == true);
        answer
    };
    answer["provider_invoked"] =
        Value::Bool(std::fs::read_to_string(marker).is_ok_and(|v| v == "called\n"));
    answer
}

pub fn run() {
    match std::env::args().nth(2).as_deref() {
        Some("provider") => {
            std::fs::write(
                std::env::var_os("MICROVMS_PARITY_CREDENTIAL_MARKER").unwrap(),
                "called\n",
            )
            .unwrap();
            eprintln!(
                "{}",
                std::env::var("MICROVMS_PARITY_CREDENTIAL_SENTINEL").unwrap()
            );
            std::process::exit(1);
        }
        Some("core") => {
            let input = input();
            let error = tokio::task::block_in_place(|| {
                tokio::runtime::Handle::current().block_on(async {
                    ControlPlane::new(Region::US_EAST_1)
                        .await?
                        .resume(input["microvm_id"].as_str().unwrap())
                        .await
                })
            })
            .unwrap_err();
            println!("{}", core_error(&input, &error));
        }
        Some("launch") => println!("{}", launch(input())),
        _ => panic!("expected provider, core, or launch"),
    }
}
