// SPDX-License-Identifier: Apache-2.0
//! MicroVM lifecycle and image administration by ID, for a process that holds only an
//! identifier.
//!
//! A thin wrapper over the core's `ControlPlane`: every call is one of the core's own, with
//! its identifier checks and retries. It carries no lifecycle state and so enforces none of
//! the STATE guards a `Sandbox` does — it answers what the service says.

use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use std::collections::HashMap;

use microvms_core::control::{ControlPlane as CoreControlPlane, Microvm as CoreMicrovm};
use microvms_core::control::{MicrovmFilter, WaitOpts, ops};
use microvms_core::prelude::*;
use microvms_core::sandbox::{
    DEFAULT_DELETE_ATTEMPTS, DEFAULT_DELETE_BACKOFF, DEFAULT_LIFECYCLE_TIMEOUT,
    LIFECYCLE_POLL_INTERVAL,
};
use napi_derive::napi;

use crate::errors::{AsyncError, js_async};
use crate::exec::seconds_async;
use crate::region::Region;

/// The idle policy the service reports a VM is running under.
#[napi(object)]
pub struct IdlePolicy {
    /// `maxIdleDurationSeconds`: inbound-traffic silence before an auto-suspend.
    pub max_idle_sec: u32,
    /// `suspendedDurationSeconds`: how long a suspended VM lasts before it is terminated.
    pub suspended_sec: u32,
    /// `autoResumeEnabled`: whether a request to a suspended VM resumes it.
    pub auto_resume: bool,
}

/// A MicroVM as `GetMicrovm` last described it.
#[napi(object)]
pub struct Microvm {
    pub id: String,
    /// As the service spells it. Eventually consistent.
    pub state: String,
    /// Why the VM is in this state, when the service said.
    pub state_reason: Option<String>,
    /// The proxy endpoint. Pair it with the agent token in `Session.attach`.
    pub endpoint: String,
    pub image_arn: String,
    pub image_version: String,
    pub idle_policy: Option<IdlePolicy>,
    /// `maximumDurationInSeconds`. Suspended time counts toward it.
    pub maximum_duration_seconds: Option<u32>,
    /// When the VM first started, as Unix seconds.
    pub started_at: Option<f64>,
    /// When the VM terminated, as Unix seconds, once it has.
    pub terminated_at: Option<f64>,
}

fn epoch(at: Option<std::time::SystemTime>) -> Option<f64> {
    at.and_then(|at| at.duration_since(UNIX_EPOCH).ok())
        .map(|since| since.as_secs_f64())
}

impl From<CoreMicrovm> for Microvm {
    fn from(vm: CoreMicrovm) -> Self {
        Self {
            idle_policy: vm.idle_policy.as_ref().map(|policy| IdlePolicy {
                max_idle_sec: policy.max_idle_duration_seconds,
                suspended_sec: policy.suspended_duration_seconds,
                auto_resume: policy.auto_resume_enabled,
            }),
            started_at: epoch(vm.started_at),
            terminated_at: epoch(vm.terminated_at),
            maximum_duration_seconds: vm.maximum_duration_seconds,
            id: vm.id,
            state: vm.state,
            state_reason: vm.state_reason,
            endpoint: vm.endpoint,
            image_arn: vm.image_arn,
            image_version: vm.image_version,
        }
    }
}

/// One `ListMicrovms` item: narrower than `Microvm`, with no endpoint or reason.
#[napi(object)]
pub struct MicrovmSummary {
    pub id: String,
    pub state: String,
    pub image_arn: String,
    pub image_version: String,
}

/// The service's optional `ListMicrovms` filters.
#[derive(Default)]
#[napi(object)]
pub struct ListMicrovmsOptions {
    pub image_identifier: Option<String>,
    pub image_version: Option<String>,
}

/// How `waitForState` polls.
#[derive(Default)]
#[napi(object)]
pub struct WaitForStateOptions {
    /// States that reject with `stateReason` instead of being waited through.
    pub fail_on: Option<Vec<String>>,
    /// Seconds before giving up. Default 300.
    pub timeout: Option<f64>,
    /// Seconds between polls. Default 5.
    pub poll_interval: Option<f64>,
}

/// One `ListMicrovmImages` item: an image's ARN, name and state.
#[napi(object)]
pub struct ImageSummary {
    pub image_arn: String,
    pub name: String,
    /// As the service spells it, such as `"CREATING"` or `"CREATED"`.
    pub state: String,
}

