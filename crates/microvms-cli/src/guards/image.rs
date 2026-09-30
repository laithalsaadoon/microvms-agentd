// SPDX-License-Identifier: Apache-2.0
//! `image-versions`, `image-set-status`, `image-builds` (#264) against the scripted transport:
//! the calls each makes, and the readback each envelope carries.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::control::ops::VersionStatus;
use microvms_core::testing::{self as fake, YieldingClock};

use super::support::{ScriptedSeam, ScriptedTransport, dispatch_with, full_infra, region_flags};
use crate::cli::{Command, ImageBuildsArgs, ImageSetStatusArgs, ImageVersionsArgs};

const ARN: &str = "arn:aws:lambda:us-east-1:123456789012:microvm-image:img";

fn seam(transport: &Arc<ScriptedTransport>) -> ScriptedSeam {
    ScriptedSeam {
        transport: Arc::clone(transport),
        clock: Arc::new(YieldingClock::default()),
    }
}

/// **`image-set-status` sends one `UpdateMicrovmImageVersion` carrying the status, and the
/// envelope is the readback.** An ARN names the image with no listing, so the update is the
/// only call.
///
/// **Falsification**: send `VersionStatus::Active` from the handler whatever the argument says,
/// and the body's `status` reads `ACTIVE` (and core's readback check refuses the INACTIVE
/// answer).
#[tokio::test]
async fn image_set_status_sends_one_update_with_the_status_and_reports_the_readback() {
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer(
        "UpdateMicrovmImageVersion",
        200,
        &fake::get_image_version_response("2.0", "SUCCESSFUL", "INACTIVE"),
    );
    let command = Command::ImageSetStatus(ImageSetStatusArgs {
        image: ARN.into(),
        image_version: "2.0".into(),
        status: VersionStatus::Inactive,
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam(&transport), &command, full_infra()).await;
    let rendered = result.expect("the update is answered with the status asked for");

    assert_eq!(transport.calls(), ["UpdateMicrovmImageVersion"]);
    assert_eq!(
        transport.first_body("UpdateMicrovmImageVersion")["status"],
        "INACTIVE"
    );
    assert!(
        transport.paths_of("UpdateMicrovmImageVersion")[0].ends_with("/versions/2.0"),
        "{:?}",
        transport.paths_of("UpdateMicrovmImageVersion")
    );
    assert_eq!(rendered.kind, "microvm.image.status");
    assert_eq!(rendered.data["imageArn"], ARN);
    assert_eq!(rendered.data["imageVersion"], "2.0");
    assert_eq!(rendered.data["status"], "INACTIVE");
    assert_eq!(rendered.data["version"]["state"], "SUCCESSFUL");
}

/// A name is resolved through the listing before the versions are read, and the envelope
/// carries every version in the service's own spelling.
#[tokio::test]
async fn image_versions_resolves_a_name_and_reports_every_version() {
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "ListMicrovmImages",
            200,
            &fake::list_images_response(&["img"], None),
        )
        .answer(
            "ListMicrovmImageVersions",
            200,
            &fake::list_versions_page(&["1.0", "2.0"], None),
        );
    let command = Command::ImageVersions(ImageVersionsArgs {
        image: "img".into(),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam(&transport), &command, full_infra()).await;
    let rendered = result.expect("the versions are listed");

    assert_eq!(
        transport.calls(),
        ["ListMicrovmImages", "ListMicrovmImageVersions"]
    );
    assert_eq!(rendered.kind, "microvm.image.versions");
    assert_eq!(rendered.data["imageArn"], ARN);
    let versions = rendered.data["versions"].as_array().expect("a list");
    let listed: Vec<&str> = versions
        .iter()
        .map(|version| version["imageVersion"].as_str().expect("a version"))
        .collect();
    assert_eq!(listed, ["1.0", "2.0"]);
    assert_eq!(versions[0]["status"], "ACTIVE");
    assert_eq!(versions[0]["codeArtifact"]["uri"], "s3://bucket/img.zip");
}

/// `--build-id` reads the one build, with its snapshot sizes, and lists nothing; without it
/// the version's builds are listed.
#[tokio::test]
async fn image_builds_lists_a_versions_builds_or_reads_one_with_its_sizes() {
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer(
        "GetMicrovmImageBuild",
        200,
        &fake::get_image_build_response(
            "build-1",
            "SUCCESSFUL",
            "4",
            None,
            Some(r#"{"memorySnapshotSizeInBytes": 1024, "diskSnapshotSizeInBytes": 2048}"#),
        ),
    );
    let one = Command::ImageBuilds(ImageBuildsArgs {
        image: ARN.into(),
        image_version: "1.0".into(),
        build_id: Some("build-1".into()),
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam(&transport), &one, full_infra()).await;
    let rendered = result.expect("the build is read");
    assert_eq!(transport.calls(), ["GetMicrovmImageBuild"]);
    assert_eq!(rendered.kind, "microvm.image.builds");
    assert_eq!(rendered.data["imageVersion"], "1.0");
    let builds = rendered.data["builds"].as_array().expect("a list");
    assert_eq!(builds.len(), 1);
    assert_eq!(builds[0]["buildId"], "build-1");
    assert_eq!(
        builds[0]["snapshotBuild"]["memorySnapshotSizeInBytes"],
        1024
    );

    let transport = Arc::new(ScriptedTransport::new());
    transport.answer(
        "ListMicrovmImageBuilds",
        200,
        &fake::list_builds_page(&[("build-1", "SUCCESSFUL"), ("build-2", "FAILED")], None),
    );
    let all = Command::ImageBuilds(ImageBuildsArgs {
        image: ARN.into(),
        image_version: "1.0".into(),
        build_id: None,
        region: region_flags(),
    });
    let (result, _) = dispatch_with(&seam(&transport), &all, full_infra()).await;
    let rendered = result.expect("the builds are listed");
    assert_eq!(transport.calls(), ["ListMicrovmImageBuilds"]);
    let states: Vec<&str> = rendered.data["builds"]
        .as_array()
        .expect("a list")
        .iter()
        .map(|build| build["buildState"].as_str().expect("a state"))
        .collect();
    assert_eq!(states, ["SUCCESSFUL", "FAILED"]);
}
