// SPDX-License-Identifier: Apache-2.0
//! `build` against the scripted transport: `--reuse`, the daemon's provisioning, and the flags
//! that reach the image's create body.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    CountingFetch, FakeBinary, RefusingSeam, ScriptedSeam, ScriptedTransport, TempDir,
    build_args_without_binary, dispatch_with, dispatch_with_fetch, full_infra, list_images_body,
    region_flags, run_args_for_image, script_prov_build,
};
use crate::cli::{BuildArgs, Command, InfraFlags, MemoryMib};
use crate::exit::Exit;
use crate::seam::Infra;

/// The name `build --reuse` derives for `binary`, computed the way the handler computes
/// it — through core's public hash over the same inputs — so the test knows the name
/// without copying the derivation logic.
fn expected_reuse_name(prefix: &str, binary: &std::path::Path) -> String {
    let bytes = std::fs::read(binary).expect("the fake binary is readable");
    let dockerfile = microvms_core::control::default_dockerfile(
        9000,
        None,
        &microvms_core::control::BaseImage::al2023(),
        None,
    );
    let hash = microvms_core::control::artifact_content_hash(&bytes, &dockerfile, None);
    format!("{prefix}-{}", &hash[..12])
}

/// **`build --reuse`, the hit: an image whose content-hash name already exists means no
/// build at all.**
///
/// The load-bearing assertion is the `CreateMicrovmImage` count: a reuse that "worked"
/// while still creating an image would bill a build and — worse — replay the
/// stale-snapshot hazard the flag exists to close. The envelope carries `reused: true`
/// and the existing image's identifier, which is what a script keys on.
///
/// **Guard proof.** Make the hit path fall through to the build (delete the early
/// `return` on `find_image_by_name`'s `Some`) and the count assertion goes red with a
/// `CreateMicrovmImage` the fake then also fails for lack of an answer.
#[tokio::test]
async fn a_reuse_build_whose_hash_name_exists_skips_the_build_entirely() {
    let binary = FakeBinary::new("reuse-hit");
    let expected = expected_reuse_name("coding-agents", &binary.0);

    let transport = Arc::new(ScriptedTransport::new());
    transport.answer(
        "ListMicrovmImages",
        200,
        &list_images_body(&[&expected], None),
    );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("coding-agents".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: true,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, stderr) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("a hit is a success");

    assert_eq!(
        transport.called("CreateMicrovmImage"),
        0,
        "a reuse hit must build nothing: {:?}",
        transport.calls()
    );
    assert_eq!(transport.called("ListMicrovmImages"), 1);
    let listing = transport.paths_of("ListMicrovmImages");
    assert!(
        listing[0].contains(&format!("nameFilter={expected}")),
        "the listing is asked for the derived name: {}",
        listing[0]
    );

    assert_eq!(rendered.data["reused"], true);
    assert_eq!(rendered.data["imageName"], expected.as_str());
    assert_eq!(
        rendered.data["imageIdentifier"],
        format!("arn:aws:lambda:us-east-1:123456789012:microvm-image:{expected}"),
        "the existing image's identifier is the envelope's answer"
    );
    assert!(stderr.contains("reusing"), "{stderr}");
}

/// **`build --reuse`, the miss: the build runs, under the derived name.**
///
/// Two claims. The build happened — `CreateMicrovmImage` went out — and the name it went
/// out under carries the content hash, which is what makes the *next* invocation with
/// the same inputs a hit. A miss that built under the bare prefix would create an image
/// reuse can never find, and the flag would rebuild forever while reporting success.
///
/// **Guard proof.** Keep the seed as the request name on the miss path (drop the
/// `request.name = name.clone()` assignment) and the `body["name"]` assertion reads
/// `coding-agents` with no hash suffix.
#[tokio::test]
async fn a_reuse_build_whose_hash_name_is_absent_builds_under_the_derived_name() {
    let binary = FakeBinary::new("reuse-miss");
    let expected = expected_reuse_name("coding-agents", &binary.0);

    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("ListMicrovmImages", 200, &list_images_body(&[], None))
        .answer(
            "CreateMicrovmImage",
            201,
            &format!(
                r#"{{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:{expected}",
                     "name": "{expected}", "state": "CREATING", "createdAt": 1754524800,
                     "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                     "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                     "codeArtifact": {{"uri": "s3://a-bucket/{expected}.zip"}},
                     "imageVersion": "1"}}"#
            ),
        )
        .answer(
            "GetMicrovmImage",
            200,
            &format!(
                r#"{{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:{expected}",
                     "name": "{expected}", "state": "CREATED", "createdAt": 1754524800}}"#
            ),
        );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("coding-agents".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: true,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("a miss builds and succeeds");

    assert_eq!(transport.called("CreateMicrovmImage"), 1, "the miss builds");
    let body = transport.first_body("CreateMicrovmImage");
    assert_eq!(
        body["name"],
        expected.as_str(),
        "the build goes out under the derived name, hash included — the bare prefix would \
         create an image reuse can never find: {body}"
    );
    assert_eq!(
        body["codeArtifact"]["uri"],
        format!("s3://a-bucket/{expected}.zip"),
        "the derived artifact key follows the derived name"
    );
    assert_eq!(rendered.data["reused"], false);
    assert_eq!(rendered.data["imageName"], expected.as_str());
}

