// SPDX-License-Identifier: Apache-2.0
//! Workload handlers for the MicroVM lifecycle hooks.
//!
//! The platform keeps a suspended VM's full memory and disk and offers no option to
//! change that; what it offers instead is the `/suspend` and `/resume` hooks, where
//! AWS's guidance is to close and reopen outbound connections and refresh
//! credentials. agentd owns the hook port, so a workload reaches those moments
//! through here: an executable at `<hooks dir>/<hook>` runs when that hook fires.
//!
//! # Semantics
//!
//! * The handler runs **before** the daemon answers the platform, so a suspend
//!   handler runs before the freeze and a resume handler runs before the platform
//!   forwards held traffic.
//! * The daemon **always answers 200**, whatever the handler did. What the platform
//!   does with a failing or slow hook is undocumented, and a VM that fails to
//!   suspend or resume is worse than one whose handler failed. The outcome is
//!   recorded on the hook's `/v1/health` entry instead.
//! * The handler is killed, with its whole process group, after the configured
//!   budget, which is kept below the image's hook timeout.
//! * One handler runs at a time.
//!
//! # Trust
//!
//! A handler is image-owned code and runs as the daemon's user (root), with the
//! daemon's environment plus the launch environment and `AGENTD_HOOK=<hook>`. The
//! agent token is never in either. The hook routes are unauthenticated and reachable
//! over loopback from inside the guest, so any process in the VM can make a handler
//! run: handlers must be safe to run spuriously. The hook log's cap bounds how many
//! times that can happen.
//!
//! Handler output goes to the daemon's log (the image's log group), never to
//! `/v1/health`, which is readable without the agent token.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use protocol::health::HandlerOutcome;
use tokio::io::{AsyncRead, AsyncReadExt};

use crate::state::AppState;

/// The hooks a handler can exist for; each is also the handler's file name.
pub const HOOK_NAMES: [&str; 4] = ["run", "suspend", "resume", "terminate"];

/// How much of each output stream is logged.
const LOGGED_OUTPUT_BYTES: u64 = 4 * 1024;

/// Runs the workload handler for `hook` if the image carries one, and records the
/// outcome on the hook-log entry at `slot`.
///
/// `slot` is `None` when the hook log's cap dropped this invocation, and then no
/// handler runs.
pub async fn run(state: &AppState, hook: &str, slot: Option<usize>) {
    let Some(slot) = slot else {
        return;
    };
    // Only a known hook name selects a file, so no caller-supplied string becomes a path.
    let Some(name) = HOOK_NAMES.iter().copied().find(|name| *name == hook) else {
        return;
    };
    let path = state.config().hooks_dir.join(name);
    if !path.exists() {
        return;
    }
    let _serialized = state.handler_lock().lock().await;
    let outcome = execute(
        &path,
        hook,
        state.config().hook_handler_timeout,
        &state.launch_env(),
    )
    .await;
    if outcome.succeeded() {
        tracing::info!(
            hook,
            duration_ms = outcome.duration_ms,
            "hook handler succeeded"
        );
    } else {
        tracing::warn!(
            hook,
            exit_code = outcome.exit_code,
            signal = outcome.signal,
            timed_out = outcome.timed_out,
            error = outcome.error.as_deref(),
            "hook handler failed; the hook still answers 200"
        );
    }
    state.record_handler(slot, outcome);
}

/// Spawns one handler and waits for it within `budget`.
pub async fn execute(
    path: &Path,
    hook: &str,
    budget: Duration,
    launch_env: &std::collections::HashMap<String, String>,
) -> HandlerOutcome {
    let started = Instant::now();
    let failed = |error: String| HandlerOutcome {
        exit_code: None,
        signal: None,
        timed_out: false,
        duration_ms: started.elapsed().as_millis() as u64,
        error: Some(error),
    };
    if let Err(error) = check_executable(path) {
        return failed(error);
    }
    let mut command = tokio::process::Command::new(path);
    command
        .envs(launch_env)
        .env("AGENTD_HOOK", hook)
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true);
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => return failed(format!("spawn failed: {error}")),
    };
    let pid = child.id();
    let stdout = tokio::spawn(capture(child.stdout.take()));
    let stderr = tokio::spawn(capture(child.stderr.take()));

    let (status, timed_out) = match tokio::time::timeout(budget, child.wait()).await {
        Ok(status) => (status.ok(), false),
        Err(_) => {
            // The group, not just the child: a handler that backgrounded work must
            // not keep running past the moment the daemon answered the platform.
            if let Some(pid) = pid.and_then(|pid| i32::try_from(pid).ok()) {
                let _ = nix::sys::signal::killpg(
                    nix::unistd::Pid::from_raw(pid),
                    nix::sys::signal::Signal::SIGKILL,
                );
            }
            (child.wait().await.ok(), true)
        }
    };
    let duration_ms = started.elapsed().as_millis() as u64;
    let (stdout, stderr) = (
        stdout.await.unwrap_or_default(),
        stderr.await.unwrap_or_default(),
    );
    if !stdout.is_empty() || !stderr.is_empty() {
        tracing::info!(hook, stdout = %stdout, stderr = %stderr, "hook handler output");
    }
    use std::os::unix::process::ExitStatusExt;
    HandlerOutcome {
        exit_code: status.and_then(|status| status.code()),
        signal: status.and_then(|status| status.signal()),
        timed_out,
        duration_ms,
        error: None,
    }
}

