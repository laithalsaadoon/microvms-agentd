// SPDX-License-Identifier: Apache-2.0
//! The client's half of the verified tunnel's end of stream (#342) and of its frame checks
//! (#297), against a stand-in daemon.
//!
//! The stand-in plays the daemon and the guest behind it in one task, and can play one from
//! before the end of stream too, which the real daemon no longer can.
//! `crates/model-conformance/tests/tunnel_end_of_stream.rs` runs the real daemon against this
//! client with an on-path party between them; these are here too because a mutant in this
//! crate runs only this crate's tests.

use std::sync::{Arc, Mutex};

use base64::Engine as _;
use futures_util::{SinkExt as _, StreamExt as _};
use microvms_app::identity::LaunchIdentity;
use microvms_app::session::proxy::ProxyAuth;
use microvms_app::testing::CountingMinter;
use microvms_edges::session::tunnel::{
    TunnelEnd, relay_connection, relay_connection_verified, verify_identity,
};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::net::TcpListener;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::server::{
    Callback, ErrorResponse, Request, Response,
};
use tokio_tungstenite::tungstenite::protocol::CloseFrame;

const TOKEN: &str = "end-of-stream-agent-token";
const VM_SEED: [u8; 32] = [7; 32];
const HOST_SEED: [u8; 32] = [9; 32];
/// Bytes the stand-in sends when it sends a stream: several 32 KiB messages.
const TOTAL: usize = 100 * 1024;
const BOUND: std::time::Duration = std::time::Duration::from_secs(10);

/// What the stand-in sends after the handshake, and how it ends.
#[derive(Clone, Copy)]
enum Plays {
    /// A current daemon whose guest sends [`TOTAL`] bytes and its EOF: the bytes, the end of
    /// stream, then a close frame with no code.
    Current,
    /// A current daemon's handshake, then the bytes and a close frame with `code` and no end of
    /// stream: a close forged on the path after the last byte looks like this.
    CloseWithoutEnd(u16),
    /// A current daemon's handshake, then the bytes and a dropped connection.
    HangUp,
    /// A daemon from before the end of stream: an empty reply payload, the bytes, then a close.
    Legacy,
    /// A current daemon whose guest sends nothing: it reads the client's frames until the
    /// client's close, and records them.
    Listens,
    /// A current daemon whose guest sends one chunk, then reads the client's frames until the
    /// client's close, and records them.
    Streams,
    /// No handshake: a plain tunnel's bytes, then a close frame with no code.
    Plain,
    /// A current daemon that sends one chunk and then the same ciphertext again (BIND-24).
    Replays,
    /// A current daemon that sends one chunk and then bytes no key holder sealed (BIND-24).
    Forges,
}

/// What the stand-in saw of the client.
#[derive(Debug, Default)]
struct Seen {
    /// The client's handshake payload.
    offer: Vec<u8>,
    /// Whether an empty Noise message, the end of stream, arrived before the client's close.
    ended: bool,
    /// Whether the client's close frame arrived.
    closed: bool,
    /// Plaintext bytes the client relayed.
    bytes: Vec<u8>,
}

/// Answers the client's upgrade with the marker subprotocol, as the endpoint proxy does
/// (`docs/PLATFORM.md`): the client offered subprotocols, and tungstenite refuses an answer that
/// names none.
///
/// A `Callback` impl rather than a closure, since a closure returning tungstenite's whole
/// `ErrorResponse` trips `clippy::result_large_err`.
struct Marker;

impl Callback for Marker {
    fn on_request(
        self,
        _request: &Request,
        mut response: Response,
    ) -> Result<Response, ErrorResponse> {
        response.headers_mut().insert(
            "sec-websocket-protocol",
            protocol::tunnel::WS_MARKER_SUBPROTOCOL
                .parse()
                .expect("a legal header value"),
        );
        Ok(response)
    }
}

fn host_public() -> [u8; 32] {
    let launch = LaunchIdentity::from_seeds(VM_SEED, HOST_SEED).expect("valid seeds");
    base64::engine::general_purpose::STANDARD
        .decode(launch.host_public_field())
        .expect("base64")
        .try_into()
        .expect("32 bytes")
}