/// One image version as `ListMicrovmImageVersions` or `UpdateMicrovmImageVersion` reads it
/// back: its build state, whether `RunMicrovm` launches it, and what it was built with.
#[napi(object)]
pub struct ImageVersion {
    pub image_arn: String,
    pub image_version: String,
    /// The version's build state, as the service spells it.
    pub state: String,
    /// `"ACTIVE"` (`RunMicrovm` launches it) or `"INACTIVE"` (it refuses; running VMs keep
    /// running).
    pub status: String,
    /// Whether `RunMicrovm` launches this version.
    pub is_active: bool,
    /// Why the version is in this state, when the service said. A failed build's reason is on
    /// its build (`listImageBuilds`), and this one is usually absent.
    pub state_reason: Option<String>,
    /// Unix seconds.
    pub created_at: f64,
    /// Unix seconds, when the service reported it.
    pub updated_at: Option<f64>,
    pub base_image_arn: String,
    /// The base version the build used, as the service spells it (`"1.0"` where the managed
    /// base lists `"1"`). A record of the build, not a value to pass back as a pin.
    pub base_image_version: Option<String>,
    pub build_role_arn: String,
    /// `codeArtifact.uri`: the artifact the version was built from.
    pub code_artifact_uri: String,
    pub description: Option<String>,
    /// `resources[0].minimumMemoryInMiB`, the list's one member: the size class the version
    /// was built for, and the only place a built image reports it.
    pub minimum_memory_mib: Option<u32>,
    pub egress_network_connectors: Option<Vec<String>>,
    pub additional_os_capabilities: Option<Vec<String>>,
    pub environment_variables: Option<HashMap<String, String>>,
    pub tags: Option<HashMap<String, String>>,
}

impl From<ops::MicrovmImageVersionSummaryWire> for ImageVersion {
    fn from(version: ops::MicrovmImageVersionSummaryWire) -> Self {
        Self {
            is_active: version.is_active(),
            minimum_memory_mib: version
                .resources
                .as_ref()
                .and_then(|resources| resources.first())
                .map(|resources| resources.minimum_memory_in_mib),
            image_arn: version.image_arn,
            image_version: version.image_version,
            state: version.state,
            status: version.status,
            state_reason: version.state_reason,
            created_at: version.created_at,
            updated_at: version.updated_at,
            base_image_arn: version.base_image_arn,
            base_image_version: version.base_image_version,
            build_role_arn: version.build_role_arn,
            code_artifact_uri: version.code_artifact.uri,
            description: version.description,
            egress_network_connectors: version.egress_network_connectors,
            additional_os_capabilities: version.additional_os_capabilities,
            environment_variables: version
                .environment_variables
                .map(|variables| variables.into_iter().collect()),
            tags: version.tags.map(|tags| tags.into_iter().collect()),
        }
    }
}

/// One build of an image version: one per Graviton generation, so a version's builds differ
/// in `chipsetGeneration`. `getImageBuild` adds the snapshot sizes the listing lacks.
#[napi(object)]
pub struct ImageBuild {
    pub image_arn: String,
    pub image_version: String,
    /// What `getImageBuild` takes, and nothing else in the API mints one.
    pub build_id: String,
    /// `buildState`, as the service spells it.
    pub build_state: String,
    pub architecture: String,
    pub chipset: String,
    pub chipset_generation: String,
    /// Unix seconds.
    pub created_at: f64,
    /// Why the build is in this state, when the service said: where a failed build's reason
    /// lives.
    pub state_reason: Option<String>,
    /// `snapshotBuild.memorySnapshotSizeInBytes`, from `getImageBuild` only, and only when the
    /// service reported it.
    pub memory_snapshot_size_in_bytes: Option<i64>,
    /// `snapshotBuild.codeInstallSizeInBytes`, from `getImageBuild` only.
    pub code_install_size_in_bytes: Option<i64>,
    /// `snapshotBuild.diskSnapshotSizeInBytes`, from `getImageBuild` only.
    pub disk_snapshot_size_in_bytes: Option<i64>,
}

/// A byte count as a JS number: every size a snapshot reports fits, and one that didn't would
/// read as the largest rather than wrap negative.
fn byte_count(bytes: Option<u64>) -> Option<i64> {
    bytes.map(|bytes| i64::try_from(bytes).unwrap_or(i64::MAX))
}

impl From<ops::GetImageBuildResponseWire> for ImageBuild {
    fn from(build: ops::GetImageBuildResponseWire) -> Self {
        let sizes = build.snapshot_build;
        Self {
            image_arn: build.image_arn,
            image_version: build.image_version,
            build_id: build.build_id,
            build_state: build.build_state,
            architecture: build.architecture,
            chipset: build.chipset,
            chipset_generation: build.chipset_generation,
            created_at: build.created_at,
            state_reason: build.state_reason,
            memory_snapshot_size_in_bytes: byte_count(
                sizes.and_then(|sizes| sizes.memory_snapshot_size_in_bytes),
            ),
            code_install_size_in_bytes: byte_count(
                sizes.and_then(|sizes| sizes.code_install_size_in_bytes),
            ),
            disk_snapshot_size_in_bytes: byte_count(
                sizes.and_then(|sizes| sizes.disk_snapshot_size_in_bytes),
            ),
        }
    }
}

