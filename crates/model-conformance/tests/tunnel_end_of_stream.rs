// SPDX-License-Identifier: Apache-2.0
//! The verified tunnel's end of stream, with the daemon's real route on one end, the client's
//! real relay on the other, and an on-path party between them (#342).
//!
//! Each half has tests of its own against a stand-in for the other:
//! `crates/agentd/tests/tunnel_relay.rs` drives the daemon with a hand-written initiator, and
//! `crates/microvms-core/tests/tunnel_end_to_end.rs` drives the client against a stand-in relay.
//! Neither can catch the two halves disagreeing about when a stream ended, which is the defect
//! this file exists for: a stand-in written to the same reading of the contract passes against
//! the half it was written beside.
//!
//! The on-path party terminates the WebSocket on each side, as the endpoint proxy does, and
//! forwards frames it can't read. It's what an attacker on that path can be: it can send its own
//! close frame, drop the connection, or withhold a frame, and it holds no key.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use futures_util::{SinkExt as _, StreamExt as _};
use microvms_app::identity::{LaunchIdentity, TunnelIdentity};
use microvms_app::session::proxy::ProxyAuth;
use microvms_app::testing::CountingMinter;
use microvms_edges::session::tunnel::{TunnelEnd, relay_connection_verified};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest as _;
use tokio_tungstenite::tungstenite::handshake::server::{
    Callback, ErrorResponse, Request, Response,
};
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;

const TOKEN: &str = "end-of-stream-agent-token";
const VM_SEED: [u8; 32] = [7; 32];
const HOST_SEED: [u8; 32] = [9; 32];
/// Bytes the sending guest writes before its EOF: many 32 KiB Noise messages, so a cut after
/// a few of them is mid-stream.
const TOTAL: usize = 1024 * 1024;
/// How long any one wait may take before the test calls it a hang.
const BOUND: std::time::Duration = std::time::Duration::from_secs(10);

/// The daemon's real router, bootstrapped with the identity a launch from these seeds delivers.
async fn daemon() -> SocketAddr {
    let launch = LaunchIdentity::from_seeds(VM_SEED, HOST_SEED).expect("valid seeds");
    let hook = protocol::hook::RunHook {
        agent_token: TOKEN.to_string(),
        env: HashMap::new(),
        identity_seed: Some(launch.seed_field()),
        identity_host_public_key: Some(launch.host_public_field()),
    };
    let material = agentd::tunnel_identity::Material::from_payload(&hook)
        .expect("valid")
        .expect("present");
    let state = agentd::state::AppState::new(agentd::config::Config::default());
    state.bootstrap_with_identity(TOKEN.as_bytes(), HashMap::new(), Some(Arc::new(material)));
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    // The daemon's own serve path, the one its binary runs.
    tokio::spawn(agentd::serve::serve(listener, agentd::routes::app(state)));
    addr
}

/// What the client pins: the host seed, and the public half of the VM seed.
fn identity() -> TunnelIdentity {
    LaunchIdentity::from_seeds(VM_SEED, HOST_SEED)
        .expect("valid seeds")
        .keep()
}

fn auth() -> Arc<ProxyAuth> {
    Arc::new(ProxyAuth::with_clock(
        Arc::new(CountingMinter::default()),
        microvms_app::session::DEFAULT_AGENT_PORT,
        Arc::new(microvms_edges::clock::TokioClock::new()),
    ))
}

/// A guest that writes [`TOTAL`] bytes of a ramp and then its EOF.
async fn sending_guest() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.expect("one connection");
        let _ = socket.write_all(&ramp(TOTAL)).await;
        let _ = socket.shutdown().await;
    });
    addr
}

/// A guest that reads its one connection to the end and reports how the read ended: `Ok` with
/// every byte on an EOF, the error's kind on a reset.
async fn reading_guest() -> (
    SocketAddr,
    tokio::sync::oneshot::Receiver<Result<Vec<u8>, std::io::ErrorKind>>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    let (report, outcome) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.expect("one connection");
        let mut read = Vec::new();
        let result = socket
            .read_to_end(&mut read)
            .await
            .map(|_| read)
            .map_err(|error| error.kind());
        let _ = report.send(result);
    });
    (addr, outcome)
}

fn ramp(total: usize) -> Vec<u8> {
    (0..total).map(|index| (index % 251) as u8).collect()
}

