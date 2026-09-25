// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for IMAGE-6 through IMAGE-11, run against the real `Sandbox`.
//!
//! The scenarios live in `tests/features/ensure_image.feature`, tagged with the requirement
//! each one verifies; this file is their step definitions, the fake platform, and the runner.
//! A `harness = false` test, so `cargo test` runs it everywhere; `CUCUMBER_JUNIT` names a
//! JUnit report file (give it absolutely).
//!
//! # The fake platform
//!
//! [`Platform`] answers the MicroVMs operations `ensure_image` makes from one map of images
//! by name, and moves them the way the service does: a created image is `CREATING` for a
//! number of describes and then `CREATED` (or `CREATE_FAILED`), a deleted one is `DELETING`
//! for one describe and then gone, and a create against a name that exists is refused with
//! 409. Every answer is literal JSON in the model's spelling. For the race, creates are held
//! until both callers have described, so both find the name free.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use cucumber::{World, WriterExt, given, then, when, writer};
use futures_util::future::BoxFuture;
use microvms_core::control::artifact::{WrapOptions, wrap_dockerfile};
use microvms_core::control::ensure::{EnsureImageRequest, EnsuredImage, prepare};
use microvms_core::control::transport::{Call, Reply, Transport};
use microvms_core::control::{BuildContext, BuildServices, Clock, ControlPlane};
use microvms_core::sandbox::Sandbox;
use microvms_core::{Error, Region, SizeClass};

const ACCOUNT: &str = "123456789012";
const BUCKET: &str = "artifact-bucket";
const ROLE: &str = "arn:aws:iam::123456789012:role/build";

// ── the platform ────────────────────────────────────────────────────────────

#[derive(Debug)]
struct Image {
    state: &'static str,
    /// Describes left before a `CREATING` image settles.
    polls_left: u32,
    /// What a settling build settles to.
    fails: bool,
}

#[derive(Debug, Default)]
struct PlatformState {
    images: HashMap<String, Image>,
    /// Describes a new build stays `CREATING` for.
    build_polls: u32,
    /// Creates wait until this many describes have happened.
    hold_creates_until: usize,
    describes: usize,
    accepted_creates: usize,
    refused_creates: usize,
    deletes: usize,
    /// "create" and "delete", in order.
    events: Vec<&'static str>,
    create_uris: Vec<String>,
}

#[derive(Debug, Default)]
struct Platform {
    state: Mutex<PlatformState>,
    moved: tokio::sync::Notify,
}

/// The image name in an operation path: the segment that decodes to an image ARN.
fn image_name_of(path: &str) -> String {
    path.split('?')
        .next()
        .unwrap_or_default()
        .split('/')
        .map(percent_decode)
        .find_map(|segment| {
            segment
                .rsplit_once("microvm-image:")
                .map(|(_, name)| name.to_string())
        })
        .unwrap_or_default()
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).expect("ascii");
            out.push(u8::from_str_radix(hex, 16).expect("hex"));
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).expect("utf-8")
}

fn arn(name: &str) -> String {
    format!("arn:aws:lambda:us-east-1:{ACCOUNT}:microvm-image:{name}")
}

fn reply(status: u16, body: String) -> Result<Reply, Error> {
    Ok(Reply {
        status,
        body: body.into_bytes(),
    })
}

