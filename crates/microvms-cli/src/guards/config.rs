// SPDX-License-Identifier: Apache-2.0
//! microvm.toml: the config merge on the wire (issue #73).

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    ConfigFile, RefusingSeam, ScriptedSeam, ScriptedTransport, TempDir, dispatch_with, full_infra,
    no_config, run_args_for_image,
};
use crate::cli::Command;
use crate::exit::Exit;
use crate::seam::Door;

/// **Config-file knobs reach the wire, and a typed flag beats the file on the same
/// field.**
///
/// Asserted on the `RunMicrovm` body rather than on the merge's own report, because the
/// file existing and its values arriving are two different facts — the launch request is
/// what the VM's policy windows are actually set from. Both halves in one scripted run:
/// `suspendedDurationSeconds` comes from the file (no flag typed), and `--max-idle-sec`
/// beats the file's value on `maxIdleTimeoutSeconds` because `explicit` says the caller
/// typed it. The env merge is per key: the file's `RUST_LOG` survives beside the flag's
/// winning `CI`.
///
/// **Falsification** — invert the `explicit` branch in `config::pick` (make a typed flag
/// lose to the file) and the `maxIdleTimeoutSeconds` assertion reads 120; drop the
/// config layer from `merge_config` and the `suspendedDurationSeconds` assertion reads
/// the built-in 600. Both were done on 2026-08-28 and both failed as stated, then were
/// restored.
#[tokio::test]
async fn config_knobs_reach_the_wire_and_a_typed_flag_beats_the_file() {
    let dir = TempDir::new("config-wire");
    let file = ConfigFile::new(
        "wire",
        r#"
memory = 4096
max-idle-sec = 120
suspended-sec = 300
egress = true
auto-resume = true

[env]
RUST_LOG = "debug"
CI = "0"
"#,
    );
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
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };
    // The caller typed `--max-idle-sec 90` and nothing else: `explicit` is the parse's
    // answer, set here the way `main.rs` sets it from `value_source`. 90 rather than
    // something smaller because core refuses idle windows under the model's minimum of
    // 60 before the wire.
    args.max_idle_sec = 90;
    args.explicit.max_idle_sec = true;
    args.launch_env = vec![("CI".to_string(), "1".to_string())];

    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run after the request is built");

    let body = transport.first_body("RunMicrovm");
    // `memory` is a build-time knob (it sizes the image, not the launch), so a
    // `run --image` body carries no memory field — its merge is pinned through
    // `merge_config`'s report in the resolved-config guard below instead.
    assert_eq!(
        body["idlePolicy"]["suspendedDurationSeconds"], 300,
        "the file's suspended window reaches the launch: {body}"
    );
    assert_eq!(
        body["idlePolicy"]["maxIdleDurationSeconds"], 90,
        "the typed flag beats the file on the same field: {body}"
    );
    assert!(
        body["egressNetworkConnectors"]
            .as_array()
            .is_some_and(|connectors| !connectors.is_empty()),
        "egress = true in the file opts into the connector: {body}"
    );
    assert_eq!(
        body["idlePolicy"]["autoResumeEnabled"], true,
        "auto-resume = true in the file reaches the launch policy: {body}"
    );
    let payload: serde_json::Value =
        serde_json::from_str(body["runHookPayload"].as_str().expect("a payload string"))
            .expect("the payload is itself JSON");
    assert_eq!(
        payload["env"]["RUST_LOG"], "debug",
        "the file's env key survives the per-key merge: {payload}"
    );
    assert_eq!(
        payload["env"]["CI"], "1",
        "the flag pair wins its own key: {payload}"
    );
}

/// **A broken config file is `ERR_CONFIG` with zero doors entered.**
///
/// The refusal is local and its cost is the acceptance criterion: a file typo must not
/// spend a credential resolution, let alone a launch. Asserted on the seam's door list,
/// the same observable the named-VM collision guard pins.
///
/// **Falsification** — move the `merge_config` call below `open_sandbox` in
/// `commands/lifecycle.rs` and the door list reads `[OpenSandbox]`. Done on 2026-08-28;
/// failed as stated; restored.
#[tokio::test]
async fn a_broken_config_file_is_refused_with_its_own_row_and_zero_doors() {
    let dir = TempDir::new("config-broken");
    let file = ConfigFile::new("broken", "memroy = 4096\n");
    let seam = RefusingSeam::new();
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        dir.0.clone(),
    );
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };

    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    let failure = result.expect_err("a broken file refuses the run");
    assert_eq!(failure.exit, Exit::Config, "{failure:?}");
    assert_eq!(failure.code(), "ERR_CONFIG");
    assert_eq!(failure.exit.as_u8(), 15);
    assert!(
        failure.message.contains("memroy"),
        "the refusal names the unknown key: {}",
        failure.message
    );
    assert!(
        failure
            .suggestions
            .iter()
            .any(|hint| hint.contains("--no-config")),
        "{failure:?}"
    );
    assert_eq!(
        seam.doors(),
        Vec::<Door>::new(),
        "a config refusal must cost zero billable calls"
    );
}

