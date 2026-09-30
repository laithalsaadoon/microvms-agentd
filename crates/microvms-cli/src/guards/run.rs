// SPDX-License-Identifier: Apache-2.0
//! `run` against the scripted transport: image name resolution, the launch env, the flags that
//! reach the `RunMicrovm` body, and the posture the envelope reports.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::Region;
use microvms_core::testing::YieldingClock;

use super::support::{
    DaemonScript, ScriptedSeam, ScriptedTransport, SyncSeam, TempDir, dispatch_with,
    dispatch_with_env, full_infra, interrupt_run_args, list_images_body, microvm_body,
    run_args_for_image, sync_launch_script,
};
use crate::cli::Command;
use crate::exit::Exit;
use crate::seam::CoreSeam;

/// **`run --image <bare-name>` resolves the name to its ARN before the launch.**
///
/// The measured defect this closes: the identifier used to pass verbatim into
/// `RunMicrovm.imageIdentifier`, and a bare name was answered with HTTP 400 "Malformed
/// ARN" — a message that says nothing about names. The assertions are on the wire: the
/// listing was asked with the model's `nameFilter`, and the launch body's
/// `imageIdentifier` is the resolved ARN rather than the name.
///
/// `RunMicrovm` is scripted to fail with a 400 so the test ends at the launch rather than
/// entering the RUNNING wait — resolution has already happened by then, which is what is
/// under test.
///
/// **Guard proof.** Revert the resolution (pass `identifier.clone()` through as before)
/// and the `imageIdentifier` assertion reads the bare name. Run 2026-08-14 against the
/// pre-change handler shape; failed exactly there.
#[tokio::test]
async fn a_bare_image_name_is_resolved_to_its_arn_before_the_launch() {
    let dir = TempDir::new("resolve-name");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "ListMicrovmImages",
            200,
            &list_images_body(&["coding-agents"], None),
        )
        .answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Run(Box::new(run_args_for_image("coding-agents", dir.0.clone())));
    let (result, stderr) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run after resolution");

    assert_eq!(transport.called("ListMicrovmImages"), 1);
    let listing = transport.paths_of("ListMicrovmImages");
    assert!(
        listing[0].contains("nameFilter=coding-agents"),
        "the listing narrows by the model's nameFilter member: {}",
        listing[0]
    );

    let body = transport.first_body("RunMicrovm");
    assert_eq!(
        body["imageIdentifier"],
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:coding-agents",
        "the launch must carry the resolved ARN, never the bare name — a name here is the \
         Malformed-ARN 400 this exists to close: {body}"
    );

    // The progress line names the resolved ARN, so an operator reading a stalled launch
    // knows which image the name landed on.
    assert!(
        stderr.contains("resolved image name coding-agents to arn:aws:lambda"),
        "{stderr}"
    );
}

/// **An identifier already shaped like an ARN passes through with zero listing calls.**
///
/// The caller who holds the ARN — every existing script — pays nothing for the
/// resolution existing. Asserted on the call count, which is the observable that
/// distinguishes "resolved to itself" from "never looked".
#[tokio::test]
async fn an_arn_image_identifier_launches_with_no_listing_call() {
    let dir = TempDir::new("resolve-arn-passthrough");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let arn = "arn:aws:lambda:us-east-1:123456789012:microvm-image:img";
    let command = Command::Run(Box::new(run_args_for_image(arn, dir.0.clone())));
    let (result, stderr) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run");

    assert_eq!(
        transport.called("ListMicrovmImages"),
        0,
        "an ARN must cost zero extra calls: {:?}",
        transport.calls()
    );
    assert_eq!(transport.first_body("RunMicrovm")["imageIdentifier"], arn);
    assert!(
        !stderr.contains("resolved image name"),
        "nothing was resolved, so nothing says so: {stderr}"
    );
}

