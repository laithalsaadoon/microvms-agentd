// SPDX-License-Identifier: Apache-2.0
//! `Sandbox::ensure_image` against AWS (#221), invoked by `conformance/run_rs.py`'s
//! ensure-image section, which records its report as named IMAGE checks and then verifies
//! cleanup independently: the image, the S3 objects under the run's key prefix, and the
//! service-created log group.
//!
//! One run, in order:
//!
//! 1. A task directory with its own Dockerfile on a non-managed `FROM`, ending on
//!    `USER nobody`, a `.dockerignore`, an executable script, an ignored file, and a symlink.
//!    The Dockerfile is wrapped with `wrap_dockerfile` and paired with
//!    `BaseImage::from_dockerfile` by `ensure_image` itself.
//! 2. Two sandboxes ensure the image at once: one builds, the other joins (IMAGE-11).
//! 3. A third ensure on one of those sandboxes reuses it with no upload (IMAGE-9) and no
//!    second account lookup (IMAGE-8).
//! 4. A VM launched from it shows the daemon running as root after the task's `USER`
//!    (IMAGE-2), the context's script, and neither the ignored file nor the link (IMAGE-7).
//! 5. A forced ensure deletes the ready image and rebuilds it under the same name (IMAGE-10).
//! 6. The image is deleted and its absence observed.
//!
//! The report is written to `MICROVM_ENSURE_REPORT` whatever happens, so a failure midway is
//! still a named check with its reason.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use futures_util::future::BoxFuture;
use microvms_core::Error;
use microvms_core::control::artifact::{WrapOptions, wrap_dockerfile};
use microvms_core::control::{
    BuildContext, BuildServices, ControlPlane, EnsureImageRequest, SignedBuildServices,
};
use microvms_core::prelude::*;
use microvms_core::region::Region;
use microvms_core::sandbox::{RunRequest, Sandbox, TeardownOpts};
use serde_json::{Value, json};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("conformance must supply {name}"))
}

fn region() -> Region {
    std::env::var("AWS_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()
        .expect("a supported region")
}

/// The real services, counted: how many account lookups and uploads each sandbox made.
struct Counted {
    inner: Arc<SignedBuildServices>,
    account_calls: AtomicUsize,
    puts: std::sync::Mutex<Vec<String>>,
}

impl BuildServices for Counted {
    fn caller_account(&self) -> BoxFuture<'_, Result<String, Error>> {
        self.account_calls.fetch_add(1, Ordering::SeqCst);
        self.inner.caller_account()
    }

    fn put_object<'a>(
        &'a self,
        bucket: &'a str,
        key: &'a str,
        bytes: Vec<u8>,
    ) -> BoxFuture<'a, Result<(), Error>> {
        self.puts
            .lock()
            .expect("not poisoned")
            .push(format!("s3://{bucket}/{key}"));
        self.inner.put_object(bucket, key, bytes)
    }
}

fn write(dir: &std::path::Path, name: &str, bytes: &[u8]) {
    let path = dir.join(name);
    std::fs::create_dir_all(path.parent().expect("a parent")).expect("dirs");
    std::fs::write(path, bytes).expect("write");
}

/// The task directory: a Dockerfile of its own on a non-managed base, ending on another
/// user, plus the files the checks look for.
fn task_directory() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "microvms-live-ensure-{}",
        microvms_core::session::mint_exec_id()
    ));
    write(
        &dir,
        "Dockerfile",
        b"FROM public.ecr.aws/amazonlinux/amazonlinux:2023\n\
          WORKDIR /task\n\
          COPY . /task/\n\
          RUN test -x /task/app/run.sh && test ! -e /task/secret.txt\n\
          USER nobody\n",
    );
    write(&dir, ".dockerignore", b"secret.txt\n");
    write(&dir, "secret.txt", b"never in the image\n");
    write(&dir, "app/run.sh", b"#!/bin/sh\necho context-ok\n");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        std::fs::set_permissions(
            dir.join("app/run.sh"),
            std::fs::Permissions::from_mode(0o755),
        )
        .expect("chmod");
        std::os::unix::fs::symlink("app/run.sh", dir.join("link.sh")).expect("symlink");
    }
    dir
}

fn start(command: &str) -> microvms_core::protocol::exec::StartRequest {
    microvms_core::protocol::exec::StartRequest {
        exec_id: microvms_core::session::mint_exec_id(),
        command: vec![command.to_string()],
        shell: microvms_core::protocol::exec::Shell::Flag(true),
        cwd: None,
        env: std::collections::HashMap::new(),
        user: None,
        group: None,
        timeout_sec: None,
        stdin: false,
        reap_group_on_exit: false,
        inherit_image_env: false,
    }
}

#[tokio::test]
#[ignore = "needs AWS credentials and the conformance stack; builds an image twice and launches a VM"]
async fn ensure_image_builds_once_reuses_and_rebuilds_under_force() {
    let report_path = env("MICROVM_ENSURE_REPORT");
    let mut report = json!({});
    let outcome = run(&mut report).await;
    if let Err(error) = &outcome {
        report["error"] = json!(error.to_string());
    }
    std::fs::write(
        &report_path,
        serde_json::to_vec_pretty(&report).expect("serializes"),
    )
    .expect("the report file");
    if let Err(error) = outcome {
        panic!("{error}");
    }
}