impl From<ops::MicrovmImageBuildSummaryWire> for ImageBuild {
    fn from(build: ops::MicrovmImageBuildSummaryWire) -> Self {
        Self {
            image_arn: build.image_arn,
            image_version: build.image_version,
            build_id: build.build_id,
            build_state: build.build_state,
            architecture: build.architecture,
            chipset: build.chipset,
            chipset_generation: build.chipset_generation,
            created_at: build.created_at,
            state_reason: build.state_reason,
            memory_snapshot_size_in_bytes: None,
            code_install_size_in_bytes: None,
            disk_snapshot_size_in_bytes: None,
        }
    }
}

/// How `deleteImage` retries while the image refuses.
#[derive(Default)]
#[napi(object)]
pub struct DeleteImageOptions {
    /// Attempts before giving up. Default: the core's teardown figure.
    pub attempts: Option<f64>,
    /// Seconds between attempts. Default: the core's teardown figure.
    pub backoff: Option<f64>,
}

/// MicroVM lifecycle by ID (get, list, suspend, resume, terminate, and wait) and image
/// administration (list, delete, versions and their status, builds).
///
/// Holds no lifecycle state, so it checks nothing a `Sandbox` would (STATE-5, STATE-7,
/// STATE-12). Use it when a process has only an identifier.
#[napi]
pub struct ControlPlane {
    inner: Arc<CoreControlPlane>,
}

#[napi]
impl ControlPlane {
    /// Resolves credentials for `region` from the default chain. A factory because
    /// credential resolution is async.
    #[napi(factory)]
    pub async fn create(region: &Region) -> Result<ControlPlane, AsyncError> {
        let plane = CoreControlPlane::new(region.inner.clone())
            .await
            .map_err(js_async)?;
        Ok(ControlPlane {
            inner: Arc::new(plane),
        })
    }

    /// `GetMicrovm`.
    #[napi]
    pub async fn get(&self, microvm_id: String) -> Result<Microvm, AsyncError> {
        let vm = self
            .inner
            .get_microvm(&microvm_id)
            .await
            .map_err(js_async)?;
        Ok(vm.into())
    }

    /// `ListMicrovms`, every page, optionally narrowed to one image and version.
    #[napi]
    pub async fn list(
        &self,
        options: Option<ListMicrovmsOptions>,
    ) -> Result<Vec<MicrovmSummary>, AsyncError> {
        let options = options.unwrap_or_default();
        let filter = MicrovmFilter {
            image_identifier: options.image_identifier,
            image_version: options.image_version,
        };
        let items = self
            .inner
            .list_microvms_matching(&filter)
            .await
            .map_err(js_async)?;
        Ok(items
            .into_iter()
            .map(|item| MicrovmSummary {
                id: item.microvm_id,
                state: item.state,
                image_arn: item.image_arn,
                image_version: item.image_version,
            })
            .collect())
    }

    /// `SuspendMicrovm`. Resolves once accepted; `waitForState` for SUSPENDED.
    #[napi]
    pub async fn suspend(&self, microvm_id: String) -> Result<(), AsyncError> {
        self.inner.suspend(&microvm_id).await.map_err(js_async)
    }

    /// `ResumeMicrovm`. Resolves once accepted; `waitForState` for RUNNING.
    #[napi]
    pub async fn resume(&self, microvm_id: String) -> Result<(), AsyncError> {
        self.inner.resume(&microvm_id).await.map_err(js_async)
    }

    /// `TerminateMicrovm`. Resolves once accepted; `waitForState` for TERMINATED.
    #[napi]
    pub async fn terminate(&self, microvm_id: String) -> Result<(), AsyncError> {
        self.inner.terminate(&microvm_id).await.map_err(js_async)
    }