/// **A `build` with no binary provisions one, builds from it, and says so on the
/// envelope; the next invocation reads the cache instead of fetching again.** The whole
/// self-provisioning promise in one guard: `microvm build`/`run` on a fresh machine needs
/// no path to this product's own component, and one download serves every later call.
///
/// **Guard proof.** Reorder the resolution chain so the fetch outranks the cache
/// (`provision.rs`) and the second dispatch fetches again — the count assertion below
/// reads 2 and goes red. Watched fail exactly that way before this landed.
#[tokio::test]
async fn a_build_with_no_binary_provisions_once_and_the_next_build_reads_the_cache() {
    let dir = TempDir::new("prov-cache");
    let fetch = CountingFetch(std::sync::atomic::AtomicUsize::new(0));

    let transport = Arc::new(ScriptedTransport::new());
    script_prov_build(&transport);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(build_args_without_binary(dir.0.clone()));
    let (result, stderr) = dispatch_with_fetch(&seam, &command, full_infra(), &fetch).await;
    let rendered = result.expect("a provisioned build succeeds");

    assert_eq!(
        transport.called("CreateMicrovmImage"),
        1,
        "the build went out"
    );
    assert_eq!(fetch.0.load(std::sync::atomic::Ordering::SeqCst), 1);
    assert_eq!(
        rendered.data["agentd"]["source"], "fetched",
        "{:?}",
        rendered.data
    );
    assert_eq!(rendered.data["agentd"]["verified"], "attestation");
    assert!(
        stderr.contains("fetching the release asset"),
        "the fetch must be visible on stderr, not silent: {stderr}"
    );

    // The second invocation: same state dir, fresh transport script, and the count must
    // not move — a chain that re-fetched would make every build cost a download.
    let transport = Arc::new(ScriptedTransport::new());
    script_prov_build(&transport);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(build_args_without_binary(dir.0.clone()));
    let (result, _) = dispatch_with_fetch(&seam, &command, full_infra(), &fetch).await;
    let rendered = result.expect("a cached build succeeds");
    assert_eq!(
        fetch.0.load(std::sync::atomic::Ordering::SeqCst),
        1,
        "no second fetch"
    );
    assert_eq!(
        rendered.data["agentd"]["source"], "cache",
        "{:?}",
        rendered.data
    );
    assert_eq!(rendered.data["agentd"]["verified"], serde_json::Value::Null);
}

/// **A caller-supplied binary suppresses provisioning entirely** — the envelope's
/// `agentd` is null and the fetch seam is never consulted. Proven by routing through
/// [`dispatch_with`], whose fetcher panics on contact.
#[tokio::test]
async fn a_supplied_binary_never_consults_the_provisioning_chain() {
    let binary = FakeBinary::new("no-prov");
    let transport = Arc::new(ScriptedTransport::new());
    script_prov_build(&transport);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = build_args_without_binary(std::env::temp_dir());
    args.binary = Some(binary.0.clone());
    let (result, _) = dispatch_with(&seam, &Command::Build(args), full_infra()).await;
    let rendered = result.expect("a supplied binary builds");
    assert_eq!(
        rendered.data["agentd"],
        serde_json::Value::Null,
        "{:?}",
        rendered.data
    );
}