/// **`--deny-egress` reaches the guest's environment and asks the platform for nothing.**
///
/// The two halves of the advisory deny, both read off the `RunMicrovm` body rather than off
/// a struct: the proxy variables are in the `runHookPayload` env in both spellings, and
/// `egressNetworkConnectors` is absent — this advisory mechanism runs entirely in the guest.
/// Enforced no-egress requires a custom VPC connector and isolated VPC routing. The posture the
/// envelope will carry is asserted beside them, so the label and the request are pinned by
/// one test.
///
/// **Falsification** — 2026-09-13. Drop the `with_deny_egress()` call from the launch arm in
/// `lifecycle::launch` and the `http_proxy` assertion goes red while the connector assertion
/// still passes; make the posture `sealed` for a connector-less launch and the label
/// assertion goes red. Both were run and restored.
#[tokio::test]
async fn deny_egress_reaches_the_launch_env_and_asks_the_platform_for_nothing() {
    let dir = TempDir::new("deny-egress");
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
    args.deny_egress = true;

    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    result.expect_err("the scripted RunMicrovm failure ends the run after the request is built");

    let body = transport.first_body("RunMicrovm");
    assert!(
        body.get("egressNetworkConnectors").is_none(),
        "the advisory deny must not put a connector on the request: {body}"
    );
    let payload: serde_json::Value =
        serde_json::from_str(body["runHookPayload"].as_str().expect("a payload string"))
            .expect("the payload is itself JSON");
    for key in microvms_core::sandbox::DENY_EGRESS_ENV_KEYS {
        assert_eq!(
            payload["env"][key],
            microvms_core::sandbox::DENY_EGRESS_PROXY_URL,
            "{key} must reach the guest: {payload}"
        );
    }
    assert_eq!(
        microvms_core::control::EgressPosture::for_launch(false, true).as_str(),
        "best-effort",
        "and the run reports the advisory deny as best-effort, never as a seal"
    );
}

#[tokio::test]
async fn vpc_connector_reaches_the_launch_request() {
    let dir = TempDir::new("vpc-connector");
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
    let connector = "arn:aws:lambda:us-east-1:123456789012:network-connector:private";
    args.egress_network_connectors = vec![connector.to_string()];
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    result.expect_err("scripted stop after serializing the request");
    assert_eq!(
        transport.first_body("RunMicrovm")["egressNetworkConnectors"],
        serde_json::json!([connector]),
    );
}

#[test]
fn configured_managed_egress_cannot_bypass_an_explicit_vpc_connector() {
    let file = ConfigFile::new("vpc-egress-conflict", "egress = true\n");
    let mut args = run_args_for_image("arn:image", std::env::temp_dir());
    args.egress_network_connectors =
        vec!["arn:aws:lambda:us-east-1:123456789012:network-connector:private".into()];
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };
    let Err(error) = crate::commands::lifecycle::merge_config(&args, &|_| None) else {
        panic!("managed internet egress must not override the VPC intent");
    };
    assert_eq!(error.exit, Exit::InvalidArg);
    assert!(error.message.contains("INTERNET_EGRESS"));
}

