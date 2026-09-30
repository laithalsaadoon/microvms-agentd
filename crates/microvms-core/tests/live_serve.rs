// SPDX-License-Identifier: Apache-2.0
//! The serving loops (#263) against a real VM, through the real endpoint proxy.
//!
//! Invoked by `conformance/run_rs.py` (`drive_serve`) against the suite's kept VM, whose attach
//! coordinates arrive in `MICROVM_LIVE_ATTACH` as a JSON object
//! (`{"microvmId", "endpoint", "agentToken", "region"}`). Launches nothing and terminates
//! nothing: the VM is the suite's. The guest server is the daemon itself, whose unauthenticated
//! `GET /v1/schema` is an HTTP server on the daemon's port that every image has.

use microvms_core::prelude::*;
use microvms_core::region::Region;
use microvms_core::session::Session;
use microvms_core::session::forward::ForwardSpec;
use microvms_core::session::serve::{
    ServeLimits, StopReason, TunnelTarget, serve_forward, serve_tunnel,
};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::net::{TcpListener, TcpStream};

/// The suite's VM, from `MICROVM_LIVE_ATTACH`.
async fn attached() -> Session {
    let raw = std::env::var("MICROVM_LIVE_ATTACH")
        .expect("conformance must supply MICROVM_LIVE_ATTACH for the kept VM");
    let attach: serde_json::Value = serde_json::from_str(&raw).expect("attach JSON");
    let field = |name: &str| {
        attach[name]
            .as_str()
            .unwrap_or_else(|| panic!("MICROVM_LIVE_ATTACH has no {name}"))
            .to_string()
    };
    let region: Region = field("region").parse().expect("a supported region");
    Session::attach(
        region,
        field("microvmId"),
        field("endpoint"),
        field("agentToken"),
        None,
        None,
    )
    .await
    .expect("attach")
}

/// `GET /v1/schema` through a local listener at `local`, read to the end.
async fn schema_through(local: std::net::SocketAddr) -> String {
    let mut client = TcpStream::connect(local).await.expect("the loop accepts");
    client
        .write_all(b"GET /v1/schema HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .expect("written");
    let mut response = Vec::new();
    tokio::time::timeout(
        std::time::Duration::from_secs(60),
        client.read_to_end(&mut response),
    )
    .await
    .expect("the answer arrives")
    .expect("read");
    String::from_utf8_lossy(&response).to_string()
}

/// **`serve_tunnel` relays one connection to the daemon's port through the proxy, then stops at
/// its limit.**
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn live_tunnel_serves_one_connection_through_the_proxy() {
    let session = attached().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let target = TunnelTarget::for_session(&session, session.port(), None);
    let serving = tokio::spawn(serve_tunnel(
        listener,
        target,
        ServeLimits {
            max_connections: Some(1),
        },
        std::future::pending(),
        |_| {},
    ));

    let answer = schema_through(local).await;
    assert!(answer.contains("protocol_version"), "{answer:.200}");
    let report = serving.await.expect("the loop joins");
    eprintln!("tunnel report={report:?}");
    assert_eq!((report.served, report.refused), (1, 0), "{report:?}");
    assert_eq!(report.stopped, StopReason::Limit);
    assert!(report.proxy_token_mints >= 1, "{report:?}");
}

/// **`serve_forward` forwards one request to the daemon's port through the proxy, then stops at
/// its limit.**
#[tokio::test]
#[ignore = "needs the conformance suite's kept VM in MICROVM_LIVE_ATTACH"]
async fn live_forward_serves_one_request_through_the_proxy() {
    let session = attached().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("a free port");
    let local = listener.local_addr().expect("bound");
    let spec = ForwardSpec::new(local, session.port(), session.endpoint());
    let serving = tokio::spawn(serve_forward(
        listener,
        spec,
        session.proxy_auth().cloned(),
        ServeLimits {
            max_connections: Some(1),
        },
        std::future::pending(),
        |_| {},
    ));

    let answer = schema_through(local).await;
    assert!(
        answer.starts_with("HTTP/1.1 200") && answer.contains("protocol_version"),
        "{answer:.200}"
    );
    let report = serving
        .await
        .expect("the loop joins")
        .expect("the forward's client builds");
    eprintln!("forward report={report:?}");
    assert_eq!((report.served, report.refused), (1, 0), "{report:?}");
    assert_eq!(report.stopped, StopReason::Limit);
}
