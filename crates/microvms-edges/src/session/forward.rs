// SPDX-License-Identifier: Apache-2.0
//! A local TCP listener that forwards HTTP and WebSocket traffic to one guest port.
//!
//! The client half of `microvm port-forward`. A caller binds a local address, and every
//! connection accepted there is replayed against the VM's endpoint with the two proxy
//! headers [`ProxyAuth`] mints for the guest port. What arrives at the guest is an ordinary
//! request from an ordinary client: the platform strips both headers before forwarding
//! (measured 2026-08-15, `docs/PLATFORM.md`), so a server inside the VM needs no MicroVM
//! awareness.
//!
//! # Why this is in core rather than in the CLI
//!
//! `crates/microvms-cli/tests/thinness.rs` forbids `hyper`, `http`, and `reqwest` in the CLI's
//! dependency set, because each of them is a second path to AWS that does not go through
//! this crate. A forwarder is an HTTP server, so it cannot live there. Putting it here also
//! puts it behind the same [`ProxyAuth`] the session's own requests use, which is the
//! property TRAP-9 rests on: there is one mint schedule, and a long-lived tunnel opened at
//! minute fifty gets a token minted at minute fifty.
//!
//! # Every hop re-reads the header, and that is the refresh
//!
//! [`ProxyAuth::headers_for_port`] is called per proxied request rather than once at bind
//! time. The token's sixty-minute ceiling is therefore crossed by consulting the cache that
//! already knows about it — no timer here, no expiry arithmetic, and
//! [`ProxyAuth::mint_count`] is the observable that says a refresh happened. A forwarder
//! that captured the headers at startup would work for thirty minutes and then return 401s
//! that read as a wrong agent token.
//!
//! # An upgrade is forwarded as bytes, not as a protocol this module understands
//!
//! A WebSocket handshake is an ordinary GET whose response is 101. After that, neither end
//! speaks HTTP, so this module stops parsing and copies bytes in both directions until one
//! side closes. This code frames nothing after the handshake, so it cannot frame anything
//! wrongly, and an application subprotocol passes through untouched.
//!
//! # The upgrade path does not reach a guest server through the endpoint proxy yet
//!
//! Measured 2026-08-29, us-east-1, against a guest RFC 6455 echo server on port 8090. An
//! upgrade re-issued here as an ordinary HTTPS `GET` — `Upgrade: websocket` and
//! `Connection: Upgrade` forwarded as request headers — is answered by the **proxy** with
//! `400` (an `x-amzn-requestid` on the response, and the guest logged no handshake at all,
//! while the same handshake sent from inside the guest to `127.0.0.1:8090` answers 101).
//!
//! The reason is in `docs/PLATFORM.md`: on the endpoint the WebSocket credential travels as
//! **`Sec-WebSocket-Protocol` values**, not as the two proxy headers, because the browser
//! `WebSocket` constructor cannot set a header. So a working tunnel has to open a real
//! `wss://` handshake offering [`microvms_app::session::Session::connect_subprotocols`]'s three
//! values, rather than replay the client's `GET` over the HTTPS path. That is a genuine
//! WebSocket client in this module, which the HTTP relay below is not.
//!
//! What *is* measured as working on that path, from the same run: a `wss://` handshake
//! offering the three minted subprotocols reaches the guest, and **binary** frames survive
//! byte-exact in both directions on a **port-scoped** token — including `0x00`/`0xFF`
//! payloads that a silent utf-8 round trip would corrupt, and a 300-byte frame that
//! exercises the extended-length header. The guest observed opcode `2` and no
//! `sec-websocket-protocol` header, so the proxy consumed all three values as documented.
//! That is the transport premise the raw-TCP tunnel (issue #70 layer 2) rests on, and it is
//! also what this module needs in order to carry an upgrade.
//!
//! Until that client exists, [`is_upgrade`] and [`splice`] are exercised by the tests below
//! against a local upstream, where the byte relay is correct — the untested edge is the
//! handshake negotiation with the platform, not the splice.
//!
//! # What a failure says
//!
//! The proxy answers **403** for a token whose scope does not cover the port and **502**
//! when the scope is right and nothing is listening in the guest. That pair is the only
//! diagnostic separating a scope mistake from a dead server, and on the WebSocket path both
//! collapse to close code 1006 with no reason. So this module surfaces the distinction from
//! the HTTP status while it still has one, in [`ForwardEvent::Refused`], rather than letting
//! a caller rediscover it from a closed socket.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use futures_util::StreamExt as _;
use http_body_util::combinators::UnsyncBoxBody;
use http_body_util::{BodyExt as _, Full, Limited, StreamBody};
use hyper::body::{Bytes, Frame, Incoming};
use hyper::service::service_fn;
use hyper_util::rt::{TokioIo, TokioTimer};

use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;

use microvms_app::error::{Error, ErrorKind};
use microvms_app::session::proxy::{PROXY_AUTH_HEADER, PROXY_PORT_HEADER, ProxyAuth};

/// The limit for the response head and for each read of a proxied response body.
///
/// Generous rather than tight: the thing on the other end is a developer's dev server, and a
/// long-polling request or a slow first compile is normal traffic rather than a fault.
/// Successful reads reset the body deadline, so a stream can outlive this duration. Time
/// blocked by a slow local reader is excluded. The
/// upgrade path is exempt — a WebSocket is idle by design, so once a connection is upgraded
/// this timeout no longer applies to it.
pub const DEFAULT_EXCHANGE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);

/// What the forwarder did, for a caller that wants to report progress.
///
/// An enum handed to a callback rather than a log line, because this crate does no logging
/// and the CLI owns every byte the user sees. The variants are the three things worth
/// telling somebody watching a tunnel: it is up, a request went through, or the proxy
/// refused one for a reason worth spelling out.
#[derive(Clone, Debug)]
pub enum ForwardEvent {
    /// The listener is bound and accepting. Carries the address actually bound, which
    /// differs from the requested one when the caller asked for port 0.
    Listening { local: SocketAddr, guest_port: u16 },
    /// One exchange completed with this status.
    Forwarded { status: u16, upgraded: bool },
    /// The proxy refused the exchange, with the distinction the status carries.
    ///
    /// `403` means the minted token's scope does not cover `guest_port`; `502` means the
    /// scope is right and nothing is listening there. Anything else is passed through
    /// unclassified rather than guessed at.
    Refused { status: u16, explanation: String },
    /// A connection ended in a way worth mentioning but not worth failing the tunnel for.
    ConnectionError { detail: String },
}

/// The 403-vs-502 sentence, or `None` for a status that needs no explaining.
///
/// Free function so the wording is tested without standing up a listener, and so the CLI
/// cannot drift from it by writing its own.
pub fn refusal_explanation(status: u16, guest_port: u16) -> Option<String> {
    match status {
        403 => Some(format!(
            "the endpoint proxy refused the request for port {guest_port} (403 Access to port \
             denied): the minted token's scope does not cover it. Nothing was wrong with the \
             guest — the credential never authorized this port."
        )),
        502 => Some(format!(
            "the endpoint proxy reached the VM and found nothing listening on port \
             {guest_port} (502). The credential is correct and the scope covers the port, so \
             this is a dead server in the guest rather than an auth problem."
        )),
        401 => Some(
            "the endpoint proxy rejected the credential (401). On a long-lived tunnel this \
             usually means a minted token expired without being refreshed; the mint count is \
             the observable that says whether a refresh happened."
                .to_string(),
        ),
        _ => None,
    }
}

