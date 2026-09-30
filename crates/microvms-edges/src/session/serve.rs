// SPDX-License-Identifier: Apache-2.0
//! The serving loops behind `microvm tunnel` and `microvm port-forward`, and behind the SDKs'
//! tunnel and port-forward handles (#263).
//!
//! Each loop accepts a local connection, serves it on a task of its own, and counts how it
//! ended. The three things the CLI used to own are parameters: a stop future (the CLI's Ctrl-C,
//! a handle's `stop()`), a limit on how many connections to accept, and a callback that hears
//! about each connection's end, so a caller can print a warning or keep a list. A connection
//! that fails never ends the loop; only the stop, the limit, or a listener that stops accepting
//! does. The report's counts are computed here, once, for every surface.
//!
//! # Every connection gets a task, both loops alike
//!
//! One WebSocket per tunnel connection matches the daemon's no-multiplexing decision, and one
//! task per forwarded connection means a slow request (a long poll, a first compile) doesn't
//! hold the browser's next one behind it, which the CLI's port-forward used to do. When the
//! loop stops accepting, it waits for the connections already open to end: a task still
//! relaying holds bytes the local client is waiting for, and dropping it would cut them short.
//!
//! # A direct session serves too
//!
//! A session with no proxy credential reaches the daemon without the endpoint proxy. The
//! tunnel then offers no proxy subprotocols, and the daemon's bearer check is the gate; the
//! forwarder mints no proxy headers and sends each request to the endpoint's host at the guest
//! port ([`super::forward::direct_url`]), since there is no proxy to route it by header.

use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;

use tokio::net::TcpListener;
use tokio::task::JoinSet;

use microvms_app::error::{Error, ErrorKind};
use microvms_app::identity::TunnelIdentity;
use microvms_app::session::Session;
use microvms_app::session::proxy::ProxyAuth;

use super::forward::{ForwardClient, ForwardEvent, ForwardSpec, serve_connection_via};
use super::tunnel::{TunnelEnd, relay_connection_inner};

/// When a loop stops on its own, besides its caller's stop.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ServeLimits {
    /// Stop accepting after this many connections. `None` serves until stopped.
    pub max_connections: Option<u32>,
}

/// Why a loop stopped accepting.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StopReason {
    /// The caller's stop future resolved.
    Requested,
    /// [`ServeLimits::max_connections`] connections were accepted.
    Limit,
    /// The listener stopped accepting, with why. A listener's failure isn't one connection's,
    /// so the loop ends rather than retrying a socket that will keep failing.
    ListenerFailed(String),
}

impl StopReason {
    /// Whether the caller stopped the loop, which the CLI reports as `interrupted`: a stop is
    /// a success, and a loop that ended at its limit or on its listener wasn't interrupted.
    pub fn was_requested(&self) -> bool {
        matches!(self, StopReason::Requested)
    }
}

/// What a tunnel reaches: the daemon, the guest port, and the credentials to present.
#[derive(Clone)]
pub struct TunnelTarget {
    pub endpoint: String,
    pub agent_token: String,
    /// The session's proxy credential; `None` for a direct session.
    pub auth: Option<Arc<ProxyAuth>>,
    pub guest_port: u16,
    /// The pair to verify the far end with (`--verify-identity`), when asked for.
    pub identity: Option<TunnelIdentity>,
}

impl TunnelTarget {
    /// The target for `guest_port` over `session`'s endpoint and credentials.
    pub fn for_session(
        session: &Session,
        guest_port: u16,
        identity: Option<TunnelIdentity>,
    ) -> Self {
        Self {
            endpoint: session.endpoint().to_string(),
            agent_token: session.agent_token().to_string(),
            auth: session.proxy_auth().cloned(),
            guest_port,
            identity,
        }
    }
}

/// How one tunnel connection ended.
#[derive(Debug)]
pub struct TunnelConnection {
    pub peer: SocketAddr,
    pub end: Result<TunnelEnd, Error>,
}

/// What a tunnel loop did.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TunnelReport {
    /// Connections accepted.
    pub served: u32,
    /// Connections the daemon refused, or that failed with an error.
    pub refused: u32,
    /// Verified connections that ended without the daemon's end of stream (#342).
    pub truncated: u32,
    /// Verified connections whose daemon predates the end of stream (#342).
    pub unproven: u32,
    /// Proxy tokens the session minted by the time the loop ended.
    pub proxy_token_mints: u64,
    pub stopped: StopReason,
}

impl TunnelReport {
    /// Counts one connection's end in its column. A clean end is only served.
    fn count(&mut self, end: &Result<TunnelEnd, Error>) {
        match end {
            Ok(TunnelEnd::Closed) => {}
            Ok(TunnelEnd::ClosedUnproven) => self.unproven += 1,
            Ok(TunnelEnd::Truncated { .. }) => self.truncated += 1,
            Ok(TunnelEnd::Refused { .. }) | Err(_) => self.refused += 1,
        }
    }
}

