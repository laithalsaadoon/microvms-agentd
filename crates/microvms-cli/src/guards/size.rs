// SPDX-License-Identifier: Apache-2.0
//! `--cpus` and `--memory-mib`: a resource request core sizes into a class (#269).

#![cfg(test)]

use microvms_core::SizeClass;

use super::support::{
    ConfigFile, RefusingSeam, TempDir, dispatch_with, full_infra, run_args_for_image,
};
use crate::cli::Cli;
use crate::exit::Exit;

const IMAGE_ARN: &str = "arn:aws:lambda:us-east-1:123456789012:microvm-image/img";

/// The class core picks for a request, as a baseline: what every assertion here compares to.
fn cores_baseline(cpus: Option<f64>, memory_mib: Option<u32>) -> u32 {
    SizeClass::from_request(cpus, memory_mib)
        .expect("a request a class covers")
        .baseline_mib()
}

/// **A size request on `run` is core's class, and it beats the file's `memory` (#269).** The
/// request is a typed choice, as `--memory` is, so `resolvedConfig.memory` names the class
/// `SizeClass::from_request` picks with the source `flag`, over `memory = 8192` in the file.
///
/// **Falsification**: `verify/guards/faults/size-request.toml` entry
/// `cli-run-size-request-loses-to-the-file` (the file's 8192 wins).
#[tokio::test]
async fn a_size_request_on_run_is_cores_class_and_beats_the_files_memory() {
    let file = ConfigFile::new("size-request", "memory = 8192\n");
    let mut args = run_args_for_image(IMAGE_ARN, std::env::temp_dir());
    args.config = crate::cli::ConfigFlags {
        config: Some(file.0.clone()),
        no_config: false,
    };
    args.size.memory_mib = Some(1500);

    let merged = crate::commands::lifecycle::merge_config(&args, &|_| None).expect("merges");
    let memory = &merged.resolved["memory"];
    assert_eq!(
        (&memory["value"], &memory["source"]),
        (&cores_baseline(None, Some(1500)).into(), &"flag".into()),
        "the request's class, over the file's 8192"
    );
    assert_eq!(
        memory["value"], 2048,
        "1500 MiB is covered by the 2048 class"
    );
    assert_eq!(merged.args.memory.size_class().baseline_mib(), 2048);
}

/// **`cost` prices the class core picks for a request (#269):** two vCPUs and 3000 MiB are
/// covered first by the 4096 class, and the report's size says so.
///
/// **Falsification**: `verify/guards/faults/size-request.toml` entry
/// `cli-size-request-ignored` (the request is dropped, and the report prices `--memory`'s
/// default 2048).
#[tokio::test]
async fn cost_prices_the_class_core_picks_for_a_request() {
    use clap::Parser as _;
    let command = Cli::try_parse_from([
        "microvm",
        "cost",
        "--cpus",
        "2",
        "--memory-mib",
        "3000",
        "--running-sec",
        "3600",
    ])
    .expect("parses")
    .command;
    let seam = RefusingSeam::new();
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("a local report");
    let baseline = &rendered.data["report"]["size"]["baselineMib"];
    assert_eq!(
        *baseline,
        cores_baseline(Some(2.0), Some(3000)),
        "the class core picks for the request"
    );
    assert_eq!(*baseline, 4096);
    assert!(seam.doors().is_empty(), "cost is local");
}

/// **A request no class covers is refused with core's message before any door, on every
/// command that takes one (#269).** `build` refuses it before the daemon's provisioning fetch,
/// which the guards' fetch would panic on.
///
/// **Falsification**: `verify/guards/faults/size-request.toml` entry
/// `cli-build-sizes-after-provisioning` (build reaches the fetch first, and panics there).
#[tokio::test]
async fn a_request_no_class_covers_is_refused_before_any_door() {
    use clap::Parser as _;
    let state = TempDir::new("size-refused");
    let state_arg = state.0.to_string_lossy().to_string();
    let rows: [Vec<&str>; 4] = [
        vec!["run", "--no-config", "--image", IMAGE_ARN, "--cpus", "64"],
        vec!["build", "--cpus", "64"],
        vec!["cost", "--cpus", "64"],
        vec!["agent-up", "--vm-name", "big", "--cpus", "64"],
    ];
    for row in rows {
        let mut argv = vec!["microvm"];
        argv.extend(row.iter().copied());
        if row[0] != "cost" {
            argv.extend(["--state-dir", &state_arg]);
        }
        let command = Cli::try_parse_from(&argv).expect("parses").command;
        let seam = RefusingSeam::new();
        let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
        let failure = result.expect_err("no class covers 64 vCPUs");
        assert_eq!(
            (failure.exit, seam.doors()),
            (Exit::InvalidArg, Vec::new()),
            "{}: {}",
            row[0],
            failure.message
        );
        assert!(
            failure
                .message
                .contains("more than the largest size class covers"),
            "{}: {}",
            row[0],
            failure.message
        );
    }
}