/// Copies bytes both ways until one side closes, then returns.
///
/// Used for the post-101 tail of an upgraded connection. `copy_bidirectional` rather than
/// two hand-rolled loops: it already handles the half-close case where one direction ends
/// while the other still has data, which is the case a naive `select!` loop truncates.
pub async fn splice<A, B>(a: &mut A, b: &mut B) -> Result<(u64, u64), Error>
where
    A: AsyncRead + AsyncWrite + Unpin + ?Sized,
    B: AsyncRead + AsyncWrite + Unpin + ?Sized,
{
    tokio::io::copy_bidirectional(a, b).await.map_err(|err| {
        Error::new(
            ErrorKind::Unexpected,
            format!("the upgraded connection ended abnormally: {err}"),
        )
    })
}

/// Where the forwarder listens and what it forwards to.
///
/// A struct rather than four arguments for [`crate::seam`]'s reason: `local_port` and
/// `guest_port` are both `u16` and both plausible in either position, so a positional call
/// is how a tunnel ends up pointing at itself.
#[derive(Clone, Debug)]
pub struct ForwardSpec {
    /// The local address to bind. Port 0 asks the OS for a free one, which the
    /// [`ForwardEvent::Listening`] event then reports. When calling
    /// [`serve_connection`] directly, use the accepted socket's local address here
    /// so the request Host can be checked against its concrete address and port.
    pub bind: SocketAddr,
    /// The port inside the guest to forward to.
    pub guest_port: u16,
    /// The VM's endpoint host, as `run` reported it.
    pub endpoint: String,
    /// The response-head deadline and the maximum gap between response-body reads.
    /// Successful body reads reset the deadline; local backpressure is excluded.
    /// Upgrades have no read deadline.
    pub exchange_timeout: std::time::Duration,
}

impl ForwardSpec {
    /// A spec with the default exchange timeout.
    pub fn new(bind: SocketAddr, guest_port: u16, endpoint: impl Into<String>) -> Self {
        Self {
            bind,
            guest_port,
            endpoint: endpoint.into(),
            exchange_timeout: DEFAULT_EXCHANGE_TIMEOUT,
        }
    }
}

/// The absolute URL a proxied request targets, given the endpoint and the request target.
///
/// A bare host is read as `https`, for the reason [`crate::session::http::ReqwestBackend`]
/// gives: the platform hands back a hostname, and defaulting to plain HTTP on the strength
/// of a missing prefix would put a bearer token on the wire in clear text.
pub fn upstream_url(endpoint: &str, target: &str) -> String {
    let base = endpoint.trim_end_matches('/');
    let base = if base.starts_with("https://") || base.starts_with("http://") {
        base.to_string()
    } else {
        format!("https://{base}")
    };
    if target.starts_with('/') {
        format!("{base}{target}")
    } else {
        format!("{base}/{target}")
    }
}

/// The URL a direct session's request targets: the endpoint's host at `guest_port`.
///
/// Through the proxy the guest port travels as a header; with no proxy in the path, the port
/// is the port. The scheme is the endpoint's, read the way [`upstream_url`] reads it.
pub fn direct_url(endpoint: &str, guest_port: u16, target: &str) -> Result<String, Error> {
    let mut url = reqwest::Url::parse(&upstream_url(endpoint, target)).map_err(|err| {
        Error::new(
            ErrorKind::InvalidArg,
            format!("{endpoint} is not an endpoint a request can be addressed to: {err}"),
        )
    })?;
    url.set_port(Some(guest_port)).map_err(|()| {
        Error::new(
            ErrorKind::InvalidArg,
            format!("{endpoint} names no host a port can be set on"),
        )
    })?;
    Ok(url.to_string())
}

// A Host names this listener, including its port, before any proxy credential is minted.
// Authority parsing rejects URL components; duplicate Host fields are never combined.
fn local_host_matches(headers: &[(String, String)], bind: SocketAddr) -> bool {
    let mut hosts = headers
        .iter()
        .filter(|(name, _)| name.eq_ignore_ascii_case("host"));
    let Some((_, value)) = hosts.next() else {
        return false;
    };
    if hosts.next().is_some() {
        return false;
    }
    let Ok(authority) = value.parse::<http::uri::Authority>() else {
        return false;
    };
    if value.contains('@') {
        return false;
    }
    let port = match authority.port_u16() {
        Some(port) => port,
        None if authority.as_str() == authority.host() => 80,
        None => return false,
    };
    if port != bind.port() {
        return false;
    }
    let host = authority.host();
    if host.eq_ignore_ascii_case("localhost") {
        return bind.ip().is_loopback();
    }
    let host = host
        .strip_prefix('[')
        .and_then(|host| host.strip_suffix(']'))
        .unwrap_or(host);
    host.parse::<std::net::IpAddr>().is_ok_and(|ip| {
        if bind.ip().is_loopback() {
            ip.is_loopback()
        } else {
            ip == bind.ip()
        }
    })
}

/// Header names this forwarder must never copy from the caller's request.
///
/// The two proxy headers are minted per hop, so a client that sent its own would either be
/// overridden silently or — worse — have its value forwarded and the mint skipped. Refusing
/// to copy them means the minted pair is the only pair, which is what makes the mint
/// schedule authoritative. `host` is dropped because the upstream host is the endpoint's,
/// not the local listener's.
pub const STRIPPED_REQUEST_HEADERS: [&str; 3] = [PROXY_AUTH_HEADER, PROXY_PORT_HEADER, "host"];

/// Whether a header from the local client is forwarded upstream.
pub fn forwards_request_header(name: &str) -> bool {
    !STRIPPED_REQUEST_HEADERS
        .iter()
        .any(|stripped| stripped.eq_ignore_ascii_case(name))
}

/// The HTTP client a forwarder's hops go through.
///
/// A newtype rather than a re-exported `reqwest::Client`, because the CLI is forbidden from
/// naming `reqwest` at all (`crates/microvms-cli/tests/thinness.rs`) — so a caller there cannot build
/// one, and handing back the foreign type would only move the compile error. Pooled for the
/// whole tunnel: a page-load of thirty assets should not pay thirty TLS handshakes.
pub struct ForwardClient {
    inner: reqwest::Client,
}

impl ForwardClient {
    /// A pooled client for one tunnel.
    pub fn new() -> Result<Self, Error> {
        reqwest::Client::builder()
            // Relay redirects to the local client without requesting their destination.
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map(|inner| Self { inner })
            .map_err(|err| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("could not build the forwarder's HTTP client: {err}"),
                )
            })
    }
}

