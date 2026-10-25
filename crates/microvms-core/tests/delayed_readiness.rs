// SPDX-License-Identifier: Apache-2.0
//! The live delayed-ready recipe must pass the same local guards as its CLI build.

// The Linux CI job provisions Python and runs the live suite's offline checks; the
// other client jobs install only Rust. This test exercises that Linux-only fixture.
#![cfg(target_os = "linux")]

use std::path::Path;
use std::process::Command;
use std::sync::Arc;

use microvms_app::testing::{FakeControlPlane, TestClock, aarch64_daemon, control_plane};
use microvms_core::Region;
use microvms_core::control::CreateImageRequest;

fn delayed_recipe() -> String {
    let conformance = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../conformance");
    // Import the live generator itself: a copied recipe could remain valid while
    // the live lane regressed to a mismatched port or a forbidden ENTRYPOINT.
    let output = Command::new("python3")
        .args([
            "-I",
            "-B",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); \
             from lanes.delayed_readiness import delayed_recipe; \
             sys.stdout.write(delayed_recipe())",
        ])
        .arg(conformance)
        .output()
        .expect("run the delayed-ready recipe generator with Python");
    assert!(
        output.status.success(),
        "delayed-ready recipe generation failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let recipe = String::from_utf8(output.stdout).expect("the recipe is UTF-8");
    assert!(!recipe.is_empty(), "the live recipe must be present");
    recipe
}

#[test]
fn delayed_readiness_recipe_passes_create_image_preflight() {
    let transport = Arc::new(FakeControlPlane::new());
    let plane = control_plane(
        transport.clone(),
        Region::US_EAST_1,
        Arc::new(TestClock::new()),
    );
    let mut request = CreateImageRequest::new(
        "microvm-cli-slow-ready-preflight",
        aarch64_daemon(b""),
        "s3://bucket/delayed-ready.zip",
        "arn:aws:iam::123456789012:role/build",
    );
    request.dockerfile = Some(delayed_recipe());

    let verdict = plane.preflight(&request);
    assert!(
        verdict.is_ok(),
        "the live delayed-ready recipe must pass image preflight: {verdict:?}"
    );
    assert!(
        transport.operations().is_empty(),
        "preflight must stay local"
    );
}

#[test]
fn delayed_readiness_launcher_preserves_daemon_bootstrap() {
    let recipe = delayed_recipe();
    let format = recipe
        .lines()
        .find_map(|line| {
            line.strip_prefix("RUN mv /agentd /real-agentd && printf '")?
                .strip_suffix("' > /agentd && chmod 0755 /agentd")
        })
        .expect("the recipe installs its launcher with printf");
    assert!(
        !format.contains('\''),
        "the launcher's single-quoted format must remain one shell argument"
    );
    // Pass the extracted format as data, so this runs only the recipe's printf;
    // the surrounding mv, output redirection, and chmod never execute on the host.
    let output = Command::new("/bin/sh")
        .args(["-c", "printf \"$1\"", "delayed-ready-launcher"])
        .arg(format)
        .output()
        .expect("render the launcher with the shell's printf");
    assert!(output.status.success(), "launcher printf failed");
    assert_eq!(
        output.stdout,
        b"#!/bin/sh\npython3 /delayed_ready.py &\nexport AGENTD_PORT=9001\nexec /real-agentd\n",
        "the launcher must start the relay and exec the real daemon with actual newlines"
    );
}