fn not_found() -> Result<Reply, Error> {
    reply(404, r#"{"message": "Image not found"}"#.to_string())
}

impl Platform {
    fn with(build_polls: u32) -> Arc<Self> {
        let platform = Arc::new(Self::default());
        platform.state.lock().expect("not poisoned").build_polls = build_polls;
        platform
    }

    fn seed(&self, name: &str, state: &'static str) {
        let mut inner = self.state.lock().expect("not poisoned");
        let polls_left = inner.build_polls;
        inner.images.insert(
            name.to_string(),
            Image {
                state,
                polls_left,
                fails: false,
            },
        );
    }

    fn describe(&self, name: &str) -> Result<Reply, Error> {
        let mut inner = self.state.lock().expect("not poisoned");
        inner.describes += 1;
        let Some(image) = inner.images.get_mut(name) else {
            return not_found();
        };
        match image.state {
            "CREATING" if image.polls_left == 0 => {
                image.state = if image.fails {
                    "CREATE_FAILED"
                } else {
                    "CREATED"
                };
            }
            "CREATING" => image.polls_left -= 1,
            "DELETING" => {
                inner.images.remove(name);
                return not_found();
            }
            _ => {}
        }
        let state = image.state;
        let version = if state == "CREATED" {
            r#", "latestActiveImageVersion": "1""#
        } else {
            ""
        };
        reply(
            200,
            format!(
                r#"{{"imageArn": "{}", "name": "{name}", "state": "{state}"{version},
                    "createdAt": 1754524800, "updatedAt": 1754528400, "tags": {{}}}}"#,
                arn(name)
            ),
        )
    }

    fn create(&self, body: &[u8]) -> Result<Reply, Error> {
        let body: serde_json::Value = serde_json::from_slice(body).expect("a JSON body");
        let name = body["name"].as_str().expect("a name").to_string();
        let mut inner = self.state.lock().expect("not poisoned");
        if inner.images.contains_key(&name) {
            inner.refused_creates += 1;
            return reply(
                409,
                r#"{"message": "An image with this name already exists"}"#.to_string(),
            );
        }
        inner.accepted_creates += 1;
        inner.events.push("create");
        inner.create_uris.push(
            body["codeArtifact"]["uri"]
                .as_str()
                .expect("a uri")
                .to_string(),
        );
        let polls_left = inner.build_polls;
        inner.images.insert(
            name.clone(),
            Image {
                state: "CREATING",
                polls_left,
                fails: false,
            },
        );
        reply(
            201,
            format!(
                r#"{{"imageArn": "{}", "name": "{name}", "state": "CREATING",
                    "createdAt": 1754524800,
                    "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                    "buildRoleArn": "{ROLE}", "codeArtifact": {{"uri": "s3://b/k"}},
                    "imageVersion": "1"}}"#,
                arn(&name)
            ),
        )
    }

    fn delete(&self, name: &str) -> Result<Reply, Error> {
        let mut inner = self.state.lock().expect("not poisoned");
        match inner.images.get_mut(name) {
            None => not_found(),
            Some(image) if image.state == "CREATING" => reply(
                409,
                r#"{"message": "The image is being created"}"#.to_string(),
            ),
            Some(image) => {
                image.state = "DELETING";
                inner.deletes += 1;
                inner.events.push("delete");
                reply(
                    200,
                    format!(
                        r#"{{"imageIdentifier": "{}", "state": "DELETING"}}"#,
                        arn(name)
                    ),
                )
            }
        }
    }
}

impl Transport for Platform {
    fn send(
        &self,
        call: Call,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Reply, Error>> + Send + '_>>
    {
        Box::pin(async move {
            let name = image_name_of(&call.path);
            let answer = match call.operation {
                "GetMicrovmImage" => {
                    let answer = self.describe(&name);
                    self.moved.notify_waiters();
                    answer
                }
                "CreateMicrovmImage" => {
                    loop {
                        let wanted = {
                            let inner = self.state.lock().expect("not poisoned");
                            inner.describes >= inner.hold_creates_until
                        };
                        if wanted {
                            break;
                        }
                        self.moved.notified().await;
                    }
                    self.create(call.body.as_deref().unwrap_or_default())
                }
                "DeleteMicrovmImage" => self.delete(&name),
                "ListMicrovmImageVersions" => reply(
                    200,
                    format!(
                        r#"{{"items": [{{
                            "baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
                            "buildRoleArn": "{ROLE}", "codeArtifact": {{"uri": "s3://b/k"}},
                            "imageArn": "{}", "imageVersion": "1", "state": "SUCCESSFUL",
                            "status": "ACTIVE", "createdAt": 1754524800}}]}}"#,
                        arn(&name)
                    ),
                ),
                other => panic!("the fake platform does not answer {other}"),
            };
            // Let a sibling caller interleave at every call, as a network round trip would.
            tokio::task::yield_now().await;
            answer
        })
    }
}