/// Serves `listener` as a TCP tunnel to `target` until `stop` resolves, the limit is reached,
/// or the listener fails, then waits for the open connections to end.
///
/// `on_end` hears about each connection as it ends, on the loop's own task.
pub async fn serve_tunnel<F>(
    listener: TcpListener,
    target: TunnelTarget,
    limits: ServeLimits,
    stop: impl Future<Output = ()>,
    mut on_end: F,
) -> TunnelReport
where
    F: FnMut(TunnelConnection),
{
    let target = Arc::new(target);
    let mut report = TunnelReport {
        served: 0,
        refused: 0,
        truncated: 0,
        unproven: 0,
        proxy_token_mints: 0,
        stopped: StopReason::Requested,
    };
    let (stopped, served) = serve(
        &listener,
        limits,
        stop,
        |stream, _| {
            let target = Arc::clone(&target);
            async move {
                relay_connection_inner(
                    stream,
                    &target.endpoint,
                    target.guest_port,
                    &target.agent_token,
                    target.auth.as_ref(),
                    target.identity.as_ref(),
                )
                .await
            }
        },
        |peer, end| {
            report.count(&end);
            on_end(TunnelConnection { peer, end });
        },
    )
    .await;
    report.served = served;
    report.stopped = stopped;
    report.proxy_token_mints = target.auth.as_ref().map_or(0, |auth| auth.mint_count());
    report
}

/// Something one forwarded connection did, for a caller's warnings.
#[derive(Debug)]
pub enum ForwardNotice {
    /// An event the forwarder reported for the connection from `peer`.
    Event {
        peer: SocketAddr,
        event: ForwardEvent,
    },
    /// The connection from `peer` ended early, with why.
    Failed { peer: SocketAddr, error: Error },
}

/// What a port-forward loop did.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ForwardReport {
    /// Connections accepted.
    pub served: u32,
    /// Exchanges the proxy refused ([`ForwardEvent::Refused`]).
    pub refused: u32,
    /// Exchanges that upgraded, a WebSocket among them.
    pub upgrades: u32,
    /// Proxy tokens the session minted by the time the loop ended.
    pub proxy_token_mints: u64,
    pub stopped: StopReason,
}

impl ForwardReport {
    /// Counts one of a connection's events in its column.
    fn count(&mut self, event: &ForwardEvent) {
        match event {
            ForwardEvent::Refused { .. } => self.refused += 1,
            ForwardEvent::Forwarded { upgraded: true, .. } => self.upgrades += 1,
            _ => {}
        }
    }
}

/// Serves `listener` as an HTTP and WebSocket forward to `spec`'s guest port until `stop`
/// resolves, the limit is reached, or the listener fails, then waits for the open connections
/// to end.
///
/// `auth` is the session's proxy credential, `None` for a direct session. `on_notice` hears
/// about each connection's events and failure as it ends, on the loop's own task.
pub async fn serve_forward<F>(
    listener: TcpListener,
    spec: ForwardSpec,
    auth: Option<Arc<ProxyAuth>>,
    limits: ServeLimits,
    stop: impl Future<Output = ()>,
    mut on_notice: F,
) -> Result<ForwardReport, Error>
where
    F: FnMut(ForwardNotice),
{
    // One client for the whole forward: connection reuse to the endpoint is what keeps a
    // page-load of thirty assets from paying thirty TLS handshakes.
    let client = Arc::new(ForwardClient::new()?);
    let spec = Arc::new(spec);
    let mut report = ForwardReport {
        served: 0,
        refused: 0,
        upgrades: 0,
        proxy_token_mints: 0,
        stopped: StopReason::Requested,
    };
    let (stopped, served) = serve(
        &listener,
        limits,
        stop,
        |stream, _| {
            let (spec, auth, client) = (Arc::clone(&spec), auth.clone(), Arc::clone(&client));
            async move {
                let mut events = Vec::new();
                let outcome =
                    serve_connection_via(stream, &spec, auth.as_ref(), &client, |event| {
                        events.push(event);
                    })
                    .await;
                outcome.map(|()| events)
            }
        },
        |peer, done| {
            let (events, failure) = match done {
                Ok(events) => (events, None),
                Err(error) => (Vec::new(), Some(error)),
            };
            for event in events {
                report.count(&event);
                on_notice(ForwardNotice::Event { peer, event });
            }
            if let Some(error) = failure {
                on_notice(ForwardNotice::Failed { peer, error });
            }
        },
    )
    .await;
    report.served = served;
    report.stopped = stopped;
    report.proxy_token_mints = auth.as_ref().map_or(0, |auth| auth.mint_count());
    Ok(report)
}