/// **A launch from an existing image reports that image's name, not the invocation's
/// default.**
///
/// Measured 2026-09-12 during the platform re-measurement for issues #154/#155: `run --image
/// <arn>` envelopes said `imageName: microvm-cli-<epoch>`, the per-invocation name a build
/// would have used, for an image named something else entirely. The name is the ARN's last
/// colon segment (docs/PLATFORM.md, "The image ARN separator is a colon"), so the envelope
/// can say the true one with zero calls. It matters beyond cosmetics: the build log group is
/// `/aws/lambda-microvms/<image-name>`, and a wrong name here is a wrong group everywhere
/// downstream reads it.
///
/// Asserted on the failure envelope of a scripted `RunMicrovm` refusal, which carries the
/// partial outcome — the same shape the interrupt guards read.
#[tokio::test]
async fn a_launch_from_an_existing_image_reports_that_images_name() {
    let dir = TempDir::new("existing-image-name");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let arn = "arn:aws:lambda:us-east-1:123456789012:microvm-image:existing-one";
    // `run_args_for_image` sets `--name img`, the name a *build* would use; the launch must
    // not report it for an image it did not build.
    let command = Command::Run(Box::new(run_args_for_image(arn, dir.0.clone())));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let failure = result.expect_err("the scripted RunMicrovm failure ends the run");
    let envelope = crate::envelope::error(&failure);
    assert_eq!(
        envelope["data"]["imageName"], "existing-one",
        "the envelope names the image that was launched: {envelope}"
    );
    assert_eq!(envelope["data"]["imageIdentifier"], arn);
}

/// **A refused launch by bare name reports the ARN core resolved, and the image's own name.**
///
/// Core resolves the name inside `Sandbox::run` (#253), so the CLI reads the ARN back off the
/// sandbox after the launch rather than from a resolution of its own. The refusal is the case
/// that needs the sandbox's record: no VM was accepted, so `microvm()` has nothing to say.
///
/// **Falsification**: drop the `launch_image_arn` read after the select in `run`, and the
/// envelope has no `imageIdentifier` and names the invocation's default image.
#[tokio::test]
async fn a_refused_launch_by_name_reports_the_resolved_image() {
    let dir = TempDir::new("resolve-refused");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "ListMicrovmImages",
            200,
            &list_images_body(&["coding-agents"], None),
        )
        .answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Run(Box::new(run_args_for_image("coding-agents", dir.0.clone())));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let failure = result.expect_err("the scripted RunMicrovm failure ends the run");
    let envelope = crate::envelope::error(&failure);
    assert_eq!(
        envelope["data"]["imageIdentifier"],
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:coding-agents",
        "the envelope names the ARN the launch asked for: {envelope}"
    );
    assert_eq!(envelope["data"]["imageName"], "coding-agents", "{envelope}");
    assert_eq!(
        transport.called("ListMicrovmImages"),
        1,
        "resolved once, in core"
    );
}

/// **`run --launch-env` reaches the `runHookPayload` the daemon parses.**
///
/// Asserted on the wire body rather than on `RunArgs`, because the flag existing and the
/// value arriving are two different facts — and the second one is what a workload depends
/// on. `RunMicrovm` is scripted to fail so the test ends at the launch, which is after the
/// payload is built.
///
/// **Guard proof.** Drop the `with_launch_env` loop from `commands/lifecycle.rs` and the
/// `env` assertions read `null`.
#[tokio::test]
async fn a_launch_env_flag_reaches_the_run_hook_payload() {
    let dir = TempDir::new("launch-env");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        dir.0.clone(),
    );
    args.launch_env = vec![
        (
            "ANTHROPIC_BASE_URL".to_string(),
            "https://gateway.example".to_string(),
        ),
        ("EMPTY".to_string(), String::new()),
    ];
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run after the payload is built");

    let body = transport.first_body("RunMicrovm");
    let payload = body["runHookPayload"]
        .as_str()
        .expect("runHookPayload is a string");
    // One parse deeper, which is where the daemon reads it from as well.
    let inner: serde_json::Value =
        serde_json::from_str(payload).expect("the payload is itself JSON");
    assert_eq!(
        inner["env"]["ANTHROPIC_BASE_URL"],
        "https://gateway.example"
    );
    assert_eq!(
        inner["env"]["EMPTY"], "",
        "an empty VALUE is a variable set to the empty string, not an omitted one"
    );
    assert!(
        inner["agent_token"].as_str().is_some_and(|t| !t.is_empty()),
        "the token still rides alongside the env: {payload}"
    );
}

/// **A run with no `--launch-env` emits no `env` key at all.**
///
/// The compatibility floor, and it is worth a guard because the cheap implementation —
/// always serialize the map — would put `"env":{}` on the wire for every existing caller
/// and spend their payload budget on nothing.
#[tokio::test]
async fn a_run_without_a_launch_env_emits_no_env_key() {
    let dir = TempDir::new("launch-env-absent");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Run(Box::new(run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        dir.0.clone(),
    )));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run");

    let payload = transport.first_body("RunMicrovm")["runHookPayload"]
        .as_str()
        .expect("a string")
        .to_string();
    assert!(
        !payload.contains("env"),
        "an unset launch env must not appear on the wire: {payload}"
    );
}

