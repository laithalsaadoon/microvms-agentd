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
//! Only an SDK handle cuts them, when its caller's `stop` gives a grace that runs out or the
//! handle is dropped.
//!
//! # A direct session serves too
//!
//! A session with no proxy credential reaches the daemon without the endpoint proxy. The
//! tunnel then offers no proxy subprotocols, and the daemon's bearer check is the gate; the
//! forwarder mints no proxy headers and sends each request to the endpoint's host at the guest
//! port ([`super::forward::direct_url`]), since there is no proxy to route it by header.

use std::future::Future;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use tokio::net::TcpListener;
use tokio::sync::{oneshot, watch};
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
    on_end: F,
) -> TunnelReport
where
    F: FnMut(TunnelConnection),
{
    tunnel_loop(
        listener,
        target,
        limits,
        stop,
        std::future::pending(),
        on_end,
    )
    .await
}

/// [`serve_tunnel`], cutting the connections still open once `cut` resolves after the stop.
async fn tunnel_loop<F>(
    listener: TcpListener,
    target: TunnelTarget,
    limits: ServeLimits,
    stop: impl Future<Output = ()>,
    cut: impl Future<Output = ()>,
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
        cut,
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
    on_notice: F,
) -> Result<ForwardReport, Error>
where
    F: FnMut(ForwardNotice),
{
    forward_loop(
        listener,
        spec,
        auth,
        limits,
        stop,
        std::future::pending(),
        on_notice,
    )
    .await
}

/// [`serve_forward`], cutting the connections still open once `cut` resolves after the stop.
async fn forward_loop<F>(
    listener: TcpListener,
    spec: ForwardSpec,
    auth: Option<Arc<ProxyAuth>>,
    limits: ServeLimits,
    stop: impl Future<Output = ()>,
    cut: impl Future<Output = ()>,
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
        cut,
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

// ── the SDKs' handles ────────────────────────────────────────────────────────

/// Where an SDK's tunnel or forward listens unless told otherwise: loopback, on a port the OS
/// picks. Loopback because a forward carries the VM's credentials to whoever connects.
pub const DEFAULT_BIND: SocketAddr = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0));

/// A local address to listen on: `bind` when given, [`DEFAULT_BIND`] otherwise.
pub fn bind_address(bind: Option<&str>) -> Result<SocketAddr, Error> {
    match bind {
        None => Ok(DEFAULT_BIND),
        Some(bind) => bind.parse().map_err(|err| {
            Error::new(
                ErrorKind::InvalidArg,
                format!("{bind:?} is not a local address to listen on, such as 127.0.0.1:0: {err}"),
            )
        }),
    }
}

/// How a connection that didn't end clean ended, as a handle's report lists it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EndKind {
    /// The daemon refused the tunnel, or the proxy refused the exchange.
    Refused,
    /// A verified tunnel ended without the daemon's end of stream (#342).
    Truncated,
    /// A verified tunnel ended into a daemon from before the end of stream (#342).
    Unproven,
    /// The connection failed with an error.
    Failed,
}

impl EndKind {
    /// The wire spelling the bindings report.
    pub fn as_str(self) -> &'static str {
        match self {
            EndKind::Refused => "refused",
            EndKind::Truncated => "truncated",
            EndKind::Unproven => "unproven",
            EndKind::Failed => "failed",
        }
    }
}

/// One connection that didn't end clean. A clean end is only counted, so a long-lived handle's
/// report grows with its failures, not with its traffic.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConnectionEnd {
    pub peer: SocketAddr,
    pub kind: EndKind,
    /// The close code or the HTTP status, when the end carried one.
    pub code: Option<u16>,
    /// The daemon's reason, the forwarder's explanation, or the error.
    pub detail: String,
}

impl ConnectionEnd {
    /// A tunnel connection's end, unless it was clean.
    pub fn of_tunnel(connection: &TunnelConnection) -> Option<Self> {
        let (kind, code, detail) = match &connection.end {
            Ok(TunnelEnd::Closed) => return None,
            Ok(TunnelEnd::ClosedUnproven) => (EndKind::Unproven, None, String::new()),
            Ok(TunnelEnd::Truncated { code }) => (EndKind::Truncated, *code, String::new()),
            Ok(TunnelEnd::Refused { code, reason }) => {
                (EndKind::Refused, Some(*code), reason.clone())
            }
            Err(error) => (EndKind::Failed, None, error.to_string()),
        };
        Some(Self {
            peer: connection.peer,
            kind,
            code,
            detail,
        })
    }

