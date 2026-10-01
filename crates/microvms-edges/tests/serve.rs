// SPDX-License-Identifier: Apache-2.0
//! The serving loops (#263) against a stand-in daemon and a stand-in upstream: a tunnel that
//! keeps serving after a refused connection, stops on request and at its limit, and runs over a
//! direct session; a port-forward that serves two slow requests at once, counts a refusal and
//! keeps serving, and reaches a direct session's guest port; and the SDKs' handles over both,
//! which stop on request, cut what's left open after a grace or on drop, and list the
//! connections that didn't end clean.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use futures_util::{SinkExt as _, StreamExt as _};
use microvms_app::session::proxy::ProxyAuth;
use microvms_app::testing::CountingMinter;
use microvms_edges::session::forward::{ForwardEvent, ForwardSpec};
use microvms_edges::session::serve::{
    ForwardNotice, ServeLimits, StopReason, TunnelConnection, TunnelTarget, serve_forward,
    serve_tunnel,
};
use microvms_edges::session::tunnel::TunnelEnd;
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::server::{
    Callback, ErrorResponse, Request, Response,
};
use tokio_tungstenite::tungstenite::protocol::CloseFrame;

const TOKEN: &str = "serve-agent-token";
const BOUND: std::time::Duration = std::time::Duration::from_secs(10);

fn auth() -> Arc<ProxyAuth> {
    Arc::new(ProxyAuth::with_clock(
        Arc::new(CountingMinter::default()),
        microvms_app::session::DEFAULT_AGENT_PORT,
        Arc::new(microvms_edges::clock::TokioClock::new()),
    ))
}