/// **A name no image carries is a local `ERR_PRECONDITION` naming the name and the
/// remedy — and no launch goes out.**
///
/// The alternative was the service's 400 "Malformed ARN", which sends the reader to
/// check their ARN syntax rather than to build the image.
#[tokio::test]
async fn an_unknown_image_name_fails_precondition_before_any_launch() {
    let dir = TempDir::new("resolve-miss");
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("ListMicrovmImages", 200, &list_images_body(&[], None));

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Run(Box::new(run_args_for_image("no-such-image", dir.0.clone())));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;

    let failure = result.expect_err("nothing to launch from");
    assert_eq!(failure.exit, Exit::Precondition);
    assert_eq!(failure.code(), "ERR_PRECONDITION");
    assert!(
        failure.message.contains("no-such-image"),
        "{}",
        failure.message
    );
    assert!(
        failure.message.contains("microvm build"),
        "the remedy is a build, and the message has to say so: {}",
        failure.message
    );
    assert_eq!(
        transport.called("RunMicrovm"),
        0,
        "no launch may go out for a name that resolved to nothing"
    );
}

/// **Resolution follows `nextToken`**, at this level too: an image on page two of the
/// account's listing is found and launched from.
///
/// Core has the same test against its own fake; this one exists because the CLI is the
/// consumer the packet names, and a delegation that dropped the token would pass core's
/// test while every CLI resolution stopped at page one.
#[tokio::test]
async fn resolution_reads_past_the_first_page_of_the_listing() {
    let dir = TempDir::new("resolve-paged");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "ListMicrovmImages",
            200,
            &list_images_body(&["unrelated"], Some("page-2")),
        )
        .answer(
            "ListMicrovmImages",
            200,
            &list_images_body(&["coding-agents"], None),
        )
        .answer("RunMicrovm", 400, r#"{"message": "scripted stop"}"#);

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Run(Box::new(run_args_for_image("coding-agents", dir.0.clone())));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run after resolution");

    assert_eq!(transport.called("ListMicrovmImages"), 2, "both pages read");
    let listing = transport.paths_of("ListMicrovmImages");
    assert!(
        listing[1].contains("nextToken=page-2"),
        "the second request carries the first page's token: {}",
        listing[1]
    );
    assert_eq!(
        transport.first_body("RunMicrovm")["imageIdentifier"],
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:coding-agents"
    );
}

/// **`run --image-version` reaches the `RunMicrovm` body**, and its absence emits nothing.
///
/// The absence half matters for compatibility: an unpinned `run` has to emit byte-for-byte the
/// request this CLI always sent, so a `"imageVersion": null` on every launch would be a new
/// member on a request that has worked for months.
///
/// **Guard proof.** Run 2026-08-16. Drop `request.image_version = args.image_version.clone()`
/// from `launch_and_exec` and the pinned assertion goes red with the member absent; nothing
/// else in the suite notices, which is the gap this test closes.
#[tokio::test]
async fn a_pinned_image_version_reaches_the_run_body_from_the_run_flag() {
    let dir = TempDir::new("pinned-launch");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("TERMINATED"))
        .answer("TerminateMicrovm", 200, "{}")
        .answer(
            "CreateMicrovmAuthToken",
            200,
            r#"{"authToken": {"X-aws-proxy-auth": "opaque"}}"#,
        )
        .answer(
            "ListMicrovmImageVersions",
            200,
            r#"{"items": [{
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://bucket/img.zip"},
                 "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "imageVersion": "1", "state": "SUCCESSFUL", "status": "ACTIVE",
                 "createdAt": 1754524800}]}"#,
        )
        .answer(
            "DeleteMicrovmImage",
            200,
            r#"{"imageIdentifier": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "state": "DELETING"}"#,
        );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        dir.0.clone(),
    );
    args.image_version = Some("2.0".into());
    let command = Command::Run(Box::new(args));
    // The launch **fails**, and that is deliberate rather than incidental. `GetMicrovm` answers
    // TERMINATED, so `wait_for_running` fails fast on TRAP-8 — which happens *after* `RunMicrovm`
    // emitted the body this test reads and *before* a session is built. Answering RUNNING instead
    // would send the CLI on to `wait_until_ready` against a daemon that does not exist, and that
    // retries: measured 2026-08-16, the same test took **240 seconds**. The body is emitted either
    // way, so the fast path is the honest one.
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    assert!(
        result.is_err(),
        "a VM that reports TERMINATED never reaches RUNNING, which is what makes this fast"
    );

    let body = transport.first_body("RunMicrovm");
    assert_eq!(
        body["imageVersion"], "2.0",
        "a canary has to launch against the version it means to test: {body}"
    );
    assert_eq!(
        body["imageIdentifier"], "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        "pinning a version does not replace the identifier"
    );

    // And an unpinned run emits nothing for the member.
    let dir = TempDir::new("unpinned-launch");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("TERMINATED"))
        .answer("TerminateMicrovm", 200, "{}")
        .answer(
            "CreateMicrovmAuthToken",
            200,
            r#"{"authToken": {"X-aws-proxy-auth": "opaque"}}"#,
        );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Run(Box::new(run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
        dir.0.clone(),
    )));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    assert!(result.is_err(), "TERMINATED before RUNNING, as above");
    assert!(
        transport
            .first_body("RunMicrovm")
            .get("imageVersion")
            .is_none(),
        "an unpinned run must send what this CLI always sent: {}",
        transport.first_body("RunMicrovm")
    );
}