/// What the on-path party does to the frames it carries. It can't read them: every one after the
/// handshake is a Noise message.
#[derive(Clone, Copy, Debug)]
enum OnPath {
    /// Carries every frame both ways.
    Faithful,
    /// Carries this many of the daemon's binary frames, then sends the client a close frame of
    /// its own with code 1000 and drops both connections.
    CloseAfter(usize),
    /// Carries this many of the daemon's binary frames, then drops the client's connection with
    /// no close frame.
    HangUpAfter(usize),
    /// Carries every frame except the daemon's last binary frame before its close frame, which
    /// is the end of stream, and then carries the close.
    WithholdDaemonEnd,
    /// The same for the client's last binary frame before its close.
    WithholdClientEnd,
}

/// Records the path, query and bearer the client's upgrade asked for, and answers with the
/// marker subprotocol, as the endpoint proxy does (`docs/PLATFORM.md`).
///
/// A `Callback` impl rather than a closure, for the reason `tunnel_end_to_end.rs` gives: a
/// closure returning tungstenite's whole `ErrorResponse` trips `clippy::result_large_err`.
struct Upgrade(Arc<Mutex<Option<(String, String)>>>);

impl Callback for Upgrade {
    fn on_request(
        self,
        request: &Request,
        mut response: Response,
    ) -> Result<Response, ErrorResponse> {
        let target = request
            .uri()
            .path_and_query()
            .map(ToString::to_string)
            .unwrap_or_default();
        let bearer = request
            .headers()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_string();
        *self.0.lock().expect("not poisoned") = Some((target, bearer));
        response.headers_mut().insert(
            "sec-websocket-protocol",
            protocol::tunnel::WS_MARKER_SUBPROTOCOL
                .parse()
                .expect("a legal header value"),
        );
        Ok(response)
    }
}

/// An on-path party in front of `daemon`, behaving as `behavior` says, for one connection.
async fn on_path(daemon: SocketAddr, behavior: OnPath) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("one connection");
        let asked = Arc::new(Mutex::new(None));
        let mut client = tokio_tungstenite::accept_hdr_async(stream, Upgrade(asked.clone()))
            .await
            .expect("the client's upgrade");
        let (target, bearer) = asked
            .lock()
            .expect("not poisoned")
            .take()
            .expect("the upgrade was recorded");
        let mut request = format!("ws://{daemon}{target}")
            .into_client_request()
            .expect("a well-formed request");
        request
            .headers_mut()
            .insert("authorization", bearer.parse().expect("a header value"));
        let (mut upstream, _) = tokio_tungstenite::connect_async(request)
            .await
            .expect("the daemon's upgrade");
        carry(&mut client, &mut upstream, behavior).await;
    });
    addr
}

/// Moves frames between the two sockets until either ends, doing what `behavior` says.
async fn carry<C, U>(client: &mut C, upstream: &mut U, behavior: OnPath)
where
    C: futures_util::Stream<Item = Result<Message, tokio_tungstenite::tungstenite::Error>>
        + futures_util::Sink<Message>
        + Unpin,
    U: futures_util::Stream<Item = Result<Message, tokio_tungstenite::tungstenite::Error>>
        + futures_util::Sink<Message>
        + Unpin,
{
    // Binary frames carried from each side. Each side's first is its handshake message, which
    // the withholding behaviors pass on: holding it back would stall the handshake rather than
    // cut the stream.
    let mut from_daemon = 0_usize;
    let mut from_client = 0_usize;
    // The frame a withholding behavior holds back until it knows whether another follows.
    let mut held: Option<Message> = None;
    loop {
        tokio::select! {
            message = upstream.next() => {
                let Some(Ok(message)) = message else { return };
                let binary = matches!(message, Message::Binary(_));
                match behavior {
                    OnPath::CloseAfter(limit) if binary && from_daemon == limit => {
                        let close = CloseFrame { code: CloseCode::Normal, reason: "".into() };
                        let _ = client.send(Message::Close(Some(close))).await;
                        return;
                    }
                    OnPath::HangUpAfter(limit) if binary && from_daemon == limit => return,
                    OnPath::WithholdDaemonEnd if binary && from_daemon > 0 => {
                        from_daemon += 1;
                        if let Some(previous) = held.replace(message)
                            && client.send(previous).await.is_err()
                        {
                            return;
                        }
                        continue;
                    }
                    _ => {}
                }
                if binary {
                    from_daemon += 1;
                }
                // A held frame is dropped here: the close follows it with nothing between.
                let closing = matches!(message, Message::Close(_));
                if client.send(message).await.is_err() || closing {
                    return;
                }
            }
            message = client.next() => {
                let Some(Ok(message)) = message else { return };
                let binary = matches!(message, Message::Binary(_));
                if matches!(behavior, OnPath::WithholdClientEnd) && binary && from_client > 0 {
                    from_client += 1;
                    if let Some(previous) = held.replace(message)
                        && upstream.send(previous).await.is_err()
                    {
                        return;
                    }
                    continue;
                }
                if binary {
                    from_client += 1;
                }
                let closing = matches!(message, Message::Close(_));
                if upstream.send(message).await.is_err() || closing {
                    // The daemon's answer to the close, so it acts on the close before this
                    // side's sockets drop.
                    let _ = tokio::time::timeout(BOUND, upstream.next()).await;
                    return;
                }
            }
        }
    }
}

