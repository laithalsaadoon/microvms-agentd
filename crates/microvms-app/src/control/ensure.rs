// SPDX-License-Identifier: Apache-2.0
//! Content-addressed build-or-reuse for an image: what `Sandbox::ensure_image` does (#221).
//!
//! A harness with many concurrent trials of one task needs one image for them all: the one
//! already built, the one a sibling trial is building, or — when there is none — one it
//! builds itself. This module is that composition, from build inputs to a usable image:
//!
//! 1. **Name** (IMAGE-6). `<prefix>-<hash12>`, the hash over the artifact's inputs (the
//!    daemon, the Dockerfile, the build context — [`artifact_content_hash_with_context`])
//!    plus the base image, the size class and every field the service sets only when it
//!    creates the image (identity repair, `inherit_workdir`, the hook timeouts, the log
//!    destination), because an image is created on one base at one size with those fixed.
//!    Equal inputs name one image; any changed input names a fresh one, which is what keeps a
//!    stale snapshot, or one built without a field the caller asked for, from being served
//!    under a reused name.
//! 2. **Artifact** (IMAGE-7). The Dockerfile, the daemon, and the context, zipped with fixed
//!    dates and modes, so equal inputs are one object at one content-addressed key.
//! 3. **ARN and upload** (IMAGE-8). The service takes image ARNs, not names, so the ARN is
//!    built from the caller's account, looked up once per sandbox
//!    ([`super::services::BuildServices::caller_account`]). The artifact goes to
//!    `s3://<bucket>/<prefix>/<name>/artifact.zip`, and only when a build is needed.
//! 4. **Decide** (IMAGE-9, IMAGE-10). After each describe, [`plan`] says what to do: reuse a
//!    ready image, wait for one building, delete a failed one (or, under `force`, any one)
//!    and wait for the name to be free, or build.
//! 5. **Race** (IMAGE-11). Two trials that both find the name free both create; the service
//!    accepts one and refuses the other. The loser describes again and waits for the winner's
//!    build, returning it only once it is ready, and marks it reused.
//!
//! `crates/model/src/image.rs` is this module as a Stateright model: two concurrent callers against
//! a platform that moves the image through its states, checked over every interleaving. The
//! decision table there is [`plan`]'s, row for row.
//!
//! # Everything local happens before the first call
//!
//! [`prepare`] derives the name, the key, the artifact, and the create request, and runs the
//! create call's preflight, with zero calls. A request this client refuses costs nothing: not
//! the account lookup, not a describe, not the upload.
//!
//! # Timeouts
//!
//! The build wait is the one `build` uses — [`WaitOpts::default`], 45 minutes with a
//! four-minute stall probe — or the caller's `wait_timeout`. The wait for a deleted name to
//! free up is [`DEFAULT_DELETE_TIMEOUT`].

use std::collections::BTreeMap;
use std::time::Duration;

use super::artifact::{BaseImage, artifact_content_hash_with_context, build_artifact_with_context};
use super::context::BuildContext;
use super::image::{DEFAULT_BUILD_TIMEOUT, Image, WaitOpts};
use super::services::BuildServices;
use super::transport::{Call, paths, send_accepting};
use super::{ControlPlane, CreateImageRequest, ops};
use crate::error::{Error, ErrorKind};
use crate::sizing::SizeClass;

/// How long a deleted image may take to free its name before `ensure_image` gives up.
///
/// Five minutes, the bound a harness provider measured for the same wait; a deletion that
/// has not finished by then is reported rather than waited on for the build's 45.
pub const DEFAULT_DELETE_TIMEOUT: Duration = Duration::from_secs(300);

/// The longest name the service's `ImageName` admits.
const MAX_IMAGE_NAME: usize = 64;

/// The hex characters of the identity hash in a name.
const NAME_HASH_LEN: usize = 12;

/// What a describe of the ensured image found.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Found {
    /// No image under the name.
    Absent,
    /// `CREATING` or `UPDATING`, or a state this client does not know, which is waited on.
    Building,
    /// `CREATED` or `UPDATED` (or a tolerated ready spelling): usable.
    Ready,
    /// `CREATE_FAILED`, `UPDATE_FAILED`, or `DELETE_FAILED`.
    Failed,
    /// `DELETING`.
    Deleting,
}

impl Found {
    /// The reading of a `MicrovmImageState`.
    pub fn from_state(state: &str) -> Self {
        if Image::is_ready(state) {
            Found::Ready
        } else if Image::is_failed(state) {
            Found::Failed
        } else if state == "DELETING" {
            Found::Deleting
        } else if state == "DELETED" {
            Found::Absent
        } else {
            Found::Building
        }
    }
}

/// What `ensure_image` does next with what a describe found.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Plan {
    /// Return the ready image; nothing is uploaded or created.
    Reuse,
    /// Wait for the running build to settle.
    Wait,
    /// Delete the image, wait until the name is free, then build.
    Delete,
    /// A deletion is under way: wait until the name is free, then build.
    AwaitAbsent,
    /// Upload the artifact and create the image.
    Build,
}

/// The decision `ensure_image` makes after each describe (IMAGE-9, IMAGE-10).
///
/// A ready image is reused unless the caller forces a rebuild; a building one is waited on
/// either way, because the service refuses to delete an image in `CREATING` — a forced
/// caller deletes what the build settles to. A failed image is deleted whether or not the
/// caller forced, because the name is content-addressed: the only way to a fresh build under
/// it is to free it first. `crates/model/src/image.rs` checks this table against two concurrent
/// callers, and `the_plan_table` holds the two equal.
pub fn plan(found: Found, force: bool) -> Plan {
    match (found, force) {
        (Found::Absent, _) => Plan::Build,
        (Found::Ready, false) => Plan::Reuse,
        (Found::Ready, true) | (Found::Failed, _) => Plan::Delete,
        (Found::Building, _) => Plan::Wait,
        (Found::Deleting, _) => Plan::AwaitAbsent,
    }
}