/// A guest server that upper-cases what it reads, so a passing test proves bytes crossed it.
async fn upper_guest() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = listener.accept().await {
            tokio::spawn(async move {
                let mut buffer = vec![0_u8; 4096];
                while let Ok(count) = socket.read(&mut buffer).await {
                    if count == 0 {
                        return;
                    }
                    let upper: Vec<u8> =
                        buffer[..count].iter().map(u8::to_ascii_uppercase).collect();
                    if socket.write_all(&upper).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    addr
}

/// Records whether the upgrade offered subprotocols, and echoes the marker only when it did,
/// as the daemon does off the proxy path.
struct Upgrade(Arc<Mutex<Vec<bool>>>);

impl Callback for Upgrade {
    fn on_request(
        self,
        request: &Request,
        mut response: Response,
    ) -> Result<Response, ErrorResponse> {
        let offered = request.headers().contains_key("sec-websocket-protocol");
        self.0.lock().expect("not poisoned").push(offered);
        if offered {
            response.headers_mut().insert(
                "sec-websocket-protocol",
                protocol::tunnel::WS_MARKER_SUBPROTOCOL
                    .parse()
                    .expect("a legal header value"),
            );
        }
        Ok(response)
    }
}

/// A stand-in daemon relaying plain tunnels to `guest`, refusing the first `refuse` of them with
/// 4502, and recording whether each upgrade offered subprotocols.
async fn stand_in(guest: SocketAddr, refuse: usize) -> (SocketAddr, Arc<Mutex<Vec<bool>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    let offered = Arc::new(Mutex::new(Vec::new()));
    let record = offered.clone();
    tokio::spawn(async move {
        let mut seen = 0;
        while let Ok((stream, _)) = listener.accept().await {
            seen += 1;
            let refused = seen <= refuse;
            let record = record.clone();
            tokio::spawn(async move {
                let Ok(mut socket) =
                    tokio_tungstenite::accept_hdr_async(stream, Upgrade(record)).await
                else {
                    return;
                };
                if refused {
                    let close = CloseFrame {
                        code: protocol::tunnel::close::NO_LISTENER.into(),
                        reason: "nothing is listening".into(),
                    };
                    let _ = socket.send(Message::Close(Some(close))).await;
                    return;
                }
                let Ok(guest) = TcpStream::connect(guest).await else {
                    return;
                };
                let (mut read, mut write) = guest.into_split();
                let mut buffer = vec![0_u8; 4096];
                loop {
                    tokio::select! {
                        inbound = socket.next() => match inbound {
                            Some(Ok(Message::Binary(bytes))) => {
                                if write.write_all(&bytes).await.is_err() { break }
                            }
                            Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                            Some(Ok(_)) => {}
                        },
                        read = read.read(&mut buffer) => match read {
                            Ok(0) | Err(_) => break,
                            Ok(count) => {
                                let frame = Message::Binary(buffer[..count].to_vec().into());
                                if socket.send(frame).await.is_err() { break }
                            }
                        },
                    }
                }
                let _ = socket.send(Message::Close(None)).await;
            });
        }
    });
    (addr, offered)
}

fn target(daemon: SocketAddr, auth: Option<Arc<ProxyAuth>>) -> TunnelTarget {
    TunnelTarget {
        endpoint: format!("http://{daemon}"),
        agent_token: TOKEN.to_string(),
        auth,
        guest_port: 8080,
        identity: None,
    }
}

/// A tunnel loop on a fresh listener: its address, the channel that stops it, and its report
/// with every connection's end.
fn start_tunnel(
    listener: TcpListener,
    target: TunnelTarget,
    limits: ServeLimits,
) -> (
    tokio::sync::oneshot::Sender<()>,
    tokio::task::JoinHandle<(
        microvms_edges::session::serve::TunnelReport,
        Vec<TunnelConnection>,
    )>,
) {
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let task = tokio::spawn(async move {
        let mut ends = Vec::new();
        let report = serve_tunnel(
            listener,
            target,
            limits,
            async move {
                let _ = stopped.await;
            },
            |connection| ends.push(connection),
        )
        .await;
        (report, ends)
    });
    (stop, task)
}

/// Sends `hello` through the tunnel at `local` and answers what came back, until EOF. Nothing,
/// when the tunnel no longer accepts, so the caller's assertion says which connection it was.
async fn round_trip(local: SocketAddr) -> Vec<u8> {
    let Ok(mut client) = TcpStream::connect(local).await else {
        return Vec::new();
    };
    client.write_all(b"hello").await.expect("written");
    let mut answer = vec![0_u8; 5];
    let read = tokio::time::timeout(BOUND, client.read_exact(&mut answer)).await;
    match read {
        Ok(Ok(_)) => answer,
        _ => Vec::new(),
    }
}

/// **A tunnel relays a connection and stops on request, with one served.**
#[tokio::test]
async fn a_tunnel_relays_a_connection_and_stops_on_request() {
    let (daemon, _) = stand_in(upper_guest().await, 0).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let (stop, task) = start_tunnel(
        listener,
        target(daemon, Some(auth())),
        ServeLimits::default(),
    );

    assert_eq!(round_trip(local).await, b"HELLO");
    stop.send(()).expect("the loop is running");
    let (report, ends) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops")
        .expect("joins");
    assert_eq!(report.served, 1);
    assert_eq!(report.refused, 0);
    assert_eq!(report.stopped, StopReason::Requested);
    assert!(report.proxy_token_mints >= 1, "{report:?}");
    assert!(
        matches!(
            ends.as_slice(),
            [TunnelConnection {
                end: Ok(TunnelEnd::Closed),
                ..
            }]
        ),
        "{ends:?}"
    );
}

/// **A refused connection is reported, and the tunnel keeps serving the next one.**
///
/// The daemon refuses the first connection with 4502 and relays the second. A loop that ended
/// on a connection's failure would never serve the second, which is what the CLI's warning
/// and the SDKs' events exist to avoid.
#[tokio::test]
async fn a_refused_connection_is_reported_and_the_tunnel_keeps_serving() {
    let (daemon, _) = stand_in(upper_guest().await, 1).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let (stop, task) = start_tunnel(
        listener,
        target(daemon, Some(auth())),
        ServeLimits::default(),
    );

    assert!(
        round_trip(local).await.is_empty(),
        "the first connection is refused"
    );
    assert_eq!(
        round_trip(local).await,
        b"HELLO",
        "the second connection is served after the first was refused"
    );
    stop.send(()).expect("the loop is running");
    let (report, ends) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops")
        .expect("joins");
    assert_eq!((report.served, report.refused), (2, 1), "{report:?}");
    assert!(
        ends.iter().any(|connection| matches!(
            connection.end,
            Ok(TunnelEnd::Refused { code, .. }) if code == protocol::tunnel::close::NO_LISTENER
        )),
        "the refusal reaches the caller with its code: {ends:?}"
    );
}

/// **The limit stops the tunnel after that many connections, and the open one finishes.**
#[tokio::test]
async fn the_limit_stops_the_tunnel_after_that_many_connections() {
    let (daemon, _) = stand_in(upper_guest().await, 0).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let (_stop, task) = start_tunnel(
        listener,
        target(daemon, Some(auth())),
        ServeLimits {
            max_connections: Some(1),
        },
    );

    assert_eq!(round_trip(local).await, b"HELLO");
    let (report, _) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops on its own")
        .expect("joins");
    assert_eq!(report.served, 1);
    assert_eq!(report.stopped, StopReason::Limit);
}

/// **A direct session tunnels without the proxy subprotocols**, with the daemon's bearer check
/// as the gate.
#[tokio::test]
async fn a_direct_session_tunnels_without_the_proxy_subprotocols() {
    let (daemon, offered) = stand_in(upper_guest().await, 0).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let (stop, task) = start_tunnel(listener, target(daemon, None), ServeLimits::default());

    assert_eq!(round_trip(local).await, b"HELLO");
    stop.send(()).expect("the loop is running");
    let (report, _) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops")
        .expect("joins");
    assert_eq!(report.served, 1);
    assert_eq!(
        report.proxy_token_mints, 0,
        "a direct session mints nothing"
    );
    assert_eq!(
        offered.lock().expect("not poisoned").as_slice(),
        [false],
        "a direct session offers no proxy subprotocols"
    );
}

// ── port-forward ─────────────────────────────────────────────────────────────

/// What an upstream saw of one request: its request line and whether it carried the proxy
/// credential.
type Seen = Arc<Mutex<Vec<(String, bool)>>>;

/// An HTTP upstream answering each request once `together` requests are in hand at once,
/// with 502 for the first `refuse` of them and 200 with the request's path after.
async fn upstream(together: usize, refuse: usize) -> (SocketAddr, Seen) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    let seen: Seen = Arc::new(Mutex::new(Vec::new()));
    let record = seen.clone();
    let gate = Arc::new(tokio::sync::Barrier::new(together));
    tokio::spawn(async move {
        let mut count = 0;
        while let Ok((mut socket, _)) = listener.accept().await {
            count += 1;
            let refused = count <= refuse;
            let (record, gate) = (record.clone(), gate.clone());
            tokio::spawn(async move {
                let mut head = Vec::new();
                let mut byte = [0_u8; 1];
                while !head.ends_with(b"\r\n\r\n") {
                    match socket.read(&mut byte).await {
                        Ok(1) => head.push(byte[0]),
                        _ => return,
                    }
                }
                let text = String::from_utf8_lossy(&head).to_string();
                let line = text.lines().next().unwrap_or_default().to_string();
                let proxied = text.to_ascii_lowercase().contains(
                    &microvms_app::session::proxy::PROXY_AUTH_HEADER.to_ascii_lowercase(),
                );
                record
                    .lock()
                    .expect("not poisoned")
                    .push((line.clone(), proxied));
                gate.wait().await;
                let (status, body) = if refused {
                    ("502 Bad Gateway", "no listener".to_string())
                } else {
                    ("200 OK", line)
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = socket.write_all(response.as_bytes()).await;
            });
        }
    });
    (addr, seen)
}

/// A forward loop on a fresh listener, with its stop channel and its report and notices.
fn start_forward(
    listener: TcpListener,
    spec: ForwardSpec,
    auth: Option<Arc<ProxyAuth>>,
) -> (
    tokio::sync::oneshot::Sender<()>,
    tokio::task::JoinHandle<(
        microvms_edges::session::serve::ForwardReport,
        Vec<ForwardNotice>,
    )>,
) {
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let task = tokio::spawn(async move {
        let mut notices = Vec::new();
        let report = serve_forward(
            listener,
            spec,
            auth,
            ServeLimits::default(),
            async move {
                let _ = stopped.await;
            },
            |notice| notices.push(notice),
        )
        .await
        .expect("the forward's client builds");
        (report, notices)
    });
    (stop, task)
}

/// One GET through the forward at `local`, answered as the whole response text. Nothing, when
/// the forward no longer accepts, so the caller's assertion says which request it was.
async fn get(local: SocketAddr, path: &str) -> String {
    let Ok(mut client) = TcpStream::connect(local).await else {
        return String::new();
    };
    client
        .write_all(format!("GET {path} HTTP/1.1\r\nhost: localhost\r\n\r\n").as_bytes())
        .await
        .expect("written");
    let mut response = Vec::new();
    let _ = tokio::time::timeout(BOUND, client.read_to_end(&mut response)).await;
    String::from_utf8_lossy(&response).to_string()
}

/// **The forward serves two slow requests at once.**
///
/// The upstream answers only once both requests are in hand. A forward that served one
/// connection at a time, as the CLI's did, would hold the second behind the first forever.
#[tokio::test]
async fn a_forward_serves_two_slow_requests_at_once() {
    let (upstream, _) = upstream(2, 0).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let spec = ForwardSpec::new(local, 8080, format!("http://{upstream}"));
    let (stop, task) = start_forward(listener, spec, Some(auth()));

    let (first, second) = tokio::join!(get(local, "/first"), get(local, "/second"));
    assert!(
        first.contains("200 OK") && first.contains("/first"),
        "both slow requests must be in flight at once, and the first wasn't answered: {first:?}"
    );
    assert!(
        second.contains("200 OK") && second.contains("/second"),
        "both slow requests must be in flight at once, and the second wasn't answered: {second:?}"
    );
    stop.send(()).expect("the loop is running");
    let (report, _) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops")
        .expect("joins");
    assert_eq!(report.served, 2);
}

/// **A refusal is counted and explained, and the forward keeps serving.**
#[tokio::test]
async fn a_refusal_is_counted_and_the_forward_keeps_serving() {
    let (upstream, _) = upstream(1, 1).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let spec = ForwardSpec::new(local, 8080, format!("http://{upstream}"));
    let (stop, task) = start_forward(listener, spec, Some(auth()));

    let refused = get(local, "/refused").await;
    assert!(refused.contains("502"), "{refused}");
    let served = get(local, "/served").await;
    assert!(
        served.contains("200 OK"),
        "the next request is served: {served}"
    );
    stop.send(()).expect("the loop is running");
    let (report, notices) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops")
        .expect("joins");
    assert_eq!((report.served, report.refused), (2, 1), "{report:?}");
    assert!(
        notices.iter().any(|notice| matches!(
            notice,
            ForwardNotice::Event { event: ForwardEvent::Refused { explanation, .. }, .. }
                if explanation.contains("8080")
        )),
        "the refusal's explanation reaches the caller: {notices:?}"
    );
}

/// **A direct session's forward reaches the endpoint's host at the guest port**, with no proxy
/// credential on the request.
#[tokio::test]
async fn a_direct_forward_reaches_the_endpoint_host_at_the_guest_port() {
    let (upstream, seen) = upstream(1, 0).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    // The endpoint's own port is the daemon's; the guest port is where the request goes.
    let spec = ForwardSpec::new(local, upstream.port(), "http://127.0.0.1:9");
    let (stop, task) = start_forward(listener, spec, None);

    let answer = get(local, "/direct").await;
    assert!(
        answer.contains("200 OK") && answer.contains("/direct"),
        "{answer}"
    );
    stop.send(()).expect("the loop is running");
    let (report, _) = tokio::time::timeout(BOUND, task)
        .await
        .expect("the loop stops")
        .expect("joins");
    assert_eq!((report.served, report.proxy_token_mints), (1, 0));
    assert_eq!(
        seen.lock().expect("not poisoned").as_slice(),
        [("GET /direct HTTP/1.1".to_string(), false)],
        "a direct session's request carries no proxy credential"
    );
}

// ── the SDKs' handles ────────────────────────────────────────────────────────

/// **A forward handle serves until stopped, and its report says what it did.**
#[tokio::test]
async fn a_forward_handle_serves_until_stopped_and_reports() {
    use microvms_edges::session::serve::{DEFAULT_BIND, start_forward};
    let (upstream, _) = upstream(1, 0).await;
    let handle = start_forward(
        DEFAULT_BIND,
        "http://127.0.0.1:9",
        upstream.port(),
        None,
        ServeLimits::default(),
    )
    .await
    .expect("the handle binds");
    let local = handle.local_address();
    assert!(local.ip().is_loopback() && local.port() != 0, "{local}");
    assert!(handle.is_running());

    let answer = get(local, "/handle").await;
    assert!(answer.contains("200 OK"), "{answer}");
    handle.request_stop();
    let summary = tokio::time::timeout(BOUND, handle.finished())
        .await
        .expect("the handle stops")
        .expect("the loop ran");
    assert_eq!(summary.report.served, 1);
    assert_eq!(summary.report.stopped, StopReason::Requested);
    assert!(
        summary.ended.is_empty(),
        "a clean exchange lists nothing: {summary:?}"
    );
    assert!(!handle.is_running());
    // Callable again, with the same answer.
    assert_eq!(handle.finished().await.expect("the loop ran"), summary);
}

/// **A tunnel handle lists the connection the daemon refused, with its code.**
#[tokio::test]
async fn a_tunnel_handle_lists_a_refused_connection() {
    use microvms_edges::session::serve::{DEFAULT_BIND, EndKind, start_tunnel};
    let (daemon, _) = stand_in(upper_guest().await, 1).await;
    let handle = start_tunnel(
        DEFAULT_BIND,
        target(daemon, Some(auth())),
        ServeLimits::default(),
    )
    .await
    .expect("the handle binds");
    assert!(round_trip(handle.local_address()).await.is_empty());
    assert_eq!(round_trip(handle.local_address()).await, b"HELLO");
    handle.request_stop();
    let summary = tokio::time::timeout(BOUND, handle.finished())
        .await
        .expect("the handle stops")
        .expect("the loop ran");
    assert_eq!((summary.report.served, summary.report.refused), (2, 1));
    assert_eq!(summary.ended.len(), 1, "{summary:?}");
    assert_eq!(summary.ended[0].kind, EndKind::Refused);
    assert_eq!(summary.ended[0].kind.as_str(), "refused");
    assert_eq!(
        summary.ended[0].code,
        Some(protocol::tunnel::close::NO_LISTENER)
    );
    assert!(summary.ended[0].detail.contains("listening"), "{summary:?}");
}

/// A tunnel handle through a stand-in daemon, and a client connection through it that has
/// relayed once and stays open.
async fn a_handle_with_an_open_connection() -> (
    microvms_edges::session::serve::Serving<microvms_edges::session::serve::TunnelSummary>,
    TcpStream,
) {
    use microvms_edges::session::serve::{DEFAULT_BIND, start_tunnel};
    let (daemon, _) = stand_in(upper_guest().await, 0).await;
    let handle = start_tunnel(
        DEFAULT_BIND,
        target(daemon, Some(auth())),
        ServeLimits::default(),
    )
    .await
    .expect("the handle binds");
    let mut client = TcpStream::connect(handle.local_address())
        .await
        .expect("the handle accepts");
    client.write_all(b"hello").await.expect("written");
    let mut answer = vec![0_u8; 5];
    tokio::time::timeout(BOUND, client.read_exact(&mut answer))
        .await
        .expect("the answer arrives")
        .expect("read");
    assert_eq!(answer, b"HELLO");
    (handle, client)
}

/// Whether `client` reaches its end (EOF or a reset) within the bound.
async fn reaches_its_end(client: &mut TcpStream) -> bool {
    let mut rest = Vec::new();
    tokio::time::timeout(BOUND, client.read_to_end(&mut rest))
        .await
        .is_ok()
}

/// **A stop with a grace cuts the connection still open after it, and lists it.**
#[tokio::test]
async fn a_stop_with_a_grace_cuts_the_connection_left_open() {
    use microvms_edges::session::serve::EndKind;
    let (handle, mut client) = a_handle_with_an_open_connection().await;
    let summary = tokio::time::timeout(
        BOUND,
        handle.stop(Some(std::time::Duration::from_millis(100))),
    )
    .await
    .expect("the grace ends the wait")
    .expect("the loop ran");
    assert_eq!((summary.report.served, summary.report.refused), (1, 1));
    assert_eq!(summary.ended.len(), 1, "{summary:?}");
    assert_eq!(summary.ended[0].kind, EndKind::Failed);
    assert!(summary.ended[0].detail.contains("cut"), "{summary:?}");
    assert!(
        reaches_its_end(&mut client).await,
        "the cut client stays open"
    );
}

/// **A stop without a grace waits for the open connection, and reports it clean once its
/// client closes.**
#[tokio::test]
async fn a_stop_without_a_grace_waits_for_the_open_connection() {
    let (handle, client) = a_handle_with_an_open_connection().await;
    let handle = Arc::new(handle);
    let stopping = tokio::spawn({
        let handle = Arc::clone(&handle);
        async move { handle.stop(None).await }
    });
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert!(
        !stopping.is_finished() && handle.is_running(),
        "the stop didn't wait for the open connection"
    );
    drop(client);
    let summary = tokio::time::timeout(BOUND, stopping)
        .await
        .expect("the stop ends once the client closes")
        .expect("joined")
        .expect("the loop ran");
    assert_eq!((summary.report.served, summary.report.refused), (1, 0));
    assert!(summary.ended.is_empty(), "{summary:?}");
    assert_eq!(summary.report.stopped, StopReason::Requested);
}

/// **Dropping a handle cuts its open connection.**
#[tokio::test]
async fn dropping_a_handle_cuts_its_open_connection() {
    let (handle, mut client) = a_handle_with_an_open_connection().await;
    drop(handle);
    assert!(
        reaches_its_end(&mut client).await,
        "the connection outlived its handle"
    );
}

/// **Dropping a handle stops its loop, and the listener closes.**
#[tokio::test]
async fn dropping_a_handle_stops_its_loop() {
    use microvms_edges::session::serve::{DEFAULT_BIND, start_forward};
    let handle = start_forward(
        DEFAULT_BIND,
        "http://127.0.0.1:9",
        8080,
        None,
        ServeLimits::default(),
    )
    .await
    .expect("the handle binds");
    let local = handle.local_address();
    drop(handle);
    let closed = tokio::time::timeout(BOUND, async {
        while TcpStream::connect(local).await.is_ok() {
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
    })
    .await;
    assert!(
        closed.is_ok(),
        "the listener at {local} is still open after its handle dropped"
    );
}

/// **The bind address defaults to loopback on a port the OS picks, and a bad one is refused
/// by name.**
#[test]
fn the_bind_address_defaults_to_loopback_and_refuses_a_bad_one() {
    use microvms_edges::session::serve::{DEFAULT_BIND, bind_address};
    assert_eq!(bind_address(None).expect("the default"), DEFAULT_BIND);
    assert!(DEFAULT_BIND.ip().is_loopback() && DEFAULT_BIND.port() == 0);
    assert_eq!(
        bind_address(Some("0.0.0.0:8080")).expect("parses"),
        "0.0.0.0:8080".parse::<SocketAddr>().expect("an address")
    );
    let refused = bind_address(Some("localhost")).expect_err("not an address");
    assert!(refused.to_string().contains("\"localhost\""), "{refused}");
}