/// Time that passes only when slept through, and lets the other caller run meanwhile.
#[derive(Debug, Default)]
struct InstantClock(Mutex<Duration>);

impl Clock for InstantClock {
    fn elapsed(&self) -> Duration {
        *self.0.lock().expect("not poisoned")
    }

    fn sleep(
        &self,
        duration: Duration,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        *self.0.lock().expect("not poisoned") += duration;
        Box::pin(tokio::task::yield_now())
    }
}

// ── STS and S3 ──────────────────────────────────────────────────────────────

#[derive(Debug, Default)]
struct Services {
    account_calls: AtomicUsize,
    puts: Mutex<Vec<(String, String, Vec<u8>)>>,
}

impl BuildServices for Services {
    fn caller_account(&self) -> BoxFuture<'_, Result<String, Error>> {
        self.account_calls.fetch_add(1, Ordering::SeqCst);
        Box::pin(async { Ok(ACCOUNT.to_string()) })
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
            .push((bucket.to_string(), key.to_string(), bytes));
        Box::pin(async { Ok(()) })
    }
}

// ── the world ───────────────────────────────────────────────────────────────

/// A scratch task directory, removed on drop.
#[derive(Debug)]
struct Scratch(std::path::PathBuf);

impl Scratch {
    fn new() -> Self {
        let mut bytes = [0u8; 8];
        getrandom_fill(&mut bytes);
        let dir = std::env::temp_dir().join(format!("microvms-bdd-ensure-{}", hex(&bytes)));
        std::fs::create_dir_all(&dir).expect("scratch dir");
        Self(dir)
    }