fn check_executable(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = std::fs::metadata(path).map_err(|error| format!("{error}"))?;
    if !metadata.is_file() {
        return Err("not a regular file".to_string());
    }
    if metadata.permissions().mode() & 0o111 == 0 {
        return Err("not executable".to_string());
    }
    Ok(())
}

/// Reads a stream to its end, keeping the first [`LOGGED_OUTPUT_BYTES`]. The rest
/// is drained so a chatty handler never blocks on a full pipe.
async fn capture(stream: Option<impl AsyncRead + Unpin>) -> String {
    let Some(mut stream) = stream else {
        return String::new();
    };
    let mut kept = Vec::new();
    let _ = (&mut stream)
        .take(LOGGED_OUTPUT_BYTES)
        .read_to_end(&mut kept)
        .await;
    let _ = tokio::io::copy(&mut stream, &mut tokio::io::sink()).await;
    String::from_utf8_lossy(&kept).into_owned()
}

/// The handler path for `hook` under `dir`, for documentation and tests.
pub fn handler_path(dir: &Path, hook: &str) -> PathBuf {
    dir.join(hook)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use std::collections::HashMap;
    use std::os::unix::fs::PermissionsExt;

    fn script(dir: &Path, hook: &str, body: &str) -> PathBuf {
        let path = handler_path(dir, hook);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write handler");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        path
    }

    fn state_with(dir: &Path, budget: Duration) -> AppState {
        AppState::new(Config {
            hooks_dir: dir.to_path_buf(),
            hook_handler_timeout: budget,
            ..Config::default()
        })
    }

    #[tokio::test]
    async fn a_handler_runs_with_its_hook_name_and_its_exit_code_is_recorded() {
        let dir = tempfile::tempdir().expect("tempdir");
        let marker = dir.path().join("marker");
        script(
            dir.path(),
            "suspend",
            &format!("echo \"$AGENTD_HOOK\" > {}; exit 3", marker.display()),
        );
        let state = state_with(dir.path(), Duration::from_secs(5));
        let slot = state.record_hook("suspend");
        run(&state, "suspend", slot).await;

        assert_eq!(std::fs::read_to_string(&marker).expect("ran"), "suspend\n");
        let (hooks, _) = state.hook_report();
        let outcome = hooks[0].handler.as_ref().expect("an outcome");
        assert_eq!(outcome.exit_code, Some(3));
        assert!(!outcome.timed_out && !outcome.succeeded());
    }

    #[tokio::test]
    async fn no_file_means_no_handler_and_no_outcome() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state = state_with(dir.path(), Duration::from_secs(5));
        let slot = state.record_hook("resume");
        run(&state, "resume", slot).await;
        assert!(state.hook_report().0[0].handler.is_none());
    }

    #[tokio::test]
    async fn a_slow_handler_and_its_children_are_killed_at_the_budget() {
        let dir = tempfile::tempdir().expect("tempdir");
        let survivor = dir.path().join("survivor");
        script(
            dir.path(),
            "suspend",
            &format!("(sleep 2; touch {}) & sleep 30", survivor.display()),
        );
        let outcome = execute(
            &handler_path(dir.path(), "suspend"),
            "suspend",
            Duration::from_millis(300),
            &HashMap::new(),
        )
        .await;
        assert!(outcome.timed_out, "{outcome:?}");
        assert!(outcome.duration_ms < 5_000, "{outcome:?}");
        tokio::time::sleep(Duration::from_secs(3)).await;
        assert!(!survivor.exists(), "a backgrounded child outlived the kill");
    }

    #[tokio::test]
    async fn a_non_executable_handler_is_reported_rather_than_run() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = handler_path(dir.path(), "run");
        std::fs::write(&path, "#!/bin/sh\nexit 0\n").expect("write");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).expect("chmod");
        let outcome = execute(&path, "run", Duration::from_secs(5), &HashMap::new()).await;
        assert_eq!(outcome.error.as_deref(), Some("not executable"));
    }

    #[tokio::test]
    async fn the_launch_env_reaches_the_handler() {
        let dir = tempfile::tempdir().expect("tempdir");
        let marker = dir.path().join("env");
        script(
            dir.path(),
            "resume",
            &format!("echo \"$SERVICE_URL\" > {}", marker.display()),
        );
        let env = HashMap::from([(
            "SERVICE_URL".to_string(),
            "https://example.test".to_string(),
        )]);
        let outcome = execute(
            &handler_path(dir.path(), "resume"),
            "resume",
            Duration::from_secs(5),
            &env,
        )
        .await;
        assert!(outcome.succeeded(), "{outcome:?}");
        assert_eq!(
            std::fs::read_to_string(&marker).expect("ran"),
            "https://example.test\n"
        );
    }

    /// Past the hook log's cap nothing is recorded, so nothing runs: the cap is also
    /// the bound on how often an in-guest caller can trigger a handler.
    #[tokio::test]
    async fn a_dropped_invocation_runs_no_handler() {
        let dir = tempfile::tempdir().expect("tempdir");
        let marker = dir.path().join("marker");
        script(
            dir.path(),
            "suspend",
            &format!("touch {}", marker.display()),
        );
        let state = state_with(dir.path(), Duration::from_secs(5));
        run(&state, "suspend", None).await;
        assert!(!marker.exists());
    }
}
