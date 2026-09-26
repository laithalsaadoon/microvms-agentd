// SPDX-License-Identifier: Apache-2.0
//! MicroVM lifecycle by ID, for a process that holds only an identifier.
//!
//! A thin wrapper over the core's `ControlPlane`: every call is one of the core's own, with
//! its identifier checks and retries. It carries no lifecycle state and so enforces none of
//! the STATE guards a `Sandbox` does — it answers what the service says.

use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use microvms_core::control::{ControlPlane as CoreControlPlane, Microvm as CoreMicrovm};
use microvms_core::control::{MicrovmFilter, WaitOpts};
use microvms_core::prelude::*;
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

/// MicroVM lifecycle by ID: get, list, suspend, resume, terminate, and wait.
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
            timeout: seconds_async(options.timeout.unwrap_or(300.0))?,
            poll_interval: seconds_async(options.poll_interval.unwrap_or(5.0))?,
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

    /// The region this plane addresses.
    #[napi(getter)]
    pub fn region(&self) -> String {
        self.inner.region().as_str().to_string()
    }
}