fn ramp(total: usize) -> Vec<u8> {
    (0..total).map(|index| (index % 251) as u8).collect()
}

fn sealed(noise: &mut snow::TransportState, plain: &[u8]) -> Message {
    let mut scratch = vec![0_u8; 65535];
    let written = noise.write_message(plain, &mut scratch).expect("encrypts");
    Message::Binary(scratch[..written].to_vec().into())
}

/// A stand-in daemon for one connection, playing `plays`, and what it saw of the client.
async fn stand_in(plays: Plays) -> (std::net::SocketAddr, Arc<Mutex<Seen>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let addr = listener.local_addr().expect("bound");
    let seen = Arc::new(Mutex::new(Seen::default()));
    let record = seen.clone();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("one connection");
        let mut socket = tokio_tungstenite::accept_hdr_async(stream, Marker)
            .await
            .expect("the client's upgrade");
        if matches!(plays, Plays::Plain) {
            for chunk in ramp(TOTAL).chunks(32 * 1024) {
                let _ = socket.send(Message::Binary(chunk.to_vec().into())).await;
            }
            let _ = socket.send(Message::Close(None)).await;
            return;
        }
        let mut noise = respond(&mut socket, plays, &record).await;
        match plays {
            Plays::Listens => listen(&mut socket, &mut noise, &record).await,
            Plays::Replays | Plays::Forges => {
                let chunk = sealed(&mut noise, b"once");
                let _ = socket.send(chunk.clone()).await;
                let second = match plays {
                    Plays::Replays => chunk,
                    _ => Message::Binary(
                        (0_u8..36)
                            .map(|byte| byte.wrapping_mul(29) ^ 0x3c)
                            .collect::<Vec<u8>>()
                            .into(),
                    ),
                };
                let _ = socket.send(second).await;
                // Held open, so the client's own check is the only thing that can end it.
                let _ = socket.next().await;
            }
            Plays::Streams => {
                // One chunk out, then the client's frames: a client that stopped reading ends
                // the tunnel as soon as its write of that chunk fails.
                let _ = socket.send(sealed(&mut noise, b"more")).await;
                listen(&mut socket, &mut noise, &record).await;
            }
            _ => {
                for chunk in ramp(TOTAL).chunks(32 * 1024) {
                    let _ = socket.send(sealed(&mut noise, chunk)).await;
                }
                match plays {
                    Plays::Current => {
                        let _ = socket.send(sealed(&mut noise, &[])).await;
                        let _ = socket.send(Message::Close(None)).await;
                    }
                    Plays::CloseWithoutEnd(code) => {
                        let close = CloseFrame {
                            code: code.into(),
                            reason: "".into(),
                        };
                        let _ = socket.send(Message::Close(Some(close))).await;
                    }
                    Plays::Legacy => {
                        let _ = socket.send(Message::Close(None)).await;
                    }
                    // Dropping the socket drops the connection with no close frame.
                    _ => {}
                }
            }
        }
    });
    (addr, seen)
}

/// The responder's half of the handshake, with the reply payload `plays` calls for.
async fn respond(
    socket: &mut WebSocketStream<tokio::net::TcpStream>,
    plays: Plays,
    record: &Mutex<Seen>,
) -> snow::TransportState {
    let mut responder =
        snow::Builder::new(protocol::identity::NOISE_PATTERN.parse().expect("parses"))
            .local_private_key(&VM_SEED)
            .expect("a 32-byte secret")
            .remote_public_key(&host_public())
            .expect("a 32-byte key")
            .build_responder()
            .expect("builds");
    let mut scratch = vec![0_u8; 65535];
    let first = loop {
        match socket.next().await {
            Some(Ok(Message::Binary(bytes))) => break bytes,
            Some(Ok(_)) => continue,
            other => panic!("the client's handshake never came: {other:?}"),
        }
    };
    let read = responder
        .read_message(&first, &mut scratch)
        .expect("the client's handshake authenticates");
    record.lock().expect("not poisoned").offer = scratch[..read].to_vec();
    let offer: &[u8] = match plays {
        Plays::Legacy => &[],
        _ => &protocol::identity::HANDSHAKE_PAYLOAD,
    };
    let written = responder
        .write_message(offer, &mut scratch)
        .expect("writes");
    socket
        .send(Message::Binary(scratch[..written].to_vec().into()))
        .await
        .expect("the reply is sent");
    responder.into_transport_mode().expect("transport")
}

