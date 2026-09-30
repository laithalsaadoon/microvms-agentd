// SPDX-License-Identifier: Apache-2.0
//! Parity: the shared case corpus, for the cases a fake has to answer (#272).
//!
//! `tests/parity_cases.rs` answers the corpus from a spawned binary; the cases here need the
//! scripted control plane (`build --reuse` and `agent-up` name an image on the recorded
//! `CreateMicrovmImage` call), the scripted daemon (a status answered to `health`, `cp` and
//! `sync`), or a seam that refuses every door (a name read that must stop before one). Both
//! tiers read `verify/parity/cases/` by the rules in
//! `crates/microvms-core/tests/parity_corpus/mod.rs`.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    DaemonScript, RefusingSeam, ScriptedSeam, ScriptedTransport, TempDir, against_daemon,
    attach_flags, dispatch_with, full_infra, list_images_body, region_flags, sync_command,
};
use crate::cli::{BuildArgs, Cli, Command, CpArgs, HealthArgs, InfraFlags};
use crate::exit::{CliError, Exit};

#[path = "../../../microvms-core/tests/parity_corpus/mod.rs"]
mod parity_corpus;

#[tokio::test]
async fn the_cli_answers_the_shared_case_corpus_against_its_fakes() {
    let mut run = parity_corpus::Run::plan(
        "cli",
        &parity_corpus::CLI_FAKE_AREAS,
        &parity_corpus::CLI_PROCESS_AREAS,
    );
    for case in run.cases() {
        let answer = match case.area.as_str() {
            "image-name" => parity_image_name(&case).await,
            "error" => parity_daemon_status(&case).await,
            "names" => parity_names(&case).await,
            other => panic!(
                "{}: parity_corpus::CLI_FAKE_AREAS gives the fakes tier area {other:?}, \
                 which it has no handler for",
                case.id
            ),
        };
        run.judge(&case, answer);
    }
    run.finish();
}

/// A failure's facets, the way the envelope and the exit code state them.
fn parity_refusal(failure: &CliError) -> serde_json::Value {
    serde_json::json!({"error": {
        "code": failure.code(),
        "wire_kind": failure.wire_kind.map(|kind| kind.as_str()),
        "retryable": failure.exit == Exit::Retryable,
    }})
}

