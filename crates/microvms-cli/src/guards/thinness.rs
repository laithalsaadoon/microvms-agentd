// SPDX-License-Identifier: Apache-2.0
//! CLI-2's behavioral half: a seam that fails closed.

#![cfg(test)]

use std::time::Duration;

use super::support::{
    CountingFetch, FakeBinary, RefusingSeam, SENTINEL, attach_flags, dispatch_with,
    dispatch_with_fetch, full_infra, no_config, region_flags,
};
use crate::cli::{
    AckArgs, AttachArgs, AttachFlags, BuildArgs, Cli, Command, CostArgs, CpArgs, DoctorArgs,
    ExecArgs, ExistsArgs, Explicit, HealthArgs, InfraFlags, KeepaliveArgs, LogsArgs, LsArgs,
    MemoryMib, PortForwardArgs, RegionFlags, ResumeArgs, RunArgs, StdinArgs, SuspendArgs,
    TerminateArgs, TunnelArgs, WaitArgs,
};
use crate::seam::Door;

/// Every AWS-touching command, its arguments, and the door it must enter.
///
/// The door is named per command rather than left implicit, because "it failed" and "it went
/// through the seam" are different claims and only the second is what CLI-2 asks for.
fn aws_commands(binary: &std::path::Path) -> Vec<(&'static str, Command, Door)> {
    vec![
        (
            // `quickstart` builds, so its door is `run`'s. Its state dir is a fresh temp
            // path because it carries no binary: the door test's scripted fetch provisions
            // one there, which is itself part of what the row proves — quickstart reaches
            // the sandbox door only through the provisioning chain.
            "quickstart",
            Command::Quickstart(crate::cli::QuickstartArgs {
                exec: "true".into(),
                state_dir: Some(std::env::temp_dir().join(format!(
                    "microvm-guard-quickstart-{}-{:?}",
                    std::process::id(),
                    std::thread::current().id()
                ))),
                region: region_flags(),
                infra: InfraFlags::default(),
            }),
            Door::OpenSandbox,
        ),
        (
            "run",
            Command::Run(Box::new(RunArgs {
                binary: Some(binary.to_path_buf()),
                image: None,
                image_version: None,
                artifact_uri: Some("s3://bucket/img.zip".into()),
                exec: Some("true".into()),
                name: Some("img".into()),
                memory: MemoryMib::Mib2048,
                size: crate::cli::SizeRequestFlags::default(),
                dockerfile: None,
                repair_identity: false,
                log_group: None,
                log_stream: None,
                egress: false,
                egress_network_connectors: Vec::new(),
                deny_egress: false,
                shell: false,
                launch_env: Vec::new(),
                user: None,
                group: None,
                keep: false,
                no_wait: false,
                identity: false,
                vm_name: None,
                timeout: Duration::from_secs(30),
                max_idle_sec: 600,
                suspended_sec: 600,
                auto_resume: false,
                max_duration_sec: 3600,
                port: None,
                state_dir: Some(std::env::temp_dir().join("microvm-guard-ledgers")),
                // No config file: the guard exercises the seam, not the merge, and an
                // ambient microvm.toml in the test runner's cwd must not leak in.
                config: no_config(),
                explicit: Explicit::default(),
                region: region_flags(),
                infra: InfraFlags::default(),
                launch: Default::default(),
            })),
            Door::OpenSandbox,
        ),
        (
            "build",
            Command::Build(BuildArgs {
                binary: Some(binary.to_path_buf()),
                state_dir: None,
                base_image_version: None,
                artifact_uri: Some("s3://bucket/img.zip".into()),
                name: Some("img".into()),
                memory: MemoryMib::Mib2048,
                size: crate::cli::SizeRequestFlags::default(),
                dockerfile: None,
                project: None,
                repair_identity: false,
                log_group: None,
                log_stream: None,
                reuse: false,
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
            Door::OpenSandbox,
        ),
        (
            // The fresh path: an unregistered name reaches the sandbox door. The state dir is
            // a fresh temp path so no registry from another test makes this the refresh path.
            "agent-up",
            Command::AgentUp(crate::cli::AgentUpArgs {
                binary: Some(binary.to_path_buf()),
                vm_name: "guard-agent".into(),
                agent: vec![crate::cli::AgentArg::ClaudeCode],
                claude_model: None,
                codex_model: None,
                claude_version: None,
                codex_version: None,
                project: None,
                memory: MemoryMib::Mib1024,
                size: crate::cli::SizeRequestFlags::default(),
                token_ttl_hours: 12,
                max_idle_sec: 600,
                suspended_sec: 600,
                auto_resume: false,
                max_duration_sec: 3600,
                port: None,
                state_dir: Some(std::env::temp_dir().join(format!(
                    "microvm-guard-agent-up-{}-{:?}",
                    std::process::id(),
                    std::thread::current().id()
                ))),
                region: region_flags(),
                infra: InfraFlags::default(),
                launch: Default::default(),
            }),
            Door::OpenSandbox,
        ),
        (
            "agent-prompt",
            Command::AgentPrompt(crate::cli::AgentPromptArgs {
                task: "count the files".into(),
                permission_mode: crate::cli::AgentPermissionModeArg::AgentDefault,
                execution_timeout: None,
                reap_group_on_exit: false,
                agent: Some(crate::cli::AgentArg::ClaudeCode),
                timeout: Duration::from_secs(30),
                detach: false,
                exec_id: None,
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "exec",
            Command::Exec(ExecArgs {
                command: Some("true".into()),
                timeout: Duration::from_secs(30),
                timeout_sec: None,
                complete: false,
                client_grace: None,
                cwd: None,
                env: Vec::new(),
                user: None,
                group: None,
                shell: None,
                inherit_image_env: false,
                exec_id: None,
                poll: None,
                detach: false,
                stream: false,
                from_offset: None,
                stdin: false,
                reap: false,
                kill_on_timeout: false,
                attach: AttachFlags {
                    state_dir: Some(std::env::temp_dir().join("microvm-guard-history")),
                    ..attach_flags()
                },
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "wait",
            Command::Wait(WaitArgs {
                timeout: Duration::from_secs(60),
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "health",
            Command::Health(HealthArgs {
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "keepalive",
            Command::Keepalive(KeepaliveArgs {
                interval: None,
                while_busy: false,
                for_sec: None,
                idle_window: None,
                tolerated_errors: microvms_core::session::keepalive::DEFAULT_TOLERATED_ERRORS,
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "tunnel",
            // `--max-connections 0` for the reason port-forward's entry gives: the guard measures
            // the door, and an entry that could serve a connection would wait for one.
            Command::Tunnel(TunnelArgs {
                ports: "5432".into(),
                bind: "127.0.0.1".into(),
                max_connections: Some(0),
                verify_identity: false,
                identity_host_seed: None,
                identity_vm_public_key: None,
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "port-forward",
            // `--max-connections 0` so the guard measures the door and returns: the seam fails
            // before a listener is ever bound, and a guard entry that could serve a connection
            // would be a guard that waits for one.
            Command::PortForward(PortForwardArgs {
                ports: "8080".into(),
                bind: "127.0.0.1".into(),
                max_connections: Some(0),
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "ack",
            Command::Ack(AckArgs {
                exec_id: "x-1".into(),
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "kill",
            Command::Kill(crate::cli::KillArgs {
                exec_id: "x-1".into(),
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "ps",
            Command::Ps(crate::cli::PsArgs {
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "stdin",
            Command::Stdin(StdinArgs {
                exec_id: "x-1".into(),
                // A literal rather than `-`: `--data -` reads this process's stdin, and a test
                // that blocked on the runner's stdin would hang rather than fail.
                data: Some("hello".into()),
                eof: true,
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "cp",
            Command::Cp(CpArgs {
                // A path that does not exist, deliberately: `cp` attaches *before* it reads the
                // local file, so the door is entered either way — and a nonexistent path proves
                // the ordering rather than assuming it. Getting it backwards would make this row
                // fail on a precondition with `entered: nothing`, which is the failure the door
                // assertion is for.
                src: "/definitely/not/here/payload".into(),
                dst: "vm:/tmp/payload".into(),
                tar: false,
                mode: None,
                lines: None,
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            "exists",
            Command::Exists(ExistsArgs {
                path: "/tmp/payload".into(),
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            // `sync`'s local precondition is only "the positional is a directory", and the
            // temp dir satisfies it without inventing a tree: the door refuses before any
            // hashing happens, which is itself part of what the row proves — a sync that
            // hashed first would spend local work on an invocation AWS is about to refuse.
            "sync",
            Command::Sync(crate::cli::SyncArgs {
                dir: std::env::temp_dir(),
                watch: false,
                full: false,
                timeout: Duration::from_secs(60),
                attach: attach_flags(),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            // `attach`'s door is the probe, and every local precondition passes without a
            // registry: a fresh state dir holds no colliding name, so the row reaches the
            // seam — and the refusal there is what proves nothing is written before it.
            "attach",
            Command::Attach(AttachArgs {
                from: None,
                name: Some("adopted".into()),
                endpoint: Some("https://mvm-1.example".into()),
                agent_token: Some("t".into()),
                microvm_id: Some("mvm-1".into()),
                identity_host_seed: None,
                identity_vm_public_key: None,
                verify_identity: false,
                port: None,
                state_dir: Some(std::env::temp_dir().join(format!(
                    "microvm-guard-attach-{}-{:?}",
                    std::process::id(),
                    std::thread::current().id()
                ))),
                region: region_flags(),
            }),
            Door::AttachSession,
        ),
        (
            // The shell's door is the control plane — its credential is a fresh
            // `CreateMicrovmShellAuthToken` mint per session, not the agent token — and
            // the door fails before raw mode is ever enabled, which is also what this
            // row proves: a guard entry that put the test harness's terminal into raw
            // mode would be a guard nobody could read the output of.
            "shell",
            Command::Shell(crate::cli::ShellArgs {
                endpoint: Some("vm-guard.example.aws".into()),
                microvm_id: Some("mvm-1".into()),
                name: None,
                state_dir: Some(std::env::temp_dir().join("microvm-guard-history")),
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "suspend",
            Command::Suspend(SuspendArgs {
                microvm_id: "mvm-1".into(),
                timeout: Duration::from_secs(30),
                state_dir: Some(std::env::temp_dir().join("microvm-guard-history")),
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "resume",
            Command::Resume(ResumeArgs {
                microvm_id: "mvm-1".into(),
                timeout: Duration::from_secs(30),
                state_dir: Some(std::env::temp_dir().join("microvm-guard-history")),
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "terminate",
            Command::Terminate(TerminateArgs {
                microvm_id: "mvm-1".into(),
                image_identifier: None,
                image_name: None,
                delete_image: false,
                wait: false,
                wait_sec: None,
                state_dir: Some(std::env::temp_dir().join("microvm-guard-history")),
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "image-versions",
            Command::ImageVersions(crate::cli::ImageVersionsArgs {
                image: "arn:image".into(),
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "image-set-status",
            Command::ImageSetStatus(crate::cli::ImageSetStatusArgs {
                image: "arn:image".into(),
                image_version: "1.0".into(),
                status: microvms_core::control::ops::VersionStatus::Inactive,
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "image-builds",
            Command::ImageBuilds(crate::cli::ImageBuildsArgs {
                image: "arn:image".into(),
                image_version: "1.0".into(),
                build_id: None,
                region: region_flags(),
            }),
            Door::ControlPlane,
        ),
        (
            "doctor",
            Command::Doctor(DoctorArgs {
                binary: None,
                infra_dir: Some(std::path::PathBuf::from("/definitely/not/a/stack")),
                config: no_config(),
                region: region_flags(),
                infra: InfraFlags::default(),
            }),
            Door::ControlPlane,
        ),
    ]
}

/// The commands that reach no door, and why each is legitimately local.
///
/// Listed with a reason rather than skipped by a naming rule, so a *new* AWS-touching command is
/// covered by the guard by default and can only leave the net by someone writing its name here.
const LOCAL_ONLY: [(&str, &str); 8] = [
    (
        "ls",
        "reads the local ledger; the whole point is that AWS cannot attribute a dead run",
    ),
    (
        "history",
        "reads the local per-VM history; the record's value is that it survives the VM, and \
         no GetMicrovm can answer about an id the platform has already forgotten",
    ),
    (
        "names",
        "reads and deletes this machine's name registry, one local file per name; a name \
         is a pointer the CLI keeps, and the account knows nothing of it",
    ),
    (
        "logs",
        "names the build log group and prints the aws logs tail invocation that reads it — no \
         CloudWatch client exists in either crate, and adding one to the CLI is what CLI-2 \
         forbids",
    ),
    (
        "cost",
        "arithmetic over the rate table pinned in microvms-core; no account is involved",
    ),
    (
        "manifest",
        "introspects the clap tree and the exit table, both of which are compile-time constants",
    ),
    (
        "constants",
        "emits microvms_core::constants::as_json for the drift gate",
    ),
    (
        "dockerfile",
        "renders one of core's Dockerfiles to stdout (the default stanza, a wrapped task \
         Dockerfile, an agent image's): strings built from compile-time constants and the \
         caller's own file, and no account is involved",
    ),
];

/// **CLI-2's behavioral guard.** Every AWS-touching command fails through the seam, with the
/// seam's own error, having entered the door it is supposed to.
///
/// Three assertions per command, and the third is the load-bearing one. A handler that reached
/// around the seam and built its own `ControlPlane` would still fail — there are no credentials
/// in a test environment — and it would fail with a *different* message, which is what the second
/// assertion catches. But a handler that failed for its own unrelated reason would pass both, and
/// only "which door was entered" separates that from a thin layer.
///
/// **Falsification** — replace `ctx.seam.control_plane(region)` in `commands::lifecycle::suspend`
/// with a direct `ControlPlane::new(region)` and the `suspend` row goes red on the door list
/// (`entered: nothing`) while still failing. Verified; see the packet's guard proofs.
#[tokio::test]
async fn every_aws_command_fails_through_the_seam_and_names_the_door_it_entered() {
    let binary = FakeBinary::new("behavioral");
    for (name, command, expected) in aws_commands(&binary.0) {
        let seam = RefusingSeam::new();
        // A scripted fetch rather than the panicking one, because `quickstart` carries no
        // binary and must provision before it can reach its door. The count assertion
        // below keeps the old property for every other row: only quickstart fetches.
        let fetch = CountingFetch(std::sync::atomic::AtomicUsize::new(0));
        let (result, _) = dispatch_with_fetch(&seam, &command, full_infra(), &fetch).await;
        assert_eq!(
            fetch.0.load(std::sync::atomic::Ordering::SeqCst),
            usize::from(name == "quickstart"),
            "{name} consulted the provisioning chain when it carries its own binary"
        );

        match result {
            Ok(rendered) => {
                // `doctor` is the one command that *reports* a failure rather than raising, so a
                // success envelope is correct — but it must still say the credential check
                // failed, and it must still have gone through the door.
                assert_eq!(
                    name, "doctor",
                    "{name} succeeded with every seam door refusing"
                );
                assert_eq!(rendered.data["ok"], false, "doctor must report the failure");
                assert!(
                    rendered.text.contains(SENTINEL),
                    "doctor's credential check must carry the seam's own error: {}",
                    rendered.text
                );
            }
            Err(failure) => {
                assert!(
                    failure.message.contains(SENTINEL),
                    "{name} failed, but not with the seam's error — it reached AWS another way, \
                     or failed for an unrelated reason: {}",
                    failure.message
                );
            }
        }
        assert!(
            seam.doors().contains(&expected),
            "{name} did not enter {}; it reached the control plane by constructing its own \
             client instead of going through the seam (entered: {:?})",
            expected.as_str(),
            seam.doors(),
        );
    }
}

/// The guard's command list covers every registered command, or names it local with a reason.
///
/// A list is exactly the thing that goes stale when a thirteenth command lands, so it is checked
/// against the clap tree rather than trusted.
#[test]
fn the_behavioral_guard_covers_every_registered_command() {
    use clap::CommandFactory;

    let binary = std::path::PathBuf::from("/tmp/unused");
    let guarded: std::collections::BTreeSet<&str> = aws_commands(&binary)
        .iter()
        .map(|(name, _, _)| *name)
        .collect();
    let local: std::collections::BTreeSet<&str> =
        LOCAL_ONLY.iter().map(|(name, _)| *name).collect();
    let registered: std::collections::BTreeSet<String> = Cli::command()
        .get_subcommands()
        .map(|sub| sub.get_name().to_string())
        .collect();
    let covered: std::collections::BTreeSet<String> = guarded
        .union(&local)
        .map(|name| (*name).to_string())
        .collect();

    assert_eq!(
        registered,
        covered,
        "commands neither guarded nor declared local: {:?}; declared but not registered: {:?}",
        registered.difference(&covered).collect::<Vec<_>>(),
        covered.difference(&registered).collect::<Vec<_>>(),
    );
    // Every local exemption states its reason, so the list cannot grow silently.
    for (name, reason) in LOCAL_ONLY {
        assert!(reason.len() > 30, "{name}'s exemption needs a real reason");
    }
}

/// A local command reaches no door at all.
///
/// The other half of the guard: without it, a "the seam was entered" assertion would be satisfied
/// by a `cost` command that pointlessly opened a control plane, and the four local commands would
/// stop being local without anything noticing.
#[tokio::test]
async fn no_local_command_touches_a_seam_door() {
    let commands = [
        Command::Ls(LsArgs {
            state_dir: Some(std::path::PathBuf::from("/nonexistent-guard-ledgers")),
            watch: false,
            interval_sec: 2.0,
            max_refreshes: None,
            remote: false,
            prune: false,
            region: RegionFlags::default(),
        }),
        // The watch path is the same local read in a loop, so it is under the same
        // guard: bounded to one refresh, and still no seam door.
        Command::Ls(LsArgs {
            state_dir: Some(std::path::PathBuf::from("/nonexistent-guard-ledgers")),
            watch: true,
            interval_sec: 2.0,
            max_refreshes: Some(1),
            remote: false,
            prune: false,
            region: RegionFlags::default(),
        }),
        Command::History(crate::cli::HistoryArgs {
            microvm_id: "mvm-1".into(),
            state_dir: Some(std::path::PathBuf::from("/nonexistent-guard-ledgers")),
        }),
        Command::Logs(LogsArgs {
            image_name: "img".into(),
            region: region_flags(),
        }),
        Command::Cost(CostArgs {
            estimate: false,
            compare: false,
            memory: MemoryMib::Mib2048,
            size: crate::cli::SizeRequestFlags::default(),
            running_sec: 1.0,
            suspended_sec: 0.0,
            build_sec: 0.0,
            image_gb: None,
            cycles: 1,
            hold_sec: Duration::from_secs(3600),
            // The budget gate is arithmetic over the same local report, so a gated
            // invocation is exercised here too: still no seam door.
            max_cost: Some("0.001".into()),
            on_breach: Some(crate::cli::OnBreach::Abort),
        }),
        Command::Manifest,
        Command::Constants(crate::cli::ConstantsArgs { emit_json: true }),
        Command::Dockerfile(crate::cli::DockerfileArgs {
            from: None,
            port: 9000,
            workdir: Some("/workspace".into()),
            wrap: None,
            inherit_workdir: false,
            agent: Vec::new(),
            claude_version: None,
            codex_version: None,
        }),
    ];
    for command in &commands {
        let seam = RefusingSeam::new();
        let (_, _) = dispatch_with(&seam, command, full_infra()).await;
        assert!(
            seam.doors().is_empty(),
            "a local command entered {:?}",
            seam.doors()
        );
    }
}