/// **A default run reports `unsealed`, and `--egress` with `--deny-egress` is refused across
/// the flag/file boundary.**
///
/// The refusal is here rather than only in clap because clap sees the command line and the
/// file is the other half: `deny-egress = true` in `microvm.toml` under a typed `--egress`
/// arrives at `merge_config` with both merged true, and clap's `conflicts_with` never fires.
/// Zero doors, because a locally-refused launch must cost nothing.
///
/// **Falsification** — 2026-09-13. Delete the pair check in `merge_config` and the refusal
/// assertion goes red (the run proceeds and reports `open` while the guest's clients fail
/// closed). Restored after.
#[tokio::test]
async fn a_default_run_is_unsealed_and_the_egress_pair_is_refused_across_the_file_boundary() {
    let posture = microvms_core::control::EgressPosture::for_launch(false, false);
    assert_eq!(
        posture.as_str(),
        "unsealed",
        "the default launch asks for no connector, and that is measured not to seal the VM"
    );

    let file = ConfigFile::new("deny-egress-pair", "deny-egress = true\n");
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        std::env::temp_dir(),
    );
    args.egress = true;
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };

    let Err(error) = crate::commands::lifecycle::merge_config(&args, &|_| None) else {
        panic!("opposite intents are refused before any call");
    };
    assert_eq!(error.exit, Exit::InvalidArg, "{}", error.message);
    assert!(
        error.message.contains("opposite things"),
        "{}",
        error.message
    );
    assert!(
        error.message.contains("Neither seals the VM"),
        "the refusal must not imply that either one would: {}",
        error.message
    );
}

/// **The envelope reports what each knob resolved to and which source won.**
///
/// `resolvedConfig` is the file's whole point made legible: a caller who stopped passing
/// flags reads what the run actually used instead of re-deriving the precedence. Scripted
/// to fail at the launch — the *failure* envelope does not carry it, so this asserts on
/// the merge output through a successful parse instead: the merged args and report are
/// checked directly, which is the same seam `run` reads.
///
/// **Falsification** — make `config::pick`'s config arm report `Source::Default` and the
/// `memory` source assertion reads `"default"`. Done on 2026-08-28; failed as stated;
/// restored.
#[tokio::test]
async fn the_resolved_config_report_names_each_knobs_source() {
    let file = ConfigFile::new(
        "resolved",
        "memory = 8192\nexec = \"pytest -q\"\nartifacts = [\"dist/**\"]\n",
    );
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        std::env::temp_dir(),
    );
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };
    args.explicit.max_idle_sec = true;
    args.max_idle_sec = 90;

    // A pinned environment: the region report's env layer must be deterministic here.
    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("merges");
    assert_eq!(merged.config_path.as_deref(), Some(file.0.as_path()));
    assert_eq!(merged.artifacts, ["dist/**"]);

    let knob = |name: &str| merged.resolved[name].clone();
    assert_eq!(knob("memory")["value"], 8192);
    assert_eq!(knob("memory")["source"], "config");
    assert_eq!(knob("exec")["value"], "pytest -q");
    assert_eq!(knob("exec")["source"], "config");
    assert_eq!(knob("maxIdleSec")["value"], 90);
    assert_eq!(knob("maxIdleSec")["source"], "flag");
    assert_eq!(knob("suspendedSec")["value"], 600);
    assert_eq!(knob("suspendedSec")["source"], "default");
    assert_eq!(knob("artifacts")["source"], "config");
    // The image was a flag (run_args_for_image sets it), so the report says so.
    assert_eq!(knob("image")["source"], "flag");
}

/// **The logging pair merges flag-over-file per knob, and a merged stream with no merged
/// group is refused** — the combination neither layer can see alone.
///
/// The stream comes from the flag and the group from the file in the first case, which is
/// the cross-layer pair the per-knob `pick` has to compose; the second case drops the
/// file and the same flag stream becomes a refusal, because a stream inside a group the
/// service names randomly is a location that does not exist.
///
/// **Falsification** — move the stream-needs-a-group check before the merge (test the
/// flags alone) and the first case fails: the flag stream plus the file group is legal
/// and would be refused.
#[tokio::test]
async fn the_log_knobs_merge_flag_over_file_and_a_cross_layer_stream_needs_its_group() {
    let file = ConfigFile::new(
        "log-knobs",
        "log-group = \"/aws/lambda-microvms/from-file\"\nlog-stream = \"file-stream\"\n",
    );
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        std::env::temp_dir(),
    );
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };
    args.log_stream = Some("flag-stream".into());

    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("merges");
    let knob = |name: &str| merged.resolved[name].clone();
    assert_eq!(knob("logGroup")["value"], "/aws/lambda-microvms/from-file");
    assert_eq!(knob("logGroup")["source"], "config");
    assert_eq!(knob("logStream")["value"], "flag-stream");
    assert_eq!(
        knob("logStream")["source"],
        "flag",
        "the typed flag beats the file's stream"
    );
    assert_eq!(
        merged.args.log_group.as_deref(),
        Some("/aws/lambda-microvms/from-file")
    );
    assert_eq!(merged.args.log_stream.as_deref(), Some("flag-stream"));

    // The same flag stream with no file is a refusal: no layer supplied a group.
    let mut orphan = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        std::env::temp_dir(),
    );
    orphan.log_stream = Some("flag-stream".into());
    // A `match` rather than `expect_err`, because `MergedRunArgs` carries no `Debug` —
    // deliberately, since `RunArgs` holds the agent-token-adjacent launch env.
    let Err(error) = crate::commands::lifecycle::merge_config(&orphan, &|_| None) else {
        panic!("a stream with no group from either layer must be refused");
    };
    assert_eq!(error.exit, Exit::InvalidArg, "{}", error.message);
    assert!(error.message.contains("log group"), "{}", error.message);
}

