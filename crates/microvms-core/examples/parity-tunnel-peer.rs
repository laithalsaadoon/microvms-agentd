// SPDX-License-Identifier: Apache-2.0
//! A portable peer for the shared tunnel cases. Its inputs state the wire contract
//! independently of the client implementation; stdout contains ready and verdict JSON.

#[path = "parity_support/credentials.rs"]
mod credentials;

use std::io::{BufRead, Write};
use std::time::Duration;

use base64::Engine as _;
use futures_util::{SinkExt as _, StreamExt as _};
use microvms_core::identity::LaunchIdentity;
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::handshake::server::{
    Callback, ErrorResponse, Request, Response,
};
use tokio_tungstenite::tungstenite::protocol::CloseFrame;

struct Marker;

impl Callback for Marker {
    fn on_request(
        self,
        request: &Request,
        mut response: Response,
    ) -> Result<Response, ErrorResponse> {
        // Direct sessions offer no subprotocol; the platform marker is echoed only
        // when a caller offered it, as required by the WebSocket handshake.
        let offered = request
            .headers()
            .get("sec-websocket-protocol")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                value
                    .split(',')
                    .any(|part| part.trim() == "lambda-microvms")
            });
        if offered {
            response.headers_mut().insert(
                "sec-websocket-protocol",
                "lambda-microvms".parse().expect("a header value"),
            );
        }
        Ok(response)
    }
}

fn emit(value: Value) {
    println!("{value}");
    std::io::stdout().flush().expect("stdout flushes");
}

#[tokio::main]
async fn main() {
    if std::env::args().nth(1).as_deref() == Some("credentials") {
        credentials::run();
        return;
    }
    let input: Value = serde_json::from_str(
        &std::io::stdin()
            .lock()
            .lines()
            .next()
            .expect("one input line")
            .expect("input reads"),
    )
    .expect("case input is JSON");
    let port =
        u16::try_from(input["guest_port"].as_u64().expect("guest_port")).expect("a guest port");
    let scenario = input["scenario"].as_str().expect("scenario");
    assert!(matches!(scenario, "server-first" | "different-port"));
    let expected_port = if scenario == "different-port" {
        port + 1
    } else {
        port
    };
    let pattern = "Noise_KK_25519_ChaChaPoly_BLAKE2s";
    let mut prologue = format!(
        "microvms-tunnel\0{pattern}\0{}\0/v1/tcp\0",
        input["protocol_version"]
            .as_str()
            .expect("protocol_version")
    )
    .into_bytes();
    prologue.extend_from_slice(&expected_port.to_be_bytes());
    let launch = LaunchIdentity::from_seeds([7; 32], [9; 32]).expect("fixed fixture seeds");
    let host_public = base64::engine::general_purpose::STANDARD
        .decode(launch.host_public_field())
        .expect("encoded key");
    let identity = launch.keep();
    let mut responder = snow::Builder::new(pattern.parse().expect("Noise pattern"))
        .prologue(&prologue)
        .expect("prologue")
        .local_private_key(&[7; 32])
        .expect("VM seed")
        .remote_public_key(&host_public)
        .expect("host key")
        .build_responder()
        .expect("responder builds");
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("loopback listener");
    emit(json!({
        "endpoint": format!("http://{}", listener.local_addr().expect("bound")),
        "guest_port": port,
        "host_seed": identity.host_seed_base64(),
        "vm_public_key": identity.vm_public_key_base64(),
    }));
    let confirmed = tokio::time::timeout(Duration::from_secs(15), async {
        let (stream, _) = listener.accept().await.expect("one connection");
        let mut socket = tokio_tungstenite::accept_hdr_async(stream, Marker)
            .await
            .expect("WebSocket upgrade");
        let mut scratch = vec![0; 65535];
        let Some(Ok(Message::Binary(first))) = socket.next().await else {
            return false;
        };
        if responder.read_message(&first, &mut scratch).is_err() {
            let _ = socket
                .send(Message::Close(Some(CloseFrame {
                    code: 4403.into(),
                    reason: "context refused".into(),
                })))
                .await;
            return false;
        }
        let count = responder.write_message(&[1], &mut scratch).expect("reply");
        socket
            .send(Message::Binary(scratch[..count].to_vec().into()))
            .await
            .expect("reply sends");
        let mut transport = responder.into_transport_mode().expect("transport");
        let Some(Ok(Message::Binary(confirmation))) = socket.next().await else {
            return false;
        };
        let count = match transport.read_message(&confirmation, &mut scratch) {
            Ok(count) => count,
            Err(_) => return false,
        };
        if scratch[..count]
            != *input["confirmation"]
                .as_str()
                .expect("confirmation")
                .as_bytes()
        {
            return false;
        }
        // No application bytes are read: this greeting proves a server-first peer can
        // send immediately after the independent confirmation frame.
        let count = transport
            .write_message(
                input["greeting"].as_str().expect("greeting").as_bytes(),
                &mut scratch,
            )
            .expect("greeting seals");
        socket
            .send(Message::Binary(scratch[..count].to_vec().into()))
            .await
            .expect("greeting sends");
        let count = transport
            .write_message(&[], &mut scratch)
            .expect("end seals");
        socket
            .send(Message::Binary(scratch[..count].to_vec().into()))
            .await
            .expect("end sends");
        let _ = socket.close(None).await;
        true
    })
    .await
    .unwrap_or(false);
    emit(json!({"confirmed": confirmed}));
}