/// A launch that fails fast after `RunMicrovm`: the body is on the wire, no session is built.
fn fast_failing_launch() -> Arc<ScriptedTransport> {
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("TERMINATED"))
        .answer("TerminateMicrovm", 200, "{}");
    transport
}

/// **`run --client-token` (#203).** The key and the `$MICROVM_AGENT_TOKEN` agent token reach
/// `RunMicrovm` verbatim, so a retry is the identical launch; without the variable, or
/// without `--image`, the launch is refused before any call.
///
/// **Guard proof.** Drop the `request.client_token = Some(client_token)` line and the first
/// assertion goes red with a minted `run-…` token on the wire.
#[tokio::test]
async fn a_client_token_run_sends_the_key_and_the_environments_agent_token() {
    let dir = TempDir::new("client-token");
    let transport = fast_failing_launch();
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = interrupt_run_args(dir.0.clone());
    args.launch.client_token = Some("job-203".into());
    let command = Command::Run(Box::new(args.clone()));
    let (result, _) = dispatch_with_env(
        &seam,
        &command,
        full_infra(),
        (crate::cli::AGENT_TOKEN_ENV, "agent-token-from-env"),
    )
    .await;
    assert!(result.is_err(), "the fake VM terminates during startup");
    let body = transport.first_body("RunMicrovm");
    assert_eq!(body["clientToken"], "job-203", "{body}");
    assert!(
        body["runHookPayload"]
            .as_str()
            .is_some_and(|payload| payload.contains("agent-token-from-env")),
        "the retry needs the same agent token: {body}"
    );

    // No token in the environment: refused before any call.
    let transport = fast_failing_launch();
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let error = result.expect_err("no agent token");
    assert!(
        error.message.contains("MICROVM_AGENT_TOKEN"),
        "{}",
        error.message
    );
    assert_eq!(transport.called("RunMicrovm"), 0);

    // A build would not be an identical retry: refused before any call.
    let mut build = args;
    build.image = None;
    build.binary = Some(dir.0.join("agentd"));
    std::fs::write(dir.0.join("agentd"), b"binary").expect("fixture");
    let transport = fast_failing_launch();
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with_env(
        &seam,
        &Command::Run(Box::new(build)),
        full_infra(),
        (crate::cli::AGENT_TOKEN_ENV, "agent-token-from-env"),
    )
    .await;
    assert!(result.is_err(), "a build with a client token is refused");
    assert_eq!(transport.called("RunMicrovm"), 0);
}

/// **Per-VM logging (#201).** `--vm-log-group` and `--no-vm-logs` reach the `RunMicrovm`
/// body; neither flag leaves the member absent, which is the byte-for-byte old request.
#[tokio::test]
async fn per_vm_logging_flags_reach_the_run_body() {
    for (group, disabled, expected) in [
        (
            Some("/team/agents"),
            false,
            serde_json::json!({"cloudWatch": {"logGroup": "/team/agents"}}),
        ),
        (None, true, serde_json::json!({"disabled": {}})),
        (None, false, serde_json::Value::Null),
    ] {
        let dir = TempDir::new("vm-logging");
        let transport = fast_failing_launch();
        let seam = ScriptedSeam {
            transport: Arc::clone(&transport),
            clock: Arc::new(YieldingClock::default()),
        };
        let mut args = interrupt_run_args(dir.0.clone());
        args.launch.vm_log_group = group.map(str::to_string);
        args.launch.no_vm_logs = disabled;
        let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
        assert!(result.is_err(), "the fake VM terminates during startup");
        let body = transport.first_body("RunMicrovm");
        assert_eq!(
            body.get("logging").cloned().unwrap_or_default(),
            expected,
            "{body}"
        );
    }
}