async fn run(report: &mut Value) -> Result<(), Box<dyn std::error::Error>> {
    let binary = std::fs::read(env("MICROVM_AGENTD_BINARY"))?;
    let bucket = env("MICROVM_BUCKET");
    let role = env("MICROVM_BUILD_ROLE_ARN");
    let execution_role = env("MICROVM_EXECUTION_ROLE_ARN");
    let prefix = env("MICROVM_ENSURE_PREFIX");
    let key_prefix = env("MICROVM_ENSURE_KEY_PREFIX");

    let dir = task_directory();
    let task = std::fs::read_to_string(dir.join("Dockerfile"))?;
    let dockerfile = wrap_dockerfile(&task, &WrapOptions::default())?;
    let request = || -> Result<EnsureImageRequest, Error> {
        let mut request = EnsureImageRequest::new(
            prefix.clone(),
            binary.clone(),
            dockerfile.clone(),
            bucket.clone(),
            role.clone(),
        );
        request.context = Some(BuildContext::from_dir(&dir)?);
        request.s3_key_prefix = Some(key_prefix.clone());
        request
            .tags
            .insert("conformance".to_string(), "ensure-image".to_string());
        request.wait_timeout = Some(Duration::from_secs(30 * 60));
        Ok(request)
    };

    let signed = Arc::new(SignedBuildServices::new(region()).await?);
    let counted = || {
        Arc::new(Counted {
            inner: Arc::clone(&signed),
            account_calls: AtomicUsize::new(0),
            puts: std::sync::Mutex::new(Vec::new()),
        })
    };
    let (services_a, services_b) = (counted(), counted());
    let mut first = Sandbox::new(region())
        .await?
        .with_build_services(services_a.clone());
    let mut second = Sandbox::new(region())
        .await?
        .with_build_services(services_b.clone());

    // 2. The race.
    let started = Instant::now();
    let (a, b) = tokio::join!(
        first.ensure_image(request()?),
        second.ensure_image(request()?)
    );
    let race_seconds = started.elapsed().as_secs_f64();
    let (a, b) = (a?, b?);
    eprintln!(
        "race: {} reused={} / {} reused={} in {race_seconds:.0}s",
        a.image.identifier, a.reused, b.image.identifier, b.reused
    );
    report["name"] = json!(a.image.name);
    report["arn"] = json!(a.image.identifier);
    report["artifactUri"] = json!(a.artifact_uri);
    report["warnings"] = json!(a.warnings);
    report["race"] = json!({
        "reused": [a.reused, b.reused],
        "uploaded": [a.uploaded, b.uploaded],
        "identifiers": [a.image.identifier, b.image.identifier],
        "versions": [a.image.version, b.image.version],
        "states": [a.image.state, b.image.state],
        "seconds": race_seconds,
    });
    let arn = a.image.identifier.clone();

    // 3. The reuse, on a sandbox that already looked its account up.
    let started = Instant::now();
    let third = first.ensure_image(request()?).await?;
    report["reuse"] = json!({
        "reused": third.reused,
        "uploaded": third.uploaded,
        "identifier": third.image.identifier,
        "seconds": started.elapsed().as_secs_f64(),
    });
    report["accountCalls"] = json!([
        services_a.account_calls.load(Ordering::SeqCst),
        services_b.account_calls.load(Ordering::SeqCst),
    ]);
    let puts: Vec<String> = [&services_a, &services_b]
        .iter()
        .flat_map(|services| services.puts.lock().expect("not poisoned").clone())
        .collect();
    report["puts"] = json!(puts);

    // 4. The guest.
    let mut launcher = Sandbox::new(region()).await?;
    let mut launch = RunRequest::new().with_image(&arn);
    launch.execution_role_arn = Some(execution_role);
    launch.max_duration_sec = 900;
    launch.max_idle_sec = 600;
    let guest = async {
        launcher.run(launch).await?;
        let session = launcher.session().ok_or("no session after the launch")?;
        let mut observed = json!({});
        for (key, command) in [
            ("uid", "id -u"),
            ("context", "/task/app/run.sh"),
            (
                "excluded",
                "test ! -e /task/secret.txt && test ! -e /task/link.sh && echo excluded",
            ),
        ] {
            let result = session
                .run_sync(start(command), Duration::from_secs(60))
                .await?;
            observed[key] = json!(result.stdout().trim());
        }
        Ok::<Value, Box<dyn std::error::Error>>(observed)
    }
    .await;
    let teardown = launcher
        .terminate(TeardownOpts::default().waiting_for_terminated())
        .await;
    report["guest"] = match &guest {
        Ok(observed) => observed.clone(),
        Err(error) => json!({"error": error.to_string()}),
    };
    report["vm"] = json!({
        "id": launcher.microvm().map(|vm| vm.id.clone()),
        "terminated": teardown.terminate_accepted,
    });

    // 5. The forced rebuild, under the same name.
    let mut forced_request = request()?;
    forced_request.force = true;
    let started = Instant::now();
    let forced = Sandbox::new(region())
        .await?
        .with_build_services(counted())
        .ensure_image(forced_request)
        .await;
    report["forced"] = match &forced {
        Ok(forced) => json!({
            "reused": forced.reused,
            "uploaded": forced.uploaded,
            "identifier": forced.image.identifier,
            "version": forced.image.version,
            "state": forced.image.state,
            "seconds": started.elapsed().as_secs_f64(),
        }),
        Err(error) => json!({"error": error.to_string()}),
    };

    // 6. Cleanup, observed.
    let plane = ControlPlane::new(region()).await?;
    let deleted = plane.delete_image(&arn, 20, Duration::from_secs(15)).await;
    let mut absent = false;
    for _ in 0..40 {
        if plane.describe_image(&arn).await?.is_none() {
            absent = true;
            break;
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    }
    report["cleanup"] = json!({"deleteAccepted": deleted, "imageAbsent": absent});
    let _ = std::fs::remove_dir_all(&dir);

    guest?;
    forced?;
    Ok(())
}
