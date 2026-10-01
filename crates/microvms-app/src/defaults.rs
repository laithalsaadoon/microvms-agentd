// SPDX-License-Identifier: Apache-2.0
//! The client's defaults, by name: what every surface uses when its caller leaves a value out
//! (#300).
//!
//! The values live where the rules that use them do, in the domain and the app, as named
//! constants and as the `Default` of the request types. [`client_defaults`] reads them back into
//! one JSON object, so a surface that restates one (a clap `default_value`, a PyO3 `signature`
//! literal) can be compared with core's figure. `microvm manifest` publishes it as
//! `data.clientDefaults`, and `tools/check-parity.py` holds each surface default to it through
//! the `[[default]]` rows of `verify/parity/capabilities.toml`.
//!
//! A value is read from the constant or the request's `Default`, never typed here: a copy in
//! this file would be one more restatement for the check to trust.

use std::time::Duration;

use serde_json::{Value, json};

use crate::agents::{DEFAULT_PROMPT_TIMEOUT, DEFAULT_SIZE, PromptOptions, bedrock};
use crate::control::artifact::WrapOptions;
use crate::control::ensure::EnsureImageRequest;
use crate::control::{CreateImageRequest, DEFAULT_AGENT_PORT};
use crate::cost::DEFAULT_RESIDENCY_CYCLES;
use crate::sandbox::{
    DEFAULT_LIFECYCLE_TIMEOUT, LIFECYCLE_POLL_INTERVAL, RunRequest, TeardownOpts,
};
use crate::session::{DEFAULT_CLIENT_GRACE, DEFAULT_EXEC_WAIT, StreamOptions};
use crate::sizing::SizeClass;

/// Every default a surface may restate, keyed as the `[[default]]` rows name them.
///
/// Durations are seconds and sizes MiB, the units the surfaces take them in. The nested objects
/// are the request types' own defaults: what a launch, an exec, a stream, a teardown and a
/// prompt do when a caller sets nothing.
pub fn client_defaults() -> Value {
    let launch = RunRequest::default();
    let start = protocol::exec::StartRequest::new(String::new(), Vec::new());
    let stream = StreamOptions::default();
    let teardown = TeardownOpts::default();
    let prompt = PromptOptions::default();
    let image = CreateImageRequest::new(String::new(), Vec::new(), String::new(), String::new());
    let ensured = EnsureImageRequest::new(
        String::new(),
        Vec::new(),
        String::new(),
        String::new(),
        String::new(),
    );
    json!({
        "sizeMib": SizeClass::DEFAULT.baseline_mib(),
        "agentSizeMib": DEFAULT_SIZE.baseline_mib(),
        "agentPort": DEFAULT_AGENT_PORT,
        "execWaitSeconds": seconds(DEFAULT_EXEC_WAIT),
        // The daemon wait a launch makes after RUNNING, and the RUNNING wait before it (#254).
        "readyTimeoutSeconds": seconds(crate::session::DEFAULT_BOOTSTRAP_TIMEOUT),
        "launchTimeoutSeconds": seconds(crate::sandbox::DEFAULT_RUNNING_TIMEOUT),
        "lifecycleTimeoutSeconds": seconds(DEFAULT_LIFECYCLE_TIMEOUT),
        "lifecyclePollIntervalSeconds": seconds(LIFECYCLE_POLL_INTERVAL),
        "promptTimeoutSeconds": seconds(DEFAULT_PROMPT_TIMEOUT),
        "clientGraceSeconds": seconds(DEFAULT_CLIENT_GRACE),
        "bedrockTokenTtlHours": bedrock::MAX_LIFETIME.as_secs() / 3600,
        "residencyCycles": DEFAULT_RESIDENCY_CYCLES,
        "keepAwakeToleratedErrors": crate::session::keepalive::DEFAULT_TOLERATED_ERRORS,
        "syncDeleteTimeoutSeconds": seconds(crate::workspace::DEFAULT_SYNC_DELETE_TIMEOUT),
        "launch": {
            "maxIdleSeconds": launch.max_idle_sec,
            "suspendedSeconds": launch.suspended_sec,
            "maxDurationSeconds": launch.max_duration_sec,
            "autoResume": launch.auto_resume,
            "egress": launch.egress,
            "denyEgress": launch.deny_egress,
            "identity": launch.identity,
            "shell": launch.shell,
            "wait": launch.wait,
            // `None` keeps the service's default destination; logging is off only when a
            // caller turns it off.
            "loggingDisabled": matches!(
                launch.logging,
                Some(crate::control::ops::Logging::Disabled { .. })
            ),
        },
        "exec": {
            "shell": matches!(start.shell, protocol::exec::Shell::Flag(true)),
            "stdin": start.stdin,
            "reapGroupOnExit": start.reap_group_on_exit,
            "inheritImageEnv": start.inherit_image_env,
        },
        "stream": {
            "reconnect": stream.reconnect,
            "errorOnGap": stream.error_on_gap,
        },
        "teardown": {
            "deleteImage": teardown.delete_image,
            "deleteLogGroup": teardown.delete_log_group,
            "waitForTerminated": teardown.wait_for_terminated.is_some(),
        },
        "prompt": {
            "reapGroupOnExit": prompt.reap_group_on_exit,
        },
        "image": {
            "inheritWorkdir": WrapOptions::default().inherit_workdir,
            "repairGuestIdentity": image.repair_guest_identity,
            "force": ensured.force,
        },
    })
}

/// A duration as the float seconds every surface takes one in.
fn seconds(duration: Duration) -> f64 {
    duration.as_secs_f64()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The object reads core's values, not copies: each figure a surface restates is the one
    /// the rule that uses it holds (#300).
    ///
    /// **Falsification**: `verify/guards/faults/parity-defaults.toml` entry
    /// `core-client-defaults-copy` types the exec wait here as a literal 301, and this goes
    /// red.
    #[test]
    fn the_client_defaults_are_cores_own_values() {
        let defaults = client_defaults();
        assert_eq!(
            defaults["execWaitSeconds"],
            DEFAULT_EXEC_WAIT.as_secs_f64(),
            "execWaitSeconds"
        );
        assert_eq!(defaults["agentPort"], DEFAULT_AGENT_PORT);
        assert_eq!(defaults["sizeMib"], SizeClass::DEFAULT.baseline_mib());
        let launch = RunRequest::default();
        assert_eq!(defaults["launch"]["maxIdleSeconds"], launch.max_idle_sec);
        assert_eq!(defaults["launch"]["wait"], launch.wait);
        let start = protocol::exec::StartRequest::new("x", vec!["true".into()]);
        assert_eq!(defaults["exec"]["stdin"], start.stdin);
        assert_eq!(defaults["exec"]["shell"], false);
        assert_eq!(
            defaults["syncDeleteTimeoutSeconds"],
            crate::workspace::DEFAULT_SYNC_DELETE_TIMEOUT.as_secs_f64()
        );
    }
}