    fn write(&self, name: &str, bytes: &[u8]) {
        let path = self.0.join(name);
        std::fs::create_dir_all(path.parent().expect("a parent")).expect("dirs");
        std::fs::write(path, bytes).expect("write");
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn getrandom_fill(bytes: &mut [u8]) {
    getrandom::fill(bytes).expect("randomness");
}

fn hex(bytes: &[u8]) -> String {
    const_hex::encode(bytes)
}

#[derive(Debug, World)]
#[world(init = Self::new)]
struct Ensure {
    dir: Scratch,
    platform: Arc<Platform>,
    services: Arc<Services>,
    sandbox: Option<Sandbox>,
    results: Vec<Result<EnsuredImage, Error>>,
    names: Vec<String>,
}

impl Ensure {
    fn new() -> Self {
        Self {
            dir: Scratch::new(),
            platform: Platform::with(2),
            services: Arc::new(Services::default()),
            sandbox: None,
            results: Vec::new(),
            names: Vec::new(),
        }
    }

    fn plane(&self) -> ControlPlane {
        ControlPlane::with_transport(
            self.platform.clone(),
            Region::UsEast1,
            Arc::new(InstantClock::default()),
        )
    }

    fn new_sandbox(&self) -> Sandbox {
        Sandbox::with_control_plane(self.plane()).with_build_services(self.services.clone())
    }

    fn request(&self, size: Option<u32>, force: bool) -> EnsureImageRequest {
        let task = std::fs::read_to_string(self.dir.0.join("Dockerfile")).expect("a Dockerfile");
        let dockerfile = wrap_dockerfile(&task, &WrapOptions::default()).expect("wraps");
        let mut request =
            EnsureImageRequest::new("task", b"\x7fELF daemon".to_vec(), dockerfile, BUCKET, ROLE);
        request.context = Some(BuildContext::from_dir(&self.dir.0).expect("a readable context"));
        request.s3_key_prefix = Some("harbor".to_string());
        request.force = force;
        if let Some(mib) = size {
            request.size = SizeClass::from_baseline_mib(mib).expect("a class");
        }
        request
    }

    fn name(&self) -> String {
        prepare(&self.plane(), self.request(None, false))
            .expect("prepares")
            .name
    }

    async fn ensure(&mut self, force: bool) {
        let request = self.request(None, force);
        let mut sandbox = self.sandbox.take().unwrap_or_else(|| self.new_sandbox());
        let result = sandbox.ensure_image(request).await;
        self.sandbox = Some(sandbox);
        self.results.push(result);
    }

    fn result(&self, index: usize) -> &EnsuredImage {
        match &self.results[index] {
            Ok(ensured) => ensured,
            Err(error) => panic!("ensure_image #{index} failed: {error}"),
        }
    }

    fn last(&self) -> &EnsuredImage {
        self.result(self.results.len() - 1)
    }

    fn uploaded_names(&self) -> Vec<String> {
        let puts = self.services.puts.lock().expect("not poisoned");
        let (_, _, bytes) = puts.last().expect("an upload");
        let archive = zip::ZipArchive::new(std::io::Cursor::new(bytes.clone())).expect("a zip");
        archive.file_names().map(str::to_string).collect()
    }

    fn platform(&self) -> std::sync::MutexGuard<'_, PlatformState> {
        self.platform.state.lock().expect("not poisoned")
    }
}

// ── steps ───────────────────────────────────────────────────────────────────

#[given(expr = "a task directory with a Dockerfile {string} and a file {string}")]
fn task_directory(world: &mut Ensure, from: String, file: String) {
    world.dir.write(
        "Dockerfile",
        format!("{from}\nWORKDIR /app\nCOPY . /app/\n").as_bytes(),
    );
    world.dir.write(&file, b"print('hello')\n");
}

#[given(expr = "the task file {string} holds {string}")]
fn task_file(world: &mut Ensure, name: String, text: String) {
    world.dir.write(&name, format!("{text}\n").as_bytes());
}

#[when(expr = "the task file {string} is changed")]
fn task_file_changed(world: &mut Ensure, name: String) {
    world.dir.write(&name, b"print('changed')\n");
}

#[given(expr = "the task has a symlink {string} to {string}")]
fn task_symlink(world: &mut Ensure, link: String, target: String) {
    #[cfg(unix)]
    std::os::unix::fs::symlink(target, world.dir.0.join(link)).expect("symlink");
    #[cfg(not(unix))]
    {
        // Windows symlinks need a privilege CI runners lack; the scenario's other checks
        // still run, and the warning check below is skipped the same way.
        let _ = (world, link, target);
    }
}

#[given("the platform is building the task's image")]
fn platform_building(world: &mut Ensure) {
    let name = world.name();
    world.platform.seed(&name, "CREATING");
}

#[given(expr = "the platform holds the task's image as {word}")]
fn platform_holds(world: &mut Ensure, state: String) {
    let name = world.name();
    let state: &'static str = match state.as_str() {
        "CREATED" => "CREATED",
        "CREATE_FAILED" => "CREATE_FAILED",
        other => panic!("no seed for {other}"),
    };
    world.platform.seed(&name, state);
}

#[when("the name for the task is derived")]
fn name_derived(world: &mut Ensure) {
    let name = world.name();
    world.names.push(name);
}

#[when(expr = "the name for the task at {int} MiB is derived")]
fn name_derived_sized(world: &mut Ensure, mib: u32) {
    let name = prepare(&world.plane(), world.request(Some(mib), false))
        .expect("prepares")
        .name;
    world.names.push(name);
}

#[when("the image is ensured")]
async fn ensured(world: &mut Ensure) {
    world.ensure(false).await;
}

#[when("the image is ensured with force")]
async fn ensured_forced(world: &mut Ensure) {
    world.ensure(true).await;
}