/// Reads the client's frames until its close, recording what arrived.
async fn listen(
    socket: &mut WebSocketStream<tokio::net::TcpStream>,
    noise: &mut snow::TransportState,
    record: &Mutex<Seen>,
) {
    let mut scratch = vec![0_u8; 65535];
    while let Some(Ok(message)) = socket.next().await {
        match message {
            Message::Binary(frame) => {
                let count = noise
                    .read_message(&frame, &mut scratch)
                    .expect("the client's frames authenticate");
                let mut seen = record.lock().expect("not poisoned");
                if count == 0 {
                    seen.ended = true;
                } else {
                    assert!(!seen.ended, "a chunk came after the client's end of stream");
                    seen.bytes.extend_from_slice(&scratch[..count]);
                }
            }
            Message::Close(_) => {
                record.lock().expect("not poisoned").closed = true;
                return;
            }
            _ => continue,
        }
    }
}

fn auth() -> Arc<ProxyAuth> {
    Arc::new(ProxyAuth::with_clock(
        Arc::new(CountingMinter::default()),
        microvms_app::session::DEFAULT_AGENT_PORT,
        Arc::new(microvms_edges::clock::TokioClock::new()),
    ))
}

fn identity() -> microvms_app::identity::TunnelIdentity {
    LaunchIdentity::from_seeds(VM_SEED, HOST_SEED)
        .expect("valid seeds")
        .keep()
}