    /// A forwarded connection's refusal or failure; its other events aren't ends worth listing.
    pub fn of_forward(notice: &ForwardNotice) -> Option<Self> {
        match notice {
            ForwardNotice::Event {
                peer,
                event:
                    ForwardEvent::Refused {
                        status,
                        explanation,
                    },
            } => Some(Self {
                peer: *peer,
                kind: EndKind::Refused,
                code: Some(*status),
                detail: explanation.clone(),
            }),
            ForwardNotice::Event { .. } => None,
            ForwardNotice::Failed { peer, error } => Some(Self {
                peer: *peer,
                kind: EndKind::Failed,
                code: None,
                detail: error.to_string(),
            }),
        }
    }
}

/// What a tunnel handle did: the loop's report, and each connection that didn't end clean.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TunnelSummary {
    pub report: TunnelReport,
    pub ended: Vec<ConnectionEnd>,
}

/// What a port-forward handle did: the loop's report, and each connection that didn't end
/// clean.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ForwardSummary {
    pub report: ForwardReport,
    pub ended: Vec<ConnectionEnd>,
}

type Outcome<S> = Result<S, (ErrorKind, String)>;

/// A serving loop on a background task: the SDKs' tunnel and port-forward handles.
///
/// Dropping it stops the loop and cuts the connections still open, so no relay outlives the
/// handle that owns it: a relay carries the VM's credentials to whoever connected.
pub struct Serving<S> {
    local: SocketAddr,
    stop: Mutex<Option<oneshot::Sender<()>>>,
    cut: Mutex<Option<oneshot::Sender<()>>>,
    outcome: Arc<Mutex<Option<Outcome<S>>>>,
    done: watch::Receiver<bool>,
}

impl<S: Clone + Send + 'static> Serving<S> {
    /// Runs `serving` on the current tokio runtime, handing it the stop and the cut futures.
    /// Panics outside a runtime, like `tokio::spawn`.
    fn spawn<Fut>(
        local: SocketAddr,
        serving: impl FnOnce(oneshot::Receiver<()>, oneshot::Receiver<()>) -> Fut,
    ) -> Self
    where
        Fut: Future<Output = Result<S, Error>> + Send + 'static,
    {
        let (stop_tx, stop_rx) = oneshot::channel();
        let (cut_tx, cut_rx) = oneshot::channel();
        let (done_tx, done_rx) = watch::channel(false);
        let outcome = Arc::new(Mutex::new(None));
        let written = Arc::clone(&outcome);
        let serving = serving(stop_rx, cut_rx);
        tokio::spawn(async move {
            let result = serving
                .await
                .map_err(|error| (error.kind(), error.to_string()));
            *written.lock().unwrap_or_else(PoisonError::into_inner) = Some(result);
            done_tx.send_replace(true);
        });
        Self {
            local,
            stop: Mutex::new(Some(stop_tx)),
            cut: Mutex::new(Some(cut_tx)),
            outcome,
            done: done_rx,
        }
    }

    /// The address the loop listens on, with the port the OS picked for a port 0.
    pub fn local_address(&self) -> SocketAddr {
        self.local
    }

    /// Whether the loop is still serving, or waiting for its open connections to end.
    pub fn is_running(&self) -> bool {
        !*self.done.borrow()
    }

    /// Asks the loop to stop accepting. Idempotent; [`Self::finished`] waits for it, and for the
    /// connections still open.
    pub fn request_stop(&self) {
        signal(&self.stop);
    }

    /// Stops accepting and waits for the connections still open to end, cutting the ones
    /// still open after `grace` when it's given, and returns what the loop did.
    ///
    /// Without a grace, a client that keeps its connection open keeps this waiting: the
    /// relay holds bytes that client may still be reading, so the loop won't cut it unasked.
    pub async fn stop(&self, grace: Option<Duration>) -> Result<S, Error> {
        self.request_stop();
        if let Some(grace) = grace {
            if let Ok(outcome) = tokio::time::timeout(grace, self.finished()).await {
                return outcome;
            }
            signal(&self.cut);
        }
        self.finished().await
    }

    /// Waits for the loop to end and returns what it did. Callable any number of times.
    pub async fn finished(&self) -> Result<S, Error> {
        let mut done = self.done.clone();
        // The sender drops only after writing the outcome, so an error here still means done.
        let _ = done.wait_for(|finished| *finished).await;
        self.outcome
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .expect("the loop records its outcome before signalling done")
            .map_err(|(kind, message)| Error::new(kind, message))
    }
}

