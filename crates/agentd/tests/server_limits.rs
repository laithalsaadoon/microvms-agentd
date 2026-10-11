// SPDX-License-Identifier: Apache-2.0
use agentd::{AppState, Config, routes};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tower::ServiceExt;

const HOOK: &str = "/aws/lambda-microvms/runtime/v1/run";

async fn serve_app(app: axum::Router) -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move {
        agentd::serve::serve(listener, app).await.unwrap();
    });
    (address, task)
}

async fn server() -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
    let state = AppState::new(Config {
        repair_identity: false,
        ..Config::default()
    });
    state.bootstrap(b"test-token", Default::default());
    serve_app(routes::app(state)).await
}

async fn read_response(stream: &mut TcpStream) -> String {
    let mut head = Vec::new();
    while !head.ends_with(b"\r\n\r\n") {
        let mut byte = [0];
        if stream.read(&mut byte).await.unwrap() == 0 {
            break;
        }
        head.push(byte[0]);
    }
    let text = String::from_utf8(head).unwrap();
    let length = text
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length:")
                .map(|value| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    let mut body = vec![0; length];
    stream.read_exact(&mut body).await.unwrap();
    text + &String::from_utf8_lossy(&body)
}

#[tokio::test]
async fn run_hook_envelopes_are_bounded_before_parsing() {
    let state = AppState::new(Config {
        repair_identity: false,
        ..Config::default()
    });
    let payload = serde_json::json!({
        "agent_token": "test-token", "extra": "\\".repeat(1200)
    })
    .to_string();
    assert!(payload.len() <= 4096);
    let allowed = serde_json::json!({"runHookPayload": payload}).to_string();
    assert!(allowed.len() > 4096);
    let response = routes::app(state.clone())
        .oneshot(
            Request::post(HOOK)
                .header("content-type", "application/json")
                .body(Body::from(allowed))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = serde_json::json!({"runHookPayload": " ".repeat(33 * 1024)}).to_string();
    let response = routes::app(state.clone())
        .oneshot(
            Request::post(HOOK)
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(state.hook_report().0.len(), 2);
}

#[tokio::test]
async fn run_hook_body_collection_has_a_deadline() {
    let body = Body::from_stream(futures_util::stream::pending::<
        Result<bytes::Bytes, std::io::Error>,
    >());
    let response = tokio::time::timeout(
        Duration::from_secs(7),
        routes::app(AppState::new(Config::default())).oneshot(
            Request::post(HOOK)
                .header("content-type", "application/json")
                .body(body)
                .unwrap(),
        ),
    )
    .await
    .expect("hook body deadline")
    .unwrap();
    assert_eq!(response.status(), StatusCode::REQUEST_TIMEOUT);
    assert_eq!(response.headers().get("connection").unwrap(), "close");
}

#[tokio::test]
async fn incomplete_headers_have_a_deadline() {
    let (address, task) = server().await;
    let mut stream = TcpStream::connect(address).await.unwrap();
    stream
        .write_all(b"GET /v1/health HTTP/1.1\r\nhost:")
        .await
        .unwrap();
    let response = tokio::time::timeout(Duration::from_secs(7), read_response(&mut stream))
        .await
        .expect("header deadline");
    assert!(
        response.is_empty() || response.starts_with("HTTP/1.1 408"),
        "{response}"
    );
    task.abort();
}

#[tokio::test]
async fn rejected_body_drain_has_a_deadline_and_closes_the_connection() {
    let (address, task) = server().await;
    let mut stream = TcpStream::connect(address).await.unwrap();
    stream.write_all(b"POST /v1/exec/start HTTP/1.1\r\nhost: local\r\nauthorization: Bearer other\r\ncontent-length: 1\r\n\r\n").await.unwrap();
    let response = tokio::time::timeout(Duration::from_secs(4), read_response(&mut stream))
        .await
        .expect("drain deadline");
    assert!(response.starts_with("HTTP/1.1 401"), "{response}");
    assert!(
        response.to_ascii_lowercase().contains("connection: close"),
        "{response}"
    );
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(1), read_response(&mut stream))
            .await
            .unwrap(),
        ""
    );
    task.abort();
}

#[tokio::test]
async fn idle_keep_alive_connections_are_closed() {
    let (address, task) = server().await;
    let mut stream = TcpStream::connect(address).await.unwrap();
    stream
        .write_all(b"GET /v1/health HTTP/1.1\r\nhost: local\r\n\r\n")
        .await
        .unwrap();
    assert!(read_response(&mut stream).await.starts_with("HTTP/1.1 200"));
    let response = tokio::time::timeout(Duration::from_secs(7), read_response(&mut stream))
        .await
        .expect("idle connection deadline");
    assert!(
        response.is_empty() || response.starts_with("HTTP/1.1 408"),
        "{response}"
    );
    task.abort();
}

#[tokio::test]
async fn connections_past_the_capacity_are_closed() {
    let app = axum::Router::new()
        .route(
            "/hold",
            axum::routing::get(|ws: axum::extract::WebSocketUpgrade| async {
                ws.on_upgrade(
                    |mut socket| async move { while let Some(Ok(_)) = socket.recv().await {} },
                )
            }),
        )
        .route("/v1/health", axum::routing::get(|| async { "ready" }));
    let (address, task) = serve_app(app).await;
    let mut held = Vec::new();
    for _ in 0..128 {
        let (stream, response) = tokio_tungstenite::connect_async(format!("ws://{address}/hold"))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SWITCHING_PROTOCOLS);
        held.push(stream);
    }
    let mut extra = TcpStream::connect(address).await.unwrap();
    let closed = tokio::time::timeout(Duration::from_secs(1), extra.read(&mut [0]))
        .await
        .expect("connection capacity");
    assert!(matches!(closed, Ok(0) | Err(_)), "{closed:?}");
    drop(held);
    // Wait for the server to observe the closed upgrades and release a slot.
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let mut next = TcpStream::connect(address).await.unwrap();
            if next
                .write_all(b"GET /v1/health HTTP/1.1\r\nhost: local\r\nconnection: close\r\n\r\n")
                .await
                .is_ok()
            {
                let mut response = Vec::new();
                if next.read_to_end(&mut response).await.is_ok()
                    && response.starts_with(b"HTTP/1.1 200")
                {
                    break;
                }
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("a released slot admits a new connection");
    task.abort();
}
