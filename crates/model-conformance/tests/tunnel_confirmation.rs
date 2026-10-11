// SPDX-License-Identifier: Apache-2.0
//! The verified tunnel's session confirmation and protocol-version boundary.

use futures_util::{SinkExt as _, StreamExt as _};
use microvms_app::identity::{LaunchIdentity, TunnelIdentity};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::{Message, client::IntoClientRequest as _};
const TOKEN: &str = "tunnel-session-test-token";
const VM_SEED: [u8; 32] = [7; 32];
const HOST_SEED: [u8; 32] = [9; 32];
const BOUND: std::time::Duration = std::time::Duration::from_secs(10);
fn identity() -> TunnelIdentity {
    LaunchIdentity::from_seeds(VM_SEED, HOST_SEED)
        .unwrap()
        .keep()
}
async fn daemon() -> SocketAddr {
    let launch = LaunchIdentity::from_seeds(VM_SEED, HOST_SEED).unwrap();
    let hook = protocol::hook::RunHook {
        agent_token: TOKEN.into(),
        env: HashMap::new(),
        identity_seed: Some(launch.seed_field()),
        identity_host_public_key: Some(launch.host_public_field()),
    };
    let material = agentd::tunnel_identity::Material::from_payload(&hook)
        .unwrap()
        .unwrap();
    let state = agentd::state::AppState::new(agentd::config::Config::default());
    state.bootstrap_with_identity(TOKEN.as_bytes(), HashMap::new(), Some(Arc::new(material)));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(agentd::serve::serve(listener, agentd::routes::app(state)));
    addr
}
/// AGENTD-18: the guest stays idle until the caller confirms the current session.
#[tokio::test]
async fn a_handshake_without_confirmation_leaves_the_guest_listener_idle() {
    let daemon = daemon().await;
    let guest = TcpListener::bind("127.0.0.1:0").await.expect("listener");
    let port = guest.local_addr().unwrap().port();
    let mut initiator = microvms_edges::identity::tunnel_initiator(&identity(), port).unwrap();
    let mut first = vec![0; 65535];
    let count = initiator
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut first)
        .unwrap();
    first.truncate(count);
    for _ in 0..2 {
        let mut request = format!("ws://{daemon}/v1/tcp?port={port}&identity=true")
            .into_client_request()
            .unwrap();
        request
            .headers_mut()
            .insert("authorization", format!("Bearer {TOKEN}").parse().unwrap());
        let (mut socket, _) = tokio_tungstenite::connect_async(request).await.unwrap();
        socket
            .send(Message::Binary(first.clone().into()))
            .await
            .unwrap();
        assert!(matches!(
            tokio::time::timeout(BOUND, socket.next()).await.unwrap(),
            Some(Ok(Message::Binary(_)))
        ));
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(100), guest.accept())
                .await
                .is_err()
        );
        socket.close(None).await.unwrap();
    }
}

/// A peer that omits the current context is refused without opening the guest.
#[tokio::test]
async fn a_context_free_handshake_is_refused() {
    let daemon = daemon().await;
    let guest = TcpListener::bind("127.0.0.1:0").await.expect("listener");
    let port = guest.local_addr().unwrap().port();
    let mut initiator = microvms_edges::identity::initiator(&identity()).unwrap();
    let mut first = vec![0; 65535];
    let count = initiator
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut first)
        .unwrap();
    let mut request = format!("ws://{daemon}/v1/tcp?port={port}&identity=true")
        .into_client_request()
        .unwrap();
    request
        .headers_mut()
        .insert("authorization", format!("Bearer {TOKEN}").parse().unwrap());
    let (mut socket, _) = tokio_tungstenite::connect_async(request).await.unwrap();
    socket
        .send(Message::Binary(first[..count].to_vec().into()))
        .await
        .unwrap();
    let response = tokio::time::timeout(BOUND, socket.next()).await.unwrap();
    assert!(
        matches!(response, Some(Ok(Message::Close(Some(frame)))) if u16::from(frame.code) == protocol::tunnel::close::IDENTITY_REFUSED)
    );
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), guest.accept())
            .await
            .is_err()
    );
}

/// AGENTD-18: confirmation has its own payload and cannot be an end-of-stream message.
#[tokio::test]
async fn a_different_confirmation_payload_leaves_the_guest_listener_idle() {
    let daemon = daemon().await;
    let guest = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = guest.local_addr().unwrap().port();
    let mut initiator = microvms_edges::identity::tunnel_initiator(&identity(), port).unwrap();
    let mut request = format!("ws://{daemon}/v1/tcp?port={port}&identity=true")
        .into_client_request()
        .unwrap();
    request
        .headers_mut()
        .insert("authorization", format!("Bearer {TOKEN}").parse().unwrap());
    let (mut socket, _) = tokio_tungstenite::connect_async(request).await.unwrap();
    let mut scratch = vec![0; 65535];
    let count = initiator
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut scratch)
        .unwrap();
    socket
        .send(Message::Binary(scratch[..count].to_vec().into()))
        .await
        .unwrap();
    let Some(Ok(Message::Binary(reply))) =
        tokio::time::timeout(BOUND, socket.next()).await.unwrap()
    else {
        panic!("the handshake reply arrives");
    };
    initiator.read_message(&reply, &mut scratch).unwrap();
    let mut transport = initiator.into_transport_mode().unwrap();
    let count = transport.write_message(&[], &mut scratch).unwrap();
    socket
        .send(Message::Binary(scratch[..count].to_vec().into()))
        .await
        .unwrap();
    let response = tokio::time::timeout(BOUND, socket.next()).await.unwrap();
    assert!(
        matches!(response, Some(Ok(Message::Close(Some(frame)))) if u16::from(frame.code) == protocol::tunnel::close::IDENTITY_REFUSED)
    );
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), guest.accept())
            .await
            .is_err()
    );
}
