// SPDX-License-Identifier: Apache-2.0
//! `build` against the scripted transport: `--reuse`, the daemon's provisioning, and the flags
//! that reach the image's create body.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    CountingFetch, FakeBinary, RefusingSeam, ScriptedSeam, ScriptedTransport, TempDir,
    build_args_without_binary, dispatch_with, dispatch_with_fetch, full_infra, microvm_body,
    region_flags, run_args_for_image, script_prov_build,
};
use crate::cli::{BuildArgs, Command, InfraFlags, MemoryMib};
use crate::exit::Exit;
use crate::seam::Infra;

/// The name `build --reuse` derives for `binary` at 2048 MiB: core's `ensure_image` name over
/// the default Dockerfile on the managed base, computed through core's public functions so the
/// test knows the name without copying the derivation.
fn expected_reuse_name(prefix: &str, binary: &std::path::Path) -> String {
    let bytes = std::fs::read(binary).expect("the fake binary is readable");
    let base = microvms_core::control::BaseImage::al2023();
    let dockerfile = microvms_core::control::default_dockerfile(9000, None, &base, None);
    let hash = microvms_core::control::artifact_content_hash(&bytes, &dockerfile, None);
    let identity = microvms_core::control::ensure::pinned_identity_hash(
        &hash,
        &base,
        None,
        MemoryMib::Mib2048.size_class(),
    );
    microvms_core::control::ensure::ensured_image_name(prefix, &identity).expect("a legal prefix")
}

/// `GetMicrovmImageResponse` for `name` in `state`, in the model's own spelling.
fn image_in(name: &str, state: &str) -> String {
    format!(
        r#"{{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}",
             "name": "{name}", "state": "{state}", "latestActiveImageVersion": "1",
             "createdAt": 1754524800}}"#
    )
}

/// `CreateMicrovmImageResponse` for `name`.
fn created(name: &str) -> String {
    format!(
        r#"{{"imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}",
             "name": "{name}", "state": "CREATING", "createdAt": 1754524800,
             "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
             "buildRoleArn": "arn:aws:iam::123456789012:role/build",
             "codeArtifact": {{"uri": "s3://a-bucket/{name}/artifact.zip"}},
             "imageVersion": "1"}}"#
    )
}