#[when("the image is ensured again on the same sandbox")]
async fn ensured_same(world: &mut Ensure) {
    world.ensure(false).await;
}

#[when("the image is ensured again on a new sandbox")]
async fn ensured_new(world: &mut Ensure) {
    world.sandbox = None;
    world.ensure(false).await;
}

async fn race(world: &mut Ensure, polls: u32) {
    {
        let mut inner = world.platform();
        inner.build_polls = polls;
        inner.hold_creates_until = 2;
    }
    let mut first = world.new_sandbox();
    let mut second = world.new_sandbox();
    let (a, b) = tokio::join!(
        first.ensure_image(world.request(None, false)),
        second.ensure_image(world.request(None, false)),
    );
    world.results.push(a);
    world.results.push(b);
}

#[when("the image is ensured by two sandboxes at once")]
async fn ensured_race(world: &mut Ensure) {
    race(world, 2).await;
}

#[when(expr = "the image is ensured by two sandboxes at once, the build taking {int} polls")]
async fn ensured_race_slow(world: &mut Ensure, polls: u32) {
    race(world, polls).await;
}

#[then("the two names differ")]
fn names_differ(world: &mut Ensure) {
    assert_eq!(world.names.len(), 2);
    assert_ne!(world.names[0], world.names[1], "IMAGE-6");
}

#[then("each name is the prefix and twelve hex characters")]
fn names_shaped(world: &mut Ensure) {
    for name in &world.names {
        let hash = name.strip_prefix("task-").expect("the prefix");
        assert_eq!(hash.len(), 12, "{name}");
        assert!(hash.chars().all(|c| c.is_ascii_hexdigit()), "{name}");
    }
}

#[then(expr = "the uploaded artifact holds {string}")]
fn artifact_holds(world: &mut Ensure, name: String) {
    let names = world.uploaded_names();
    assert!(names.contains(&name), "IMAGE-7: {names:?}");
}

#[then(expr = "the uploaded artifact does not hold {string}")]
fn artifact_lacks(world: &mut Ensure, name: String) {
    let names = world.uploaded_names();
    assert!(!names.contains(&name), "IMAGE-7: {names:?}");
}

#[then(expr = "the warnings name {string}")]
fn warnings_name(world: &mut Ensure, name: String) {
    if cfg!(unix) {
        let warnings = world.last().warnings.join("\n");
        assert!(warnings.contains(&name), "IMAGE-7: {warnings}");
    }
}

#[then("the uploaded artifact's Dockerfile ends with the agentd stanza")]
fn artifact_dockerfile(world: &mut Ensure) {
    use std::io::Read as _;
    let puts = world.services.puts.lock().expect("not poisoned");
    let (_, _, bytes) = puts.last().expect("an upload");
    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(bytes.clone())).expect("a zip");
    let mut dockerfile = String::new();
    archive
        .by_name("Dockerfile")
        .expect("a Dockerfile entry")
        .read_to_string(&mut dockerfile)
        .expect("reads");
    assert!(
        dockerfile.ends_with("ENTRYPOINT []\nCMD [\"/agentd\"]\n"),
        "{dockerfile}"
    );
    assert!(
        dockerfile.starts_with("FROM python:3.12-slim\n"),
        "{dockerfile}"
    );
}

#[then(expr = "the artifact was uploaded to {string}")]
fn uploaded_to(world: &mut Ensure, key: String) {
    let name = world.last().image.name.clone();
    let key = key.replace("<name>", &name);
    let puts = world.services.puts.lock().expect("not poisoned");
    let (bucket, got, _) = puts.last().expect("an upload");
    assert_eq!(
        (bucket.as_str(), got.as_str()),
        (BUCKET, key.as_str()),
        "IMAGE-8"
    );
}

#[then("the create named that artifact")]
fn create_named(world: &mut Ensure) {
    let uri = world.last().artifact_uri.clone();
    assert_eq!(world.platform().create_uris, [uri], "IMAGE-8");
}