/// Sends a handle's stop or cut, once.
fn signal(sender: &Mutex<Option<oneshot::Sender<()>>>) {
    if let Some(sender) = sender.lock().unwrap_or_else(PoisonError::into_inner).take() {
        let _ = sender.send(());
    }
}

/// A handle's stop or cut future: the sender's `send`, or its drop.
async fn signalled(signal: oneshot::Receiver<()>) {
    let _ = signal.await;
}

/// Binds `bind` and starts a tunnel to `target` on the current tokio runtime: the SDKs'
/// `Session.tunnel`.
pub async fn start_tunnel(
    bind: SocketAddr,
    target: TunnelTarget,
    limits: ServeLimits,
) -> Result<Serving<TunnelSummary>, Error> {
    let listener =
        super::forward::bind(&ForwardSpec::new(bind, target.guest_port, &target.endpoint)).await?;
    let local = local_address(&listener)?;
    Ok(Serving::spawn(local, move |stop, cut| async move {
        let mut ended = Vec::new();
        let report = tunnel_loop(
            listener,
            target,
            limits,
            signalled(stop),
            signalled(cut),
            |connection| ended.extend(ConnectionEnd::of_tunnel(&connection)),
        )
        .await;
        Ok(TunnelSummary { report, ended })
    }))
}

/// Binds `bind` and starts a port-forward to `guest_port` behind `endpoint` on the current
/// tokio runtime: the SDKs' `Session.port_forward`. `auth` is `None` for a direct session.
pub async fn start_forward(
    bind: SocketAddr,
    endpoint: &str,
    guest_port: u16,
    auth: Option<Arc<ProxyAuth>>,
    limits: ServeLimits,
) -> Result<Serving<ForwardSummary>, Error> {
    let spec = ForwardSpec::new(bind, guest_port, endpoint);
    let listener = super::forward::bind(&spec).await?;
    let local = local_address(&listener)?;
    Ok(Serving::spawn(local, move |stop, cut| async move {
        let mut ended = Vec::new();
        let report = forward_loop(
            listener,
            spec,
            auth,
            limits,
            signalled(stop),
            signalled(cut),
            |notice| ended.extend(ConnectionEnd::of_forward(&notice)),
        )
        .await?;
        Ok(ForwardSummary { report, ended })
    }))
}

fn local_address(listener: &TcpListener) -> Result<SocketAddr, Error> {
    listener.local_addr().map_err(|err| {
        Error::new(
            ErrorKind::Unexpected,
            format!("the listener has no address: {err}"),
        )
    })
}