/// **A typed `BINARY` positional suppresses the file's `image`, because the pair is one
/// decision: `run` builds exactly when the merged image is absent.**
///
/// The failure this closes: a developer in a project whose file pins `image` types
/// `microvm run ./fresh-agentd` expecting a build-and-launch of that binary; a file that
/// silently won would run their tests against the stale pinned image.
///
/// **Falsification** — drop the `args.binary.is_some() && args.image.is_none()`
/// suppression from `merge_config` and the image assertion reads `"ci-image"` while
/// building stays false. Done on 2026-08-28; failed as stated; restored.
#[tokio::test]
async fn a_typed_binary_positional_beats_the_files_image() {
    let file = ConfigFile::new("binary-beats-image", "image = \"ci-image\"\n");
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        std::env::temp_dir(),
    );
    args.image = None;
    args.binary = Some("./fresh-agentd".into());
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };

    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("merges");
    assert_eq!(
        merged.args.image, None,
        "the typed positional suppresses the file's image: {:?}",
        merged.resolved
    );
    assert_eq!(
        merged.args.binary.as_deref(),
        Some(std::path::Path::new("./fresh-agentd"))
    );
    // With nothing typed for the pair, the file's image wins as usual.
    args.binary = None;
    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("merges");
    assert_eq!(merged.args.image.as_deref(), Some("ci-image"));
    assert_eq!(merged.resolved["image"]["source"], "config");
}

/// **The region report walks the run's whole chain: past the file sit the environment
/// variables, then the built-in — never `null` from `default` while the launch goes
/// where `$AWS_REGION` points.**
///
/// **Falsification** — report the pre-`resolve` flag value instead of continuing the
/// chain and the env case reads `null`/`"default"`. Done on 2026-08-28; failed as
/// stated; restored.
#[tokio::test]
async fn the_region_report_names_the_environments_region_when_the_environment_decides() {
    let mut args = run_args_for_image(
        "arn:aws:lambda:us-east-1:123456789012:microvm-image/img",
        std::env::temp_dir(),
    );
    args.config = no_config();
    // `run_args_for_image` pins a region flag; the chain under test starts below it.
    args.region = crate::cli::RegionFlags::default();

    // No flag, no file: the environment decides, and the report says so.
    let env = |name: &str| (name == "AWS_REGION").then(|| "eu-west-1".to_string());
    let merged = crate::commands::lifecycle::merge_config(&args, &env).expect("merges");
    assert_eq!(merged.resolved["region"]["value"], "eu-west-1");
    assert_eq!(merged.resolved["region"]["source"], "env");

    // No flag, no file, no environment: the built-in, named rather than null.
    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("merges");
    assert_eq!(merged.resolved["region"]["value"], "us-east-1");
    assert_eq!(merged.resolved["region"]["source"], "default");
}

#[test]
fn vpc_connectors_from_config_are_replaced_by_explicit_flags() {
    let file = ConfigFile::new(
        "vpc-config",
        "egress-network-connectors = ['arn:aws:lambda:us-east-1:123456789012:network-connector:configured']\n",
    );
    let mut args = run_args_for_image("arn:image", std::env::temp_dir());
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };
    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("config");
    assert_eq!(
        merged.args.egress_network_connectors,
        ["arn:aws:lambda:us-east-1:123456789012:network-connector:configured"]
    );
    args.egress_network_connectors =
        vec!["arn:aws:lambda:us-east-1:123456789012:network-connector:flag".into()];
    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("flag");
    assert_eq!(
        merged.args.egress_network_connectors,
        args.egress_network_connectors
    );
    args.egress_network_connectors.clear();
    args.egress = true;
    assert!(crate::commands::lifecycle::merge_config(&args, &|_| None).is_err());
}