/// **`quickstart` is `run` — same preconditions, same refusals, same order.** With no
/// infrastructure configured it fails `run`'s own role check, locally, before the fetch
/// (the panicking fetcher proves the ordering) and before any AWS call (the refusing seam
/// proves that). A quickstart that fetched or called AWS before the cheap refusal would
/// spend a first-time user's seconds discovering what one env read already knew.
#[tokio::test]
async fn quickstart_refuses_missing_infrastructure_before_fetching_or_calling_aws() {
    let command = Command::Quickstart(crate::cli::QuickstartArgs {
        exec: "echo hello".into(),
        state_dir: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&RefusingSeam::new(), &command, Infra::default()).await;
    let failure = result.expect_err("no roles configured");
    assert_eq!(failure.exit, Exit::Precondition);
    assert!(
        failure.message.contains("build_role_arn") || failure.message.contains("BUILD_ROLE"),
        "the refusal names the missing value: {}",
        failure.message
    );
}

/// A plain `build` (no `--reuse`) never touches the listing, and its envelope still
/// carries `reused: false` — the key is always present, so no consumer guards for it.
#[tokio::test]
async fn a_plain_build_never_lists_and_reports_reused_false() {
    let binary = FakeBinary::new("plain-build");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "CreateMicrovmImage",
            201,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATING", "createdAt": 1754524800,
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://a-bucket/img.zip"},
                 "imageVersion": "1"}"#,
        )
        .answer(
            "GetMicrovmImage",
            200,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATED", "createdAt": 1754524800}"#,
        );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("img".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("builds");
    assert_eq!(
        transport.called("ListMicrovmImages"),
        0,
        "no --reuse, no listing"
    );
    assert_eq!(rendered.data["reused"], false);
    assert_eq!(rendered.data["imageName"], "img");
}