impl std::fmt::Debug for ForwardClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ForwardClient").finish_non_exhaustive()
    }
}

/// Binds the listener and reports the address, without serving anything yet.
///
/// Split from serving so a caller can print "listening on 8080" before the first
/// connection, and so a bind failure — the common one, an address already in use — is a
/// distinct error from a forwarding failure.
pub async fn bind(spec: &ForwardSpec) -> Result<TcpListener, Error> {
    TcpListener::bind(spec.bind).await.map_err(|err| {
        Error::new(
            ErrorKind::InvalidArg,
            format!(
                "could not bind {}: {err}. Another process is probably already listening \
                 there; pass a different local port.",
                spec.bind
            ),
        )
    })
}

/// The minted proxy headers for this spec's guest port, plus the `host` the endpoint wants.
///
/// One place, called per exchange. See the module docs on why this is not hoisted out of the
/// request path.
pub async fn hop_headers(
    auth: &Arc<ProxyAuth>,
    guest_port: u16,
) -> Result<Vec<(String, String)>, Error> {
    auth.headers_for_port(guest_port).await
}

/// Accepts one connection, for a caller driving an accept loop of its own.
///
/// The CLI's and the SDKs' loop is [`super::serve::serve_forward`], which takes the stop, the
/// limit and a callback as parameters; this is for a caller that wants the primitive.
pub async fn accept(listener: &TcpListener) -> Result<(TcpStream, SocketAddr), Error> {
    listener.accept().await.map_err(|err| {
        Error::new(
            ErrorKind::Unexpected,
            format!("the local listener stopped accepting: {err}"),
        )
    })
}

/// Whether this request/response pair is a protocol upgrade the forwarder must splice.
///
/// Reads the **response** status rather than the request's `Upgrade` header, deliberately: a
/// client may offer an upgrade the guest declines, and in that case the exchange is ordinary
/// HTTP with an ordinary body. 101 is the only status that means "neither end speaks HTTP
/// from here", which is the only condition under which splicing bytes is correct.
pub fn is_upgrade(status: u16) -> bool {
    status == 101
}

/// One end of the local connection, as a duplex stream.
///
/// A trait alias in all but name, so the serving functions below take either a real
/// [`TcpStream`] or the in-memory duplex a test uses. Without it every test of the serving
/// path would need a real socket, and the failure modes worth testing — a refusal, an
/// upgrade, a mint inside the request path — have nothing to do with sockets.
pub trait LocalStream: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> LocalStream for T {}

/// Serves one local connection: reads its requests, forwards them, and either relays each
/// response or splices an upgraded stream.
///
/// # The request is parsed, not tunnelled
///
/// A local client speaks cleartext HTTP to this listener while the upstream hop is HTTPS to
/// the endpoint, so the two cannot be a byte pipe: the request has to be understood well
/// enough to be re-issued with the minted headers, and the response well enough to know
/// whether 101 arrived. Once it has, parsing stops — see the module docs.
///
/// # A refusal is answered, not dropped
///
/// A 403 or 502 from the proxy becomes a plain-text HTTP response carrying
/// [`refusal_explanation`]'s sentence, so a browser renders the reason. Dropping the socket
/// would render as "the site can't be reached", which names the wrong component.
pub async fn serve_connection<S, F>(
    local: S,
    spec: &ForwardSpec,
    auth: &Arc<ProxyAuth>,
    client: &ForwardClient,
    on_event: F,
) -> Result<(), Error>
where
    S: LocalStream,
    F: FnMut(ForwardEvent),
{
    serve_connection_via(local, spec, Some(auth), client, on_event).await
}

/// [`serve_connection`] over an optional proxy credential.
///
/// `None` is a direct session's, which is already on the far side of the endpoint proxy: no
/// proxy headers are minted, and the request goes to the endpoint's host at the guest port
/// ([`direct_url`]), since there is no proxy to route it there by header. The serving loop
/// (`super::serve`) takes whichever the session has.
pub(crate) async fn serve_connection_via<S, F>(
    local: S,
    spec: &ForwardSpec,
    auth: Option<&Arc<ProxyAuth>>,
    client: &ForwardClient,
    mut on_event: F,
) -> Result<(), Error>
where
    S: LocalStream,
    F: FnMut(ForwardEvent),
{
    let (events, mut pending_events) = mpsc::unbounded_channel();
    let upgraded = Mutex::new(None);
    let answered = AtomicBool::new(false);
    let service = service_fn(|request| {
        let result = forward_request(request, spec, auth, client, &events, &upgraded);
        let answered = &answered;
        Box::pin(async move {
            let response = result.await;
            if response.is_ok() {
                answered.store(true, Ordering::Relaxed);
            }
            response
        })
    });
    let mut builder = hyper::server::conn::http1::Builder::new();
    builder
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_TIMEOUT)
        .max_buf_size(MAX_REQUEST_HEAD_BYTES);
    // The public primitive accepts borrowed streams. Hyper's automatic upgrade API
    // requires 'static I/O, so take its buffered bytes and socket back explicitly.
    let mut connection = builder.serve_connection(TokioIo::new(local), service);
    let result = {
        let polling = futures_util::future::poll_fn(|cx| connection.poll_without_shutdown(cx));
        tokio::pin!(polling);
        loop {
            tokio::select! {
                result = &mut polling => break result,
                Some(event) = pending_events.recv() => on_event(event),
            }
        }
    };
    while let Ok(event) = pending_events.try_recv() {
        on_event(event);
    }
    let parts = connection.into_parts();
    if let Err(error) = result {
        // The next request's timer also bounds idle keep-alive. Closing an empty
        // idle connection is normal; partial headers still report a timeout.
        let idle =
            error.is_timeout() && answered.load(Ordering::Relaxed) && parts.read_buf.is_empty();
        if !idle {
            return Err(Error::new(
                ErrorKind::Protocol,
                format!("the local HTTP connection failed: {error}"),
            ));
        }
    }
    let upstream = upgraded.lock().expect("upgrade lock poisoned").take();
    if let Some(mut upstream) = upstream {
        // Replay read-ahead through the same bidirectional pump, so either peer can
        // keep sending while the buffered client bytes wait for upstream capacity.
        let (read, write) = tokio::io::split(parts.io.into_inner());
        let read = tokio::io::AsyncReadExt::chain(std::io::Cursor::new(parts.read_buf), read);
        let mut local = tokio::io::join(read, write);
        splice(&mut local, &mut upstream).await?;
    }
    Ok(())
}

// Bound both initial headers and idle keep-alive connections. Bodies get their own
// deadline below, after Host validation, and cannot allocate past the fixed cap.
const HEADER_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);
const MAX_REQUEST_BODY_BYTES: usize = 16 * 1024 * 1024;

type ForwardBody = UnsyncBoxBody<Bytes, Error>;

fn full_body(body: Bytes) -> ForwardBody {
    Full::new(body)
        .map_err(|never| match never {})
        .boxed_unsync()
}

