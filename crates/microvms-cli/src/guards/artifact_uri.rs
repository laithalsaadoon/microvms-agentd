// SPDX-License-Identifier: Apache-2.0
//! A caller's `--artifact-uri` (#249).

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    FakeBinary, ScriptedSeam, ScriptedTransport, TempDir, build_args_without_binary, dispatch_with,
    full_infra, region_flags, run_args_for_image, script_prov_build,
};
use crate::cli::{BuildArgs, Command, InfraFlags, MemoryMib, RunArgs};
use crate::seam::Infra;

/// A transport that lets a build reach its create call and stops it there: a `--reuse` listing
/// finds nothing and `CreateMicrovmImage` answers 400. The command fails, but the create body
/// and any upload before it are on the record, and those are all these guards read.
fn scripted_create_stop() -> Arc<ScriptedTransport> {
    let transport = Arc::new(ScriptedTransport::new());
    // `GetMicrovmImage` answers 404 for `run`'s ensure (#258): the name is free, so it builds
    // and the create is where it stops.
    transport
        .answer("GetMicrovmImage", 404, r#"{"message": "Image not found"}"#)
        .answer("CreateMicrovmImage", 400, r#"{"message": "scripted stop"}"#);
    transport
}

/// `build <binary> --name img`, with `--artifact-uri` as given.
fn artifact_uri_build_args(binary: &std::path::Path, artifact_uri: Option<&str>) -> BuildArgs {
    BuildArgs {
        binary: Some(binary.to_path_buf()),
        state_dir: None,
        base_image_version: None,
        artifact_uri: artifact_uri.map(str::to_string),
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
    }
}

/// `run <binary> --name img`, taking `run`'s build arm, with `--artifact-uri` as given.
fn artifact_uri_run_args(
    binary: &std::path::Path,
    state_dir: std::path::PathBuf,
    artifact_uri: Option<&str>,
) -> RunArgs {
    let mut args = run_args_for_image("unused", state_dir);
    args.image = None;
    args.binary = Some(binary.to_path_buf());
    args.artifact_uri = artifact_uri.map(str::to_string);
    args
}

/// What `command` left on the record against [`scripted_create_stop`]: the URIs it uploaded
/// to, the artifact URI its create call named, and its stderr.
async fn upload_record(
    command: &Command,
    infra: Infra,
) -> (Vec<String>, serde_json::Value, String) {
    let transport = scripted_create_stop();
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let (result, stderr) = dispatch_with(&seam, command, infra).await;
    assert!(result.is_err(), "the scripted create refuses every build");
    let uri = transport.first_body("CreateMicrovmImage")["codeArtifact"]["uri"].clone();
    (transport.uploads(), uri, stderr)
}

/// **Issue #249: a caller's `--artifact-uri` is never uploaded over, whatever the bucket
/// says.** Both uploading paths, `build` and `run`'s build arm, with a bucket set, and `build`
/// with none. One arm names the very key the CLI would derive, `s3://<bucket>/<name>.zip`: a
/// caller may keep their own artifact there, and the skip has to follow from their naming a URI,
/// not from the URI differing from the one the CLI would pick.
///
/// The flag says the object is already at that URI, and the image is built from it. The upload
/// step used to decide from the bucket alone, and the bucket defaults to `$MICROVM_BUCKET`, so a
/// shell that exported it for ordinary builds replaced the caller's object with the CLI's own
/// artifact, and the create call then named that URI. The progress line is held too: with a
/// bucket it names the bucket that went unused, so a caller who meant the bucket sees why
/// nothing was uploaded, and with none it stays quiet.
///
/// **Falsification**, run 2026-09-28. Four breaks, each registered in
/// verify/guards/faults/cli-artifact-uri.toml. Skip the upload only when there's no bucket, main's old
/// condition (`cli-artifact-uri-not-uploaded-over`): red on `build:` with the PUT recorded. Pass
/// `None` for the caller's URI from `run`'s build arm (`cli-artifact-uri-run-arm`): red on `run:`.
/// Pass `None` from `build` (`cli-artifact-uri-build-arm`): red on `build:`. Print the
/// unused-bucket line with no bucket (`cli-artifact-uri-no-bucket-quiet`): red on `no bucket:`.
/// Round 1's falsifier: skip only when the caller's URI differs from the derived key
/// (`cli-artifact-uri-derived-key`): red on `build, the derived key:`.
#[tokio::test]
async fn a_caller_supplied_artifact_uri_is_never_uploaded_over_even_with_a_bucket() {
    const THEIRS: &str = "s3://caller-bucket/theirs.zip";
    // `full_infra()`'s bucket and the args' `--name img`.
    const AT_THE_DERIVED_KEY: &str = "s3://a-bucket/img.zip";
    let binary = FakeBinary::new("caller-uri-bin");
    let ledgers = TempDir::new("caller-uri-ledger");
    let no_bucket = Infra {
        bucket: None,
        ..full_infra()
    };
    let arms = [
        (
            "build",
            THEIRS,
            Command::Build(artifact_uri_build_args(&binary.0, Some(THEIRS))),
            full_infra(),
        ),
        (
            "run",
            THEIRS,
            Command::Run(Box::new(artifact_uri_run_args(
                &binary.0,
                ledgers.0.clone(),
                Some(THEIRS),
            ))),
            full_infra(),
        ),
        (
            "no bucket",
            THEIRS,
            Command::Build(artifact_uri_build_args(&binary.0, Some(THEIRS))),
            no_bucket,
        ),
        (
            "build, the derived key",
            AT_THE_DERIVED_KEY,
            Command::Build(artifact_uri_build_args(&binary.0, Some(AT_THE_DERIVED_KEY))),
            full_infra(),
        ),
    ];
    for (arm, theirs, command, infra) in arms {
        let with_bucket = infra.bucket.is_some();
        let (uploads, uri, stderr) = upload_record(&command, infra).await;
        assert_eq!(
            uploads,
            Vec::<String>::new(),
            "{arm}: a caller-supplied --artifact-uri must not be uploaded over"
        );
        assert_eq!(uri, theirs, "{arm}: the create call names the caller's URI");
        assert!(
            !stderr.contains("uploading "),
            "{arm}: no upload progress line: {stderr}"
        );
        if with_bucket {
            assert!(
                stderr.contains("the bucket a-bucket is unused for this build"),
                "{arm}: the unused bucket is named: {stderr}"
            );
        } else {
            assert!(
                !stderr.contains("is unused for this build"),
                "{arm}: with no bucket there's none to call unused: {stderr}"
            );
        }
    }
}

/// **Issue #249: a bucket with no `--artifact-uri` still uploads, to the derived key.** The
/// other side of the guard above, through the same two paths.
///
/// Nothing else holds that a build uploads at all: every other guard that reads the uploads
/// asserts there were none. Without this one, a fix that skipped the upload every time would
/// pass the whole suite and then fail every real build on an empty S3 key.
///
/// **Falsification**, run 2026-09-28. Skip the upload whenever there's no caller URI as well as
/// when there's no bucket (`cli-bucket-build-uploads`): red on `build:` with no upload recorded.
#[tokio::test]
async fn a_bucket_without_an_artifact_uri_uploads_to_the_derived_key() {
    let binary = FakeBinary::new("bucket-only-bin");
    let ledgers = TempDir::new("bucket-only-ledger");
    // `build --name img` keeps the name it was given; `run`'s build is an ensure (#258), whose
    // name is `img-<hash12>` and whose key is `<name>/artifact.zip`.
    let arms = [
        (
            "build",
            Command::Build(artifact_uri_build_args(&binary.0, None)),
            "s3://a-bucket/img.zip",
        ),
        (
            "run",
            Command::Run(artifact_uri_run_args(&binary.0, ledgers.0.clone(), None).into()),
            "/artifact.zip",
        ),
    ];
    for (arm, command, derived) in arms {
        let (uploads, uri, stderr) = upload_record(&command, full_infra()).await;
        assert!(
            uploads.len() == 1 && uploads[0].ends_with(derived),
            "{arm}: a bucket and no --artifact-uri uploads to the derived key: {uploads:?}"
        );
        assert!(
            uploads[0].starts_with("s3://a-bucket/img"),
            "{arm}: in the bucket, under the name: {uploads:?}"
        );
        assert_eq!(
            uri,
            uploads[0].as_str(),
            "{arm}: the create call names the uploaded key"
        );
        assert!(
            !stderr.contains("is unused for this build"),
            "{arm}: the bucket was used: {stderr}"
        );
    }
}

/// **Issue #249: `build --reuse` refuses `--artifact-uri` at parse time.** The reuse name is a
/// hash of the local build inputs (the binary, the Dockerfile, the project pair), never of the
/// caller's object. An image built from that object under such a name would answer a later
/// plain `build --reuse` of the same binary, which would then run the caller's bytes as if they
/// were its own. The kind is checked, not just the error, so a pair that fails to parse for
/// some other reason (a renamed flag) doesn't pass it.
///
/// **Falsification**, run 2026-09-28. Drop `conflicts_with = "artifact_uri"` from
/// `BuildArgs::reuse` (`cli-reuse-refuses-artifact-uri`): the pair parses, red on
/// `build --reuse --artifact-uri must not parse`.
#[test]
fn build_reuse_refuses_a_caller_artifact_uri_at_parse_time() {
    use clap::Parser as _;
    for alone in [
        ["microvm", "build", "agentd", "--reuse"].as_slice(),
        [
            "microvm",
            "build",
            "agentd",
            "--artifact-uri",
            "s3://c/t.zip",
        ]
        .as_slice(),
    ] {
        crate::cli::Cli::try_parse_from(alone).unwrap_or_else(|error| panic!("{alone:?}: {error}"));
    }
    for both in [
        [
            "microvm",
            "build",
            "agentd",
            "--reuse",
            "--artifact-uri",
            "s3://c/t.zip",
        ],
        [
            "microvm",
            "build",
            "agentd",
            "--artifact-uri",
            "s3://c/t.zip",
            "--reuse",
        ],
    ] {
        let error = crate::cli::Cli::try_parse_from(both)
            .map(|_| ())
            .expect_err(&format!(
                "build --reuse --artifact-uri must not parse: {both:?}"
            ));
        assert_eq!(
            error.kind(),
            clap::error::ErrorKind::ArgumentConflict,
            "{both:?}: {error}"
        );
    }
}

/// **Issue #249: `--artifact-uri` refuses the flags that only shape an artifact the CLI
/// builds.** `--dockerfile` on `run` and `build`, and `--project` on `build`, reach the image
/// only through the artifact `upload_artifact` builds, and it builds none beside a caller's
/// URI. Accepted, they'd be dropped with nothing said, and a Dockerfile that was never used
/// could still fail the command in `preflight`. The kind is checked, as in the `--reuse` guard
/// above, and each flag parses on its own so the refusal is the pair's. `--port` stays legal
/// beside the URI: it sets the create call's `hooks.port`, which names the port the caller's
/// own daemon listens on, so it still reaches the image.
///
/// **Falsification**, run 2026-09-28. Drop `conflicts_with = "dockerfile"` from
/// `RunArgs::artifact_uri` (`cli-run-artifact-uri-refuses-dockerfile`): red on
/// `run --artifact-uri --dockerfile must not parse`. Leave only `"project"` in
/// `BuildArgs::artifact_uri`'s list (`cli-build-artifact-uri-refuses-dockerfile`): red on
/// `build --artifact-uri --dockerfile`. Leave only `"dockerfile"`
/// (`cli-build-artifact-uri-refuses-project`): red on `build --artifact-uri --project`.
#[test]
fn artifact_uri_refuses_the_local_artifact_inputs_at_parse_time() {
    use clap::Parser as _;
    const URI: [&str; 2] = ["--artifact-uri", "s3://c/t.zip"];
    let cases: [(&str, &str, [&str; 2]); 3] = [
        (
            "run",
            "run --artifact-uri --dockerfile",
            ["--dockerfile", "D"],
        ),
        (
            "build",
            "build --artifact-uri --dockerfile",
            ["--dockerfile", "D"],
        ),
        (
            "build",
            "build --artifact-uri --project",
            ["--project", "p"],
        ),
    ];
    for (command, label, flag) in cases {
        for alone in [URI, flag] {
            let argv = [["microvm", command, "agentd"].as_slice(), &alone].concat();
            crate::cli::Cli::try_parse_from(&argv)
                .unwrap_or_else(|error| panic!("{argv:?}: {error}"));
        }
        for both in [[URI, flag].concat(), [flag, URI].concat()] {
            let argv = [["microvm", command, "agentd"].as_slice(), &both].concat();
            let error = crate::cli::Cli::try_parse_from(&argv)
                .map(|_| ())
                .expect_err(&format!("{label} must not parse: {argv:?}"));
            assert_eq!(
                error.kind(),
                clap::error::ErrorKind::ArgumentConflict,
                "{label}: {argv:?}: {error}"
            );
        }
    }
}

/// **Issue #249: a caller's `--artifact-uri` with no binary provisions no daemon.** The
/// caller's object already holds one. Fetching the release asset anyway cost a network fetch
/// (and failed an offline build), and `build`'s envelope then reported that fetched, attested
/// daemon as `agentd` for an image that doesn't contain it. Both paths run with the fetcher
/// that panics on contact, `build` to a finished envelope and `run` to the scripted create stop.
/// A binary the caller names is still read and not refused: `run`'s positional can be a sync
/// directory and a config file's `binary` can supply it, so clap can't tell an unused one apart,
/// and its envelope `agentd` is null already.
///
/// **Falsification**, run 2026-09-28. Delete `build`'s `None if args.artifact_uri.is_some()`
/// arm (`cli-artifact-uri-build-no-provision`): the panicking fetcher is reached, red on its
/// panic. The same in `run` (`cli-artifact-uri-run-no-provision`): red the same way.
#[tokio::test]
async fn a_caller_artifact_uri_provisions_no_daemon() {
    const THEIRS: &str = "s3://caller-bucket/theirs.zip";
    let dir = TempDir::new("caller-uri-no-prov");
    let transport = Arc::new(ScriptedTransport::new());
    script_prov_build(&transport);
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };
    let mut args = build_args_without_binary(dir.0.clone());
    args.artifact_uri = Some(THEIRS.into());
    let (result, _) = dispatch_with(&seam, &Command::Build(args), full_infra()).await;
    let rendered = result.expect("build: a caller's object builds with no daemon on hand");
    assert_eq!(
        rendered.data["agentd"],
        serde_json::Value::Null,
        "build: no daemon went into the image from here: {:?}",
        rendered.data
    );
    assert_eq!(
        transport.first_body("CreateMicrovmImage")["codeArtifact"]["uri"],
        THEIRS,
        "build: the create call names the caller's URI"
    );
    assert_eq!(
        transport.uploads(),
        Vec::<String>::new(),
        "build: nothing uploaded"
    );

    let mut args =
        artifact_uri_run_args(std::path::Path::new("unused"), dir.0.clone(), Some(THEIRS));
    args.binary = None;
    let (uploads, uri, _) = upload_record(&Command::Run(Box::new(args)), full_infra()).await;
    assert_eq!(uri, THEIRS, "run: the create call names the caller's URI");
    assert_eq!(uploads, Vec::<String>::new(), "run: nothing uploaded");
}