/// **Issue #47: a request core itself refuses costs zero transport calls — including the
/// S3 upload.** Both uploading paths, `build` and `run`, against a Dockerfile core's own
/// guards reject (no `CMD`, so the daemon would never start).
///
/// The guards always ran; the defect was ordering. `upload_artifact` came before
/// `build_image`, so a caller iterating on a refused Dockerfile paid one S3 PUT per
/// attempt for a rejection that was knowable locally. The contract is the one
/// `create_image`'s docs state: nothing billable before everything checkable is checked.
///
/// **Falsification** — run 2026-08-17. Swap `sandbox.preflight(&request)?` back below
/// `upload_artifact` in either path and that path's `uploads` assertion goes red with the
/// PUT recorded; the guard still refuses, so only this ordering test catches it.
#[tokio::test]
async fn a_locally_refused_dockerfile_costs_no_upload_and_no_call() {
    let dockerfile_path = std::env::temp_dir().join(format!(
        "microvm-guard-no-cmd-{}-{:?}.Dockerfile",
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::write(
        &dockerfile_path,
        "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\nCOPY agentd /agentd\n",
    )
    .expect("writes");

    // The build path.
    let binary = FakeBinary::new("refused-build");
    let transport = Arc::new(ScriptedTransport::new());
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("refused".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: Some(dockerfile_path.clone()),
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let error = result.expect_err("core refuses a Dockerfile with no CMD");
    assert_eq!(error.exit, Exit::InvalidArg, "{}", error.message);
    assert_eq!(
        transport.uploads(),
        Vec::<String>::new(),
        "build: the refused request must not cost the S3 PUT"
    );
    assert_eq!(transport.calls(), Vec::<String>::new(), "build: zero calls");

    // The run path's build arm.
    let binary = FakeBinary::new("refused-run");
    // A distinct label from the FakeBinary above: both helpers derive the same
    // `microvm-guard-<label>-<pid>-<tid>` path, and a shared label is a file/dir collision.
    let ledgers = TempDir::new("refused-run-ledger");
    let transport = Arc::new(ScriptedTransport::new());
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = run_args_for_image("unused", ledgers.0.clone());
    args.image = None;
    args.binary = Some(binary.0.clone());
    args.dockerfile = Some(dockerfile_path.clone());
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    let error = result.expect_err("the run path refuses the same Dockerfile");
    assert_eq!(error.exit, Exit::InvalidArg, "{}", error.message);
    assert_eq!(
        transport.uploads(),
        Vec::<String>::new(),
        "run: the refused request must not cost the S3 PUT"
    );
    assert_eq!(transport.calls(), Vec::<String>::new(), "run: zero calls");

    let _ = std::fs::remove_file(&dockerfile_path);
}

/// **BIND-20 (#257): a daemon that isn't an aarch64 ELF costs no upload and no call.** `build` and
/// `run`'s build arm, each handed an explicit binary holding an x86_64 ELF header and then one
/// that isn't an ELF at all, are refused as preconditions before the S3 PUT. The CLI checks
/// only that the file exists, so before core refused the bytes they were uploaded and built,
/// and the build failed as a run-hook timeout.
///
/// **Falsification**: drop the `require_aarch64` calls from `ControlPlane::preflight` and
/// `build_artifact_with_context` and both paths upload the bytes.
#[tokio::test]
async fn a_daemon_that_is_not_an_aarch64_elf_costs_no_upload_and_no_call() {
    let mut x86 = vec![0u8; 20];
    x86[..4].copy_from_slice(b"\x7fELF");
    x86[5] = 1;
    x86[18..20].copy_from_slice(&0x3Eu16.to_le_bytes());
    for (label, bytes, why) in [
        ("x86-64", x86, "ELF machine 0x3e, not aarch64"),
        (
            "script",
            b"#!/bin/sh\n".to_vec(),
            "not an ELF binary at all",
        ),
    ] {
        // One file for both paths, named the way `FakeBinary` names its own.
        let binary = FakeBinary::new(&format!("wrong-arch-{label}"));
        std::fs::write(&binary.0, &bytes).expect("writes");

        let transport = Arc::new(ScriptedTransport::new());
        let seam = ScriptedSeam {
            transport: Arc::clone(&transport),
            clock: Arc::new(YieldingClock::default()),
        };
        let command = Command::Build(BuildArgs {
            binary: Some(binary.0.clone()),
            state_dir: None,
            base_image_version: None,
            artifact_uri: None,
            name: Some("wrong-arch".into()),
            memory: MemoryMib::Mib2048,
            dockerfile: None,
            project: None,
            repair_identity: false,
            log_group: None,
            log_stream: None,
            reuse: false,
            port: None,
            region: region_flags(),
            infra: InfraFlags::default(),
        });
        let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
        let error = result.expect_err(why);
        assert_eq!(error.exit, Exit::Precondition, "{label}: {}", error.message);
        assert!(error.message.contains(why), "{label}: {}", error.message);
        assert_eq!(
            transport.uploads(),
            Vec::<String>::new(),
            "build, {label}: no S3 PUT"
        );
        assert_eq!(
            transport.calls(),
            Vec::<String>::new(),
            "build, {label}: zero calls"
        );

        let ledgers = TempDir::new(&format!("wrong-arch-{label}-ledger"));
        let transport = Arc::new(ScriptedTransport::new());
        let seam = ScriptedSeam {
            transport: Arc::clone(&transport),
            clock: Arc::new(YieldingClock::default()),
        };
        let mut args = run_args_for_image("unused", ledgers.0.clone());
        args.image = None;
        args.binary = Some(binary.0.clone());
        let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
        let error = result.expect_err(why);
        assert_eq!(
            error.exit,
            Exit::Precondition,
            "run, {label}: {}",
            error.message
        );
        assert!(
            error.message.contains(why),
            "run, {label}: {}",
            error.message
        );
        assert_eq!(
            transport.uploads(),
            Vec::<String>::new(),
            "run, {label}: no S3 PUT"
        );
        assert_eq!(
            transport.calls(),
            Vec::<String>::new(),
            "run, {label}: zero calls"
        );
    }
}

/// **`build --base-image-version` reaches the `CreateMicrovmImage` body**, and its absence
/// emits nothing.
///
/// Read off the emitted body rather than off `BuildArgs`, which is the whole point: a field on
/// the args struct proves nothing about what got sent, and the wiring from flag to wire member
/// runs through three hops — `BuildArgs`, `BuildSpec`, `CreateImageRequest` — any of which
/// could drop it while every other test still passed.
///
/// **Guard proof.** Run 2026-08-16. Set `base_image_version: None` in `build`'s `BuildSpec`
/// (the flag parsed, the spec ignores it) and the pinned assertion goes red with the member
/// absent from the body; every other CLI test stays green, which is why this test exists.
#[tokio::test]
async fn a_pinned_base_image_version_reaches_the_create_body_from_the_build_flag() {
    let binary = FakeBinary::new("pinned-base");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "CreateMicrovmImage",
            201,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATING", "createdAt": 1754524800,
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://a-bucket/img.zip"},
                 "imageVersion": "1"}"#,
        )
        .answer(
            "GetMicrovmImage",
            200,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATED", "createdAt": 1754524800}"#,
        );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        // The managed base's versions are bare integers, measured 2026-08-16.
        base_image_version: Some("1".into()),
        artifact_uri: None,
        name: Some("img".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    result.expect("builds");

    let body = transport.first_body("CreateMicrovmImage");
    assert_eq!(
        body["baseImageVersion"], "1",
        "the flag has to reach the wire, or a build still floats on the service default: {body}"
    );
    // Both are sent: pinning a version does not replace the base ARN.
    assert_eq!(
        body["baseImageArn"],
        "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1"
    );
}

/// **`build --log-group`/`--log-stream` reach the `CreateMicrovmImage` body with the
/// per-build discriminator applied, and the envelope reports the resolved exact stream.**
///
/// Read off the emitted body for the base-image-version test's reason: the wiring runs
/// through three hops — `BuildArgs`, `BuildSpec`, `CreateImageRequest` — any of which
/// could drop it while every other test stayed green. The discriminator claim is the
/// load-bearing one: the wire stream must be `<user value>/<16 hex>`, never verbatim,
/// because the member is an exact stream name and one build is three VMs writing three
/// streams (issue #98). And the envelope's `logStream` must equal the wire's byte for
/// byte — the nonce is minted inside core's create call, so the envelope is the only
/// place a caller can learn the name.
///
/// **Guard proof.** Run 2026-08-30. Set `log_stream: None` in `build`'s `BuildSpec` (the
/// flag parsed, the spec ignores it) and the body assertion goes red with no `logging`
/// member; every other CLI test stays green. Restored.
#[tokio::test]
async fn a_build_log_stream_reaches_the_wire_suffixed_and_the_envelope_reports_it() {
    let binary = FakeBinary::new("log-stream");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "CreateMicrovmImage",
            201,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATING", "createdAt": 1754524800,
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://a-bucket/img.zip"},
                 "imageVersion": "1"}"#,
        )
        .answer(
            "GetMicrovmImage",
            200,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATED", "createdAt": 1754524800}"#,
        );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("img".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: Some("/aws/lambda-microvms/conformance-builds".into()),
        log_stream: Some("img-ci".into()),
        reuse: false,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("builds");

    let body = transport.first_body("CreateMicrovmImage");
    assert_eq!(
        body["logging"]["cloudWatch"]["logGroup"], "/aws/lambda-microvms/conformance-builds",
        "the flag has to reach the wire: {body}"
    );
    let wire_stream = body["logging"]["cloudWatch"]["logStream"]
        .as_str()
        .expect("a stream was sent");
    assert_ne!(
        wire_stream, "img-ci",
        "the flag's value must never reach the wire verbatim — an exact stream name \
         collapses every build's three streams into one"
    );
    assert!(wire_stream.starts_with("img-ci/"), "{wire_stream}");
    let suffix = &wire_stream["img-ci/".len()..];
    assert_eq!(suffix.len(), 16, "{wire_stream}");
    assert!(
        suffix.bytes().all(|b| b.is_ascii_hexdigit()),
        "{wire_stream}"
    );

    // The envelope reports the resolved name, byte-identical to the wire's, plus the
    // configured group as buildLogGroup — not the derived default.
    assert_eq!(rendered.data["logStream"], wire_stream);
    assert_eq!(
        rendered.data["buildLogGroup"],
        "/aws/lambda-microvms/conformance-builds"
    );
}

/// A build with no logging flags emits **no** `logging` member and a null `logStream`
/// key: absent on the wire (byte-for-byte the request this CLI always sent), present as
/// null in the envelope (so a consumer never guards for the key).
#[tokio::test]
async fn a_build_without_logging_flags_emits_no_logging_member_and_a_null_stream() {
    let binary = FakeBinary::new("no-logging");
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "CreateMicrovmImage",
            201,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATING", "createdAt": 1754524800,
                 "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                 "buildRoleArn": "arn:aws:iam::123456789012:role/build",
                 "codeArtifact": {"uri": "s3://a-bucket/img.zip"},
                 "imageVersion": "1"}"#,
        )
        .answer(
            "GetMicrovmImage",
            200,
            r#"{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                 "name": "img", "state": "CREATED", "createdAt": 1754524800}"#,
        );

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(BuildArgs {
        binary: Some(binary.0.clone()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("img".into()),
        memory: MemoryMib::Mib2048,
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    });
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("builds");

    let body = transport.first_body("CreateMicrovmImage");
    assert!(
        body.get("logging").is_none(),
        "an unconfigured build must emit byte-for-byte what this CLI always sent: {body}"
    );
    assert_eq!(
        rendered.data["logStream"],
        serde_json::Value::Null,
        "the key is always present so a consumer never guards for it"
    );
    assert_eq!(
        rendered.data["buildLogGroup"], "/aws/lambda-microvms/img",
        "no configured group means the derived default"
    );
}
