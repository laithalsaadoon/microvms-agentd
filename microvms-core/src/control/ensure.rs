// SPDX-License-Identifier: Apache-2.0
//! Content-addressed build-or-reuse for an image (#221). Not yet implemented.

use std::collections::BTreeMap;
use std::time::Duration;

use super::artifact::BaseImage;
use super::context::BuildContext;
use super::image::Image;
use super::services::BuildServices;
use super::{ControlPlane, ops};
use crate::error::{Error, ErrorKind};
use crate::sizing::SizeClass;

/// What a describe of the ensured image found.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Found {
    Absent,
    Building,
    Ready,
    Failed,
    Deleting,
}

impl Found {
    /// The reading of a `MicrovmImageState`. Not yet implemented.
    pub fn from_state(_state: &str) -> Self {
        Found::Building
    }
}

/// What `ensure_image` does next with what a describe found.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Plan {
    Reuse,
    Wait,
    Delete,
    AwaitAbsent,
    Build,
}

/// The decision table. Not yet implemented.
pub fn plan(_found: Found, _force: bool) -> Plan {
    Plan::Build
}

/// The image name for a prefix and an identity hash. Not yet implemented.
pub fn ensured_image_name(_prefix: &str, _identity_hash: &str) -> Result<String, Error> {
    Err(Error::new(
        ErrorKind::Unexpected,
        "ensured_image_name is not implemented yet (#221)",
    ))
}

/// The identity an ensured image is named for. Not yet implemented.
pub fn image_identity_hash(_artifact_hash: &str, _base: &BaseImage, _size: SizeClass) -> String {
    String::new()
}

/// The artifact's S3 key. Not yet implemented.
pub fn artifact_key(_key_prefix: Option<&str>, _name: &str) -> String {
    String::new()
}

/// Everything `Sandbox::ensure_image` needs.
#[derive(Clone, Debug)]
pub struct EnsureImageRequest {
    /// The stem of the image name; the name is `<prefix>-<hash12>`.
    pub name_prefix: String,
    /// The daemon binary's bytes.
    pub binary: Vec<u8>,
    /// The Dockerfile, usually `wrap_dockerfile`'s output.
    pub dockerfile: String,
    /// The files the Dockerfile may `COPY`, or `None` for none.
    pub context: Option<BuildContext>,
    /// The bucket the artifact is uploaded to.
    pub s3_bucket: String,
    /// A key prefix inside the bucket, or `None` for the bucket root.
    pub s3_key_prefix: Option<String>,
    /// The build role.
    pub build_role_arn: String,
    /// The size class the image is created with.
    pub size: SizeClass,
    /// The base image, or `None` for `BaseImage::from_dockerfile(dockerfile)`.
    pub base_image: Option<BaseImage>,
    /// Delete what exists under the name and build afresh.
    pub force: bool,
    /// Tags for a created image.
    pub tags: BTreeMap<String, String>,
    /// The build wait's deadline, or `None` for the default.
    pub wait_timeout: Option<Duration>,
}

impl EnsureImageRequest {
    /// A request with the defaults.
    pub fn new(
        name_prefix: impl Into<String>,
        binary: Vec<u8>,
        dockerfile: impl Into<String>,
        s3_bucket: impl Into<String>,
        build_role_arn: impl Into<String>,
    ) -> Self {
        Self {
            name_prefix: name_prefix.into(),
            binary,
            dockerfile: dockerfile.into(),
            context: None,
            s3_bucket: s3_bucket.into(),
            s3_key_prefix: None,
            build_role_arn: build_role_arn.into(),
            size: SizeClass::DEFAULT,
            base_image: None,
            force: false,
            tags: BTreeMap::new(),
            wait_timeout: None,
        }
    }
}

/// What `Sandbox::ensure_image` returns.
#[derive(Clone, Debug)]
pub struct EnsuredImage {
    /// The ready image.
    pub image: Image,
    /// True when this call's own create did not build it.
    pub reused: bool,
    /// Where the artifact is, whether or not this call uploaded it.
    pub artifact_uri: String,
    /// Whether this call uploaded the artifact.
    pub uploaded: bool,
    /// What reading the build context skipped.
    pub warnings: Vec<String>,
}