/// The logging flags' own exclusions are parser properties.
#[test]
fn the_vm_logging_flags_refuse_contradictions_at_parse_time() {
    use clap::Parser as _;
    let base = ["microvm", "run", "--image", "arn:image"];
    for extra in [
        vec!["--vm-log-stream", "s"],
        vec!["--vm-log-group", "/g", "--no-vm-logs"],
    ] {
        let argv: Vec<&str> = base.iter().copied().chain(extra.iter().copied()).collect();
        assert!(crate::cli::Cli::try_parse_from(&argv).is_err(), "{argv:?}");
    }
    let argv = [
        "microvm",
        "agent-up",
        "--vm-name",
        "a",
        "--client-token",
        "k",
        "--vm-log-group",
        "/g",
    ];
    crate::cli::Cli::try_parse_from(argv).expect("agent-up takes the launch flags");
}

/// **BIND-12: the run envelope and the session a binding holds report the same posture.**
///
/// The CLI and the bindings are two consumers of one core derivation: the envelope's
/// `egressPosture` comes from `egress_posture_for` in `lifecycle::run`, and a binding's
/// `Session.egress_posture` / `session.egressPosture()` is the core session's own value. So the
/// parity is asserted where both meet: the same launch options, once through the CLI's shipped
/// dispatcher and once through `Sandbox::run` on the same scripted seam, for each of the four
/// launchable shapes.
///
/// **Falsification** — 2026-09-24. Derive the envelope's posture from `args.egress` alone in
/// `lifecycle::run` and the `--deny-egress` row reads `unsealed` against the session's
/// `best-effort`; restored.
#[tokio::test]
async fn the_run_envelope_and_the_launched_session_report_the_same_posture() {
    const HEALTH: &str = r#"{"version": "0.1.0", "bootstrapped": true, "disk": null,
                             "identity_degraded": false, "identity_repaired": true}"#;
    let image = "arn:aws:lambda:us-east-1:123456789012:microvm-image/img";
    let arn = "arn:aws:lambda:us-east-1:123456789012:network-connector:isolated-vpc";
    let rows: [(bool, Vec<String>, bool, &str); 4] = [
        (false, Vec::new(), false, "unsealed"),
        (true, Vec::new(), false, "open"),
        (false, Vec::new(), true, "best-effort"),
        (false, vec![arn.to_string()], false, "unsealed"),
    ];
    for (egress, connectors, deny, expected) in rows {
        let dir = TempDir::new("posture-parity");
        let daemon = DaemonScript::new();
        for _ in 0..4 {
            daemon.reply(200, HEALTH);
        }
        let seam = SyncSeam {
            transport: sync_launch_script(),
            clock: Arc::new(YieldingClock::default()),
            daemon,
        };
        let mut args = run_args_for_image(image, dir.0.clone());
        args.egress = egress;
        args.egress_network_connectors = connectors.clone();
        args.deny_egress = deny;
        let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
        let rendered = result.expect("the scripted run succeeds");
        let envelope = rendered.data["egressPosture"].clone();

        let mut sandbox = seam
            .open_sandbox(Region::UsEast1, None)
            .await
            .expect("a scripted sandbox");
        let mut request = microvms_core::sandbox::RunRequest::new().with_image(image);
        request.egress = egress;
        request.egress_network_connectors = connectors.clone();
        request.deny_egress = deny;
        let session = sandbox.run(request).await.expect("the same launch");
        let row = format!(
            "egress={egress} connectors={} deny={deny}",
            connectors.len()
        );
        assert_eq!(envelope, expected, "{row}: the envelope");
        assert_eq!(
            session.egress_posture().as_str(),
            expected,
            "{row}: the session"
        );
        assert_eq!(
            microvms_core::control::egress_posture_for(
                egress,
                &connectors,
                deny,
                Some(&Region::UsEast1)
            )
            .expect("launchable")
            .as_str(),
            expected,
            "{row}: the request-side answer"
        );
        sandbox.detach().expect("hand the scripted VM off quietly");
    }
}
