// SPDX-License-Identifier: Apache-2.0
//! `session.keepAwake(...)`: the core's keepalive task, handed to JavaScript.
//!
//! The poll loop and the background task are the core's
//! ([`microvms_core::session::KeepAwakeTask`]). A sandbox-held session is gated on the
//! sandbox's lifecycle watch rather than its lock, for the reason the Python twin gives:
//! the lock is held for the whole of a long exec, which is when the keepalive must poll.

use microvms_core::session::KeepAwakeTask;
use microvms_core::session::{KeepAwake as CoreKeepAwake, KeepAwakeReport as CoreReport};
use napi_derive::napi;

use crate::errors::{AsyncError, js_async};
use crate::exec::seconds_async;

/// What a finished keepalive did.
#[napi(object)]
pub struct KeepAwakeReport {
    /// Why it ended: `"stopped"`, `"idle"`, `"elapsed"`, or `"not-running"`.
    pub end: String,
    /// Health polls that answered.
    pub polls: i64,
    /// `busy` from the last answered poll, or `null` when none answered.
    pub last_busy: Option<bool>,
    /// Seconds from start to end.
    pub elapsed_sec: f64,
}

impl KeepAwakeReport {
    fn wrap(report: CoreReport) -> Self {
        Self {
            end: report.end.as_str().to_string(),
            polls: i64::try_from(report.polls).unwrap_or(i64::MAX),
            last_busy: report.last_busy,
            elapsed_sec: report.elapsed.as_secs_f64(),
        }
    }
}

/// The options bag for `keepAwake`.
#[napi(object)]
#[derive(Default)]
pub struct KeepAwakeOptions {
    /// Seconds between polls. Default: a third of the idle window, at most 20.
    pub interval_sec: Option<f64>,
    /// End once no exec is running.
    pub while_busy: Option<bool>,
    /// End after this many seconds even if still busy.
    pub max_duration_sec: Option<f64>,
    /// The VM's `maxIdleDurationSeconds`. A sandbox-held session knows it; otherwise the
    /// platform minimum of 60 is assumed. The interval may be at most half of it.
    pub idle_window_sec: Option<f64>,
}

impl KeepAwakeOptions {
    pub(crate) fn policy(
        &self,
        known_window: Option<std::time::Duration>,
    ) -> Result<CoreKeepAwake, AsyncError> {
        let window = self.idle_window_sec.map(seconds_async).transpose()?;
        let mut policy = CoreKeepAwake::new(window.or(known_window))
            .while_busy(self.while_busy.unwrap_or(false))
            .max_duration(self.max_duration_sec.map(seconds_async).transpose()?);
        if let Some(interval) = self.interval_sec {
            policy = policy.interval(seconds_async(interval)?);
        }
        Ok(policy)
    }
}

/// A running keepalive. Call `stop()` when the work is done; `done()` resolves when it
/// ends on its own. A garbage-collected handle stops the keepalive.
#[napi]
pub struct KeepAwake {
    task: KeepAwakeTask,
}

impl KeepAwake {
    pub(crate) fn wrap(task: KeepAwakeTask) -> Self {
        Self { task }
    }
}

#[napi]
impl KeepAwake {
    /// Whether the keepalive is still polling.
    #[napi(getter)]
    pub fn running(&self) -> bool {
        self.task.is_running()
    }

    /// Stops polling and resolves with the report; rejects with the error that ended it.
    #[napi]
    pub async fn stop(&self) -> Result<KeepAwakeReport, AsyncError> {
        self.task.request_stop();
        self.done().await
    }

    /// Resolves when the keepalive ends (`whileBusy`, `maxDurationSec`, or `stop()`).
    #[napi]
    pub async fn done(&self) -> Result<KeepAwakeReport, AsyncError> {
        Ok(KeepAwakeReport::wrap(
            self.task.finished().await.map_err(js_async)?,
        ))
    }
}
