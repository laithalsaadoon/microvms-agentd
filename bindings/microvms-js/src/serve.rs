// SPDX-License-Identifier: Apache-2.0
//! `session.tunnel(...)` and `session.portForward(...)`: core's serving loops, handed to
//! JavaScript, and the tunnel identity a verified tunnel checks the far end against.
//!
//! The loops, the stop, the grace, the counting and the list of connections that didn't end
//! clean are the core's ([`microvms_core::session::serve`]); this module holds the handle and
//! converts the report. A garbage-collected handle stops its loop and cuts its open
//! connections, so a relay can't outlive the object that owns it.

use microvms_core::identity::TunnelIdentity as CoreTunnelIdentity;
use std::net::SocketAddr;

use microvms_core::session::serve::{
    ConnectionEnd as CoreEnd, ForwardSummary, ServeLimits, Serving, StopReason, TunnelSummary,
    bind_address,
};
use napi_derive::napi;

use crate::errors::{AsyncError, js, js_async};
use crate::exec::seconds_async;

/// What a launcher keeps to verify its VM: the host's secret seed and the VM's public key,
/// both base64.
///
/// From `sandbox.tunnelIdentity()` after a `run({ identity: true })`, from a `NameRecord`, or
/// built from the two values `microvm run --identity` prints. Holds a secret: `hostSeed()` is a
/// method rather than a getter so it's never read by accident, and `toString()` leaves it out.
#[napi]
pub struct TunnelIdentity {
    pub(crate) inner: CoreTunnelIdentity,
}

impl From<CoreTunnelIdentity> for TunnelIdentity {
    fn from(inner: CoreTunnelIdentity) -> Self {
        Self { inner }
    }
}

#[napi]
impl TunnelIdentity {
    /// Rebuilds the pair from its base64 spellings. Refuses a value that doesn't decode, or a
    /// seed or key of the wrong length.
    #[napi(constructor)]
    pub fn new(host_seed: String, vm_public_key: String) -> napi::Result<Self, String> {
        CoreTunnelIdentity::from_encoded_parts(&host_seed, &vm_public_key)
            .map(|inner| Self { inner })
            .map_err(js)
    }

    /// The host's secret half, base64. Store only privately.
    #[napi]
    pub fn host_seed(&self) -> String {
        self.inner.host_seed_base64()
    }

    /// The VM's public key, base64: the pin. Safe to print and compare.
    #[napi(getter)]
    pub fn vm_public_key(&self) -> String {
        self.inner.vm_public_key_base64()
    }

    /// The pair without its secret.
    #[napi(js_name = "toString")]
    pub fn describe(&self) -> String {
        format!(
            "TunnelIdentity(vmPublicKey={:?}, hostSeed=<redacted>)",
            self.inner.vm_public_key_base64()
        )
    }
}

/// Where a tunnel or a port-forward listens, and when it stops on its own.
#[napi(object)]
#[derive(Default)]
pub struct ServeOptions {
    /// A `host:port` to listen on. Default: loopback, on a port the OS picks; the handle's
    /// `localAddress` says which. Bind beyond loopback only on a network you trust: whoever
    /// connects reaches the VM with this session's credentials.
    pub bind: Option<String>,
    /// Stop accepting after this many connections. Default: serve until stopped.
    pub max_connections: Option<f64>,
}

impl ServeOptions {
    /// The address to bind and the limits, checked before anything binds.
    pub(crate) fn resolve(options: Option<Self>) -> Result<(SocketAddr, ServeLimits), AsyncError> {
        let options = options.unwrap_or_default();
        let bind = bind_address(options.bind.as_deref()).map_err(js_async)?;
        let max_connections =
            crate::numbers::optional_u32(options.max_connections, "maxConnections")
                .map_err(js_async)?;
        Ok((bind, ServeLimits { max_connections }))
    }
}

/// A connection that didn't end clean.
#[napi(object)]
pub struct ConnectionEnd {
    /// The local client's address, `host:port`.
    pub peer: String,
    /// `"refused"`, `"truncated"`, `"unproven"`, or `"failed"`.
    pub kind: String,
    /// The close code or the HTTP status, when the end carried one.
    pub code: Option<u16>,
    /// The daemon's reason, the forwarder's explanation, or the error.
    pub detail: String,
}

fn ended(ends: Vec<CoreEnd>) -> Vec<ConnectionEnd> {
    ends.into_iter()
        .map(|end| ConnectionEnd {
            peer: end.peer.to_string(),
            kind: end.kind.as_str().to_string(),
            code: end.code,
            detail: end.detail,
        })
        .collect()
}

fn stopped(reason: &StopReason) -> String {
    match reason {
        StopReason::Requested => "stopped".to_string(),
        StopReason::Limit => "limit".to_string(),
        StopReason::ListenerFailed(error) => format!("listener-failed: {error}"),
    }
}

fn mints(count: u64) -> i64 {
    i64::try_from(count).unwrap_or(i64::MAX)
}