/// The client's relay from a local pipe to `guest` through `endpoint`, with the bytes the local
/// side read and how the tunnel ended.
async fn download(endpoint: SocketAddr, guest: SocketAddr) -> (Vec<u8>, TunnelEnd) {
    let (mut local, relayed) = tokio::io::duplex(64 * 1024);
    let endpoint = format!("http://{endpoint}");
    let relay = tokio::spawn(async move {
        let auth = auth();
        relay_connection_verified(relayed, &endpoint, guest.port(), TOKEN, &auth, &identity()).await
    });
    let mut received = Vec::new();
    tokio::time::timeout(BOUND, local.read_to_end(&mut received))
        .await
        .expect("the relay ends the local connection")
        .expect("the local read");
    let ended = tokio::time::timeout(BOUND, relay)
        .await
        .expect("the relay ends")
        .expect("the relay task joins")
        .expect("the tunnel ends with a value, not an error");
    (received, ended)
}

/// **A stream the guest finishes arrives whole and ends `Closed`** (BIND-23).
///
/// The daemon's end of stream is what the client reads as the guest's EOF, so this is the
/// clean case a stand-in can't vouch for: the real daemon sends it and the real client accepts
/// it. Through a faithful on-path party, which is also what shows the cut tests below fail on
/// the cut and not on the party.
#[tokio::test]
async fn bind_23_a_stream_the_guest_finishes_arrives_whole_and_ends_closed() {
    let daemon = daemon().await;
    let guest = sending_guest().await;

    let (direct, ended) = download(daemon, guest).await;
    assert_eq!(
        ended,
        TunnelEnd::Closed,
        "the daemon's end of stream is the clean end"
    );
    assert!(
        direct == ramp(TOTAL),
        "every byte the guest sent arrives, in order: {} of {TOTAL} bytes",
        direct.len()
    );

    let guest = sending_guest().await;
    let path = on_path(daemon, OnPath::Faithful).await;
    let (carried, ended) = download(path, guest).await;
    assert_eq!(ended, TunnelEnd::Closed, "a faithful path changes nothing");
    assert!(carried == ramp(TOTAL), "{} of {TOTAL} bytes", carried.len());
}

/// **A close frame forged mid-stream doesn't read as a clean end** (BIND-23).
///
/// The on-path party carries a few of the daemon's messages and then sends the client a close
/// frame with code 1000, which is what the daemon itself sends after a clean end. Before #342
/// the client read it as `Closed`, so a download cut short looked complete.
#[tokio::test]
async fn bind_23_a_close_frame_forged_mid_stream_ends_truncated() {
    let daemon = daemon().await;
    let guest = sending_guest().await;
    let path = on_path(daemon, OnPath::CloseAfter(4)).await;

    let (received, ended) = download(path, guest).await;
    assert!(
        !received.is_empty() && received.len() < TOTAL,
        "the path carried part of the stream: {} of {TOTAL} bytes",
        received.len()
    );
    assert!(
        received == ramp(received.len()),
        "what arrived is the stream's start"
    );
    assert_ne!(
        ended,
        TunnelEnd::Closed,
        "a stream cut short must not read as a clean end"
    );
    assert_eq!(ended, TunnelEnd::Truncated { code: Some(1000) });
}