impl ControlPlane {
    /// The image under `identifier`, or `None` when there is none. Not yet implemented.
    pub async fn describe_image(
        &self,
        _identifier: &str,
    ) -> Result<Option<ops::GetMicrovmImageResponseWire>, Error> {
        Err(Error::new(
            ErrorKind::Unexpected,
            "describe_image is not implemented yet (#221)",
        ))
    }
}

/// Everything `ensure_image` decides locally, before its first call: the name, the key, the
/// artifact, and the create request, all refused-or-accepted with zero calls.
#[derive(Clone, Debug)]
pub struct Prepared {
    pub name: String,
    pub bucket: String,
    pub key: String,
    pub artifact: Vec<u8>,
    pub create: super::CreateImageRequest,
    pub force: bool,
    pub wait_timeout: Option<Duration>,
    pub warnings: Vec<String>,
}

/// The local half. Not yet implemented.
pub fn prepare(_control: &ControlPlane, _request: EnsureImageRequest) -> Result<Prepared, Error> {
    Err(Error::new(
        ErrorKind::Unexpected,
        "ensure_image is not implemented yet (#221)",
    ))
}

/// Build or reuse. Not yet implemented.
pub(crate) async fn ensure(
    _control: &ControlPlane,
    _services: &dyn BuildServices,
    _account: &str,
    _prepared: Prepared,
) -> Result<EnsuredImage, Error> {
    Err(Error::new(
        ErrorKind::Unexpected,
        "ensure_image is not implemented yet (#221)",
    ))
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use super::*;
    use crate::control::fake::{self, Answer, FakeControlPlane, TestClock};
    use crate::region::Region;
    use crate::sandbox::Sandbox;

    const ACCOUNT: &str = "123456789012";
    const BUCKET: &str = "artifact-bucket";
    const ROLE: &str = "arn:aws:iam::123456789012:role/build";

    /// STS and S3, recorded.
    #[derive(Default)]
    struct FakeServices {
        account_calls: AtomicUsize,
        puts: Mutex<Vec<(String, String, Vec<u8>)>>,
    }

    impl BuildServices for FakeServices {
        fn caller_account(&self) -> futures_util::future::BoxFuture<'_, Result<String, Error>> {
            self.account_calls.fetch_add(1, Ordering::SeqCst);
            Box::pin(async { Ok(ACCOUNT.to_string()) })
        }

        fn put_object<'a>(
            &'a self,
            bucket: &'a str,
            key: &'a str,
            bytes: Vec<u8>,
        ) -> futures_util::future::BoxFuture<'a, Result<(), Error>> {
            self.puts.lock().expect("not poisoned").push((
                bucket.to_string(),
                key.to_string(),
                bytes,
            ));
            Box::pin(async { Ok(()) })
        }
    }

    fn sandbox(fake: &Arc<FakeControlPlane>, services: &Arc<FakeServices>) -> Sandbox {
        let plane =
            ControlPlane::with_transport(fake.clone(), Region::UsEast1, Arc::new(TestClock::new()));
        Sandbox::with_control_plane(plane).with_build_services(services.clone())
    }

    fn dockerfile() -> String {
        super::super::artifact::wrap_dockerfile(
            "FROM python:3.12-slim\nWORKDIR /app\n",
            &super::super::artifact::WrapOptions::default(),
        )
        .expect("wraps")
    }

    fn request() -> EnsureImageRequest {
        let mut request =
            EnsureImageRequest::new("task", b"daemon".to_vec(), dockerfile(), BUCKET, ROLE);
        request.s3_key_prefix = Some("harbor/images".to_string());
        request
    }

    /// The name `request()` derives, computed from the public pieces.
    fn expected_name() -> String {
        let request = request();
        let base = BaseImage::from_dockerfile(&request.dockerfile).expect("a FROM");
        let hash = super::super::artifact::artifact_content_hash_with_context(
            &request.binary,
            &request.dockerfile,
            None,
            None,
        );
        ensured_image_name("task", &image_identity_hash(&hash, &base, request.size))
            .expect("a legal name")
    }

    /// `GetMicrovmImageOutput` for a usable image.
    fn ready(name: &str) -> String {
        format!(
            r#"{{"imageArn": "arn:aws:lambda:us-east-1:{ACCOUNT}:microvm-image:{name}",
                "name": "{name}", "state": "CREATED", "latestActiveImageVersion": "1",
                "createdAt": 1754524800, "updatedAt": 1754528400, "tags": {{}}}}"#
        )
    }

    fn state(name: &str, state: &str) -> Answer {
        Answer::ok(fake::get_image_response(name, state))
    }

    fn absent() -> Answer {
        Answer::failure(404, "Image not found")
    }

    fn uploads(services: &FakeServices) -> Vec<(String, String)> {
        services
            .puts
            .lock()
            .expect("not poisoned")
            .iter()
            .map(|(bucket, key, _)| (bucket.clone(), key.clone()))
            .collect()
    }

    /// **The table `model/src/image.rs` specifies**, row for row (`the_plan_table` there).
    #[test]
    fn the_plan_table() {
        for (found, force, expected) in [
            (Found::Absent, false, Plan::Build),
            (Found::Absent, true, Plan::Build),
            (Found::Ready, false, Plan::Reuse),
            (Found::Ready, true, Plan::Delete),
            (Found::Building, false, Plan::Wait),
            (Found::Building, true, Plan::Wait),
            (Found::Failed, false, Plan::Delete),
            (Found::Failed, true, Plan::Delete),
            (Found::Deleting, false, Plan::AwaitAbsent),
            (Found::Deleting, true, Plan::AwaitAbsent),
        ] {
            assert_eq!(plan(found, force), expected, "{found:?} force={force}");
        }
        for (spelling, found) in [
            ("CREATING", Found::Building),
            ("UPDATING", Found::Building),
            ("CREATED", Found::Ready),
            ("UPDATED", Found::Ready),
            ("ACTIVE", Found::Ready),
            ("CREATE_FAILED", Found::Failed),
            ("UPDATE_FAILED", Found::Failed),
            ("DELETE_FAILED", Found::Failed),
            ("DELETING", Found::Deleting),
            ("DELETED", Found::Absent),
        ] {
            assert_eq!(Found::from_state(spelling), found, "{spelling}");
        }
    }

    /// **IMAGE-6, the name.** `<prefix>-<hash12>`, the prefix reduced to the characters the
    /// service's `ImageName` admits and cut so the whole fits its 64; a prefix with nothing
    /// left is refused.
    #[test]
    fn the_name_is_the_sanitized_prefix_and_twelve_hex() {
        let hash = "0123456789abcdef".repeat(4);
        assert_eq!(
            ensured_image_name("harbor/task:1", &hash).expect("legal"),
            "harbor-task-1-0123456789ab"
        );
        let long = ensured_image_name(&"p".repeat(100), &hash).expect("legal");
        assert_eq!(long.len(), 64, "IMAGE-6: {long}");
        assert!(long.ends_with("-0123456789ab"));
        super::super::require_valid_image_name(&long).expect("the service's pattern");
        for empty in ["", "///", "   "] {
            let error = ensured_image_name(empty, &hash).expect_err("nothing left");
            assert_eq!(error.kind(), ErrorKind::InvalidArg);
        }
    }

    /// **IMAGE-6, the identity.** The base image and the size class are part of the name,
    /// because an image is created at one size on one base: two tasks with one Dockerfile and
    /// different sizes need two images.
    #[test]
    fn the_identity_covers_the_base_and_the_size() {
        let hash = "a".repeat(64);
        let base = BaseImage::al2023();
        let identity = image_identity_hash(&hash, &base, SizeClass::DEFAULT);
        assert_eq!(
            identity,
            image_identity_hash(&hash, &base, SizeClass::DEFAULT)
        );
        assert_eq!(identity.len(), 64);
        let other_size = SizeClass::from_baseline_mib(4096).expect("a class");
        assert_ne!(
            identity,
            image_identity_hash(&hash, &base, other_size),
            "IMAGE-6: size"
        );
        let other_base = BaseImage {
            name: "al2023-2".to_string(),
            ..BaseImage::al2023()
        };
        assert_ne!(
            identity,
            image_identity_hash(&hash, &other_base, SizeClass::DEFAULT)
        );
        assert_ne!(
            identity,
            image_identity_hash(&"b".repeat(64), &base, SizeClass::DEFAULT)
        );
    }

    /// **IMAGE-8, the key.** `<prefix>/<name>/artifact.zip`, with the prefix's slashes
    /// trimmed so no key carries an empty segment.
    #[test]
    fn the_artifact_key_is_content_addressed() {
        assert_eq!(
            artifact_key(Some("harbor/images"), "task-0123456789ab"),
            "harbor/images/task-0123456789ab/artifact.zip"
        );
        assert_eq!(artifact_key(Some("/harbor/"), "n"), "harbor/n/artifact.zip");
        assert_eq!(artifact_key(None, "n"), "n/artifact.zip");
        assert_eq!(artifact_key(Some(""), "n"), "n/artifact.zip");
    }

    /// **IMAGE-9, reuse.** A ready image is returned as it is: one account lookup, one
    /// describe, no upload, no create.
    #[tokio::test]
    async fn a_ready_image_is_reused_with_no_upload_and_no_create() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", Answer::ok(ready(&name)));
        let services = Arc::new(FakeServices::default());
        let ensured = sandbox(&fake, &services)
            .ensure_image(request())
            .await
            .expect("reused");
        assert!(ensured.reused, "IMAGE-9");
        assert!(!ensured.uploaded);
        assert_eq!(ensured.image.name, name);
        assert_eq!(ensured.image.version, "1");
        assert_eq!(fake.operations(), ["GetMicrovmImage"], "IMAGE-9");
        assert!(uploads(&services).is_empty());
        assert!(
            fake.paths()[0].contains(&paths_encoded(&format!(
                "arn:aws:lambda:us-east-1:{ACCOUNT}:microvm-image:{name}"
            ))),
            "IMAGE-8: the describe names the ARN built from the account: {:?}",
            fake.paths()
        );
    }

    fn paths_encoded(arn: &str) -> String {
        crate::control::transport::paths::encode_segment(arn)
    }

    /// **IMAGE-9, wait.** A build already running under the name is waited out and reused;
    /// this caller uploads and creates nothing.
    #[tokio::test]
    async fn a_running_build_is_waited_out_and_reused() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", Answer::ok(ready(&name)));
        let services = Arc::new(FakeServices::default());
        let ensured = sandbox(&fake, &services)
            .ensure_image(request())
            .await
            .expect("waited");
        assert!(ensured.reused, "IMAGE-9");
        assert_eq!(fake.call_count("CreateMicrovmImage"), 0, "IMAGE-9");
        assert!(uploads(&services).is_empty(), "IMAGE-9");
    }

    /// **IMAGE-8, the build.** No image under the name: the artifact goes to the
    /// content-addressed key, the create names that URI and the derived name, and the image
    /// this call built is not reused.
    #[tokio::test]
    async fn an_absent_image_is_uploaded_to_its_key_and_built() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", absent())
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", Answer::ok(ready(&name)))
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response(&name)),
            );
        let services = Arc::new(FakeServices::default());
        let mut request = request();
        request
            .tags
            .insert("harbor:task".to_string(), "t".to_string());
        let ensured = sandbox(&fake, &services)
            .ensure_image(request)
            .await
            .expect("built");
        assert!(!ensured.reused);
        assert!(ensured.uploaded);
        let key = format!("harbor/images/{name}/artifact.zip");
        assert_eq!(
            uploads(&services),
            [(BUCKET.to_string(), key.clone())],
            "IMAGE-8"
        );
        assert_eq!(ensured.artifact_uri, format!("s3://{BUCKET}/{key}"));
        let body = fake.first_body("CreateMicrovmImage");
        assert_eq!(body["name"], name.as_str());
        assert_eq!(body["codeArtifact"]["uri"], ensured.artifact_uri.as_str());
        assert_eq!(body["tags"]["harbor:task"], "t");
        assert_eq!(
            body["baseImageArn"], "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
            "the derived base keeps the managed name"
        );
        assert_eq!(services.account_calls.load(Ordering::SeqCst), 1);

        // The uploaded bytes are the artifact the request describes.
        let (_, _, bytes) = services.puts.lock().expect("not poisoned")[0].clone();
        let archive = zip::ZipArchive::new(std::io::Cursor::new(bytes)).expect("a zip");
        assert_eq!(archive.len(), 2);
    }

    /// **IMAGE-10, a failure.** A failed image is deleted, the name awaited free, and then
    /// rebuilt; the delete comes before the create.
    #[tokio::test]
    async fn a_failed_image_is_deleted_then_rebuilt() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", state(&name, "CREATE_FAILED"))
            .answer("GetMicrovmImage", absent())
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", Answer::ok(ready(&name)))
            .answer(
                "ListMicrovmImageVersions",
                Answer::ok(fake::list_versions_response("1")),
            )
            .answer(
                "DeleteMicrovmImage",
                Answer::ok(fake::delete_image_response()),
            )
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response(&name)),
            );
        let services = Arc::new(FakeServices::default());
        let ensured = sandbox(&fake, &services)
            .ensure_image(request())
            .await
            .expect("rebuilt");
        assert!(!ensured.reused);
        let operations = fake.operations();
        let deleted = operations
            .iter()
            .position(|op| *op == "DeleteMicrovmImage")
            .expect("IMAGE-10: deleted");
        let created = operations
            .iter()
            .position(|op| *op == "CreateMicrovmImage")
            .expect("rebuilt");
        assert!(deleted < created, "IMAGE-10: {operations:?}");
    }

    /// **IMAGE-10, force.** A ready image is rebuilt only when the caller forces it.
    #[tokio::test]
    async fn a_ready_image_is_rebuilt_only_under_force() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", Answer::ok(ready(&name)))
            .answer("GetMicrovmImage", absent())
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", Answer::ok(ready(&name)))
            .answer(
                "ListMicrovmImageVersions",
                Answer::ok(fake::list_versions_response("1")),
            )
            .answer(
                "DeleteMicrovmImage",
                Answer::ok(fake::delete_image_response()),
            )
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response(&name)),
            );
        let services = Arc::new(FakeServices::default());
        let mut forced = request();
        forced.force = true;
        let ensured = sandbox(&fake, &services)
            .ensure_image(forced)
            .await
            .expect("rebuilt");
        assert!(!ensured.reused, "IMAGE-10");
        assert_eq!(fake.call_count("DeleteMicrovmImage"), 1, "IMAGE-10");
        assert_eq!(fake.call_count("CreateMicrovmImage"), 1);
    }

    /// **IMAGE-10, a deletion under way.** The name is awaited free, then built.
    #[tokio::test]
    async fn a_deletion_under_way_is_awaited_then_built() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", state(&name, "DELETING"))
            .answer("GetMicrovmImage", absent())
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", Answer::ok(ready(&name)))
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response(&name)),
            );
        let services = Arc::new(FakeServices::default());
        let ensured = sandbox(&fake, &services)
            .ensure_image(request())
            .await
            .expect("built");
        assert!(!ensured.reused);
        assert_eq!(
            fake.call_count("DeleteMicrovmImage"),
            0,
            "not this caller's delete"
        );
    }

    /// **IMAGE-11, the race.** The create is refused because a sibling created the name
    /// first: describe again, wait for the sibling's build, and return it as reused.
    #[tokio::test]
    async fn a_refused_create_joins_the_winners_build() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", absent())
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer("GetMicrovmImage", Answer::ok(ready(&name)))
            .answer(
                "CreateMicrovmImage",
                Answer::failure(409, "An image with this name already exists"),
            );
        let services = Arc::new(FakeServices::default());
        let ensured = sandbox(&fake, &services)
            .ensure_image(request())
            .await
            .expect("IMAGE-11: joined");
        assert!(
            ensured.reused,
            "IMAGE-11: this caller's create did not build it"
        );
        assert_eq!(
            ensured.image.state, "CREATED",
            "IMAGE-11: returned only once ready"
        );
        assert_eq!(fake.call_count("CreateMicrovmImage"), 1);
    }

    /// **IMAGE-11, not a race.** A refused create with nothing under the name afterwards is
    /// the create's own failure, surfaced as it came.
    #[tokio::test]
    async fn a_refused_create_with_no_winner_surfaces_the_refusal() {
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", absent()).answer(
            "CreateMicrovmImage",
            Answer::failure(400, "buildRoleArn is invalid"),
        );
        let services = Arc::new(FakeServices::default());
        let error = sandbox(&fake, &services)
            .ensure_image(request())
            .await
            .expect_err("no winner");
        assert!(
            error.to_string().contains("buildRoleArn is invalid"),
            "{error}"
        );
    }

    /// **IMAGE-8, once per sandbox.** Two ensures on one sandbox make one account lookup.
    #[tokio::test]
    async fn the_account_is_resolved_once_per_sandbox() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", Answer::ok(ready(&name)));
        let services = Arc::new(FakeServices::default());
        let mut sandbox = sandbox(&fake, &services);
        sandbox.ensure_image(request()).await.expect("first");
        sandbox.ensure_image(request()).await.expect("second");
        assert_eq!(services.account_calls.load(Ordering::SeqCst), 1, "IMAGE-8");
    }

    /// Every local refusal costs zero calls — not the account lookup, not a describe, not
    /// the upload.
    #[tokio::test]
    async fn a_locally_refused_request_makes_no_call() {
        let fake = Arc::new(FakeControlPlane::new());
        let services = Arc::new(FakeServices::default());
        let mut sandbox = sandbox(&fake, &services);
        for (mutate, cause) in [
            (
                Box::new(|r: &mut EnsureImageRequest| {
                    r.dockerfile = "FROM x\nENTRYPOINT [\"/bin/sh\"]\nCMD [\"/agentd\"]\n".into()
                }) as Box<dyn Fn(&mut EnsureImageRequest)>,
                "ENTRYPOINT",
            ),
            (Box::new(|r| r.name_prefix = "//".into()), "prefix"),
            (Box::new(|r| r.s3_bucket = String::new()), "bucket"),
            (
                Box::new(|r| r.build_role_arn = "not-an-arn".into()),
                "buildRoleArn",
            ),
            (
                Box::new(|r| r.s3_key_prefix = Some("a\nb".into())),
                "key prefix",
            ),
        ] {
            let mut request = request();
            mutate(&mut request);
            let error = sandbox.ensure_image(request).await.expect_err(cause);
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
            assert!(error.to_string().contains(cause), "{cause}: {error}");
        }
        assert!(fake.calls().is_empty());
        assert_eq!(services.account_calls.load(Ordering::SeqCst), 0);
        assert!(uploads(&services).is_empty());
    }

    /// The build wait honours `wait_timeout`: a build that never settles is a timeout at the
    /// caller's deadline, not the 45-minute default.
    #[tokio::test]
    async fn the_wait_timeout_bounds_the_build_wait() {
        let name = expected_name();
        let fake = Arc::new(FakeControlPlane::new());
        fake.answer("GetMicrovmImage", absent())
            .answer("GetMicrovmImage", state(&name, "CREATING"))
            .answer(
                "CreateMicrovmImage",
                Answer::created(fake::create_image_response(&name)),
            )
            .answer(
                "ListMicrovmImageBuilds",
                Answer::ok(fake::list_builds_response("IN_PROGRESS")),
            )
            .answer(
                "ListMicrovmImageVersions",
                Answer::ok(fake::list_versions_response("1")),
            );
        let services = Arc::new(FakeServices::default());
        let mut request = request();
        request.wait_timeout = Some(Duration::from_secs(60));
        let error = sandbox(&fake, &services)
            .ensure_image(request)
            .await
            .expect_err("never settles");
        assert_eq!(error.kind(), ErrorKind::Timeout, "{error}");
        assert!(
            fake.call_count("GetMicrovmImage") < 10,
            "bounded by 60s, not 45 minutes"
        );
    }
}