/// A verified relay from a local pipe through `endpoint`, reading the local side to its end:
/// the bytes read, and how the tunnel ended.
async fn download(endpoint: std::net::SocketAddr, verified: bool) -> (Vec<u8>, TunnelEnd) {
    let (mut local, relayed) = tokio::io::duplex(64 * 1024);
    let endpoint = format!("http://{endpoint}");
    let relay = tokio::spawn(async move {
        let auth = auth();
        if verified {
            relay_connection_verified(relayed, &endpoint, 8080, TOKEN, &auth, &identity()).await
        } else {
            relay_connection(relayed, &endpoint, 8080, TOKEN, &auth).await
        }
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

/// **The daemon's end of stream ends a download `Closed`, after every byte.**
#[tokio::test]
async fn the_daemons_end_of_stream_ends_a_download_closed() {
    let (daemon, seen) = stand_in(Plays::Current).await;
    let (received, ended) = download(daemon, true).await;
    assert_eq!(ended, TunnelEnd::Closed);
    assert!(
        received == ramp(TOTAL),
        "{} of {TOTAL} bytes",
        received.len()
    );
    assert!(
        protocol::identity::offers_end_of_stream(&seen.lock().expect("not poisoned").offer),
        "the client's handshake offers the end of stream"
    );
}

/// **A close frame with no failure code and no end of stream is `Truncated`, with its code**
/// (BIND-23).
///
/// Every byte arrived and it still isn't a clean end: nothing inside the session said the guest
/// finished, and a close frame after the last byte is what a forged one looks like.
#[tokio::test]
async fn bind_23_a_close_without_the_offered_end_of_stream_is_truncated() {
    let (daemon, _) = stand_in(Plays::CloseWithoutEnd(protocol::tunnel::close::NORMAL)).await;
    let (_, ended) = download(daemon, true).await;
    assert_ne!(
        ended,
        TunnelEnd::Closed,
        "a close frame alone proves nothing"
    );
    assert_eq!(ended, TunnelEnd::Truncated { code: Some(1000) });
}

/// **A dropped connection with no end of stream is `Truncated`, with no code** (BIND-23).
#[tokio::test]
async fn bind_23_a_hangup_without_the_offered_end_of_stream_is_truncated() {
    let (daemon, _) = stand_in(Plays::HangUp).await;
    let (_, ended) = download(daemon, true).await;
    assert_eq!(ended, TunnelEnd::Truncated { code: None });
}

/// **A failure code without the end of stream stays the daemon's refusal.**
///
/// It's a failure however it arrived, and its code is the diagnosis a caller can act on.
#[tokio::test]
async fn a_failure_code_without_the_end_of_stream_keeps_its_code() {
    let code = protocol::tunnel::close::RELAY_FAILED;
    let (daemon, _) = stand_in(Plays::CloseWithoutEnd(code)).await;
    let (_, ended) = download(daemon, true).await;
    assert_eq!(
        ended,
        TunnelEnd::Refused {
            code,
            reason: String::new()
        }
    );
}

/// **A daemon from before the end of stream ends a download `ClosedUnproven`** (BIND-23).
///
/// The skew rule: its reply offered nothing, so it never sends an end of stream, and reading
/// its close as a failure would fail every verified tunnel into an older image. It isn't
/// `Closed` either, since nothing proved the stream finished.
#[tokio::test]
async fn bind_23_a_daemon_that_offers_no_end_of_stream_ends_closed_unproven() {
    let (daemon, _) = stand_in(Plays::Legacy).await;
    let (received, ended) = download(daemon, true).await;
    assert_eq!(ended, TunnelEnd::ClosedUnproven);
    assert_eq!(
        received.len(),
        TOTAL,
        "an older daemon's stream still relays"
    );
}

/// **A plain tunnel has no end of stream to miss: its close is `Closed`, as it always was.**
#[tokio::test]
async fn a_plain_tunnels_close_is_still_closed() {
    let (daemon, _) = stand_in(Plays::Plain).await;
    let (received, ended) = download(daemon, false).await;
    assert_eq!(ended, TunnelEnd::Closed);
    assert_eq!(received.len(), TOTAL);
}

/// **The local side's EOF sends this side's end of stream after every byte, then the close.**
///
/// What the daemon reads as "the caller sent everything": without it, the daemon resets the
/// guest connection instead of closing it (AGENTD-20).
#[tokio::test]
async fn the_local_eof_sends_the_end_of_stream_before_the_close() {
    let (daemon, seen) = stand_in(Plays::Listens).await;
    let (mut local, relayed) = tokio::io::duplex(64 * 1024);
    let endpoint = format!("http://{daemon}");
    let relay = tokio::spawn(async move {
        let auth = auth();
        relay_connection_verified(relayed, &endpoint, 8080, TOKEN, &auth, &identity()).await
    });
    tokio::time::timeout(BOUND, local.write_all(&ramp(TOTAL)))
        .await
        .expect("the relay reads the upload")
        .expect("written");
    local.shutdown().await.expect("the local EOF");
    let ended = tokio::time::timeout(BOUND, relay)
        .await
        .expect("the relay ends")
        .expect("the relay task joins")
        .expect("a value");
    assert_eq!(ended, TunnelEnd::Closed, "this side ended the tunnel");
    wait_for_close(&seen).await;
    let seen = seen.lock().expect("not poisoned");
    assert!(seen.ended, "the end of stream came before the close");
    assert!(
        seen.bytes == ramp(TOTAL),
        "{} of {TOTAL} bytes",
        seen.bytes.len()
    );
}

/// A local client that has gone away without an EOF: a write to it fails, and a read never
/// returns, so the relay learns it's gone only from the write.
struct Gone;

impl tokio::io::AsyncRead for Gone {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        _: &mut std::task::Context<'_>,
        _: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::task::Poll::Pending
    }
}

impl tokio::io::AsyncWrite for Gone {
    fn poll_write(
        self: std::pin::Pin<&mut Self>,
        _: &mut std::task::Context<'_>,
        _: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        std::task::Poll::Ready(Err(std::io::ErrorKind::BrokenPipe.into()))
    }

    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        _: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::task::Poll::Ready(Ok(()))
    }

    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        _: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::task::Poll::Ready(Ok(()))
    }
}

