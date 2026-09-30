// SPDX-License-Identifier: Apache-2.0
//! `doctor`: a credential chain that resolves nothing, and the region each check is asked about.

#![cfg(test)]

use std::sync::{Arc, Mutex};

#[expect(
    clippy::disallowed_types,
    reason = "a scripted transport answers the calls core makes; it never sends one"
)]
use microvms_core::control::transport::{Call, Reply, Transport};
use microvms_core::control::{Clock, ControlPlane};
use microvms_core::prelude::*;
use microvms_core::sandbox::Sandbox;
use microvms_core::session::Session;
use microvms_core::testing::YieldingClock;
use microvms_core::{Error, ErrorKind, Region};

use super::support::{
    ScriptedTransport, dispatch_with, dispatch_with_env, full_infra, no_config, region_flags,
};
use crate::cli::{Command, DoctorArgs, InfraFlags, RegionFlags};
use crate::commands::Rendered;
use crate::seam::futures_util_shim::BoxFuture;
use crate::seam::{Attach, CoreSeam};

/// A control plane whose credential chain resolves nothing, and which answers no call.
struct NoCredentialsSeam;

struct NoCredentialsTransport;

#[expect(
    clippy::disallowed_types,
    reason = "a scripted transport answers the calls core makes; it never sends one"
)]
impl Transport for NoCredentialsTransport {
    fn send(&self, call: Call) -> BoxFuture<'_, Result<Reply, Error>> {
        Box::pin(async move {
            Err(Error::new(
                ErrorKind::Platform,
                format!("{} was sent after the credentials failed", call.operation),
            ))
        })
    }

    fn resolve_credentials(&self) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async {
            Err(Error::new(
                ErrorKind::Credentials,
                "the default credential chain resolved no credentials",
            ))
        })
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for NoCredentialsSeam {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        let plane = ControlPlane::with_transport(
            Arc::new(NoCredentialsTransport) as Arc<dyn Transport>,
            region,
            Arc::new(YieldingClock::default()) as Arc<dyn Clock>,
        );
        Box::pin(async move { Ok(plane) })
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Platform, "doctor opens no sandbox")) })
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Platform, "doctor attaches nothing")) })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Platform, "doctor uploads nothing")) })
    }
}

/// **BIND-15, shared with `doctor`: the credentials line resolves the chain.** A control plane
/// is always constructible, because the default chain always has a provider; before this
/// line was core's preflight check, `doctor` reported "resolved a provider" for a machine with
/// no identity at all. Now the line fails, fatally, naming the chain.
///
/// **Falsification** — 2026-09-24. Make `credentials_check` pass for any built plane (the old
/// behavior) and the `credentials` assertion reads `ok: true`; restored.
#[tokio::test]
async fn doctor_reports_a_credential_chain_that_resolves_nothing() {
    let command = Command::Doctor(DoctorArgs {
        binary: None,
        infra_dir: Some(std::path::PathBuf::from("/definitely/not/a/stack")),
        config: no_config(),
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&NoCredentialsSeam, &command, full_infra()).await;
    let rendered = result.expect("doctor reports rather than raises");
    let checks = rendered.data["checks"].as_array().expect("a check list");
    let credentials = checks
        .iter()
        .find(|check| check["name"] == "credentials")
        .expect("a credentials line");
    assert_eq!(credentials["ok"], false, "{credentials}");
    assert_eq!(credentials["fatal"], true, "{credentials}");
    assert!(
        credentials["detail"]
            .as_str()
            .is_some_and(|detail| detail.contains("resolved no credentials")),
        "{credentials}"
    );
    assert_eq!(rendered.data["ok"], false);
}

/// A control plane that records the region each `control_plane` call asks for, over a
/// scripted transport that answers the two managed-base reads `doctor` sends.
struct RegionRecordingSeam {
    transport: Arc<ScriptedTransport>,
    regions: Mutex<Vec<String>>,
}

impl RegionRecordingSeam {
    /// A listing that publishes the al2023 base in `published`, and one version of it.
    fn publishing_in(published: &str) -> Self {
        let arn = format!("arn:aws:lambda:{published}:aws:microvm-image:al2023-1");
        let transport = Arc::new(ScriptedTransport::new());
        transport
            .answer(
                "ListManagedMicrovmImages",
                200,
                &format!(r#"{{"items": [{{"imageArn": "{arn}", "createdAt": 1.0}}]}}"#),
            )
            .answer(
                "ListManagedMicrovmImageVersions",
                200,
                &format!(
                    r#"{{"items": [{{"imageArn": "{arn}", "imageVersion": "1", "createdAt": 1.0}}]}}"#
                ),
            );
        Self {
            transport,
            regions: Mutex::new(Vec::new()),
        }
    }

    fn regions(&self) -> Vec<String> {
        self.regions.lock().expect("not poisoned").clone()
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "a fake seam, the test's stand-in for src/seam.rs: it builds its plane or session over a scripted transport"
)]
impl CoreSeam for RegionRecordingSeam {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        self.regions
            .lock()
            .expect("not poisoned")
            .push(region.as_str().to_string());
        let plane = ControlPlane::with_transport(
            Arc::clone(&self.transport) as Arc<dyn Transport>,
            region,
            Arc::new(YieldingClock::default()) as Arc<dyn Clock>,
        );
        Box::pin(async move { Ok(plane) })
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Platform, "doctor opens no sandbox")) })
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Platform, "doctor attaches nothing")) })
    }

    fn put_artifact(&self, _uri: &str, _bytes: Vec<u8>) -> BoxFuture<'_, Result<(), Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Platform, "doctor uploads nothing")) })
    }
}