fn local_response(status: u16, text: String) -> http::Response<ForwardBody> {
    let mut response = http::Response::new(full_body(Bytes::from(text)));
    *response.status_mut() =
        http::StatusCode::from_u16(status).unwrap_or(http::StatusCode::BAD_GATEWAY);
    response.headers_mut().insert(
        http::header::CONTENT_TYPE,
        http::HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    response.headers_mut().insert(
        http::header::CONNECTION,
        http::HeaderValue::from_static("close"),
    );
    response
}

// Connection options name headers that belong to this hop, including custom ones.
// Only a negotiated upgrade carries its connection and upgrade headers onward.
fn strip_hop_headers(headers: &mut http::HeaderMap, upgrade: bool) {
    let nominated = headers
        .get_all(http::header::CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .filter_map(|name| http::HeaderName::from_bytes(name.trim().as_bytes()).ok())
        .collect::<Vec<_>>();
    for name in nominated {
        if !upgrade || name != http::header::UPGRADE {
            headers.remove(name);
        }
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
    ] {
        if !upgrade || (name != "connection" && name != "upgrade") {
            headers.remove(name);
        }
    }
    if upgrade {
        headers.insert(
            http::header::CONNECTION,
            http::HeaderValue::from_static("upgrade"),
        );
    }
}

async fn forward_request(
    request: http::Request<Incoming>,
    spec: &ForwardSpec,
    auth: Option<&Arc<ProxyAuth>>,
    client: &ForwardClient,
    events: &mpsc::UnboundedSender<ForwardEvent>,
    upgraded: &Mutex<Option<reqwest::Upgraded>>,
) -> Result<http::Response<ForwardBody>, Error> {
    let headers = request
        .headers()
        .get_all(http::header::HOST)
        .iter()
        .map(|value| ("host".to_string(), value.to_str().unwrap_or("").to_string()))
        .collect::<Vec<_>>();
    if !local_host_matches(&headers, spec.bind) {
        let explanation = "the request Host must name the local forward and its port".to_string();
        let _ = events.send(ForwardEvent::Refused {
            status: 403,
            explanation: explanation.clone(),
        });
        return Ok(local_response(403, explanation));
    }
    if request
        .headers()
        .get(http::header::CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .is_some_and(|length| length > MAX_REQUEST_BODY_BYTES as u64)
    {
        return Ok(local_response(
            413,
            "the local request body exceeds 16 MiB".into(),
        ));
    }
    let (mut head, body) = request.into_parts();
    let body = match tokio::time::timeout(
        HEADER_TIMEOUT,
        Limited::new(body, MAX_REQUEST_BODY_BYTES).collect(),
    )
    .await
    {
        Ok(Ok(body)) => body.to_bytes(),
        Ok(Err(error)) if error.is::<http_body_util::LengthLimitError>() => {
            return Ok(local_response(
                413,
                "the local request body exceeds 16 MiB".into(),
            ));
        }
        Ok(Err(_)) => {
            return Ok(local_response(
                400,
                "the local request body is malformed".into(),
            ));
        }
        Err(_) => {
            return Ok(local_response(
                408,
                "the local request body timed out".into(),
            ));
        }
    };
    let target = head
        .uri
        .path_and_query()
        .map_or("/", |target| target.as_str());
    // Minted per exchange, including each request on a kept-alive connection.
    let (minted, url) = match auth {
        Some(auth) => (
            hop_headers(auth, spec.guest_port).await?,
            upstream_url(&spec.endpoint, target),
        ),
        None => (
            Vec::new(),
            direct_url(&spec.endpoint, spec.guest_port, target)?,
        ),
    };
    let offered_upgrade = head.headers.contains_key(http::header::UPGRADE);
    strip_hop_headers(&mut head.headers, offered_upgrade);
    let head_only = head.method == http::Method::HEAD;
    let mut request = client.inner.request(head.method, &url);
    for (name, value) in &head.headers {
        // Hyper decoded the local transfer framing; reqwest frames the collected bytes.
        if forwards_request_header(name.as_str())
            && name != http::header::TRANSFER_ENCODING
            && name != http::header::CONTENT_LENGTH
        {
            request = request.header(name, value);
        }
    }
    for (name, value) in &minted {
        request = request.header(name, value);
    }
    // A head needs an absolute deadline so an upstream cannot keep it alive by
    // trickling header bytes. Body reads have their own deadline below.
    let response = tokio::time::timeout(spec.exchange_timeout, request.body(body).send())
        .await
        .map_err(|_| Error::new(ErrorKind::Unexpected, "the guest's response head timed out"))?
        .map_err(|err| {
            Error::new(
                ErrorKind::Unexpected,
                format!("forwarding the request to the endpoint failed: {err}"),
            )
        })?;
    let status = response.status();
    if let Some(explanation) = refusal_explanation(status.as_u16(), spec.guest_port) {
        let _ = events.send(ForwardEvent::Refused {
            status: status.as_u16(),
            explanation: explanation.clone(),
        });
        return Ok(local_response(status.as_u16(), explanation));
    }
    let mut headers = response.headers().clone();
    let is_upgrade = is_upgrade(status.as_u16());
    strip_hop_headers(&mut headers, is_upgrade);
    let event = ForwardEvent::Forwarded {
        status: status.as_u16(),
        upgraded: is_upgrade,
    };
    let body = if is_upgrade {
        let stream = response.upgrade().await.map_err(|err| {
            Error::new(
                ErrorKind::Unexpected,
                format!("the endpoint answered 101 but did not upgrade: {err}"),
            )
        })?;
        *upgraded.lock().expect("upgrade lock poisoned") = Some(stream);
        let _ = events.send(event);
        full_body(Bytes::new())
    } else if head_only
        || status == http::StatusCode::NO_CONTENT
        || status == http::StatusCode::NOT_MODIFIED
        || response.content_length() == Some(0)
    {
        let _ = events.send(event);
        full_body(Bytes::new())
    } else {
        streaming_body(response, events.clone(), event, spec.exchange_timeout)
    };
    let mut response = http::Response::new(body);
    *response.status_mut() = status;
    *response.headers_mut() = headers;
    Ok(response)
}

// Report a successful exchange only once the upstream body has completed. The
// channel keeps the response body independent of the caller's borrowed callback.
fn streaming_body(
    response: reqwest::Response,
    events: mpsc::UnboundedSender<ForwardEvent>,
    event: ForwardEvent,
    timeout: std::time::Duration,
) -> ForwardBody {
    let remaining = response.content_length();
    let stream = response.bytes_stream().boxed();
    let state = (stream, remaining, Some(event), events, timeout);
    let frames = futures_util::stream::unfold(
        state,
        |(mut stream, mut remaining, mut event, events, timeout)| async move {
            // Start the deadline only when hyper asks for another chunk. A reqwest
            // client read_timeout stays armed while a slow local reader applies
            // backpressure, which can expire before already-ready data is polled.
            match tokio::time::timeout(timeout, stream.next()).await {
                Ok(Some(Ok(chunk))) => {
                    if let Some(left) = remaining.as_mut() {
                        *left = left.saturating_sub(chunk.len() as u64);
                        if *left == 0 {
                            if let Some(event) = event.take() {
                                let _ = events.send(event);
                            }
                        }
                    }
                    Some((
                        Ok(Frame::data(chunk)),
                        (stream, remaining, event, events, timeout),
                    ))
                }
                Ok(Some(Err(error))) => Some((
                    Err(Error::new(
                        ErrorKind::Unexpected,
                        format!("reading the guest's response body failed: {error}"),
                    )),
                    (stream, remaining, None, events, timeout),
                )),
                Err(_) => Some((
                    Err(Error::new(
                        ErrorKind::Unexpected,
                        "reading the guest's response body timed out",
                    )),
                    (stream, remaining, None, events, timeout),
                )),
                Ok(None) => {
                    if let Some(event) = event {
                        let _ = events.send(event);
                    }
                    None
                }
            }
        },
    );
    StreamBody::new(frames).boxed_unsync()
}

/// The cap on a request head, so a client that never sends the terminator cannot grow the
/// buffer without bound. Generous: cookies and long URLs are ordinary.
const MAX_REQUEST_HEAD_BYTES: usize = 64 * 1024;

/// Writes an HTTP error response to a local client whose exchange could not be proxied.
///
/// Plain text and a real status rather than dropping the socket: a browser pointed at a
/// tunnel whose token lost its scope should render the reason, and a dropped connection
/// renders as "the site can't be reached", which names the wrong component.
pub async fn write_local_error<W>(stream: &mut W, status: u16, body: &str) -> Result<(), Error>
where
    W: AsyncWrite + Unpin + ?Sized,
{
    // A synthetic request lets hyper serialize the response while retaining this
    // write-only public API, including borrowed writers.
    let reader =
        std::io::Cursor::new(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
    let io = tokio::io::join(reader, stream);
    let service = service_fn(|_| {
        std::future::ready(Ok::<_, std::convert::Infallible>(local_response(
            status,
            body.to_string(),
        )))
    });
    hyper::server::conn::http1::Builder::new()
        .half_close(true)
        .serve_connection(TokioIo::new(io), service)
        .await
        .map_err(|err| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not answer the local client: {err}"),
            )
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use microvms_app::session::proxy::DEFAULT_AGENT_PORT;
    use microvms_app::testing::{CountingMinter, TestClock};
    use tokio::io::AsyncWriteExt as _;

    // A minimal reader for the canned test upstreams, independent of the relay.
    struct RequestHead {
        method: String,
        target: String,
        headers: Vec<(String, String)>,
    }

    async fn read_request_head<S: AsyncRead + Unpin>(
        local: &mut S,
    ) -> Result<Option<RequestHead>, std::io::Error> {
        use tokio::io::AsyncReadExt as _;
        let mut raw = Vec::new();
        while !raw.ends_with(b"\r\n\r\n") {
            let byte = match local.read_u8().await {
                Ok(byte) => byte,
                Err(err) if raw.is_empty() && err.kind() == std::io::ErrorKind::UnexpectedEof => {
                    return Ok(None);
                }
                Err(err) => return Err(err),
            };
            raw.push(byte);
        }
        let text = String::from_utf8_lossy(&raw);
        let mut lines = text.split("\r\n");
        let mut first = lines.next().unwrap().split_whitespace();
        let method = first.next().unwrap().to_string();
        let target = first.next().unwrap().to_string();
        let headers: Vec<(String, String)> = lines
            .filter_map(|line| line.split_once(':'))
            .map(|(name, value)| (name.to_string(), value.trim().to_string()))
            .collect();
        let length = headers
            .iter()
            .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
            .and_then(|(_, value)| value.parse().ok())
            .unwrap_or(0);
        let mut body = vec![0; length];
        local.read_exact(&mut body).await?;
        Ok(Some(RequestHead {
            method,
            target,
            headers,
        }))
    }

    /// A [`ProxyAuth`] over the shared counting minter, so a test can read the mint count and
    /// the scopes that were asked for.
    fn auth_pair() -> (Arc<ProxyAuth>, Arc<CountingMinter>) {
        let minter = Arc::new(CountingMinter::default());
        let auth = ProxyAuth::with_refresh_after(
            minter.clone(),
            DEFAULT_AGENT_PORT,
            std::time::Duration::from_secs(30 * 60),
            Arc::new(TestClock::default()),
        )
        .expect("the default refresh interval is below the ceiling");
        (Arc::new(auth), minter)
    }

    /// A one-shot upstream HTTP/1.1 server on loopback.
    ///
    /// Real sockets rather than a mocked reqwest, because what these tests check is that the
    /// bytes reqwest puts on the wire carry the minted headers — and a fake client would be
    /// asserting against the fake. Returns the bound address and a handle yielding the request
    /// text the server saw.
    async fn upstream(response: &'static str) -> (String, tokio::task::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
        let addr = listener.local_addr().expect("bound");
        let handle = tokio::spawn(async move {
            // Bounded, so a forwarder that never reaches the upstream fails the test rather
            // than hanging it.
            let (mut socket, _) =
                tokio::time::timeout(std::time::Duration::from_secs(10), listener.accept())
                    .await
                    .expect("the forwarder reaches the upstream")
                    .expect("one connection");
            let head = read_request_head(&mut socket)
                .await
                .expect("a readable request")
                .expect("a request arrived");
            socket
                .write_all(response.as_bytes())
                .await
                .expect("the canned response is written");
            socket.flush().await.expect("flushed");
            let headers = head
                .headers
                .iter()
                .map(|(name, value)| format!("{}: {value}", name.to_ascii_lowercase()))
                .collect::<Vec<_>>()
                .join("\n");
            format!("{} {}\n{headers}", head.method, head.target)
        });
        (format!("http://{addr}"), handle)
    }

    /// Drives one exchange through [`serve_connection`] and returns what the local client saw
    /// plus every event the forwarder reported.
    async fn exchange(
        request: &str,
        endpoint: String,
        guest_port: u16,
        auth: &Arc<ProxyAuth>,
    ) -> (String, Vec<ForwardEvent>) {
        let (mut client, server) = tokio::io::duplex(64 * 1024);
        let spec = ForwardSpec::new(
            "127.0.0.1:8080".parse().expect("an addr"),
            guest_port,
            endpoint,
        );
        let http = ForwardClient::new().expect("a client");

        let auth = auth.clone();
        let served = tokio::spawn(async move {
            let mut events = Vec::new();
            let result =
                serve_connection(server, &spec, &auth, &http, |event| events.push(event)).await;
            (result, events)
        });

        client
            .write_all(
                request
                    .replacen("HTTP/1.1\r\n", "HTTP/1.1\r\nconnection: close\r\n", 1)
                    .as_bytes(),
            )
            .await
            .expect("the request is written");
        client.flush().await.expect("flushed");

        let (result, events) = served.await.expect("the task joins");
        result.expect("the exchange completes");

        let mut seen = Vec::new();
        tokio::io::AsyncReadExt::read_to_end(&mut client, &mut seen)
            .await
            .expect("the client reads the response");
        (String::from_utf8_lossy(&seen).to_string(), events)
    }

    #[tokio::test]
    async fn requests_name_the_local_listener_before_minting() {
        for host in [
            "",
            "example.com:8080",
            "localhost:8081",
            "127.0.0.1",
            "localhost:8080@elsewhere.example",
            "user@localhost:8080",
            "localhost:8080/path",
            "localhost:8080\r\nHost: localhost:8080",
        ] {
            let (auth, _) = auth_pair();
            let request = if host.is_empty() {
                "GET / HTTP/1.1\r\n\r\n".to_string()
            } else {
                format!("GET / HTTP/1.1\r\nHost: {host}\r\n\r\n")
            };
            let (response, events) =
                exchange(&request, "http://127.0.0.1:1".to_string(), 8080, &auth).await;
            assert!(
                response.starts_with("HTTP/1.1 403 Forbidden"),
                "{host}: {response}"
            );
            assert_eq!(auth.mint_count(), 0, "{host}");
            assert!(matches!(
                events.as_slice(),
                [ForwardEvent::Refused { status: 403, .. }]
            ));
        }
    }

    #[test]
    fn host_authorities_match_the_bound_address_and_port() {
        let bind = "127.0.0.1:8080".parse().expect("an address");
        for host in [
            "localhost:8080",
            "LOCALHOST:8080",
            "127.0.0.1:8080",
            "[::1]:8080",
        ] {
            assert!(
                local_host_matches(&[("Host".into(), host.into())], bind),
                "{host}"
            );
        }
        assert!(local_host_matches(
            &[("Host".into(), "localhost".into())],
            "127.0.0.1:80".parse().unwrap()
        ));
        for host in ["localhost", "localhost:8081", "localhost:0"] {
            assert!(!local_host_matches(&[("Host".into(), host.into())], bind));
        }
        assert!(!local_host_matches(
            &[("Host".into(), "localhost:invalid".into())],
            bind
        ));
        assert!(!local_host_matches(
            &[("Host".into(), "localhost:65536".into())],
            bind
        ));
        assert!(!local_host_matches(
            &[("Host".into(), "localhost:65536".into())],
            "127.0.0.1:80".parse().unwrap()
        ));
        assert!(!local_host_matches(
            &[("Host".into(), "localhost:8080".into())],
            "192.0.2.1:8080".parse().unwrap()
        ));
        assert!(local_host_matches(
            &[("Host".into(), "192.0.2.1:8080".into())],
            "192.0.2.1:8080".parse().unwrap()
        ));
    }

    #[tokio::test]
    async fn an_ordinary_get_reaches_the_guest_carrying_both_minted_headers() {
        // The whole point of layer 1: a local client's request arrives upstream with the
        // port-scoped credential attached, and the guest's response comes back intact.
        let (endpoint, upstream_saw) =
            upstream("HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nhello").await;
        let (auth, minter) = auth_pair();

        let (seen, events) = exchange(
            "GET /index.html HTTP/1.1\r\nhost: localhost:8080\r\nuser-agent: probe\r\n\r\n",
            endpoint,
            8080,
            &auth,
        )
        .await;

        let request = upstream_saw.await.expect("the upstream task joins");
        assert!(request.starts_with("GET /index.html"), "{request}");
        assert!(
            request.contains(&format!(
                "{}: jwe-0",
                PROXY_AUTH_HEADER.to_ascii_lowercase()
            )),
            "the minted auth header did not reach upstream: {request}"
        );
        assert!(
            request.contains(&format!("{}: 8080", PROXY_PORT_HEADER.to_ascii_lowercase())),
            "the port header did not name the guest port: {request}"
        );
        assert!(request.contains("user-agent: probe"), "{request}");

        assert!(seen.starts_with("HTTP/1.1 200 OK\r\n"), "{seen}");
        assert!(seen.ends_with("hello"), "{seen}");
        assert!(
            matches!(
                events.as_slice(),
                [ForwardEvent::Forwarded {
                    status: 200,
                    upgraded: false
                }]
            ),
            "{events:?}"
        );

        // The scope asked for is the guest port, not the agent port alone — the 2026-08-15
        // defect. `mint_for_ports` records what was requested.
        let requested = minter.requested();
        assert!(
            requested.iter().any(|ports| ports.contains(&8080)),
            "the mint never asked for port 8080: {requested:?}"
        );
    }

    /// **Falsification**: drop the `forwards_request_header` filter in `serve_connection` and
    /// the forged value arrives beside the minted one, which is a request whose credential
    /// the caller chose.
    #[tokio::test]
    async fn the_caller_cannot_smuggle_its_own_proxy_headers_upstream() {
        let (endpoint, upstream_saw) =
            upstream("HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok").await;
        let (auth, _minter) = auth_pair();

        let request = format!(
            "GET / HTTP/1.1\r\nhost: localhost:8080\r\n{PROXY_AUTH_HEADER}: forged-jwe\r\n\
             {PROXY_PORT_HEADER}: 1\r\n\r\n"
        );
        let (_seen, _events) = exchange(&request, endpoint, 8080, &auth).await;

        let saw = upstream_saw.await.expect("the upstream task joins");
        assert!(
            !saw.contains("forged-jwe"),
            "a caller's forged credential was forwarded: {saw}"
        );
        assert!(
            saw.contains(&format!("{}: 8080", PROXY_PORT_HEADER.to_ascii_lowercase())),
            "the minted port header should be the only one: {saw}"
        );
        assert!(
            !saw.contains("host: localhost:8080"),
            "the local host header must not override the endpoint's: {saw}"
        );
    }

    #[tokio::test]
    async fn a_scope_refusal_is_answered_with_the_sentence_that_names_it() {
        // 403 is the one status that means "the credential never authorized this port", and a
        // browser must render that rather than "the site can't be reached".
        let (endpoint, upstream_saw) =
            upstream("HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n").await;
        let (auth, _minter) = auth_pair();

        let (seen, events) = exchange(
            "GET / HTTP/1.1\r\nhost: localhost:8080\r\n\r\n",
            endpoint,
            8080,
            &auth,
        )
        .await;
        let _ = upstream_saw.await;

        assert!(seen.starts_with("HTTP/1.1 403 Forbidden\r\n"), "{seen}");
        assert!(seen.contains("scope does not cover"), "{seen}");
        match events.as_slice() {
            [
                ForwardEvent::Refused {
                    status: 403,
                    explanation,
                },
            ] => {
                assert!(explanation.contains("8080"), "{explanation}");
            }
            other => panic!("expected one 403 refusal, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_dead_guest_server_is_reported_as_502_rather_than_as_an_auth_problem() {
        let (endpoint, upstream_saw) =
            upstream("HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\n\r\n").await;
        let (auth, _minter) = auth_pair();

        let (seen, events) = exchange(
            "GET / HTTP/1.1\r\nhost: localhost:8080\r\n\r\n",
            endpoint,
            5432,
            &auth,
        )
        .await;
        let _ = upstream_saw.await;

        assert!(seen.contains("nothing listening on port 5432"), "{seen}");
        assert!(
            matches!(
                events.as_slice(),
                [ForwardEvent::Refused { status: 502, .. }]
            ),
            "{events:?}"
        );
    }

    #[tokio::test]
    async fn the_guests_own_404_is_relayed_and_not_reinterpreted() {
        // A 404 from the dev server is the dev server's answer. A forwarder that explained it
        // would be inventing a diagnosis for someone else's status.
        let (endpoint, upstream_saw) =
            upstream("HTTP/1.1 404 Not Found\r\ncontent-length: 9\r\n\r\nno-route!").await;
        let (auth, _minter) = auth_pair();

        let (seen, events) = exchange(
            "GET /nope HTTP/1.1\r\nhost: localhost:8080\r\n\r\n",
            endpoint,
            8080,
            &auth,
        )
        .await;
        let _ = upstream_saw.await;

        assert!(seen.starts_with("HTTP/1.1 404 Not Found\r\n"), "{seen}");
        assert!(seen.ends_with("no-route!"), "{seen}");
        assert!(
            matches!(
                events.as_slice(),
                [ForwardEvent::Forwarded { status: 404, .. }]
            ),
            "{events:?}"
        );
    }

    #[tokio::test]
    async fn a_post_body_is_forwarded_and_the_response_length_is_re_derived() {
        // The upstream announces a content-length; the forwarder must write one that matches
        // the bytes it actually relays, or a browser hangs waiting for a body that ended.
        let (endpoint, upstream_saw) = upstream(
            "HTTP/1.1 200 OK\r\ncontent-length: 3\r\ntransfer-encoding: identity\r\n\r\nack",
        )
        .await;
        let (auth, _minter) = auth_pair();

        let (seen, _events) = exchange(
            "POST /submit HTTP/1.1\r\nhost: localhost:8080\r\ncontent-length: 7\r\n\r\npayload",
            endpoint,
            8080,
            &auth,
        )
        .await;
        let saw = upstream_saw.await.expect("the upstream task joins");

        assert!(saw.starts_with("POST /submit"), "{saw}");
        assert!(saw.contains("content-length: 7"), "{saw}");
        assert_eq!(
            seen.matches("content-length:").count(),
            1,
            "exactly one framing header should be written: {seen}"
        );
        assert!(seen.contains("content-length: 3\r\n"), "{seen}");
        assert!(
            !seen.to_ascii_lowercase().contains("transfer-encoding"),
            "the upstream framing header must not be copied: {seen}"
        );
        assert!(seen.ends_with("ack"), "{seen}");
    }

    #[tokio::test]
    async fn a_101_relays_the_handshake_and_then_stops_parsing() {
        // The WebSocket path. The 101 and its negotiated subprotocol reach the client
        // verbatim, and everything after it is bytes in both directions — this module frames
        // nothing, so it cannot frame anything wrongly.
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
        let addr = listener.local_addr().expect("bound");
        let guest = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("one connection");
            let _ = read_request_head(&mut socket)
                .await
                .expect("a request")
                .expect("present");
            socket
                .write_all(
                    b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\
                      connection: upgrade\r\nsec-websocket-protocol: my-app-protocol\r\n\r\n",
                )
                .await
                .expect("the handshake is written");
            socket.flush().await.expect("flushed");
            // Now speak the upgraded protocol: read the client's frame, answer it.
            let mut frame = [0_u8; 5];
            tokio::io::AsyncReadExt::read_exact(&mut socket, &mut frame)
                .await
                .expect("the client's bytes arrive");
            socket
                .write_all(b"PONG")
                .await
                .expect("the answer is written");
            socket.flush().await.expect("flushed");
            frame.to_vec()
        });

        let (auth, _minter) = auth_pair();
        let (mut client, server) = tokio::io::duplex(64 * 1024);
        let spec = ForwardSpec::new(
            "127.0.0.1:8080".parse().expect("an addr"),
            8080,
            format!("http://{addr}"),
        );
        let http = ForwardClient::new().expect("a client");
        let served = tokio::spawn(async move {
            let mut events = Vec::new();
            let result =
                serve_connection(server, &spec, &auth, &http, |event| events.push(event)).await;
            (result, events)
        });

        client
            .write_all(
                b"GET /socket HTTP/1.1\r\nhost: localhost:8080\r\nupgrade: websocket\r\n\
                  connection: upgrade\r\nsec-websocket-protocol: my-app-protocol\r\n\r\n",
            )
            .await
            .expect("the handshake request is written");
        client.flush().await.expect("flushed");

        // The handshake response arrives before any upgraded byte, which is what lets a
        // client know the negotiation succeeded.
        let mut head = Vec::new();
        let mut byte = [0_u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            tokio::io::AsyncReadExt::read_exact(&mut client, &mut byte)
                .await
                .expect("the handshake response arrives");
            head.push(byte[0]);
        }
        let head = String::from_utf8_lossy(&head).to_string();
        assert!(
            head.starts_with("HTTP/1.1 101 Switching Protocols\r\n"),
            "{head}"
        );
        assert!(
            head.to_ascii_lowercase()
                .contains("sec-websocket-protocol: my-app-protocol"),
            "the guest's negotiated subprotocol must reach the client: {head}"
        );

        client
            .write_all(b"PING!")
            .await
            .expect("a frame is written");
        client.flush().await.expect("flushed");
        let mut answer = [0_u8; 4];
        tokio::io::AsyncReadExt::read_exact(&mut client, &mut answer)
            .await
            .expect("the guest's answer arrives");
        assert_eq!(&answer, b"PONG");

        drop(client);
        let guest_saw = guest.await.expect("the guest task joins");
        assert_eq!(
            &guest_saw, b"PING!",
            "the client's bytes reached the guest unframed"
        );

        let (result, events) = served.await.expect("the task joins");
        result.expect("the splice ends cleanly");
        assert!(
            events.iter().any(|event| matches!(
                event,
                ForwardEvent::Forwarded {
                    status: 101,
                    upgraded: true
                }
            )),
            "{events:?}"
        );
    }

    #[tokio::test]
    async fn every_exchange_mints_through_the_cache_rather_than_capturing_headers_once() {
        // TRAP-9's shape for a tunnel: the credential is re-read per hop, so crossing the
        // sixty-minute ceiling is the cache's job and not a timer here. Two exchanges over one
        // ProxyAuth must both go through it.
        let (auth, minter) = auth_pair();

        for _ in 0..2 {
            let (endpoint, upstream_saw) =
                upstream("HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok").await;
            let (_seen, _events) = exchange(
                "GET / HTTP/1.1\r\nhost: localhost:8080\r\n\r\n",
                endpoint,
                8080,
                &auth,
            )
            .await;
            let saw = upstream_saw.await.expect("the upstream task joins");
            assert!(
                saw.contains(&PROXY_AUTH_HEADER.to_ascii_lowercase()),
                "every hop carries the minted credential: {saw}"
            );
        }

        // One mint, two hops: the second read the cache. That is the correct behaviour — the
        // assertion that matters is that neither hop went out uncredentialed, and that the
        // count is the observable saying whether a refresh happened.
        assert_eq!(
            auth.mint_count(),
            1,
            "the second hop should reuse the cached token rather than re-mint"
        );
        assert_eq!(minter.requested().len(), 1);
    }

    #[test]
    fn only_a_101_counts_as_an_upgrade() {
        // A client may offer an upgrade the guest declines, and then the exchange is ordinary
        // HTTP with an ordinary body. Reading the request's Upgrade header instead would
        // splice a connection that still had a body to relay.
        assert!(is_upgrade(101));
        assert!(!is_upgrade(200));
        assert!(!is_upgrade(426));
    }

    #[test]
    fn a_bare_endpoint_host_is_read_as_https() {
        // The reason is in the module docs: defaulting to http on a missing prefix puts a
        // bearer token on the wire in clear text.
        assert_eq!(
            upstream_url("vm-abc.example.aws", "/index.html"),
            "https://vm-abc.example.aws/index.html"
        );
    }

    #[test]
    fn an_explicit_scheme_is_left_alone() {
        assert_eq!(
            upstream_url("https://vm-abc.example.aws/", "/a"),
            "https://vm-abc.example.aws/a"
        );
        assert_eq!(
            upstream_url("http://127.0.0.1:9000", "/a"),
            "http://127.0.0.1:9000/a"
        );
    }

    #[test]
    fn a_target_without_a_leading_slash_still_forms_one_path() {
        assert_eq!(
            upstream_url("vm.example", "index.html"),
            "https://vm.example/index.html"
        );
    }

    #[test]
    fn the_two_proxy_headers_are_never_copied_from_the_caller() {
        // A client that sent its own X-aws-proxy-auth must not have it forwarded: the
        // minted pair is the only pair, which is what keeps the mint schedule authoritative.
        assert!(!forwards_request_header("x-aws-proxy-auth"));
        assert!(!forwards_request_header("X-AWS-PROXY-AUTH"));
        assert!(!forwards_request_header("x-aws-proxy-port"));
        assert!(!forwards_request_header("Host"));
        assert!(forwards_request_header("user-agent"));
        assert!(forwards_request_header("sec-websocket-protocol"));
    }

    #[test]
    fn the_refusal_wording_separates_a_scope_mistake_from_a_dead_server() {
        // The one diagnostic pair PLATFORM.md names, and the reason this function exists
        // rather than the CLI writing its own sentence.
        let scope = refusal_explanation(403, 8080).expect("403 explains itself");
        assert!(scope.contains("scope does not cover"), "{scope}");
        assert!(scope.contains("8080"), "{scope}");

        let dead = refusal_explanation(502, 8080).expect("502 explains itself");
        assert!(dead.contains("nothing listening"), "{dead}");
        assert!(dead.contains("8080"), "{dead}");

        assert!(
            refusal_explanation(401, 8080).is_some(),
            "401 is worth naming"
        );
        assert!(
            refusal_explanation(200, 8080).is_none(),
            "a success explains nothing"
        );
        assert!(
            refusal_explanation(404, 8080).is_none(),
            "the guest's own 404 is not ours"
        );
    }

    #[tokio::test]
    async fn a_bind_collision_names_the_address_and_suggests_another_port() {
        let held = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
        let addr = held.local_addr().expect("a bound address");

        let spec = ForwardSpec::new(addr, 8080, "vm.example");
        let error = bind(&spec).await.expect_err("the address is taken");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains(&addr.to_string()), "{error}");
        assert!(
            error.to_string().contains("different local port"),
            "{error}"
        );
    }

    #[tokio::test]
    async fn a_local_error_response_is_a_readable_http_message() {
        // A browser pointed at a scope-refused tunnel should render the reason; a dropped
        // socket renders as "the site can't be reached", which names the wrong component.
        let (mut client, mut server) = tokio::io::duplex(4096);
        let body = refusal_explanation(403, 8080).expect("wording");
        write_local_error(&mut server, 403, &body)
            .await
            .expect("the response is written");
        drop(server);

        let mut got = Vec::new();
        tokio::io::AsyncReadExt::read_to_end(&mut client, &mut got)
            .await
            .expect("the client reads it");
        let text = String::from_utf8(got).expect("utf-8");
        assert!(text.starts_with("HTTP/1.1 403 Forbidden\r\n"), "{text}");
        assert!(text.contains("content-length: "), "{text}");
        assert!(text.ends_with(&body), "{text}");
    }

    #[tokio::test]
    async fn splice_copies_both_directions_and_reports_the_byte_counts() {
        let (mut left_near, mut left_far) = tokio::io::duplex(4096);
        let (mut right_near, mut right_far) = tokio::io::duplex(4096);

        let pump = tokio::spawn(async move { splice(&mut left_far, &mut right_near).await });

        left_near.write_all(b"to-guest").await.expect("write");
        left_near.shutdown().await.expect("half close");
        right_far.write_all(b"from-guest!").await.expect("write");
        right_far.shutdown().await.expect("half close");

        let mut to_guest = Vec::new();
        tokio::io::AsyncReadExt::read_to_end(&mut right_far, &mut to_guest)
            .await
            .expect("read");
        let mut from_guest = Vec::new();
        tokio::io::AsyncReadExt::read_to_end(&mut left_near, &mut from_guest)
            .await
            .expect("read");

        let (a_to_b, b_to_a) = pump.await.expect("the task joins").expect("a clean splice");
        assert_eq!(to_guest, b"to-guest");
        assert_eq!(from_guest, b"from-guest!");
        assert_eq!(a_to_b, 8);
        assert_eq!(b_to_a, 11);
    }

    #[test]
    fn hop_headers_preserve_only_negotiated_upgrades() {
        for upgrade in [false, true] {
            let mut headers = http::HeaderMap::new();
            for (name, value) in [
                ("connection", "x-private, upgrade"),
                ("upgrade", "example"),
                ("x-private", "one hop only"),
                ("x-end-to-end", "preserved"),
                ("keep-alive", "timeout=5"),
                ("proxy-connection", "keep-alive"),
                ("transfer-encoding", "chunked"),
                ("te", "trailers"),
                ("trailer", "x-checksum"),
            ] {
                headers.insert(
                    http::HeaderName::from_static(name),
                    http::HeaderValue::from_static(value),
                );
            }
            strip_hop_headers(&mut headers, upgrade);
            assert_eq!(headers["x-end-to-end"], "preserved");
            for name in [
                "x-private",
                "keep-alive",
                "proxy-connection",
                "transfer-encoding",
                "te",
                "trailer",
            ] {
                assert!(
                    !headers.contains_key(name),
                    "{name} crossed a hop (upgrade={upgrade})"
                );
            }
            if upgrade {
                assert_eq!(headers["connection"], "upgrade");
                assert_eq!(headers["upgrade"], "example");
            } else {
                assert!(!headers.contains_key("connection"));
                assert!(!headers.contains_key("upgrade"));
            }
        }
    }
}