/// `build --reuse` for `binary` under the prefix `coding-agents`.
fn reuse_args(binary: &std::path::Path) -> BuildArgs {
    BuildArgs {
        binary: Some(binary.to_path_buf()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: None,
        name: Some("coding-agents".into()),
        memory: MemoryMib::Mib2048,
        size: crate::cli::SizeRequestFlags::default(),
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: true,
        s3_key_prefix: None,
        force: false,
        tags: Vec::new(),
        base_image: None,
        inherit_workdir: false,
        run_hook_timeout_sec: None,
        build_hook_timeout_sec: None,
        port: None,
        region: region_flags(),
        infra: InfraFlags::default(),
    }
}

/// **`build --reuse`, the hit: a ready image under the derived name means no build at all.**
///
/// The load-bearing assertion is the `CreateMicrovmImage` count: a reuse that "worked"
/// while still creating an image would bill a build and — worse — replay the
/// stale-snapshot hazard the flag exists to close. The envelope carries `reused: true`
/// and the existing image's identifier, which is what a script keys on.
///
/// **Guard proof.** Map `Found::Ready` to `Plan::Build` in core's `plan` and the count
/// assertion goes red with a `CreateMicrovmImage` the fake then also fails for lack of an
/// answer.
#[tokio::test]
async fn a_reuse_build_whose_hash_name_exists_skips_the_build_entirely() {
    let binary = FakeBinary::new("reuse-hit");
    let expected = expected_reuse_name("coding-agents", &binary.0);

    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("GetMicrovmImage", 200, &image_in(&expected, "CREATED"));

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(reuse_args(&binary.0));
    let (result, stderr) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("a hit is a success");

    assert_eq!(
        transport.called("CreateMicrovmImage"),
        0,
        "a reuse hit must build nothing: {:?}",
        transport.calls()
    );
    assert!(
        transport.uploads().is_empty(),
        "and upload nothing: {:?}",
        transport.uploads()
    );
    assert!(
        transport.paths_of("GetMicrovmImage")[0].contains(&expected),
        "the describe asks for the derived name: {:?}",
        transport.paths_of("GetMicrovmImage")
    );

    assert_eq!(rendered.data["reused"], true);
    assert_eq!(rendered.data["imageName"], expected.as_str());
    assert_eq!(
        rendered.data["imageIdentifier"],
        format!("arn:aws:lambda:us-east-1:123456789012:microvm-image:{expected}"),
        "the existing image's identifier is the envelope's answer"
    );
    assert_eq!(
        rendered.data["artifactUri"],
        format!("s3://a-bucket/{expected}/artifact.zip")
    );
    assert!(stderr.contains("reusing"), "{stderr}");
}

/// **`build --reuse`, the miss: the build runs, under the derived name, from core's upload
/// to the content-addressed key.**
///
/// The build happened — `CreateMicrovmImage` went out — under the name that carries the
/// hash, which is what makes the *next* invocation with the same inputs a hit, and the
/// artifact went to `s3://<bucket>/<name>/artifact.zip` through the sandbox's build services.
///
/// **Guard proof.** Drop the size class from `pinned_identity_hash` and `expected_reuse_name`
/// parts from the name the handler sends, because `build`'s name is ensure's.
#[tokio::test]
async fn a_reuse_build_whose_hash_name_is_absent_builds_under_the_derived_name() {
    let binary = FakeBinary::new("reuse-miss");
    let expected = expected_reuse_name("coding-agents", &binary.0);

    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("GetMicrovmImage", 404, r#"{"message": "Image not found"}"#)
        .answer("GetMicrovmImage", 200, &image_in(&expected, "CREATED"))
        .answer("CreateMicrovmImage", 201, &created(&expected));

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(reuse_args(&binary.0));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("a miss builds and succeeds");

    assert_eq!(transport.called("CreateMicrovmImage"), 1, "the miss builds");
    let body = transport.first_body("CreateMicrovmImage");
    let key = format!("s3://a-bucket/{expected}/artifact.zip");
    assert_eq!(
        body["name"],
        expected.as_str(),
        "the build goes out under the derived name, hash included — the bare prefix would \
         create an image reuse can never find: {body}"
    );
    assert_eq!(body["codeArtifact"]["uri"], key.as_str());
    assert_eq!(
        transport.uploads(),
        vec![key.clone()],
        "core uploaded it there"
    );
    assert_eq!(rendered.data["reused"], false);
    assert_eq!(rendered.data["imageName"], expected.as_str());
    assert_eq!(rendered.data["artifactUri"], key.as_str());
}

/// **#258: `build --reuse` over a FAILED image under the derived name deletes it and builds
/// afresh, rather than handing the failed image back as reused.** The CLI's own reuse
/// returned whatever the listing had under the name, failed or still creating.
///
/// **Falsification**: map `Found::Failed` to `Plan::Reuse` in core's `plan` and the guard
/// reads `reused: true` with no delete and no create.
#[tokio::test]
async fn a_reuse_build_over_a_failed_image_deletes_it_and_builds() {
    let binary = FakeBinary::new("reuse-failed");
    let expected = expected_reuse_name("coding-agents", &binary.0);

    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "GetMicrovmImage",
            200,
            &image_in(&expected, "CREATE_FAILED"),
        )
        // The describe that finds it failed (above), the one that sees the name free after
        // the delete, and the build's wait.
        .answer("GetMicrovmImage", 404, r#"{"message": "Image not found"}"#)
        .answer("GetMicrovmImage", 200, &image_in(&expected, "CREATED"))
        .answer(
            "ListMicrovmImageVersions",
            200,
            &microvms_core::testing::list_versions_response("1"),
        )
        .answer(
            "DeleteMicrovmImage",
            200,
            &microvms_core::testing::delete_image_response(),
        )
        .answer("CreateMicrovmImage", 201, &created(&expected));

    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let command = Command::Build(reuse_args(&binary.0));
    let (result, _) = dispatch_with(&seam, &command, full_infra()).await;
    let rendered = result.expect("the failed image is replaced");

    let calls = transport.calls();
    let deleted = calls
        .iter()
        .position(|call| call == "DeleteMicrovmImage")
        .unwrap_or_else(|| panic!("the failed image is deleted: {calls:?}"));
    let created_at = calls
        .iter()
        .position(|call| call == "CreateMicrovmImage")
        .unwrap_or_else(|| panic!("and built afresh: {calls:?}"));
    assert!(deleted < created_at, "deleted before the create: {calls:?}");
    assert_eq!(rendered.data["reused"], false);
    assert_eq!(rendered.data["imageName"], expected.as_str());
}

/// What one `run --name img` of `binary` left behind: the control-plane calls, the uploads, the
/// ledger on disk, and the VM's history.
struct RunTrace {
    calls: Vec<String>,
    uploads: Vec<String>,
    ledgers: Vec<serde_json::Value>,
    history: Vec<serde_json::Value>,
}

/// `run --name img` of `binary` over `transport`, whose launch fails fast (the VM reports
/// TERMINATED before RUNNING) and whose terminate is refused, so the ledger survives the
/// teardown and can be read back.
async fn run_trace(binary: &std::path::Path, transport: &Arc<ScriptedTransport>) -> RunTrace {
    transport
        .answer("RunMicrovm", 200, &microvm_body("PENDING"))
        .answer("GetMicrovm", 200, &microvm_body("TERMINATED"))
        .answer(
            "TerminateMicrovm",
            409,
            r#"{"message": "ConflictException"}"#,
        )
        .answer(
            "ListMicrovmImageVersions",
            200,
            &microvms_core::testing::list_versions_response("1"),
        )
        .answer(
            "DeleteMicrovmImage",
            200,
            &microvms_core::testing::delete_image_response(),
        );
    let state = TempDir::new("run-trace-ledger");
    let seam = ScriptedSeam {
        transport: Arc::clone(transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = run_args_for_image("unused", state.0.clone());
    args.image = None;
    args.binary = Some(binary.to_path_buf());
    let (result, _) = dispatch_with(&seam, &Command::Run(Box::new(args)), full_infra()).await;
    assert!(result.is_err(), "the scripted VM never reaches RUNNING");
    RunTrace {
        calls: transport.calls(),
        uploads: transport.uploads(),
        ledgers: crate::ledger::read_all(&state.0),
        history: crate::history::read_events(&state.0, "mvm-abc123"),
    }
}

/// **D-I2 (#258): `run` deletes at teardown only an image it built.** `run`'s build is an
/// ensure, so `run --name img` names the content-addressed `img-<hash12>`, which another run of
/// the same inputs may be launching from. Two runs of one binary: the first finds the name free
/// and builds, the second finds it ready and reuses it.
///
/// The built image is uploaded, deleted at teardown, named on the ledger and recorded in the
/// VM's history as built. The reused one is none of those: deleting it would pull the image out
/// from under the other run, and a ledger naming it would send the operator to delete it too.
/// Each run's terminate is refused, so its ledger survives the teardown to be read.
///
/// **Falsification**, run 2026-09-30. Three breaks, each registered in
/// verify/guards/faults/ensure-image-paths.toml. Delete the image whatever the ensure said
/// (`cli-run-deletes-a-reused-image`): red on `the reused image is not deleted`. Record the
/// image on the ledger whatever the ensure said (`cli-run-lists-a-reused-image`): red on `the
/// ledger does not name the reused image`. Write `imageBuilt` for a reused image
/// (`cli-run-history-says-a-reused-image-was-built`): red on `the history does not say it was
/// built`.
#[tokio::test]
async fn a_run_deletes_the_image_it_built_and_never_one_it_reused() {
    let binary = FakeBinary::new("run-reuse");
    let expected = expected_reuse_name("img", &binary.0);
    let arn = format!("arn:aws:lambda:us-east-1:123456789012:microvm-image:{expected}");
    let key = format!("s3://a-bucket/{expected}/artifact.zip");
    let built_events = |history: &[serde_json::Value]| {
        history
            .iter()
            .filter(|event| event["event"] == "imageBuilt")
            .map(|event| event["imageIdentifier"].clone())
            .collect::<Vec<_>>()
    };

    // The name is free: the describe answers 404, then the build's wait sees it CREATED.
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("GetMicrovmImage", 404, r#"{"message": "Image not found"}"#)
        .answer("GetMicrovmImage", 200, &image_in(&expected, "CREATED"))
        .answer("CreateMicrovmImage", 201, &created(&expected));
    let built = run_trace(&binary.0, &transport).await;
    assert_eq!(
        built.uploads,
        [key.as_str()],
        "the build uploads its artifact"
    );
    assert_eq!(
        built
            .calls
            .iter()
            .filter(|call| *call == "CreateMicrovmImage")
            .count(),
        1,
        "{:?}",
        built.calls
    );
    assert_eq!(
        built
            .calls
            .iter()
            .filter(|call| *call == "DeleteMicrovmImage")
            .count(),
        1,
        "the image this run built is deleted at teardown: {:?}",
        built.calls
    );
    assert_eq!(built.ledgers.len(), 1, "{:?}", built.ledgers);
    assert_eq!(
        built.ledgers[0]["imageIdentifier"],
        arn.as_str(),
        "the ledger names the image this run built: {:?}",
        built.ledgers
    );
    assert_eq!(
        built_events(&built.history),
        [serde_json::json!(arn)],
        "the history says this run built it: {:?}",
        built.history
    );

    // The same inputs again, and the name is ready.
    let transport = Arc::new(ScriptedTransport::new());
    transport.answer("GetMicrovmImage", 200, &image_in(&expected, "CREATED"));
    let reused = run_trace(&binary.0, &transport).await;
    assert_eq!(
        reused.uploads,
        Vec::<String>::new(),
        "a reuse uploads nothing"
    );
    assert!(
        !reused.calls.iter().any(|call| call == "CreateMicrovmImage"),
        "a reuse builds nothing: {:?}",
        reused.calls
    );
    assert!(
        reused.calls.iter().any(|call| call == "TerminateMicrovm"),
        "the teardown ran: {:?}",
        reused.calls
    );
    assert!(
        !reused.calls.iter().any(|call| call == "DeleteMicrovmImage"),
        "the reused image is not deleted: {:?}",
        reused.calls
    );
    assert_eq!(reused.ledgers.len(), 1, "{:?}", reused.ledgers);
    assert_eq!(
        reused.ledgers[0]["imageIdentifier"],
        serde_json::Value::Null,
        "the ledger does not name the reused image: {:?}",
        reused.ledgers
    );
    assert_eq!(
        reused.ledgers[0]["leaked"],
        serde_json::json!(["mvm-abc123"]),
        "only the VM whose terminate was refused is outstanding: {:?}",
        reused.ledgers
    );
    assert_eq!(
        built_events(&reused.history),
        Vec::<serde_json::Value>::new(),
        "the history does not say it was built: {:?}",
        reused.history
    );
    assert!(
        reused
            .history
            .iter()
            .any(|event| event["event"] == "launched"),
        "the history was written: {:?}",
        reused.history
    );
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
        size: crate::cli::SizeRequestFlags::default(),
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        s3_key_prefix: None,
        force: false,
        tags: Vec::new(),
        base_image: None,
        inherit_workdir: false,
        run_hook_timeout_sec: None,
        build_hook_timeout_sec: None,
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
        size: crate::cli::SizeRequestFlags::default(),
        dockerfile: Some(dockerfile_path.clone()),
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        s3_key_prefix: None,
        force: false,
        tags: Vec::new(),
        base_image: None,
        inherit_workdir: false,
        run_hook_timeout_sec: None,
        build_hook_timeout_sec: None,
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
            size: crate::cli::SizeRequestFlags::default(),
            dockerfile: None,
            project: None,
            repair_identity: false,
            log_group: None,
            log_stream: None,
            reuse: false,
            s3_key_prefix: None,
            force: false,
            tags: Vec::new(),
            base_image: None,
            inherit_workdir: false,
            run_hook_timeout_sec: None,
            build_hook_timeout_sec: None,
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
        size: crate::cli::SizeRequestFlags::default(),
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        s3_key_prefix: None,
        force: false,
        tags: Vec::new(),
        base_image: None,
        inherit_workdir: false,
        run_hook_timeout_sec: None,
        build_hook_timeout_sec: None,
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
        size: crate::cli::SizeRequestFlags::default(),
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: Some("/aws/lambda-microvms/conformance-builds".into()),
        log_stream: Some("img-ci".into()),
        reuse: false,
        s3_key_prefix: None,
        force: false,
        tags: Vec::new(),
        base_image: None,
        inherit_workdir: false,
        run_hook_timeout_sec: None,
        build_hook_timeout_sec: None,
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
        size: crate::cli::SizeRequestFlags::default(),
        dockerfile: None,
        project: None,
        repair_identity: false,
        log_group: None,
        log_stream: None,
        reuse: false,
        s3_key_prefix: None,
        force: false,
        tags: Vec::new(),
        base_image: None,
        inherit_workdir: false,
        run_hook_timeout_sec: None,
        build_hook_timeout_sec: None,
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

/// `build` over `argv` after `microvm build <binary> --name img --region us-east-1`, parsed by
/// clap, against a transport that answers one create and a ready poll.
async fn build_with_flags(
    binary: &std::path::Path,
    argv: &[&str],
) -> (
    Result<crate::commands::Rendered, crate::exit::CliError>,
    Arc<ScriptedTransport>,
) {
    use clap::Parser as _;
    let binary = binary.to_string_lossy().to_string();
    let head = [
        "microvm",
        "build",
        binary.as_str(),
        "--name",
        "img",
        "--region",
        "us-east-1",
    ];
    let full = [head.as_slice(), argv].concat();
    let cli =
        crate::cli::Cli::try_parse_from(&full).unwrap_or_else(|error| panic!("{full:?}: {error}"));
    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer("CreateMicrovmImage", 201, &created("img"))
        .answer("GetMicrovmImage", 200, &image_in("img", "CREATED"));
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, _) = dispatch_with(&seam, &cli.command, full_infra()).await;
    (result, transport)
}

/// **#264 slice D: `build`'s image options reach the create call.** Two tags (one whose value
/// holds an `=`), a custom managed base paired with the Dockerfile's `FROM`, `--inherit-workdir`
/// over a Dockerfile that declares a `WORKDIR`, and both hook timeouts: each is on the
/// `CreateMicrovmImage` body in the model's members, the hook timeouts in their own families.
/// `inherit_workdir` has no wire member; it is the local check the next guard holds.
///
/// **Falsification**, run 2026-09-30, registered in verify/guards/faults/build-options.toml.
/// Drop the tags (`cli-build-tags-dropped`): red on `the tags reach the create body`. Keep the
/// default base (`cli-build-base-image-ignored`): red on `the base the flag names`. Drop either
/// hook timeout (`cli-build-run-hook-timeout-dropped`, `cli-build-build-hook-timeout-dropped`):
/// red on its family's member.
#[tokio::test]
async fn the_build_image_options_reach_the_create_body() {
    let binary = FakeBinary::new("build-options");
    let dockerfile = TempDir::new("build-options-dockerfile");
    let path = dockerfile.0.join("Dockerfile");
    let text = microvms_core::control::wrap_dockerfile(
        "FROM public.ecr.aws/docker/library/python:3.12-slim\nWORKDIR /work\n",
        &microvms_core::control::WrapOptions::default(),
    )
    .expect("a task Dockerfile core can wrap");
    std::fs::write(&path, text).expect("writes");
    let path = path.to_string_lossy().to_string();

    let (result, transport) = build_with_flags(
        &binary.0,
        &[
            "--dockerfile",
            &path,
            "--tag",
            "team=x",
            "--tag",
            "query=a=b",
            "--base-image",
            "custom-base",
            "--inherit-workdir",
            "--run-hook-timeout-sec",
            "45",
            "--build-hook-timeout-sec",
            "900",
        ],
    )
    .await;
    result.unwrap_or_else(|error| panic!("the build succeeds: {}", error.message));
    let body = transport.first_body("CreateMicrovmImage");
    assert_eq!(
        body["tags"],
        serde_json::json!({"team": "x", "query": "a=b"}),
        "the tags reach the create body: {body}"
    );
    assert_eq!(
        body["baseImageArn"], "arn:aws:lambda:us-east-1:aws:microvm-image:custom-base",
        "the base the flag names: {body}"
    );
    assert_eq!(
        body["hooks"]["microvmHooks"]["runTimeoutInSeconds"], 45,
        "the run family's timeout: {body}"
    );
    assert_eq!(
        body["hooks"]["microvmImageHooks"]["readyTimeoutInSeconds"], 900,
        "the build family's timeout: {body}"
    );
}

/// **#264 slice D: `--inherit-workdir` over an image that declares no `WORKDIR` is refused
/// before the upload**, with core's message and no call. The derived default Dockerfile sets
/// none and neither does the managed base, so an exec with no cwd would run in `/`. A repeated
/// `--tag` key is refused the same way, rather than one of its values dropped.
///
/// **Falsification**, run 2026-09-30, registered in verify/guards/faults/build-options.toml.
/// Send `inherit_workdir: false` whatever the flag says (`cli-build-inherit-workdir-dropped`):
/// red on `nothing declares a WORKDIR`. Let a repeated key through
/// (`cli-build-tag-key-repeated`): red on `a repeated tag key`.
#[tokio::test]
async fn a_build_refuses_an_inherit_workdir_or_a_tag_it_cannot_keep_before_any_call() {
    let binary = FakeBinary::new("build-refusals");
    let rows: [(&str, &[&str], &str); 2] = [
        (
            "nothing declares a WORKDIR",
            &["--inherit-workdir"],
            "nothing to inherit",
        ),
        (
            "a repeated tag key",
            &["--tag", "team=x", "--tag", "team=y"],
            "is given twice",
        ),
    ];
    for (label, argv, message) in rows {
        let (result, transport) = build_with_flags(&binary.0, argv).await;
        let Err(error) = result else {
            panic!(
                "{label}: refused, but the build ran: {:?}",
                transport.calls()
            );
        };
        assert_eq!(error.exit, Exit::InvalidArg, "{label}: {}", error.message);
        assert!(
            error.message.contains(message),
            "{label}: {}",
            error.message
        );
        assert_eq!(transport.calls(), Vec::<String>::new(), "{label}: no call");
        assert_eq!(
            transport.uploads(),
            Vec::<String>::new(),
            "{label}: no upload"
        );
    }
}

/// **#264 slice D: a bad tag or hook timeout is refused at parse time, through core's own
/// checks**, and `--base-image` needs a Dockerfile or an artifact to pair with. Each value is
/// refused by the parser before anything is read, with the text of core's `require_valid_tags`,
/// `RunHookTimeout` or `BuildHookTimeout`, so the CLI and the bindings refuse the same values the
/// same way. The legal edges parse.
///
/// **Falsification**, run 2026-09-30, registered in verify/guards/faults/build-options.toml.
/// Parse a tag without core's check (`cli-tag-parse-unchecked`): red on `an empty key`.
#[test]
fn a_bad_tag_or_hook_timeout_is_refused_at_parse_time() {
    use clap::Parser as _;
    let long_key = format!("{}=v", "k".repeat(129));
    let refused: [(&str, Vec<&str>, &str); 7] = [
        ("no `=`", vec!["--tag", "team"], "no `=`"),
        (
            "an empty key",
            vec!["--tag", "=x"],
            "TagKey requires at least 1 character",
        ),
        (
            "a long key",
            vec!["--tag", &long_key],
            "over the TagKey ceiling",
        ),
        (
            "a run hook past its ceiling",
            vec!["--run-hook-timeout-sec", "61"],
            "microvmHooks timeout of 61s",
        ),
        (
            "a zero build hook",
            vec!["--build-hook-timeout-sec", "0"],
            "microvmImageHooks timeout of 0s",
        ),
        (
            "a fractional hook",
            vec!["--run-hook-timeout-sec", "1.5"],
            "not a whole number",
        ),
        (
            "a base with nothing to pair",
            vec!["--base-image", "custom-base"],
            "--dockerfile",
        ),
    ];
    for (label, flags, message) in refused {
        let argv = [["microvm", "build", "agentd"].as_slice(), &flags].concat();
        let error = crate::cli::Cli::try_parse_from(&argv)
            .map(|_| ())
            .expect_err(label);
        assert!(error.to_string().contains(message), "{label}: {error}");
    }
    for flags in [
        vec!["--tag", "team="],
        vec![
            "--run-hook-timeout-sec",
            "60",
            "--build-hook-timeout-sec",
            "3600",
        ],
        vec![
            "--base-image",
            "custom-base",
            "--artifact-uri",
            "s3://c/t.zip",
        ],
    ] {
        let argv = [["microvm", "build", "agentd"].as_slice(), &flags].concat();
        crate::cli::Cli::try_parse_from(&argv).unwrap_or_else(|error| panic!("{flags:?}: {error}"));
    }
}