fn doctor_in(region: RegionFlags) -> Command {
    Command::Doctor(DoctorArgs {
        binary: None,
        infra_dir: Some(std::path::PathBuf::from("/definitely/not/a/stack")),
        config: no_config(),
        region,
        infra: InfraFlags::default(),
    })
}

fn doctor_line(rendered: &Rendered, name: &str) -> serde_json::Value {
    rendered.data["checks"]
        .as_array()
        .expect("a check list")
        .iter()
        .find(|check| check["name"] == name)
        .unwrap_or_else(|| panic!("no {name} line"))
        .clone()
}

/// **#250: every line of `doctor` is about the region `--region` or `--unlisted-region`
/// names.** For each flag, with no region in the environment and with `AWS_REGION` naming
/// another one, the credentials check and both managed-base reads ask the seam for the flag's
/// region, the credentials line names it, the versions read names that region's base ARN, and
/// the `managed-bases` line names it.
///
/// **Falsification** 2026-09-28. Put `resolve_region(None, None, ctx.env)` back as the region
/// `check_credentials` or `check_managed_bases` asks for, and the recorded regions read
/// `us-east-1` (or `eu-west-1`) for that check. Resolve with
/// `resolve_region(args.region.region.map(|r| r.region()), None, ctx.env)`, dropping
/// `--unlisted-region`, and the ca-central-1 pass records `us-east-1`. Skip the versions read
/// and its count reads 0. Restored.
#[tokio::test]
async fn doctor_asks_every_check_about_the_region_its_flag_names() {
    let flags = [
        (
            RegionFlags {
                region: Some(crate::cli::RegionArg::UsWest2),
                unlisted_region: None,
            },
            "us-west-2",
        ),
        (
            RegionFlags {
                region: None,
                unlisted_region: Some("ca-central-1".to_string()),
            },
            "ca-central-1",
        ),
    ];
    for (flag, named) in flags {
        for env in [None, Some(("AWS_REGION", "eu-west-1"))] {
            let seam = RegionRecordingSeam::publishing_in(named);
            let command = doctor_in(flag.clone());
            let (result, _) = match env {
                None => dispatch_with(&seam, &command, full_infra()).await,
                Some(var) => dispatch_with_env(&seam, &command, full_infra(), var).await,
            };
            let rendered = result.expect("doctor reports rather than raises");
            let case = format!("{named}, env {env:?}");
            assert_eq!(
                seam.regions(),
                [named, named],
                "{case}: the credentials check and the managed-base reads each ask for a plane"
            );
            let credentials = doctor_line(&rendered, "credentials");
            assert!(
                credentials["detail"]
                    .as_str()
                    .is_some_and(|detail| detail.ends_with(&format!("for {named}"))),
                "{case}: {credentials}"
            );
            let bases = doctor_line(&rendered, "managed-bases");
            assert_eq!(bases["ok"], true, "{case}: {bases}");
            assert!(
                bases["detail"]
                    .as_str()
                    .is_some_and(|detail| detail.contains(&format!("in {named}"))),
                "{case}: {bases}"
            );
            assert_eq!(
                seam.transport.called("ListManagedMicrovmImageVersions"),
                1,
                "{case}: the versions read is sent once"
            );
            let versions_paths = seam.transport.paths_of("ListManagedMicrovmImageVersions");
            assert!(
                versions_paths[0].contains(named),
                "{case}: the versions read names the flag's base ARN: {versions_paths:?}"
            );
            let versions = doctor_line(&rendered, "base-image-versions");
            assert_eq!(versions["ok"], true, "{case}: {versions}");
        }
    }
}