/// The accept loop both serving loops share: accept until stopped, a task per connection, then
/// wait for the open ones. Answers why it stopped and how many connections it accepted.
///
/// `spawn` makes a connection's task. `ended` takes each connection's result as its task ends,
/// on this task; a task that panicked or was cancelled reads as an error.
async fn serve<T, Fut>(
    listener: &TcpListener,
    limits: ServeLimits,
    stop: impl Future<Output = ()>,
    mut spawn: impl FnMut(tokio::net::TcpStream, SocketAddr) -> Fut,
    mut ended: impl FnMut(SocketAddr, Result<T, Error>),
) -> (StopReason, u32)
where
    T: Send + 'static,
    Fut: Future<Output = Result<T, Error>> + Send + 'static,
{
    let mut stop = std::pin::pin!(stop);
    let mut tasks = JoinSet::new();
    // Peers by task, so a task that panics is still reported against its connection.
    let mut peers = Peers::new();
    type Peers = std::collections::HashMap<tokio::task::Id, SocketAddr>;
    let mut finish =
        |peers: &mut Peers,
         joined: Result<(tokio::task::Id, Result<T, Error>), tokio::task::JoinError>| {
            let (id, result) = match joined {
                Ok((id, result)) => (id, result),
                Err(error) => (
                    error.id(),
                    Err(Error::new(
                        ErrorKind::Unexpected,
                        format!("the connection's task ended abnormally: {error}"),
                    )),
                ),
            };
            let peer = peers
                .remove(&id)
                .unwrap_or_else(|| SocketAddr::from(([0, 0, 0, 0], 0)));
            ended(peer, result);
        };
    let mut served = 0_u32;
    let stopped = loop {
        if limits.max_connections.is_some_and(|max| served >= max) {
            break StopReason::Limit;
        }
        tokio::select! {
            // Biased so a stop wins over a connection that arrived in the same wakeup: the
            // caller asked to stop, and serving one more first looks like the stop did nothing.
            biased;
            () = &mut stop => break StopReason::Requested,
            Some(joined) = tasks.join_next_with_id(), if !tasks.is_empty() => finish(&mut peers, joined),
            next = listener.accept() => match next {
                Ok((stream, peer)) => {
                    served += 1;
                    let handle = tasks.spawn(spawn(stream, peer));
                    peers.insert(handle.id(), peer);
                }
                Err(error) => {
                    break StopReason::ListenerFailed(format!(
                        "the local listener stopped accepting: {error}"
                    ));
                }
            },
        }
    };
    // Drained rather than abandoned: see the module docs.
    while let Some(joined) = tasks.join_next_with_id().await {
        finish(&mut peers, joined);
    }
    (stopped, served)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tunnel_report() -> TunnelReport {
        TunnelReport {
            served: 0,
            refused: 0,
            truncated: 0,
            unproven: 0,
            proxy_token_mints: 0,
            stopped: StopReason::Requested,
        }
    }

    /// Each way a tunnel connection ends lands in its own column, once.
    #[test]
    fn each_tunnel_end_is_counted_once_in_its_column() {
        let mut report = tunnel_report();
        for end in [
            Ok(TunnelEnd::Closed),
            Ok(TunnelEnd::ClosedUnproven),
            Ok(TunnelEnd::ClosedUnproven),
            Ok(TunnelEnd::Truncated { code: None }),
            Ok(TunnelEnd::Truncated { code: Some(1000) }),
            Ok(TunnelEnd::Truncated { code: Some(1000) }),
            Ok(TunnelEnd::Refused {
                code: 4502,
                reason: String::new(),
            }),
            Err(Error::new(ErrorKind::Unexpected, "a transport error")),
        ] {
            report.count(&end);
        }
        assert_eq!(
            (report.refused, report.truncated, report.unproven),
            (2, 3, 2),
            "{report:?}"
        );
    }

    /// Only the caller's own stop reads as requested.
    #[test]
    fn only_the_callers_stop_was_requested() {
        assert!(StopReason::Requested.was_requested());
        assert!(!StopReason::Limit.was_requested());
        assert!(!StopReason::ListenerFailed("gone".to_string()).was_requested());
    }

    /// A refusal and an upgrade each land in their column; other events count nowhere.
    #[test]
    fn each_forward_event_is_counted_once_in_its_column() {
        let mut report = ForwardReport {
            served: 0,
            refused: 0,
            upgrades: 0,
            proxy_token_mints: 0,
            stopped: StopReason::Requested,
        };
        for event in [
            ForwardEvent::Refused {
                status: 502,
                explanation: String::new(),
            },
            ForwardEvent::Forwarded {
                status: 101,
                upgraded: true,
            },
            ForwardEvent::Forwarded {
                status: 101,
                upgraded: true,
            },
            ForwardEvent::Forwarded {
                status: 200,
                upgraded: false,
            },
            ForwardEvent::ConnectionError {
                detail: String::new(),
            },
        ] {
            report.count(&event);
        }
        assert_eq!((report.refused, report.upgrades), (1, 2), "{report:?}");
    }
}