/// The name a build went out under, read off the recorded `CreateMicrovmImage` call, which the
/// transport refuses so the command ends there.
async fn parity_image_name(case: &parity_corpus::Case) -> serde_json::Value {
    let binary = std::env::temp_dir().join(format!(
        "microvm-guard-parity-{}-{}",
        std::process::id(),
        case.id.replace('/', "-")
    ));
    std::fs::write(&binary, case.input_binary()).expect("writes the binary");
    let memory = u32::try_from(case.input_u64("size_mib"))
        .ok()
        .and_then(crate::cli::memory_from_mib)
        .unwrap_or_else(|| panic!("{}: size_mib is a --memory value", case.id));
    let state = TempDir::new("parity-agent-up");
    let command = match case.capability.as_str() {
        // #258: `build --reuse` is the CLI's own reuse, which the table exempts from
        // `ensure-image`; the case measures how its name differs.
        "ensure-image" => Command::Build(BuildArgs {
            binary: Some(binary.clone()),
            state_dir: None,
            base_image_version: None,
            artifact_uri: None,
            name: Some(case.input_str("name_prefix").to_string()),
            memory,
            dockerfile: None,
            project: None,
            repair_identity: false,
            log_group: None,
            log_stream: None,
            reuse: true,
            port: None,
            region: region_flags(),
            infra: InfraFlags::default(),
        }),
        "agent-image-name" => Command::AgentUp(crate::cli::AgentUpArgs {
            binary: Some(binary.clone()),
            vm_name: "parity-agent".into(),
            agent: case
                .input_strings("agents")
                .iter()
                .map(|name| {
                    <crate::cli::AgentArg as clap::ValueEnum>::from_str(name, false)
                        .unwrap_or_else(|_| panic!("{}: an --agent value: {name}", case.id))
                })
                .collect(),
            claude_model: None,
            codex_model: None,
            claude_version: None,
            codex_version: None,
            project: None,
            memory,
            token_ttl_hours: 12,
            max_idle_sec: 600,
            suspended_sec: 600,
            auto_resume: false,
            max_duration_sec: 3600,
            port: None,
            state_dir: Some(state.0.clone()),
            region: region_flags(),
            infra: InfraFlags::default(),
            launch: Default::default(),
        }),
        other => panic!("{}: no CLI handler for {other:?} in image-name", case.id),
    };
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("ListMicrovmImages", 200, &list_images_body(&[], None))
        .answer("CreateMicrovmImage", 400, r#"{"message": "scripted stop"}"#);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let _ = std::fs::remove_file(&binary);
    if transport.called("CreateMicrovmImage") == 0 {
        return match result {
            Err(failure) => parity_refusal(&failure),
            Ok(rendered) => panic!("{}: no build went out: {:?}", case.id, rendered.data),
        };
    }
    serde_json::json!({"name": transport.first_body("CreateMicrovmImage")["name"]})
}

/// The case's status answered to one attached command.
async fn parity_daemon_status(case: &parity_corpus::Case) -> serde_json::Value {
    let status = u16::try_from(case.input_u64("status")).expect("a status");
    let body = case.input_str("body");
    let dir = TempDir::new("parity-status");
    let script = DaemonScript::new();
    let command = match case.capability.as_str() {
        "health" => {
            script.reply(status, body);
            Command::Health(HealthArgs {
                attach: attach_flags(),
                region: region_flags(),
            })
        }
        "upload-file" => {
            script.reply(status, body);
            let local = dir.0.join("upload.bin");
            std::fs::write(&local, b"parity").expect("writes");
            Command::Cp(CpArgs {
                src: local.to_string_lossy().to_string(),
                dst: format!("vm:{}", case.input_str("path")),
                tar: false,
                mode: None,
                attach: attach_flags(),
                region: region_flags(),
            })
        }
        // No manifest in the guest yet, so the whole tree travels and meets the status.
        "sync-directory" => {
            script.reply(404, "no such file").reply(status, body);
            std::fs::write(dir.0.join("upload.bin"), b"parity").expect("writes");
            sync_command(&dir.0, |_| {})
        }
        other => panic!("{}: no CLI handler for {other:?} in error", case.id),
    };
    match against_daemon(&script, &command).await.0 {
        Ok(_) => serde_json::json!({"ok": true}),
        Err(failure) => parity_refusal(&failure),
    }
}

/// The case's record written into a state directory's registry, then `exec --name` over it
/// with `--region`, against a seam that refuses every door: the name read must answer before
/// any of them, and a door reached is the seam's `ERR_PLATFORM`, which no case expects.
async fn parity_names(case: &parity_corpus::Case) -> serde_json::Value {
    use clap::Parser as _;
    assert_eq!(
        case.capability, "from-name",
        "{}: a from-name case",
        case.id
    );
    let state = TempDir::new("parity-names");
    let name = case.input_str("name");
    let names = state.0.join("names");
    std::fs::create_dir_all(&names).expect("the names directory");
    std::fs::write(
        names.join(format!("{name}.json")),
        case.input_str("record_text"),
    )
    .expect("writes the record");
    let state_dir = state.0.to_string_lossy().to_string();
    let argv = [
        "microvm",
        "exec",
        "--name",
        name,
        "--region",
        case.input_str("region"),
        "--state-dir",
        &state_dir,
        "true",
    ];
    let command = Cli::try_parse_from(argv)
        .unwrap_or_else(|error| panic!("{}: {}", case.id, error.render()))
        .command;
    let seam = RefusingSeam::new();
    match dispatch_with(&seam, &command, full_infra()).await.0 {
        Ok(rendered) => panic!("{}: exec answered {:?}", case.id, rendered.data),
        Err(failure) => {
            let mut answer = parity_refusal(&failure);
            answer["message_mentions"] = case.message_mentions(&failure.message);
            answer
        }
    }
}