/// **#250, the unresolved case: no managed-base read goes to a region nobody chose.** A
/// region the environment holds that the parser refuses is reported on the region line. The
/// credentials line still resolves the chain, on us-east-1 as before, and the `managed-bases`
/// line says it wasn't read instead of listing us-east-1's bases. The credentials line says
/// us-east-1 stood in, so the report doesn't name two regions without saying why.
///
/// **Falsification** 2026-09-28. Fall back to `Region::UsEast1` in `check_managed_bases` when
/// the region doesn't resolve, and a second plane is asked for. Skip the credentials check
/// there, and none is. Drop the stand-in note from the credentials line and its detail
/// doesn't say it. Restored.
#[tokio::test]
async fn doctor_reads_no_managed_base_when_the_region_does_not_resolve() {
    let seam = RegionRecordingSeam::publishing_in("us-east-1");
    let command = doctor_in(RegionFlags {
        region: None,
        unlisted_region: None,
    });
    let (result, _) = dispatch_with_env(
        &seam,
        &command,
        full_infra(),
        ("AWS_REGION", "not-a-region"),
    )
    .await;
    let rendered = result.expect("doctor reports rather than raises");
    assert_eq!(doctor_line(&rendered, "region")["ok"], false);
    // The credentials check still asks, for us-east-1, and nothing else does.
    assert_eq!(seam.regions(), ["us-east-1"]);
    let credentials = doctor_line(&rendered, "credentials");
    assert_eq!(credentials["ok"], true, "{credentials}");
    // It names us-east-1 under a region line that doesn't, so it says us-east-1 stood in.
    assert!(
        credentials["detail"]
            .as_str()
            .is_some_and(|detail| detail.contains("us-east-1 stood in")),
        "{credentials}"
    );
    assert_eq!(seam.transport.called("ListManagedMicrovmImages"), 0);
    assert_eq!(seam.transport.called("ListManagedMicrovmImageVersions"), 0);
    let bases = doctor_line(&rendered, "managed-bases");
    assert_eq!(bases["ok"], false, "{bases}");
    assert_eq!(bases["fatal"], false, "{bases}");
    assert!(
        bases["detail"]
            .as_str()
            .is_some_and(|detail| detail.contains("region")),
        "{bases}"
    );
}

/// **#250, the unresolved case keeps a missing identity fatal.** With `AWS_REGION` set to a
/// name the parser refuses and no identity in the chain, the region line stays advisory, the
/// credentials line still resolves the chain and fails fatally, and `doctor` isn't ok.
///
/// **Falsification** 2026-09-28. Skip the credentials check when the region doesn't resolve
/// and there's no credentials line. Make its line advisory there and it isn't fatal. Restored.
#[tokio::test]
async fn doctor_keeps_a_missing_identity_fatal_when_the_region_does_not_resolve() {
    let command = doctor_in(RegionFlags {
        region: None,
        unlisted_region: None,
    });
    let (result, _) = dispatch_with_env(
        &NoCredentialsSeam,
        &command,
        full_infra(),
        ("AWS_REGION", "not-a-region"),
    )
    .await;
    let rendered = result.expect("doctor reports rather than raises");
    let region = doctor_line(&rendered, "region");
    assert_eq!(region["fatal"], false, "{region}");
    let credentials = doctor_line(&rendered, "credentials");
    assert_eq!(credentials["ok"], false, "{credentials}");
    assert_eq!(credentials["fatal"], true, "{credentials}");
    assert!(
        credentials["detail"]
            .as_str()
            .is_some_and(|detail| detail.contains("resolved no credentials")),
        "{credentials}"
    );
    // The credentials line is the one fatal failure, so it's what makes `doctor` not ok.
    let fatal_failures: Vec<&serde_json::Value> = rendered.data["checks"]
        .as_array()
        .expect("a check list")
        .iter()
        .filter(|check| check["ok"] == false && check["fatal"] == true)
        .map(|check| &check["name"])
        .collect();
    assert_eq!(fatal_failures, ["credentials"]);
    assert_eq!(rendered.data["ok"], false);
}