/// **A connection dropped mid-stream doesn't read as a clean end either** (BIND-23).
#[tokio::test]
async fn bind_23_a_connection_dropped_mid_stream_ends_truncated() {
    let daemon = daemon().await;
    let guest = sending_guest().await;
    let path = on_path(daemon, OnPath::HangUpAfter(4)).await;

    let (received, ended) = download(path, guest).await;
    assert!(
        !received.is_empty() && received.len() < TOTAL,
        "the path carried part of the stream: {} of {TOTAL} bytes",
        received.len()
    );
    assert_ne!(
        ended,
        TunnelEnd::Closed,
        "a stream cut short must not read as a clean end"
    );
    assert_eq!(ended, TunnelEnd::Truncated { code: None });
}

/// **The daemon's end of stream withheld and its close carried doesn't read as a clean end**
/// (BIND-23).
///
/// The smallest cut there is: every byte the guest sent arrives, and only the end of stream is
/// missing. The on-path party can't tell which frame that is, but it doesn't need to: it's the
/// last one before the close.
#[tokio::test]
async fn bind_23_the_daemons_end_of_stream_withheld_ends_truncated() {
    let daemon = daemon().await;
    let guest = sending_guest().await;
    let path = on_path(daemon, OnPath::WithholdDaemonEnd).await;

    let (_, ended) = download(path, guest).await;
    assert_ne!(
        ended,
        TunnelEnd::Closed,
        "a stream cut short must not read as a clean end"
    );
    assert_eq!(ended, TunnelEnd::Truncated { code: Some(1000) });
}

/// The client's relay from a local pipe that sends `upload` and then its EOF, and what the guest
/// behind `endpoint` read.
async fn upload(
    endpoint: SocketAddr,
    upload: &[u8],
) -> (TunnelEnd, Result<Vec<u8>, std::io::ErrorKind>) {
    let (guest, outcome) = reading_guest().await;
    let (mut local, relayed) = tokio::io::duplex(64 * 1024);
    let endpoint = format!("http://{endpoint}");
    let relay = tokio::spawn(async move {
        let auth = auth();
        relay_connection_verified(relayed, &endpoint, guest.port(), TOKEN, &auth, &identity()).await
    });
    tokio::time::timeout(BOUND, local.write_all(upload))
        .await
        .expect("the relay reads the upload")
        .expect("written");
    local.shutdown().await.expect("the local EOF");
    let ended = tokio::time::timeout(BOUND, relay)
        .await
        .expect("the relay ends")
        .expect("the relay task joins")
        .expect("the tunnel ends with a value, not an error");
    let read = tokio::time::timeout(BOUND, outcome)
        .await
        .expect("the daemon ends the guest connection")
        .expect("the guest reports");
    (ended, read)
}

/// **The client's end of stream reaches the guest as an EOF after the whole upload.**
///
/// The complement of the reset below, through a faithful on-path party.
#[tokio::test]
async fn the_clients_end_of_stream_reaches_the_guest_as_eof_after_the_upload() {
    let daemon = daemon().await;
    let path = on_path(daemon, OnPath::Faithful).await;

    let (ended, read) = upload(path, &ramp(TOTAL)).await;
    assert_eq!(
        ended,
        TunnelEnd::Closed,
        "the local EOF ends the tunnel cleanly"
    );
    // Compared as a verdict, so a failure prints a length rather than a mebibyte.
    let read = read.map(|bytes| (bytes.len(), bytes == ramp(TOTAL)));
    assert_eq!(
        read,
        Ok((TOTAL, true)),
        "the guest reads the whole upload, then EOF"
    );
}

/// **The client's end of stream withheld resets the guest rather than closing it** (AGENTD-20).
///
/// Every chunk of the upload is carried and so is the client's close, and only the end of
/// stream is missing. A guest reading the upload to its end must not read an EOF, which is what
/// "the caller sent everything" looks like.
#[tokio::test]
async fn agentd_20_the_clients_end_of_stream_withheld_resets_the_guest() {
    let daemon = daemon().await;
    let path = on_path(daemon, OnPath::WithholdClientEnd).await;

    let (_, read) = upload(path, &ramp(TOTAL)).await;
    assert_eq!(
        read.map(|bytes| bytes.len()),
        Err(std::io::ErrorKind::ConnectionReset),
        "an upload whose end of stream never arrived must reach the guest as a reset"
    );
}