/// The image name for a prefix and an identity hash: `<prefix>-<hash12>` (IMAGE-6).
///
/// The prefix is reduced to what the service's `ImageName` pattern admits
/// (`[a-zA-Z0-9-_]`), every other character becoming `-`, trimmed of `-` at both ends, and
/// cut so the whole name fits the pattern's 64 characters. A prefix with nothing left is
/// refused: the name would be the hash alone, and nothing in the account would say what it
/// is for.
pub fn ensured_image_name(prefix: &str, identity_hash: &str) -> Result<String, Error> {
    let sanitized: String = prefix
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect();
    let room = MAX_IMAGE_NAME - 1 - NAME_HASH_LEN;
    let cut: String = sanitized.trim_matches('-').chars().take(room).collect();
    let cut = cut.trim_end_matches('-');
    if cut.is_empty() {
        return Err(Error::invalid_arg(format!(
            "the image name prefix {prefix:?} has no character the service's ImageName pattern \
             admits ([a-zA-Z0-9-_]), so the name would be a bare hash. Pass a prefix that says \
             what the image is for, such as the task's name."
        )));
    }
    let hash: String = identity_hash.chars().take(NAME_HASH_LEN).collect();
    Ok(format!("{cut}-{hash}"))
}

/// The identity an ensured image is named for (IMAGE-6): the artifact's content hash, the
/// base image's name (what `baseImageArn` names), and the size class's baseline, for an image
/// created with [`CreateImageRequest::new`]'s create-only fields.
///
/// Length-prefixed and tagged, so no two field boundaries can collide.
pub fn image_identity_hash(artifact_hash: &str, base: &BaseImage, size: SizeClass) -> String {
    pinned_identity_hash(artifact_hash, base, None, size)
}

/// [`image_identity_hash`] with the base version a build pins (#258): a pinned base builds
/// on that version whatever the service's default later becomes, so the pin is identity.
///
/// The create-only fields are [`CreateImageRequest::new`]'s defaults; an ensure whose request
/// sets others names its image for those (#280).
pub fn pinned_identity_hash(
    artifact_hash: &str,
    base: &BaseImage,
    base_version: Option<&str>,
    size: SizeClass,
) -> String {
    let defaults = CreateImageRequest::new("", Vec::new(), "", "");
    identity_hash(
        artifact_hash,
        base,
        base_version,
        size,
        &CreateOnly::of(&defaults),
    )
}

/// The identity of the image `create` would create, over the artifact `artifact_hash` names:
/// what [`prepare`] names an ensured image for, and what the agent recipe's two-step path
/// names the same image for.
pub(crate) fn create_identity_hash(artifact_hash: &str, create: &CreateImageRequest) -> String {
    identity_hash(
        artifact_hash,
        &create.base_image,
        create.base_image_version.as_deref(),
        create.size,
        &CreateOnly::of(create),
    )
}

/// The fields of a created image the service sets at `CreateMicrovmImage` and no later call
/// changes (#280). They are identity because a reuse returns the image as it was created: an
/// ensure that left them out of the name could hand a caller asking for identity repair an
/// image built without it. Tags are not here, because a reused image keeps its own.
struct CreateOnly<'a> {
    repair_guest_identity: bool,
    inherit_workdir: bool,
    run_hook_timeout: u32,
    build_hook_timeout: u32,
    log_group: Option<&'a str>,
    log_stream: Option<&'a str>,
}

impl<'a> CreateOnly<'a> {
    fn of(create: &'a CreateImageRequest) -> Self {
        Self {
            repair_guest_identity: create.repair_guest_identity,
            inherit_workdir: create.inherit_workdir,
            run_hook_timeout: create.run_hook_timeout.as_secs(),
            build_hook_timeout: create.build_hook_timeout.as_secs(),
            log_group: create.log_group.as_deref(),
            log_stream: create.log_stream.as_deref(),
        }
    }
}

/// The one identity stream. Every field is length-prefixed and each optional or create-only
/// one is tagged, so no two field boundaries can collide and an absent value never reads as an
/// empty one.
///
/// The domain tag is `microvms-ensure-image/2`: adding the create-only fields renames every
/// ensured image once (#280), and the version says which stream a name came from. An image
/// named by `/1` is never reused, which is the point: it may have been created without a field
/// its next caller asks for.
fn identity_hash(
    artifact_hash: &str,
    base: &BaseImage,
    base_version: Option<&str>,
    size: SizeClass,
    create: &CreateOnly<'_>,
) -> String {
    use sha2::{Digest as _, Sha256};

    fn put(hasher: &mut Sha256, field: &[u8]) {
        hasher.update((field.len() as u64).to_be_bytes());
        hasher.update(field);
    }
    fn put_optional(hasher: &mut Sha256, tag: &[u8], value: Option<&str>) {
        put(hasher, tag);
        match value {
            None => put(hasher, &[0]),
            Some(value) => {
                put(hasher, &[1]);
                put(hasher, value.as_bytes());
            }
        }
    }

    let mut hasher = Sha256::new();
    put(&mut hasher, b"microvms-ensure-image/2");
    put(&mut hasher, artifact_hash.as_bytes());
    put(&mut hasher, base.name.as_bytes());
    put(&mut hasher, &size.baseline_mib().to_be_bytes());
    put_optional(&mut hasher, b"baseImageVersion", base_version);
    for (tag, on) in [
        (
            b"repairGuestIdentity".as_slice(),
            create.repair_guest_identity,
        ),
        (b"inheritWorkdir".as_slice(), create.inherit_workdir),
    ] {
        put(&mut hasher, tag);
        put(&mut hasher, &[u8::from(on)]);
    }
    for (tag, secs) in [
        (b"microvmHooks".as_slice(), create.run_hook_timeout),
        (b"microvmImageHooks".as_slice(), create.build_hook_timeout),
    ] {
        put(&mut hasher, tag);
        put(&mut hasher, &secs.to_be_bytes());
    }
    put_optional(&mut hasher, b"logGroup", create.log_group);
    put_optional(&mut hasher, b"logStream", create.log_stream);
    const_hex::encode(hasher.finalize())
}