/// What a stopped tunnel did.
#[napi(object)]
pub struct TunnelReport {
    /// Connections accepted.
    pub served: u32,
    /// Connections the daemon refused, or that failed with an error.
    pub refused: u32,
    /// Verified connections that ended without the daemon's end of stream, so their stream
    /// may have been cut short.
    pub truncated: u32,
    /// Verified connections into a daemon from before the end of stream, whose end nothing
    /// proved.
    pub unproven: u32,
    /// Proxy tokens the session minted by the time the tunnel stopped.
    pub proxy_token_mints: i64,
    /// Why it stopped: `"stopped"`, `"limit"`, or `"listener-failed: <why>"`.
    pub stopped: String,
    /// Each connection that didn't end clean, in the order they ended.
    pub ended: Vec<ConnectionEnd>,
}

impl TunnelReport {
    fn wrap(summary: TunnelSummary) -> Self {
        let report = summary.report;
        Self {
            served: report.served,
            refused: report.refused,
            truncated: report.truncated,
            unproven: report.unproven,
            proxy_token_mints: mints(report.proxy_token_mints),
            stopped: stopped(&report.stopped),
            ended: ended(summary.ended),
        }
    }
}

/// What a stopped port-forward did.
#[napi(object)]
pub struct PortForwardReport {
    /// Connections accepted.
    pub served: u32,
    /// Exchanges the endpoint proxy refused, a 403 or a 502 among them.
    pub refused: u32,
    /// Exchanges that upgraded, a WebSocket among them.
    pub upgrades: u32,
    /// Proxy tokens the session minted by the time the forward stopped.
    pub proxy_token_mints: i64,
    /// Why it stopped: `"stopped"`, `"limit"`, or `"listener-failed: <why>"`.
    pub stopped: String,
    /// Each connection that didn't end clean, in the order they ended.
    pub ended: Vec<ConnectionEnd>,
}

impl PortForwardReport {
    fn wrap(summary: ForwardSummary) -> Self {
        let report = summary.report;
        Self {
            served: report.served,
            refused: report.refused,
            upgrades: report.upgrades,
            proxy_token_mints: mints(report.proxy_token_mints),
            stopped: stopped(&report.stopped),
            ended: ended(summary.ended),
        }
    }
}

/// `stop()`'s grace, from its `timeout` argument.
fn grace(timeout: Option<f64>) -> Result<Option<std::time::Duration>, AsyncError> {
    timeout.map(seconds_async).transpose()
}

/// A running tunnel. Call `stop()` when done; a garbage-collected handle stops the tunnel and
/// cuts its open connections.
#[napi]
pub struct Tunnel {
    task: Serving<TunnelSummary>,
}

impl Tunnel {
    pub(crate) fn wrap(task: Serving<TunnelSummary>) -> Self {
        Self { task }
    }
}

#[napi]
impl Tunnel {
    /// The local address to connect to, `host:port`, with the port the OS picked.
    #[napi(getter)]
    pub fn local_address(&self) -> String {
        self.task.local_address().to_string()
    }

    /// Whether the tunnel is still serving.
    #[napi(getter)]
    pub fn running(&self) -> bool {
        self.task.is_running()
    }

    /// Stops accepting, waits for the connections still open to end, and resolves with the
    /// report.
    ///
    /// With `timeout` (seconds), connections still open after it are cut and listed as
    /// `"failed"`. Without it, a client that keeps its connection open keeps this waiting.
    /// Callable again, with the same report.
    #[napi]
    pub async fn stop(&self, timeout: Option<f64>) -> Result<TunnelReport, AsyncError> {
        let grace = grace(timeout)?;
        Ok(TunnelReport::wrap(
            self.task.stop(grace).await.map_err(js_async)?,
        ))
    }

    /// The handle without its credentials.
    #[napi(js_name = "toString")]
    pub fn describe(&self) -> String {
        format!(
            "Tunnel(localAddress={:?}, running={})",
            self.task.local_address().to_string(),
            self.task.is_running()
        )
    }
}

/// A running port-forward. Call `stop()` when done; a garbage-collected handle stops the
/// forward and cuts its open connections.
#[napi]
pub struct PortForward {
    task: Serving<ForwardSummary>,
}

impl PortForward {
    pub(crate) fn wrap(task: Serving<ForwardSummary>) -> Self {
        Self { task }
    }
}

#[napi]
impl PortForward {
    /// The local address to connect to, `host:port`, with the port the OS picked.
    #[napi(getter)]
    pub fn local_address(&self) -> String {
        self.task.local_address().to_string()
    }

    /// Whether the forward is still serving.
    #[napi(getter)]
    pub fn running(&self) -> bool {
        self.task.is_running()
    }

    /// Stops accepting, waits for the connections still open to end, and resolves with the
    /// report.
    ///
    /// With `timeout` (seconds), connections still open after it are cut and listed as
    /// `"failed"`. Without it, a client that keeps its connection open keeps this waiting.
    /// Callable again, with the same report.
    #[napi]
    pub async fn stop(&self, timeout: Option<f64>) -> Result<PortForwardReport, AsyncError> {
        let grace = grace(timeout)?;
        Ok(PortForwardReport::wrap(
            self.task.stop(grace).await.map_err(js_async)?,
        ))
    }

    /// The handle without its credentials.
    #[napi(js_name = "toString")]
    pub fn describe(&self) -> String {
        format!(
            "PortForward(localAddress={:?}, running={})",
            self.task.local_address().to_string(),
            self.task.is_running()
        )
    }
}
