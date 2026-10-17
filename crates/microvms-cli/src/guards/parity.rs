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
    attach_flags, dispatch_with, dispatch_with_env, full_infra, region_flags, sync_command,
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
            "control-plane" => parity_control_plane(&case).await,
            "forward" => parity_forward(&case).await,
            "image-name" => parity_image_name(&case).await,
            "error" => parity_daemon_status(&case).await,
            "names" => parity_names(&case).await,
            "tunnel" => parity_tunnel(&case).await,
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

#[expect(
    clippy::disallowed_types,
    reason = "the shared case peer is a test-only subprocess, never part of the CLI"
)]
async fn parity_tunnel(case: &parity_corpus::Case) -> serde_json::Value {
    use clap::Parser as _;
    use std::process::Stdio;
    use tokio::io::AsyncReadExt as _;

    let child = std::process::Command::new(parity_corpus::tunnel::fixture_path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("build the fixture with mise run parity:fixtures");
    let mut peer = parity_corpus::tunnel::Peer::started(child, &case.input);
    let ready = &peer.ready;
    let reserved = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = reserved.local_addr().unwrap();
    drop(reserved);
    let ports = format!("{}:{}", address.port(), case.input_u64("guest_port"));
    let command = Cli::try_parse_from([
        "microvm",
        "tunnel",
        &ports,
        "--endpoint",
        ready["endpoint"].as_str().unwrap(),
        "--microvm-id",
        "mvm-parity",
        "--agent-token",
        "parity-tunnel-token",
        "--region",
        "us-east-1",
        "--max-connections",
        "1",
        "--verify-identity",
        "--identity-host-seed",
        ready["host_seed"].as_str().unwrap(),
        "--identity-vm-public-key",
        ready["vm_public_key"].as_str().unwrap(),
    ])
    .unwrap()
    .command;
    let seam = super::support::ScriptedSessionSeam {
        script: DaemonScript::at_endpoint(ready["endpoint"].as_str().unwrap().into()),
    };
    let read = async {
        let mut client = loop {
            match tokio::net::TcpStream::connect(address).await {
                Ok(client) => break client,
                Err(_) => tokio::time::sleep(std::time::Duration::from_millis(10)).await,
            }
        };
        let mut bytes = Vec::new();
        // Report the TCP verdict alongside the bytes, including a refused verified tunnel.
        let reset = match client.read_to_end(&mut bytes).await {
            Ok(_) => false,
            Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => true,
            Err(error) => panic!("the tunnel read failed: {error}"),
        };
        (bytes, reset)
    };
    let ((result, _), (bytes, reset)) =
        tokio::time::timeout(std::time::Duration::from_secs(20), async {
            tokio::join!(dispatch_with(&seam, &command, full_infra()), read)
        })
        .await
        .expect("the CLI tunnel completes");
    let rendered = result.expect("the CLI reports the completed connection");
    let verdict = peer.finish();
    serde_json::json!({"received": String::from_utf8(bytes).unwrap(),
        "refused": rendered.data["connectionsRefused"], "confirmed": verdict["confirmed"],
        "reset": reset})
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
        // `build --reuse` is core's `ensure_image` (#258), so its name is the one core gives.
        "ensure-image" => Command::Build(BuildArgs {
            binary: Some(binary.clone()),
            state_dir: None,
            base_image_version: None,
            artifact_uri: None,
            name: Some(case.input_str("name_prefix").to_string()),
            memory,
            size: crate::cli::SizeRequestFlags::default(),
            dockerfile: None,
            project: None,
            repair_identity: false,
            log_group: None,
            log_stream: None,
            reuse: true,
            s3_key_prefix: None,
            force: false,
            tags: Vec::new(),
            base_image: None,
            inherit_workdir: false,
            run_hook_timeout_sec: None,
            build_hook_timeout_sec: None,
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
            size: crate::cli::SizeRequestFlags::default(),
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
    // Both builds are ensures (#258): the name is free, so each builds and the create is
    // where it stops.
    transport
        .answer("GetMicrovmImage", 404, r#"{"message": "Image not found"}"#)
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
    if case.input.get("response").is_some() {
        let upstream = parity_corpus::ForwardUpstream::for_case(case).await;
        let script = DaemonScript::over_http(format!("http://{}", upstream.address));
        let command = if case.capability == "stream" {
            super::support::exec_command(|args| {
                args.stream = true;
            })
        } else if case.capability == "upload-file" {
            let dir = TempDir::new("parity-upload");
            let local = dir.0.join("upload.bin");
            std::fs::write(&local, b"parity").expect("writes");
            let command = Command::Cp(CpArgs {
                src: local.to_string_lossy().to_string(),
                dst: format!("vm:{}", case.input_str("path")),
                tar: false,
                mode: None,
                lines: None,
                attach: attach_flags(),
                region: region_flags(),
            });
            let mut answer = match against_daemon(&script, &command).await.0 {
                Ok(_) => serde_json::json!({"ok":true}),
                Err(failure) => parity_refusal(&failure),
            };
            answer["upstream_requests"] = serde_json::json!(upstream.request_count());
            return answer;
        } else {
            Command::Health(HealthArgs {
                attach: attach_flags(),
                region: region_flags(),
            })
        };
        let mut answer = match against_daemon(&script, &command).await.0 {
            Ok(_) => serde_json::json!({"ok":true}),
            Err(failure) => parity_refusal(&failure),
        };
        // Starting an exec is specific to the CLI's stream entry point.
        answer["upstream_requests"] =
            serde_json::json!(upstream.request_count() - usize::from(case.capability == "stream"));
        return answer;
    }
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
            script.reply(200, "").reply(status, body);
            let local = dir.0.join("upload.bin");
            std::fs::write(&local, b"parity").expect("writes");
            Command::Cp(CpArgs {
                src: local.to_string_lossy().to_string(),
                dst: format!("vm:{}", case.input_str("path")),
                tar: false,
                mode: None,
                lines: None,
                attach: attach_flags(),
                region: region_flags(),
            })
        }
        // No manifest in the guest yet, so the whole tree travels and meets the status.
        "sync-directory" => {
            script
                .reply(404, "no such file")
                .reply(200, "")
                .reply(status, body);
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
    let home = case.input.get("home").and_then(serde_json::Value::as_str);
    if home.is_none() {
        let names = state.0.join("names");
        std::fs::create_dir_all(&names).expect("the names directory");
        std::fs::write(
            names.join(format!("{name}.json")),
            case.input_str("record_text"),
        )
        .expect("writes the record");
    }
    let state_dir = state.0.to_string_lossy().to_string();
    let mut argv = vec![
        "microvm",
        "exec",
        "--name",
        name,
        "--region",
        case.input_str("region"),
    ];
    if home.is_none() {
        argv.extend(["--state-dir", &state_dir]);
    }
    argv.push("true");
    let command = Cli::try_parse_from(argv)
        .unwrap_or_else(|error| panic!("{}: {}", case.id, error.render()))
        .command;
    let seam = RefusingSeam::new();
    let result = match home {
        Some(home) => dispatch_with_env(&seam, &command, full_infra(), ("HOME", home)).await,
        None => dispatch_with(&seam, &command, full_infra()).await,
    };
    match result.0 {
        Ok(rendered) => panic!("{}: exec answered {:?}", case.id, rendered.data),
        Err(failure) => {
            let mut answer = parity_refusal(&failure);
            answer["message_mentions"] = case.message_mentions(&failure.message);
            answer
        }
    }
}

async fn parity_forward(case: &parity_corpus::Case) -> serde_json::Value {
    use super::support::ScriptedSessionSeam;
    let upstream = parity_corpus::ForwardUpstream::for_case(case).await;
    let reserved = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("reserves local port");
    let address = reserved.local_addr().expect("local address");
    let command = Command::PortForward(crate::cli::PortForwardArgs {
        ports: format!("{}:{}", address.port(), upstream.address.port()),
        bind: case
            .input
            .get("bind_ip")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("127.0.0.1")
            .to_string(),
        max_connections: Some(1),
        attach: attach_flags(),
        region: region_flags(),
    });
    let seam = ScriptedSessionSeam {
        script: DaemonScript::at_endpoint(format!("http://{}", upstream.address)),
    };
    let request = parity_corpus::forward_request(case, address.port());
    // The CLI binds its own socket; release the reservation immediately before dispatch.
    drop(reserved);
    let (dispatched, response) = tokio::join!(
        dispatch_with(&seam, &command, full_infra()),
        parity_corpus::forward_exchange(address, &request),
    );
    let data = dispatched.0.expect("port-forward completes").data;
    let mut answer = parity_corpus::forward_answer(&response, case);
    answer["served"] = data["connectionsServed"].clone();
    answer["refused"] = data["connectionsRefused"].clone();
    answer["upstream_requests"] = serde_json::json!(upstream.request_count());
    answer
}

async fn parity_control_plane(case: &parity_corpus::Case) -> serde_json::Value {
    use clap::Parser as _;
    let transport =
        microvms_core::testing::ParityControlPlane::from_json(&case.input("script").to_string())
            .unwrap();
    let seam = ParitySeam {
        transport: transport.clone(),
        refusing: RefusingSeam::new(),
    };
    let state = TempDir::new("parity-control-plane");
    if case.input_strings("actions") == ["list"] {
        let command = Cli::try_parse_from([
            "microvm",
            "ls",
            "--remote",
            "--region",
            "us-east-1",
            "--state-dir",
            state.0.to_str().unwrap(),
        ])
        .unwrap()
        .command;
        let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
        let data = result.unwrap().data;
        let items = data["remote"]["microvms"].as_array().unwrap();
        return serde_json::json!({"items": items.iter().map(|item| serde_json::json!({
            "id": item["microvmId"], "state": item["state"], "image_arn": item["imageArn"],
            "started_at": item["startedAt"],
        })).collect::<Vec<_>>(), "calls": transport.calls()});
    }
    let command = |action: &str| {
        Cli::try_parse_from([
            "microvm",
            action,
            case.input_str("microvm_id"),
            "--region",
            "us-east-1",
            "--state-dir",
            state.0.to_str().unwrap(),
        ])
        .unwrap()
        .command
    };
    for action in case.input_strings("actions") {
        let (result, _) = dispatch_with(&seam, &command(&action), full_infra()).await;
        match result {
            Ok(_) => assert_eq!(action, "suspend"),
            Err(error) => {
                let mut answer = parity_refusal(&error);
                if case.input.get("message_mentions").is_some() {
                    answer["message_mentions"] = case.message_mentions(&error.message);
                }
                answer["calls"] = serde_json::json!(transport.calls());
                return answer;
            }
        }
    }
    panic!("a control-plane case must end with a refusal")
}

/// The CLI receives the same ordered script as the bindings, with no alternate response map.
struct ParitySeam {
    transport: Arc<microvms_core::testing::ParityControlPlane>,
    refusing: RefusingSeam,
}

impl crate::seam::CoreSeam for ParitySeam {
    fn control_plane(
        &self,
        _region: microvms_core::Region,
    ) -> crate::seam::futures_util_shim::BoxFuture<
        '_,
        Result<microvms_core::control::ControlPlane, microvms_core::Error>,
    > {
        let plane = self.transport.plane();
        Box::pin(async move { Ok(plane) })
    }

    fn open_sandbox(
        &self,
        region: microvms_core::Region,
        port: Option<u16>,
    ) -> crate::seam::futures_util_shim::BoxFuture<
        '_,
        Result<microvms_core::sandbox::Sandbox, microvms_core::Error>,
    > {
        crate::seam::CoreSeam::open_sandbox(&self.refusing, region, port)
    }

    fn attach_session(
        &self,
        region: microvms_core::Region,
        attach: crate::seam::Attach,
    ) -> crate::seam::futures_util_shim::BoxFuture<
        '_,
        Result<microvms_core::session::Session, microvms_core::Error>,
    > {
        crate::seam::CoreSeam::attach_session(&self.refusing, region, attach)
    }
}