/// The artifact's S3 key: `<prefix>/<name>/artifact.zip`, with the prefix's empty segments
/// dropped so no key starts with or doubles a `/` (IMAGE-8).
pub fn artifact_key(key_prefix: Option<&str>, name: &str) -> String {
    let prefix: Vec<&str> = key_prefix
        .unwrap_or("")
        .split('/')
        .filter(|segment| !segment.is_empty())
        .collect();
    if prefix.is_empty() {
        format!("{name}/artifact.zip")
    } else {
        format!("{}/{name}/artifact.zip", prefix.join("/"))
    }
}

/// The bucket and key of an `s3://<bucket>/<key>` URI, refused when it isn't one S3 admits.
///
/// For the upload of a build whose artifact URI the caller chose ([`crate::sandbox::Sandbox::
/// upload_artifact_for`]); an ensure derives its own.
pub fn s3_location(uri: &str) -> Result<(&str, &str), Error> {
    let (bucket, key) = uri
        .strip_prefix("s3://")
        .and_then(|rest| rest.split_once('/'))
        .filter(|(_, key)| !key.is_empty())
        .ok_or_else(|| {
            Error::invalid_arg(format!(
                "the artifact URI {uri:?} is not s3://<bucket>/<key>, the only place \
                 CreateMicrovmImage reads an artifact from."
            ))
        })?;
    require_valid_bucket(bucket)?;
    Ok((bucket, key))
}

/// Refuses a bucket name S3 does not admit, before any call.
fn require_valid_bucket(bucket: &str) -> Result<(), Error> {
    let chars_ok = bucket
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.' || b == b'-');
    let ends_ok = bucket
        .bytes()
        .next()
        .zip(bucket.bytes().last())
        .is_some_and(|(first, last)| first.is_ascii_alphanumeric() && last.is_ascii_alphanumeric());
    if (3..=63).contains(&bucket.len()) && chars_ok && ends_ok && !bucket.contains("..") {
        return Ok(());
    }
    Err(Error::invalid_arg(format!(
        "the S3 bucket {bucket:?} is not a bucket name S3 admits: 3 to 63 characters of \
         lowercase letters, digits, `.` and `-`, starting and ending with a letter or digit. \
         Pass the bucket the build role can read, in the sandbox's region."
    )))
}

/// Refuses a key prefix that could not form a legal S3 key.
fn require_valid_key_prefix(prefix: Option<&str>, name: &str) -> Result<(), Error> {
    let Some(prefix) = prefix else {
        return Ok(());
    };
    if prefix.chars().any(char::is_control) {
        return Err(Error::invalid_arg(format!(
            "the S3 key prefix {prefix:?} carries a control character, which no S3 key \
             should; pass a plain path such as `harbor/images`."
        )));
    }
    let key = artifact_key(Some(prefix), name);
    if key.len() > 1024 {
        return Err(Error::invalid_arg(format!(
            "the S3 key prefix makes a {}-byte key, over S3's 1024; shorten it.",
            key.len()
        )));
    }
    Ok(())
}

/// Everything `Sandbox::ensure_image` needs.
#[derive(Clone, Debug)]
pub struct EnsureImageRequest {
    /// The stem of the image name; the name is `<prefix>-<hash12>`.
    pub name_prefix: String,
    /// The daemon binary's bytes.
    pub binary: Vec<u8>,
    /// The Dockerfile, usually `wrap_dockerfile`'s output. Checked by the create call's
    /// preflight before anything else happens.
    pub dockerfile: String,
    /// The files the Dockerfile may `COPY` ([`super::context::from_dir`]), or `None`.
    pub context: Option<BuildContext>,
    /// The bucket the artifact is uploaded to, in the sandbox's region.
    pub s3_bucket: String,
    /// A key prefix inside the bucket, or `None` for the bucket root.
    pub s3_key_prefix: Option<String>,
    /// The build role, which must read the bucket and write logs under
    /// `/aws/lambda-microvms/`.
    pub build_role_arn: String,
    /// The size class the image is created with; part of the name.
    pub size: SizeClass,
    /// The base image, or `None` for [`BaseImage::from_dockerfile`] of the Dockerfile.
    pub base_image: Option<BaseImage>,
    /// Delete what exists under the name and build afresh.
    pub force: bool,
    /// Tags for a created image. Not part of the name: a reused image keeps its own, and
    /// tags are not fixed at create.
    pub tags: BTreeMap<String, String>,
    /// The build wait's deadline, or `None` for [`DEFAULT_BUILD_TIMEOUT`].
    pub wait_timeout: Option<Duration>,
    /// A project's manifest and lockfile, baked as an environment layer (#74). Part of the
    /// name: they enter the artifact.
    pub project_files: Option<super::artifact::ProjectFiles>,
    /// `baseImageVersion`, or `None` for the service's default. Part of the name when set.
    pub base_image_version: Option<String>,
    /// `logging.cloudWatch.logGroup` for a created image. Part of the name (#280): the
    /// service sets an image's logging when it creates it, so a reused image logs where its
    /// creator asked.
    pub log_group: Option<String>,
    /// The log-stream prefix for a created image; see [`CreateImageRequest::log_stream`].
    /// Part of the name, as `log_group` is.
    pub log_stream: Option<String>,
    /// [`CreateImageRequest::repair_guest_identity`] for a created image. Part of the name
    /// (#280): a reused image built without the repair would answer a caller who asked for it.
    pub repair_guest_identity: bool,
    /// [`CreateImageRequest::inherit_workdir`] for a created image. Part of the name.
    pub inherit_workdir: bool,
    /// The run-family hook timeouts for a created image. Part of the name.
    pub run_hook_timeout: crate::hooks::RunHookTimeout,
    /// The build-family hook timeouts for a created image. Part of the name.
    pub build_hook_timeout: crate::hooks::BuildHookTimeout,
}

