// SPDX-License-Identifier: Apache-2.0
//! `egress-posture`: core's `egress_posture_for` over `run`'s merged options (#269).

#![cfg(test)]

use microvms_core::Region;

use super::support::{ConfigFile, RefusingSeam, dispatch_with, full_infra};
use crate::cli::Cli;
use crate::exit::Exit;

const CONNECTOR: &str = "arn:aws:lambda:us-east-1:123456789012:network-connector:isolated-vpc";

/// Parses `egress-posture` with `rest` and dispatches it with every door refused.
async fn posture(
    rest: &[&str],
) -> (
    Result<crate::commands::Rendered, crate::exit::CliError>,
    RefusingSeam,
) {
    use clap::Parser as _;
    let argv = ["microvm", "egress-posture", "--region", "us-east-1"]
        .into_iter()
        .chain(rest.iter().copied());
    let command = Cli::try_parse_from(argv).expect("parses").command;
    let seam = RefusingSeam::new();
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    (result, seam)
}

/// **`egress-posture` answers core's `egress_posture_for` for `run`'s egress flags, with no
/// door entered (#269).** Each row's answer is the core function's for the same options in
/// the same region, so the command can't hold a different opinion from the launch.
///
/// **Falsification**: `verify/guards/faults/egress-posture.toml` entry
/// `cli-egress-posture-drops-deny-egress` (`--deny-egress` never reaches the merge, and its row
/// reads `unsealed`).
#[tokio::test]
async fn egress_posture_answers_cores_posture_for_runs_flags() {
    let rows: [(&[&str], bool, &[&str], bool); 4] = [
        (&["--no-config"], false, &[], false),
        (&["--no-config", "--egress"], true, &[], false),
        (&["--no-config", "--deny-egress"], false, &[], true),
        (
            &["--no-config", "--egress-network-connector", CONNECTOR],
            false,
            &[CONNECTOR],
            false,
        ),
    ];
    for (flags, egress, connectors, deny) in rows {
        let connectors: Vec<String> = connectors.iter().map(|arn| (*arn).to_string()).collect();
        let expected = microvms_core::control::egress_posture_for(
            egress,
            &connectors,
            deny,
            Some(&Region::UsEast1),
        )
        .expect("a launch core accepts");
        let (result, seam) = posture(flags).await;
        let rendered = result.unwrap_or_else(|error| panic!("{flags:?}: {}", error.message));
        assert_eq!(rendered.kind, "microvm.egress-posture");
        assert_eq!(
            rendered.data["posture"],
            expected.as_str(),
            "{flags:?}: core's posture for these options"
        );
        assert_eq!(rendered.data["detail"], expected.describe());
        assert!(seam.doors().is_empty(), "{flags:?}: no door");
    }
}

/// **`egress-posture` reads microvm.toml as `run` does (#269):** the file's `deny-egress`
/// answers `best-effort` with the file named, and a file's `egress = true` under a typed
/// `--deny-egress` is refused with `run`'s ERR_INVALID_ARG, before any door.
///
/// **Falsification**: `verify/guards/faults/egress-posture.toml` entry
/// `cli-egress-posture-skips-the-merge` (the file is read and ignored, and the first case reads
/// `unsealed`).
#[tokio::test]
async fn egress_posture_reads_runs_config_file() {
    let deny = ConfigFile::new("posture-deny", "deny-egress = true\n");
    let deny_path = deny.0.to_string_lossy().to_string();
    let (result, _) = posture(&["--config", &deny_path]).await;
    let rendered = result.expect("a posture");
    assert_eq!(
        rendered.data["posture"], "best-effort",
        "the file's deny-egress, as run merges it"
    );
    assert_eq!(rendered.data["configPath"], deny_path);

    let open = ConfigFile::new("posture-open", "egress = true\n");
    let open_path = open.0.to_string_lossy().to_string();
    let (result, seam) = posture(&["--config", &open_path, "--deny-egress"]).await;
    let failure = result.expect_err("opposite intents across the file and the flag");
    assert_eq!(
        (failure.exit, seam.doors()),
        (Exit::InvalidArg, Vec::new()),
        "{}",
        failure.message
    );
}

/// A connector ARN core refuses is refused here with core's message.
#[tokio::test]
async fn egress_posture_refuses_a_connector_core_refuses() {
    let (result, seam) =
        posture(&["--no-config", "--egress-network-connector", "not-an-arn"]).await;
    let failure = result.expect_err("not a connector ARN");
    assert_eq!(failure.exit, Exit::InvalidArg, "{}", failure.message);
    let core = microvms_core::control::egress_posture_for(
        false,
        &["not-an-arn".to_string()],
        false,
        Some(&Region::UsEast1),
    )
    .expect_err("core refuses it too");
    assert_eq!(failure.message, core.to_string());
    assert!(seam.doors().is_empty());
}