/// **A local client that goes away ends the tunnel from this side, with its end of stream.**
///
/// It stopped reading, so it's done, and every byte it sent was relayed.
#[tokio::test]
async fn a_local_client_that_goes_away_sends_the_end_of_stream() {
    let (daemon, seen) = stand_in(Plays::Streams).await;
    let endpoint = format!("http://{daemon}");
    let relay = tokio::spawn(async move {
        let auth = auth();
        relay_connection_verified(Gone, &endpoint, 8080, TOKEN, &auth, &identity()).await
    });
    let ended = tokio::time::timeout(BOUND, relay)
        .await
        .expect("the relay ends")
        .expect("the relay task joins")
        .expect("a value");
    assert_eq!(ended, TunnelEnd::Closed);
    wait_for_close(&seen).await;
    assert!(seen.lock().expect("not poisoned").ended);
}

/// **`verify_identity` ends its handshake-only tunnel with the end of stream too.**
///
/// The daemon dials its own port for it, and resets that connection when a caller that offered
/// the end of stream closes without one.
#[tokio::test]
async fn verify_identity_sends_the_end_of_stream_before_its_close() {
    let (daemon, seen) = stand_in(Plays::Listens).await;
    let ended = tokio::time::timeout(
        BOUND,
        verify_identity(&format!("http://{daemon}"), TOKEN, &auth(), &identity()),
    )
    .await
    .expect("the handshake completes")
    .expect("a value");
    assert_eq!(ended, TunnelEnd::Closed, "the pin verified");
    wait_for_close(&seen).await;
    let seen = seen.lock().expect("not poisoned");
    assert!(seen.ended, "the end of stream came before the close");
    assert!(seen.bytes.is_empty(), "no local connection, so no bytes");
}

/// Waits for the stand-in to record the client's close, bounded.
async fn wait_for_close(seen: &Mutex<Seen>) {
    tokio::time::timeout(BOUND, async {
        while !seen.lock().expect("not poisoned").closed {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("the client's close reaches the stand-in");
}

/// A verified relay from a local pipe through a stand-in playing `plays`: what the local side
/// read, and the relay's error.
async fn refused_download(plays: Plays) -> (Vec<u8>, String) {
    let (daemon, _) = stand_in(plays).await;
    let (mut local, relayed) = tokio::io::duplex(64 * 1024);
    let endpoint = format!("http://{daemon}");
    let relay = tokio::spawn(async move {
        let auth = auth();
        relay_connection_verified(relayed, &endpoint, 8080, TOKEN, &auth, &identity()).await
    });
    let mut received = Vec::new();
    tokio::time::timeout(BOUND, local.read_to_end(&mut received))
        .await
        .expect("the relay ends the local connection")
        .expect("the local read");
    let ended = tokio::time::timeout(BOUND, relay)
        .await
        .expect("the relay ends")
        .expect("the relay task joins");
    let error = match ended {
        Err(error) => error.to_string(),
        Ok(end) => {
            panic!("a frame that doesn't authenticate must fail the tunnel, not end {end:?}")
        }
    };
    (received, error)
}

/// **A replayed frame fails the tunnel and never reaches the local connection** (BIND-24).
///
/// The same ciphertext twice: it opened at position 0 and can't at position 1. Writing it would
/// hand the local application the chunk twice, on the path that promised it wouldn't.
#[tokio::test]
async fn bind_24_a_replayed_frame_fails_the_tunnel_unwritten() {
    let (received, error) = refused_download(Plays::Replays).await;
    assert_eq!(
        received, b"once",
        "the replay must never reach the local connection"
    );
    assert!(error.contains("did not authenticate"), "{error}");
}

/// **A forged frame fails the tunnel and never reaches the local connection** (BIND-24).
#[tokio::test]
async fn bind_24_a_forged_frame_fails_the_tunnel_unwritten() {
    let (received, error) = refused_download(Plays::Forges).await;
    assert_eq!(
        received, b"once",
        "the forgery must never reach the local connection"
    );
    assert!(error.contains("did not authenticate"), "{error}");
}