impl EnsureImageRequest {
    /// A request with the defaults: no context, no key prefix, the default size class, the
    /// base derived from the Dockerfile, no force, no tags, the default wait, and the create
    /// request's own defaults for everything a created image carries.
    pub fn new(
        name_prefix: impl Into<String>,
        binary: Vec<u8>,
        dockerfile: impl Into<String>,
        s3_bucket: impl Into<String>,
        build_role_arn: impl Into<String>,
    ) -> Self {
        // The create request's defaults, read off it rather than restated, so a created
        // image carries what `build_image` would have given it.
        let defaults = CreateImageRequest::new("", Vec::new(), "", "");
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
            project_files: None,
            base_image_version: None,
            log_group: None,
            log_stream: None,
            repair_guest_identity: defaults.repair_guest_identity,
            inherit_workdir: defaults.inherit_workdir,
            run_hook_timeout: defaults.run_hook_timeout,
            build_hook_timeout: defaults.build_hook_timeout,
        }
    }

    /// The ensure request for a create request's inputs (#258): its name becomes the prefix
    /// and every field it carries a created image carries too. `dockerfile` is the text the
    /// build takes, which a caller with no Dockerfile of its own gets from
    /// [`ControlPlane::dockerfile_for`]. The artifact URI and token label are dropped: the
    /// ensure derives both from the name it computes.
    pub fn from_create(
        create: CreateImageRequest,
        dockerfile: String,
        s3_bucket: impl Into<String>,
    ) -> Self {
        let mut request = Self::new(
            create.name,
            create.binary,
            dockerfile,
            s3_bucket,
            create.build_role_arn,
        );
        request.size = create.size;
        request.base_image = Some(create.base_image);
        request.tags = create.tags;
        request.project_files = create.project_files;
        request.base_image_version = create.base_image_version;
        request.log_group = create.log_group;
        request.log_stream = create.log_stream;
        request.repair_guest_identity = create.repair_guest_identity;
        request.inherit_workdir = create.inherit_workdir;
        request.run_hook_timeout = create.run_hook_timeout;
        request.build_hook_timeout = create.build_hook_timeout;
        request
    }
}

/// What `Sandbox::ensure_image` returns.
#[derive(Clone, Debug)]
pub struct EnsuredImage {
    /// The ready image.
    pub image: Image,
    /// True when this call's own create did not build the image: it was ready, or a build
    /// already running was waited out, or a sibling won the create race.
    pub reused: bool,
    /// Where the artifact is, whether or not this call uploaded it.
    pub artifact_uri: String,
    /// Whether this call uploaded the artifact.
    pub uploaded: bool,
    /// What reading the build context skipped, one line each ([`BuildContext::warnings`]).
    pub warnings: Vec<String>,
}

/// Everything [`prepare`] decided locally: the name, the key, the artifact, and the create
/// request, with the create's preflight already passed.
#[derive(Clone, Debug)]
pub struct Prepared {
    /// `<prefix>-<hash12>`.
    pub name: String,
    pub bucket: String,
    pub key: String,
    /// The zip to upload when a build is needed.
    pub artifact: Vec<u8>,
    /// The create request, named and pointed at the artifact.
    pub create: CreateImageRequest,
    pub force: bool,
    pub wait_timeout: Option<Duration>,
    pub warnings: Vec<String>,
}

impl Prepared {
    /// `s3://<bucket>/<key>`.
    pub fn artifact_uri(&self) -> String {
        format!("s3://{}/{}", self.bucket, self.key)
    }
}

/// The local half of `ensure_image`: every name, key, and artifact decision and every guard,
/// with zero calls.
pub fn prepare(control: &ControlPlane, request: EnsureImageRequest) -> Result<Prepared, Error> {
    require_valid_bucket(&request.s3_bucket)?;
    let base = match request.base_image {
        Some(base) => base,
        None => BaseImage::from_dockerfile(&request.dockerfile)?,
    };
    let artifact_hash = artifact_content_hash_with_context(
        &request.binary,
        &request.dockerfile,
        request.project_files.as_ref(),
        request.context.as_ref(),
    );
    // The create request first, unnamed, so the name is the identity of exactly the image it
    // creates (#280): every field it carries is read from it rather than restated.
    let mut create = CreateImageRequest::new(
        String::new(),
        request.binary,
        String::new(),
        request.build_role_arn,
    );
    create.base_image = base;
    create.dockerfile = Some(request.dockerfile);
    create.size = request.size;
    create.tags = request.tags;
    create.project_files = request.project_files;
    create.base_image_version = request.base_image_version;
    create.log_group = request.log_group;
    create.log_stream = request.log_stream;
    create.repair_guest_identity = request.repair_guest_identity;
    create.inherit_workdir = request.inherit_workdir;
    create.run_hook_timeout = request.run_hook_timeout;
    create.build_hook_timeout = request.build_hook_timeout;

    let name = ensured_image_name(
        &request.name_prefix,
        &create_identity_hash(&artifact_hash, &create),
    )?;
    require_valid_key_prefix(request.s3_key_prefix.as_deref(), &name)?;
    let key = artifact_key(request.s3_key_prefix.as_deref(), &name);
    create.code_artifact_uri = format!("s3://{}/{key}", request.s3_bucket);
    create.token_scope = Some(name.clone());
    create.name = name.clone();
    control.preflight(&create)?;

    let artifact = build_artifact_with_context(
        &create.binary,
        create.dockerfile.as_deref().unwrap_or_default(),
        create.project_files.as_ref(),
        request.context.as_ref(),
    )?;
    Ok(Prepared {
        name,
        bucket: request.s3_bucket,
        key,
        artifact,
        create,
        force: request.force,
        wait_timeout: request.wait_timeout,
        warnings: request
            .context
            .map(|context| context.warnings().to_vec())
            .unwrap_or_default(),
    })
}