#[then(expr = "the account was looked up {int} time(s)")]
fn account_calls(world: &mut Ensure, count: usize) {
    assert_eq!(
        world.services.account_calls.load(Ordering::SeqCst),
        count,
        "IMAGE-8"
    );
}

#[then("the first call built the image")]
fn first_built(world: &mut Ensure) {
    assert!(!world.result(0).reused, "IMAGE-9");
}

#[then("the second call reused it with no upload and no create")]
fn second_reused(world: &mut Ensure) {
    let second = world.result(1);
    assert!(second.reused, "IMAGE-9");
    assert!(!second.uploaded, "IMAGE-9");
    assert_eq!(world.platform().accepted_creates, 1, "IMAGE-9");
    assert_eq!(world.services.puts.lock().expect("not poisoned").len(), 1);
}

#[then("the call reused the image")]
fn call_reused(world: &mut Ensure) {
    let last = world.last();
    assert!(last.reused, "IMAGE-9");
    assert_eq!(last.image.state, "CREATED");
}

#[then("the call built the image")]
fn call_built(world: &mut Ensure) {
    let last = world.last();
    assert!(!last.reused, "IMAGE-10");
    assert_eq!(last.image.state, "CREATED");
}

#[then("the platform saw no create from the call")]
fn no_create(world: &mut Ensure) {
    let inner = world.platform();
    assert_eq!(inner.accepted_creates + inner.refused_creates, 0, "IMAGE-9");
}

#[then("the platform deleted the image before the create")]
fn deleted_first(world: &mut Ensure) {
    assert_eq!(world.platform().events, ["delete", "create"], "IMAGE-10");
}

#[then("the platform deleted nothing")]
fn deleted_nothing(world: &mut Ensure) {
    assert_eq!(world.platform().deletes, 0, "IMAGE-10");
}

#[then("exactly one call built the image and the other reused it")]
fn one_built(world: &mut Ensure) {
    let reused: Vec<bool> = (0..2).map(|i| world.result(i).reused).collect();
    assert_eq!(
        reused.iter().filter(|r| !**r).count(),
        1,
        "IMAGE-11: {reused:?}"
    );
}

#[then(expr = "the platform accepted {int} create and refused {int}")]
fn creates(world: &mut Ensure, accepted: usize, refused: usize) {
    let inner = world.platform();
    assert_eq!(
        (inner.accepted_creates, inner.refused_creates),
        (accepted, refused),
        "IMAGE-11"
    );
}

#[then("both calls returned the same ready image")]
fn same_image(world: &mut Ensure) {
    let (a, b) = (world.result(0), world.result(1));
    assert_eq!(a.image.identifier, b.image.identifier, "IMAGE-11");
    assert_eq!(a.image.version, b.image.version);
    for ensured in [a, b] {
        assert_eq!(
            ensured.image.state, "CREATED",
            "IMAGE-11: only a ready image"
        );
    }
}

fn main() {
    let features = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/features/ensure_image.feature"
    );
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_ensure ensure_image".contains(filter))
    {
        return;
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .expect("a runtime");
    runtime.block_on(async {
        let cucumber = Ensure::cucumber().fail_on_skipped();
        match std::env::var_os("CUCUMBER_JUNIT") {
            Some(path) => {
                let report = std::fs::File::create(&path).expect("the JUnit report file");
                cucumber
                    .with_writer(
                        writer::Basic::raw(std::io::stdout(), writer::Coloring::Never, 0)
                            .summarized()
                            .tee::<Ensure, _>(writer::JUnit::for_tee(report, 0))
                            .normalized(),
                    )
                    .with_cli(cucumber::cli::Opts::<_, _, _, cucumber::cli::Empty>::default())
                    .run_and_exit(features)
                    .await;
            }
            None => {
                cucumber
                    .with_cli(cucumber::cli::Opts::<_, _, _, cucumber::cli::Empty>::default())
                    .run_and_exit(features)
                    .await;
            }
        }
    });
}