/// The accept loop both serving loops share: accept until stopped, a task per connection, then
/// wait for the open ones, cutting them if `cut` resolves first. Answers why it stopped and how
/// many connections it accepted.
///
/// `spawn` makes a connection's task. `ended` takes each connection's result as its task ends,
/// on this task; a task that panicked or was cut reads as an error.
async fn serve<T, Fut>(
    listener: &TcpListener,
    limits: ServeLimits,
    stop: impl Future<Output = ()>,
    cut: impl Future<Output = ()>,
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
                Err(error) if error.is_cancelled() => (
                    error.id(),
                    Err(Error::new(
                        ErrorKind::Unexpected,
                        "the connection was cut when its loop stopped",
                    )),
                ),
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
    // Drained rather than abandoned (see the module docs), until the caller cuts what's left.
    let mut cut = std::pin::pin!(cut);
    let mut cutting = false;
    loop {
        tokio::select! {
            joined = tasks.join_next_with_id() => match joined {
                Some(joined) => finish(&mut peers, joined),
                None => break,
            },
            () = &mut cut, if !cutting => {
                cutting = true;
                tasks.abort_all();
            }
        }
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

    /// Each end but a clean one is listed with its kind, its code and its reason; a forward's
    /// events other than a refusal aren't ends.
    #[test]
    fn each_end_but_a_clean_one_is_listed_with_its_kind() {
        let peer: SocketAddr = "127.0.0.1:5000".parse().expect("an address");
        let tunnel = |end| ConnectionEnd::of_tunnel(&TunnelConnection { peer, end });
        let listed = |end: Option<ConnectionEnd>| {
            end.map(|end| (end.peer, end.kind.as_str(), end.code, end.detail))
        };
        assert_eq!(tunnel(Ok(TunnelEnd::Closed)), None);
        assert_eq!(
            listed(tunnel(Ok(TunnelEnd::ClosedUnproven))),
            Some((peer, "unproven", None, String::new()))
        );
        assert_eq!(
            listed(tunnel(Ok(TunnelEnd::Truncated { code: Some(1006) }))),
            Some((peer, "truncated", Some(1006), String::new()))
        );
        assert_eq!(
            listed(tunnel(Ok(TunnelEnd::Refused {
                code: 4502,
                reason: "nothing is listening".into(),
            }))),
            Some((peer, "refused", Some(4502), "nothing is listening".into()))
        );
        let failed = listed(tunnel(Err(Error::new(ErrorKind::Unexpected, "a reset"))));
        assert!(
            matches!(&failed, Some((_, "failed", None, detail)) if detail.contains("a reset")),
            "{failed:?}"
        );

        let forward = |event| ConnectionEnd::of_forward(&ForwardNotice::Event { peer, event });
        assert_eq!(
            listed(forward(ForwardEvent::Refused {
                status: 403,
                explanation: "out of scope".into(),
            })),
            Some((peer, "refused", Some(403), "out of scope".into()))
        );
        assert_eq!(
            forward(ForwardEvent::Forwarded {
                status: 200,
                upgraded: false,
            }),
            None
        );
        let failed = listed(ConnectionEnd::of_forward(&ForwardNotice::Failed {
            peer,
            error: Error::new(ErrorKind::Unexpected, "a reset"),
        }));
        assert!(
            matches!(&failed, Some((_, "failed", None, detail)) if detail.contains("a reset")),
            "{failed:?}"
        );
    }

    /// A connection's task that panics reads as ended abnormally, and one the caller cuts
    /// reads as cut: the two are the caller's to tell apart.
    #[tokio::test]
    async fn a_panicked_connection_and_a_cut_one_are_told_apart() {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
        let local = listener.local_addr().expect("bound");
        let (cut, cut_rx) = oneshot::channel::<()>();
        tokio::spawn(async move {
            let _first = tokio::net::TcpStream::connect(local).await;
            let _second = tokio::net::TcpStream::connect(local).await;
            tokio::time::sleep(Duration::from_millis(200)).await;
            let _ = cut.send(());
            std::future::pending::<()>().await;
        });
        let mut accepted = 0;
        let mut ended = Vec::new();
        let serving = serve(
            &listener,
            ServeLimits {
                max_connections: Some(2),
            },
            std::future::pending(),
            signalled(cut_rx),
            |stream, _| {
                accepted += 1;
                let panics = accepted == 1;
                async move {
                    let _held = stream;
                    assert!(!panics, "the first connection's task panics");
                    std::future::pending::<Result<(), Error>>().await
                }
            },
            |_, result| ended.push(result.expect_err("neither ends clean").to_string()),
        );
        let (stopped, served) = tokio::time::timeout(Duration::from_secs(10), serving)
            .await
            .expect("the cut ends the drain");
        assert_eq!((stopped, served), (StopReason::Limit, 2));
        assert_eq!(ended.len(), 2, "{ended:?}");
        let panicked = ended.iter().filter(|end| end.contains("ended abnormally"));
        let cut = ended.iter().filter(|end| end.contains("was cut"));
        assert_eq!((panicked.count(), cut.count()), (1, 1), "{ended:?}");
    }
}