impl ControlPlane {
    /// The image under `identifier`, or `None` when the service answers that there is none.
    ///
    /// `GetMicrovmImage`'s 404 is an answer here rather than an error: "no image under this
    /// name" is the state `ensure_image` builds from. Every other failure is an error as
    /// usual, and throttles are retried.
    pub async fn describe_image(
        &self,
        identifier: &str,
    ) -> Result<Option<ops::GetMicrovmImageResponseWire>, Error> {
        super::require_valid_identifier("imageIdentifier", identifier)?;
        let call = Call::get("GetMicrovmImage", paths::microvm_image(identifier));
        let reply = send_accepting(self.transport(), call, &[404]).await?;
        if reply.status == 404 {
            return Ok(None);
        }
        Ok(Some(reply.json("GetMicrovmImage")?))
    }

    /// Polls until `identifier` is no longer building, answering what it settled to.
    async fn await_settled(&self, identifier: &str, opts: WaitOpts) -> Result<Found, Error> {
        let started = self.clock().elapsed();
        loop {
            let found = self
                .describe_image(identifier)
                .await?
                .map_or(Found::Absent, |image| Found::from_state(&image.state));
            if found != Found::Building {
                return Ok(found);
            }
            let elapsed = self.clock().elapsed().saturating_sub(started);
            if elapsed >= opts.timeout {
                return Err(super::timed_out(
                    &format!("image {identifier} did not finish building"),
                    elapsed,
                ));
            }
            self.clock().sleep(opts.poll_interval).await;
        }
    }

    /// Deletes `identifier` and polls until its name is free (IMAGE-10).
    ///
    /// The delete is [`ControlPlane::delete_image`]'s, versions first. The poll ends when the
    /// describe answers 404, or when the image under the name is no longer the one deleted —
    /// a sibling rebuilt it, and the create that follows will join that build — or with
    /// [`ErrorKind::Platform`] for `DELETE_FAILED` and a timeout at
    /// [`DEFAULT_DELETE_TIMEOUT`].
    async fn delete_and_await_absent(
        &self,
        identifier: &str,
        deleted_state: &str,
        poll_interval: Duration,
    ) -> Result<(), Error> {
        self.delete_image(identifier, 3, poll_interval).await;
        self.await_absent(identifier, Some(deleted_state), poll_interval)
            .await
    }

    /// Polls until the name is free, treating `deleted_state` — the state a delete was just
    /// issued against — as a deletion the describe has not caught up with yet.
    async fn await_absent(
        &self,
        identifier: &str,
        deleted_state: Option<&str>,
        poll_interval: Duration,
    ) -> Result<(), Error> {
        let started = self.clock().elapsed();
        loop {
            let Some(image) = self.describe_image(identifier).await? else {
                return Ok(());
            };
            if image.state == "DELETE_FAILED" {
                return Err(Error::new(
                    ErrorKind::Platform,
                    format!(
                        "image {identifier} reports DELETE_FAILED, so its name cannot be \
                         freed for a rebuild. Delete it by hand (`aws lambda-microvms \
                         delete-microvm-image`), or pass a different name prefix."
                    ),
                ));
            }
            let pending = image.state == "DELETING" || Some(image.state.as_str()) == deleted_state;
            if !pending {
                return Ok(());
            }
            let elapsed = self.clock().elapsed().saturating_sub(started);
            if elapsed >= DEFAULT_DELETE_TIMEOUT {
                return Err(super::timed_out(
                    &format!("image {identifier} was still present after its deletion"),
                    elapsed,
                ));
            }
            self.clock().sleep(poll_interval).await;
        }
    }

    /// Whether the name is free or being freed: the image a wait was on has gone.
    async fn vanished(&self, identifier: &str) -> Result<bool, Error> {
        Ok(match self.describe_image(identifier).await? {
            None => true,
            Some(image) => Found::from_state(&image.state) == Found::Deleting,
        })
    }
}

fn image_from_wire(image: ops::GetMicrovmImageResponseWire, size: SizeClass) -> Image {
    Image {
        identifier: image.image_arn,
        name: image.name,
        version: image.latest_active_image_version.unwrap_or_default(),
        state: image.state,
        size,
        log_group: None,
        log_stream: None,
    }
}

/// Where one `ensure_image` call is, across its describes.
#[derive(Default)]
struct Progress {
    uploaded: bool,
    /// This call deleted what it found once; a forced rebuild is one-shot.
    deleted: bool,
    /// This call went back to the describe after the image it waited on disappeared.
    redescribed: bool,
}