    /// Polls `GetMicrovm` until the state is one of `wanted`.
    ///
    /// Reaching one of `failOn` first rejects naming the state and `stateReason`; running
    /// past `timeout` rejects with a timeout.
    #[napi]
    pub async fn wait_for_state(
        &self,
        microvm_id: String,
        wanted: Vec<String>,
        options: Option<WaitForStateOptions>,
    ) -> Result<Microvm, AsyncError> {
        let options = options.unwrap_or_default();
        let opts = WaitOpts {
            timeout: seconds_async(
                options
                    .timeout
                    .unwrap_or(DEFAULT_LIFECYCLE_TIMEOUT.as_secs_f64()),
            )?,
            poll_interval: seconds_async(
                options
                    .poll_interval
                    .unwrap_or(LIFECYCLE_POLL_INTERVAL.as_secs_f64()),
            )?,
            stall_grace: Duration::MAX,
        };
        let fail_on = options.fail_on.unwrap_or_default();
        let wanted: Vec<&str> = wanted.iter().map(String::as_str).collect();
        let fail_on: Vec<&str> = fail_on.iter().map(String::as_str).collect();
        let vm = self
            .inner
            .wait_for_state(&microvm_id, &wanted, &fail_on, opts)
            .await
            .map_err(js_async)?;
        Ok(vm.into())
    }

    /// `ListMicrovmImages`, every page: every image in the account and region.
    #[napi]
    pub async fn list_images(&self) -> Result<Vec<ImageSummary>, AsyncError> {
        let items = self.inner.list_images().await.map_err(js_async)?;
        Ok(items
            .into_iter()
            .map(|item| ImageSummary {
                image_arn: item.image_arn,
                name: item.name,
                state: item.state,
            })
            .collect())
    }

    /// Deletes the image, its extra versions first, retrying while it refuses (an image still
    /// `CREATING`, or one a terminating VM holds).
    ///
    /// Resolves `true` once the service took the deletion and `false` when every attempt
    /// failed or `identifier` is not one the service accepts. It doesn't reject, as a
    /// teardown's delete shouldn't.
    #[napi]
    pub async fn delete_image(
        &self,
        identifier: String,
        options: Option<DeleteImageOptions>,
    ) -> Result<bool, AsyncError> {
        let options = options.unwrap_or_default();
        let attempts = crate::numbers::optional_u32(options.attempts, "attempts")
            .map_err(js_async)?
            .unwrap_or(DEFAULT_DELETE_ATTEMPTS);
        let backoff = match options.backoff {
            Some(backoff) => seconds_async(backoff)?,
            None => DEFAULT_DELETE_BACKOFF,
        };
        Ok(self
            .inner
            .delete_image(&identifier, attempts, backoff)
            .await)
    }

    /// `ListMicrovmImageVersions`, every page: each version, its status, and its build
    /// configuration.
    #[napi]
    pub async fn list_image_versions(
        &self,
        identifier: String,
    ) -> Result<Vec<ImageVersion>, AsyncError> {
        let items = self
            .inner
            .list_image_versions(&identifier)
            .await
            .map_err(js_async)?;
        Ok(items.into_iter().map(ImageVersion::from).collect())
    }

    /// `UpdateMicrovmImageVersion`: `status` is `"ACTIVE"` or `"INACTIVE"`.
    ///
    /// `INACTIVE` is the non-destructive retire: `RunMicrovm` refuses the version, running VMs
    /// keep running, and the version's readback stays. Resolves with the readback, and rejects
    /// when it doesn't carry the status asked for, so a 200 that didn't take isn't a rollback.
    #[napi]
    pub async fn set_image_version_status(
        &self,
        identifier: String,
        version: String,
        status: String,
    ) -> Result<ImageVersion, AsyncError> {
        let status: ops::VersionStatus = status.parse().map_err(js_async)?;
        let updated = self
            .inner
            .set_image_version_status(&identifier, &version, status)
            .await
            .map_err(js_async)?;
        Ok(updated.into())
    }

    /// `ListMicrovmImageBuilds` for one version, every page: one build per Graviton
    /// generation. Each `buildId` is what `getImageBuild` takes.
    #[napi]
    pub async fn list_image_builds(
        &self,
        identifier: String,
        version: String,
    ) -> Result<Vec<ImageBuild>, AsyncError> {
        let items = self
            .inner
            .list_image_builds(&identifier, &version)
            .await
            .map_err(js_async)?;
        Ok(items.into_iter().map(ImageBuild::from).collect())
    }

    /// `GetMicrovmImageBuild`: one build, with the snapshot sizes the listing doesn't carry.
    #[napi]
    pub async fn get_image_build(
        &self,
        identifier: String,
        version: String,
        build_id: String,
    ) -> Result<ImageBuild, AsyncError> {
        let build = self
            .inner
            .get_image_build(&identifier, &version, &build_id)
            .await
            .map_err(js_async)?;
        Ok(build.into())
    }

    /// The region this plane addresses.
    #[napi(getter)]
    pub fn region(&self) -> String {
        self.inner.region().as_str().to_string()
    }
}