/// The remote half of `ensure_image`: describe, decide, and build, reuse, wait, or join.
pub(crate) async fn ensure(
    control: &ControlPlane,
    services: &dyn BuildServices,
    account: &str,
    prepared: Prepared,
) -> Result<EnsuredImage, Error> {
    let arn = format!(
        "arn:aws:lambda:{}:{account}:microvm-image:{}",
        control.region().as_str(),
        prepared.name
    );
    let size = prepared.create.size;
    let wait = WaitOpts {
        timeout: prepared.wait_timeout.unwrap_or(DEFAULT_BUILD_TIMEOUT),
        ..WaitOpts::default()
    };
    let done = |image: Image, reused: bool, uploaded: bool| EnsuredImage {
        image,
        reused,
        artifact_uri: prepared.artifact_uri(),
        uploaded,
        warnings: prepared.warnings.clone(),
    };
    let mut progress = Progress::default();

    loop {
        let described = control.describe_image(&arn).await?;
        let found = described
            .as_ref()
            .map_or(Found::Absent, |image| Found::from_state(&image.state));
        let force = prepared.force && !progress.deleted;
        match plan(found, force) {
            Plan::Reuse => {
                let image = described.expect("a ready image was described");
                return Ok(done(image_from_wire(image, size), true, progress.uploaded));
            }
            Plan::Wait if force => {
                // The service refuses to delete a build in progress: let it settle, then
                // delete whatever it settled to.
                let settled = control.await_settled(&arn, wait).await?;
                if settled != Found::Absent {
                    let state = control
                        .describe_image(&arn)
                        .await?
                        .map(|image| image.state)
                        .unwrap_or_default();
                    control
                        .delete_and_await_absent(&arn, &state, wait.poll_interval)
                        .await?;
                }
                progress.deleted = true;
            }
            Plan::Wait => match control.wait_for_image(&arn, size, wait).await {
                Ok(image) => return Ok(done(image, true, progress.uploaded)),
                Err(error) => {
                    if !progress.redescribed && control.vanished(&arn).await? {
                        progress.redescribed = true;
                        continue;
                    }
                    return Err(error);
                }
            },
            Plan::Delete => {
                let state = described.map(|image| image.state).unwrap_or_default();
                control
                    .delete_and_await_absent(&arn, &state, wait.poll_interval)
                    .await?;
                progress.deleted = true;
            }
            Plan::AwaitAbsent => control.await_absent(&arn, None, wait.poll_interval).await?,
            Plan::Build => {}
        }

        // The name is free as far as this call knows: upload, create, and wait.
        if !progress.uploaded {
            services
                .put_object(&prepared.bucket, &prepared.key, prepared.artifact.clone())
                .await?;
            progress.uploaded = true;
        }
        match control.create_image(prepared.create.clone()).await {
            Ok(created) => match control
                .wait_for_image(&created.identifier, size, wait)
                .await
            {
                Ok(mut built) => {
                    // The logging config survives the wait from the create's own record, as
                    // in `Sandbox::build_image`: the readback cannot carry it.
                    built.log_group = created.log_group;
                    built.log_stream = created.log_stream;
                    return Ok(done(built, false, true));
                }
                Err(error) => {
                    if !progress.redescribed && control.vanished(&arn).await? {
                        progress.redescribed = true;
                        continue;
                    }
                    return Err(error);
                }
            },
            // IMAGE-11: a refused create is a race lost when something now holds the name.
            Err(refused) if refused.kind() == ErrorKind::Platform => {
                match control.describe_image(&arn).await? {
                    Some(image) if Found::from_state(&image.state) == Found::Ready => {
                        return Ok(done(image_from_wire(image, size), true, true));
                    }
                    Some(image)
                        if matches!(
                            Found::from_state(&image.state),
                            Found::Building | Found::Failed
                        ) =>
                    {
                        // Waits for the winner's build; a failed build answers its own
                        // diagnosis rather than this call's refused create.
                        match control.wait_for_image(&arn, size, wait).await {
                            Ok(image) => return Ok(done(image, true, true)),
                            Err(error) => {
                                if !progress.redescribed && control.vanished(&arn).await? {
                                    progress.redescribed = true;
                                    continue;
                                }
                                return Err(error);
                            }
                        }
                    }
                    _ if !progress.redescribed => {
                        progress.redescribed = true;
                        continue;
                    }
                    _ => return Err(refused),
                }
            }
            Err(error) => return Err(error),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    use super::*;
    use crate::control::fake::{self, Answer, FakeControlPlane, TestClock};
    use crate::region::Region;
    use crate::sandbox::Sandbox;
    use crate::testing::aarch64_daemon;

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
        let plane = crate::testing::control_plane(
            fake.clone(),
            Region::UsEast1,
            Arc::new(TestClock::new()),
        );
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
        let mut request = EnsureImageRequest::new(
            "task",
            aarch64_daemon(b"daemon"),
            dockerfile(),
            BUCKET,
            ROLE,
        );
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

    /// **The table `crates/model/src/image.rs` specifies**, row for row (`the_plan_table` there).
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

    /// **IMAGE-6 with a pinned base (#258).** An unpinned identity is the historical one
    /// exactly, so every ensured image keeps its name; a pin is identity, and two pins differ.
    #[test]
    fn a_pinned_base_version_is_part_of_the_identity() {
        let hash = "a".repeat(64);
        let base = BaseImage::al2023();
        let unpinned = image_identity_hash(&hash, &base, SizeClass::DEFAULT);
        assert_eq!(
            pinned_identity_hash(&hash, &base, None, SizeClass::DEFAULT),
            unpinned,
            "no pin, the name every ensured image already has"
        );
        let one = pinned_identity_hash(&hash, &base, Some("1"), SizeClass::DEFAULT);
        assert_ne!(one, unpinned);
        assert_ne!(
            one,
            pinned_identity_hash(&hash, &base, Some("0"), SizeClass::DEFAULT)
        );
    }

    /// **The create fields an ensure carries (#258).** Project files, a pinned base, logging,
    /// identity repair, `inherit_workdir` and the hook timeouts name a different image (#280),
    /// and all of them reach the create request a build sends.
    #[test]
    fn a_created_image_carries_every_field_of_the_request() {
        use crate::control::artifact::{Ecosystem, ProjectFiles};

        let plane = crate::testing::control_plane(
            Arc::new(FakeControlPlane::new()),
            Region::UsEast1,
            Arc::new(TestClock::new()),
        );
        let plain = prepare(&plane, request()).expect("prepares");

        let mut with_project = request();
        with_project.project_files = Some(ProjectFiles {
            ecosystem: Ecosystem::Uv,
            manifest: b"[project]\nname = \"demo\"\n".to_vec(),
            lockfile: b"version = 1\n".to_vec(),
        });
        // The wrapped Dockerfile doesn't COPY the pair, which core refuses (#74), so the
        // project's own default stanza stands in.
        with_project.dockerfile = crate::control::artifact::default_dockerfile(
            crate::control::DEFAULT_AGENT_PORT,
            None,
            &BaseImage::al2023(),
            Some(Ecosystem::Uv),
        );
        with_project.base_image = Some(BaseImage::al2023());
        let mut without_project = with_project.clone();
        without_project.project_files = None;
        let project = prepare(&plane, with_project).expect("prepares");
        assert!(project.create.project_files.is_some());
        assert_ne!(
            project.name,
            prepare(&plane, without_project).expect("prepares").name,
            "the project files enter the artifact, so they name a different image"
        );

        let mut pinned = request();
        pinned.base_image_version = Some("1".to_string());
        let pinned = prepare(&plane, pinned).expect("prepares");
        assert_ne!(pinned.name, plain.name, "a pin names a different image");
        assert_eq!(pinned.create.base_image_version.as_deref(), Some("1"));

        let mut configured = request();
        configured.log_group = Some("/team/builds".to_string());
        configured.log_stream = Some("task".to_string());
        configured.repair_guest_identity = true;
        configured.inherit_workdir = true;
        configured.run_hook_timeout =
            crate::hooks::RunHookTimeout::try_new(7).expect("a legal timeout");
        let configured = prepare(&plane, configured).expect("prepares");
        assert_ne!(
            configured.name, plain.name,
            "the create-only fields are identity (#280)"
        );
        assert_eq!(configured.create.log_group.as_deref(), Some("/team/builds"));
        assert_eq!(configured.create.log_stream.as_deref(), Some("task"));
        assert!(configured.create.repair_guest_identity);
        assert!(configured.create.inherit_workdir);
        assert_eq!(configured.create.run_hook_timeout.as_secs(), 7);
    }

    /// **Each create-only field names a different image (#280).** The service sets identity
    /// repair, `inherit_workdir`, the hook timeouts and the log destination when it creates
    /// an image, and no later call changes them, so an image reused under a name that left
    /// them out can be one built without them. Each one changed alone renames the image, and
    /// the same values name the same image again.
    #[test]
    fn each_create_only_field_names_a_different_image() {
        let plane = crate::testing::control_plane(
            Arc::new(FakeControlPlane::new()),
            Region::UsEast1,
            Arc::new(TestClock::new()),
        );
        let plain = prepare(&plane, request()).expect("prepares").name;
        assert_eq!(
            prepare(&plane, request()).expect("prepares").name,
            plain,
            "equal inputs name one image"
        );

        type Change = fn(&mut EnsureImageRequest);
        let changes: [(&str, Change); 7] = [
            ("repair_guest_identity", |r| r.repair_guest_identity = true),
            ("inherit_workdir", |r| r.inherit_workdir = true),
            ("run_hook_timeout", |r| {
                r.run_hook_timeout = crate::hooks::RunHookTimeout::try_new(7).expect("legal");
            }),
            ("build_hook_timeout", |r| {
                r.build_hook_timeout = crate::hooks::BuildHookTimeout::try_new(901).expect("legal");
            }),
            ("log_group", |r| {
                r.log_group = Some("/team/builds".to_string());
            }),
            ("another log_group", |r| {
                r.log_group = Some("/team/other".to_string());
            }),
            ("log_stream", |r| {
                r.log_group = Some("/team/builds".to_string());
                r.log_stream = Some("task".to_string());
            }),
        ];
        let mut names = vec![plain.clone()];
        for (field, change) in changes {
            let mut changed = request();
            change(&mut changed);
            let name = prepare(&plane, changed).expect("prepares").name;
            assert_ne!(name, plain, "{field} is create-only, so it is identity");
            assert!(
                !names.contains(&name),
                "{field} names an image another field already names: {name}"
            );
            names.push(name);
        }

        // Tags are not: a reused image keeps its own.
        let mut tagged = request();
        tagged.tags.insert("team".to_string(), "x".to_string());
        assert_eq!(prepare(&plane, tagged).expect("prepares").name, plain);
    }

    /// `from_create` hands an ensure every field a create request carries, and names the
    /// image the way an ensure of the same inputs would.
    #[test]
    fn an_ensure_from_a_create_request_keeps_its_fields() {
        let mut create = CreateImageRequest::new("task", aarch64_daemon(b"daemon"), "", ROLE);
        create.dockerfile = Some(dockerfile());
        create.base_image = BaseImage::from_dockerfile(&dockerfile()).expect("a FROM");
        create.base_image_version = Some("1".to_string());
        create.log_group = Some("/g".to_string());
        create.repair_guest_identity = true;
        create.tags.insert("team".to_string(), "x".to_string());
        let from = EnsureImageRequest::from_create(create, dockerfile(), BUCKET);
        assert_eq!(from.name_prefix, "task");
        assert_eq!(from.s3_bucket, BUCKET);
        assert_eq!(from.base_image_version.as_deref(), Some("1"));
        assert_eq!(from.log_group.as_deref(), Some("/g"));
        assert!(from.repair_guest_identity);
        assert_eq!(from.tags.get("team").map(String::as_str), Some("x"));

        let mut direct = request();
        direct.base_image_version = Some("1".to_string());
        direct.log_group = Some("/g".to_string());
        direct.repair_guest_identity = true;
        direct.s3_key_prefix = None;
        let plane = crate::testing::control_plane(
            Arc::new(FakeControlPlane::new()),
            Region::UsEast1,
            Arc::new(TestClock::new()),
        );
        assert_eq!(
            prepare(&plane, from).expect("prepares").name,
            prepare(&plane, direct).expect("prepares").name
        );
    }

    /// **A caller-named build uploads through the build services (#258).** The artifact goes to
    /// the bucket and key its URI names, once, with the account unasked; a URI that isn't one
    /// puts nothing.
    #[tokio::test]
    async fn a_named_builds_artifact_is_put_where_its_uri_says() {
        let fake = Arc::new(FakeControlPlane::new());
        let services = Arc::new(FakeServices::default());
        let mut sandbox = sandbox(&fake, &services);
        let mut create = CreateImageRequest::new(
            "named",
            aarch64_daemon(b"daemon"),
            "s3://artifact-bucket/builds/named.zip",
            ROLE,
        );
        create.dockerfile = Some(dockerfile());
        create.base_image = BaseImage::from_dockerfile(&dockerfile()).expect("a FROM");
        sandbox.upload_artifact_for(&create).await.expect("uploads");
        let puts = services.puts.lock().expect("not poisoned").clone();
        assert_eq!(puts.len(), 1);
        assert_eq!(
            (puts[0].0.as_str(), puts[0].1.as_str()),
            ("artifact-bucket", "builds/named.zip")
        );
        assert_eq!(
            puts[0].2,
            sandbox
                .build_artifact_for(&create)
                .expect("the same artifact")
        );
        assert_eq!(services.account_calls.load(Ordering::SeqCst), 0);
        assert!(fake.calls().is_empty(), "no control-plane call");

        create.code_artifact_uri = "artifact-bucket/named.zip".to_string();
        let error = sandbox
            .upload_artifact_for(&create)
            .await
            .expect_err("not an S3 URI");
        assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
        assert_eq!(uploads(&services).len(), 1, "nothing more was put");
    }

    /// An artifact URI is `s3://<bucket>/<key>` with a bucket S3 admits, or it is refused.
    #[test]
    fn an_artifact_uri_names_a_bucket_and_a_key() {
        assert_eq!(
            s3_location("s3://artifact-bucket/a/b.zip").expect("an S3 URI"),
            ("artifact-bucket", "a/b.zip")
        );
        for uri in [
            "https://artifact-bucket/a.zip",
            "s3://artifact-bucket",
            "s3://artifact-bucket/",
            "s3://Not_A_Bucket/a.zip",
        ] {
            let error = s3_location(uri).expect_err(uri);
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{uri}: {error}");
        }
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

    /// The account lookup of the test below: it records whether the other task's timer had
    /// fired by the time the ensure asked, then refuses, so the ensure goes no further.
    struct TimedLookup {
        fired: Arc<std::sync::atomic::AtomicBool>,
        seen: Mutex<Option<bool>>,
    }

    impl BuildServices for TimedLookup {
        fn caller_account(&self) -> futures_util::future::BoxFuture<'_, Result<String, Error>> {
            *self.seen.lock().expect("not poisoned") = Some(self.fired.load(Ordering::SeqCst));
            Box::pin(async { Err(Error::new(ErrorKind::Credentials, "the probe's refusal")) })
        }

        fn put_object<'a>(
            &'a self,
            _bucket: &'a str,
            _key: &'a str,
            _bytes: Vec<u8>,
        ) -> futures_util::future::BoxFuture<'a, Result<(), Error>> {
            Box::pin(async { panic!("the probe refuses before any upload") })
        }
    }

    /// **`ensure_image` prepares its artifact off the caller's worker (#309).** On a
    /// current-thread runtime, a one-millisecond timer another task set before the ensure
    /// began fires while a daemon-sized artifact is hashed and zipped, before the ensure asks
    /// for its account.
    ///
    /// Inline, `prepare` holds the runtime's only worker from its first line to that lookup,
    /// so no other task runs in between: a concurrent ensure's credential fetch parked
    /// behind it is what timed out under instance-role credentials. The daemon here is 2 MiB
    /// that deflate can't shrink, like the real one, which takes hundreds of milliseconds to
    /// prepare in a debug build, so the timer has that long to fire.
    ///
    /// **Falsification**: call `prepare` inline in `Sandbox::ensure_image` again and the
    /// lookup finds the timer unfired.
    #[tokio::test(flavor = "current_thread")]
    async fn the_artifact_is_prepared_while_the_callers_other_tasks_run() {
        let fired = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let lookup = Arc::new(TimedLookup {
            fired: Arc::clone(&fired),
            seen: Mutex::new(None),
        });
        let fake = Arc::new(FakeControlPlane::new());
        let plane = crate::testing::control_plane(
            fake.clone(),
            Region::UsEast1,
            Arc::new(TestClock::new()),
        );
        let mut sandbox = Sandbox::with_control_plane(plane).with_build_services(lookup.clone());
        let mut request = request();
        // xorshift64: bytes with no repeats for deflate to find, from no dependency, behind
        // the aarch64 header the preflight asks for.
        let mut state = 0x9e37_79b9_7f4a_7c15_u64;
        let payload: Vec<u8> = (0..(2 << 20) / 8)
            .flat_map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                state.to_le_bytes()
            })
            .collect();
        request.binary = aarch64_daemon(&payload);

        let timer = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(1)).await;
            fired.store(true, Ordering::SeqCst);
        });
        let error = sandbox
            .ensure_image(request)
            .await
            .expect_err("the probe refuses the lookup");
        assert!(error.to_string().contains("the probe's refusal"), "{error}");
        assert_eq!(
            *lookup.seen.lock().expect("not poisoned"),
            Some(true),
            "the other task's timer fires while the artifact is prepared, not after"
        );
        assert!(fake.calls().is_empty(), "no control-plane call came first");
        timer.await.expect("the timer task");
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
