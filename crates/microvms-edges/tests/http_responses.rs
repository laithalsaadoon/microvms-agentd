// SPDX-License-Identifier: Apache-2.0
//! Response handling through the production HTTP and forwarding clients.

use microvms_app::error::WireKind;
use microvms_app::session::http::{HttpBackend, HttpRequest};
use microvms_app::session::proxy::ProxyAuth;
use microvms_app::testing::CountingMinter;
use microvms_edges::session::ReqwestBackend;
use microvms_edges::session::forward::{ForwardClient, ForwardSpec, serve_connection};
use std::{sync::Arc, time::Duration};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
use tokio::net::TcpListener;

const BOUND: Duration = Duration::from_secs(5);

async fn server(response: Vec<u8>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
    let endpoint = format!("http://{}", listener.local_addr().expect("address"));
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut head = Vec::new();
        let mut byte = [0];
        while !head.ends_with(b"\r\n\r\n") {
            if stream.read(&mut byte).await.expect("read") == 0 {
                return;
            }
            head.push(byte[0]);
        }
        let _ = stream.write_all(&response).await;
    });
    endpoint
}

async fn redirect_destination() -> (
    String,
    tokio::sync::oneshot::Receiver<()>,
    tokio::task::JoinHandle<()>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("destination");
    let location = format!("http://{}/next", listener.local_addr().expect("address"));
    let (sent, received) = tokio::sync::oneshot::channel();
    let task = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = [0; 4096];
        let _ = stream.read(&mut request).await;
        let _ = stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nother")
            .await;
        let _ = sent.send(());
    });
    (location, received, task)
}

#[tokio::test]
async fn control_redirects_are_returned_without_a_followup_request() {
    for streaming in [false, true] {
        let (location, received, destination) = redirect_destination().await;
        let endpoint = server(
            format!("HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n")
                .into_bytes(),
        )
        .await;
        let backend = ReqwestBackend::new(&endpoint, BOUND).expect("backend");
        let mut request = HttpRequest::new("GET", "/v1/health");
        request
            .headers
            .push(("x-aws-proxy-auth".into(), "test-proxy-token".into()));
        let response = if streaming {
            backend
                .open_stream(request, BOUND)
                .await
                .expect("response")
                .0
        } else {
            backend.send(request).await.expect("response")
        };
        assert_eq!(response.status, 302);
        assert_eq!(response.headers.get("location"), Some(&location));
        assert!(
            tokio::time::timeout(Duration::from_millis(100), received)
                .await
                .is_err()
        );
        destination.abort();
    }
}

#[tokio::test]
async fn collected_error_responses_obey_the_byte_ceiling() {
    for streaming in [false, true] {
        for chunked in [false, true] {
            for length in [(64 << 10) - 1, 64 << 10, (64 << 10) + 1] {
                let body = vec![b'x'; length];
                let mut response = if chunked {
                    b"HTTP/1.1 503 Service Unavailable\r\nTransfer-Encoding: chunked\r\n\r\n"
                        .to_vec()
                } else {
                    format!(
                        "HTTP/1.1 503 Service Unavailable\r\nContent-Length: {}\r\n\r\n",
                        body.len()
                    )
                    .into_bytes()
                };
                if chunked {
                    response.extend_from_slice(format!("{:x}\r\n", body.len()).as_bytes());
                }
                response.extend(body);
                if chunked {
                    response.extend_from_slice(b"\r\n0\r\n\r\n");
                }
                let endpoint = server(response).await;
                let backend = ReqwestBackend::new(&endpoint, BOUND).expect("backend");
                let request = HttpRequest::new("GET", "/v1/health");
                let result = if streaming {
                    backend
                        .open_stream(request, BOUND)
                        .await
                        .map(|(response, _)| response)
                } else {
                    backend.send(request).await
                };
                if length <= 64 << 10 {
                    let response = result.expect("within the limit");
                    assert_eq!(response.status, 503);
                    assert_eq!(response.body.len(), length);
                } else {
                    let error = result.expect_err("oversize response");
                    assert_eq!(error.wire_kind(), Some(WireKind::TooLarge));
                    assert!(error.to_string().contains("exceeds 65536 bytes"), "{error}");
                }
            }
        }
    }
}

#[tokio::test]
async fn forwarded_redirects_keep_the_status_location_and_body() {
    let (location, received, destination) = redirect_destination().await;
    let endpoint = server(
        format!("HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 4\r\n\r\nnext")
            .into_bytes(),
    )
    .await;
    let auth = Arc::new(ProxyAuth::with_clock(
        Arc::new(CountingMinter::default()),
        microvms_app::session::DEFAULT_AGENT_PORT,
        Arc::new(microvms_edges::clock::TokioClock::new()),
    ));
    let spec = ForwardSpec::new("127.0.0.1:8080".parse().expect("address"), 8080, endpoint);
    let http = ForwardClient::new().expect("client");
    let (mut browser, relay) = tokio::io::duplex(64 * 1024);
    let task =
        tokio::spawn(async move { serve_connection(relay, &spec, &auth, &http, |_| {}).await });
    browser
        .write_all(b"GET / HTTP/1.1\r\nHost: localhost:8080\r\nConnection: close\r\n\r\n")
        .await
        .expect("request");
    task.await.expect("task").expect("relay");
    let mut response = String::new();
    browser
        .read_to_string(&mut response)
        .await
        .expect("response");
    assert!(response.starts_with("HTTP/1.1 302"), "{response}");
    assert!(
        response
            .to_ascii_lowercase()
            .contains(&format!("location: {location}")),
        "{response}"
    );
    assert!(response.ends_with("next"), "{response}");
    assert!(
        tokio::time::timeout(Duration::from_millis(100), received)
            .await
            .is_err()
    );
    destination.abort();
}

#[tokio::test]
async fn an_oversize_success_length_is_refused_before_collection() {
    let endpoint = server(b"HTTP/1.1 200 OK\r\nContent-Length: 536870913\r\n\r\n".to_vec()).await;
    let backend = ReqwestBackend::new(&endpoint, BOUND).expect("backend");
    let error = backend
        .send(HttpRequest::new("GET", "/v1/fs/file?path=large"))
        .await
        .expect_err("oversize response");
    assert_eq!(error.wire_kind(), Some(WireKind::TooLarge));
    assert!(
        error.to_string().contains("exceeds 536870912 bytes"),
        "{error}"
    );
}

#[tokio::test]
async fn a_stream_error_body_obeys_the_idle_timeout() {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
    let endpoint = format!("http://{}", listener.local_addr().expect("address"));
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = [0; 4096];
        let _ = stream.read(&mut request).await.expect("request");
        stream
            .write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 1\r\n\r\n")
            .await
            .expect("head");
        std::future::pending::<()>().await;
    });
    let backend = ReqwestBackend::new(&endpoint, BOUND).expect("backend");
    let response = tokio::time::timeout(
        BOUND,
        backend.open_stream(
            HttpRequest::new("GET", "/v1/health"),
            Duration::from_millis(20),
        ),
    )
    .await
    .expect("the error body has an idle bound");
    let error = match response {
        Err(error) => error,
        Ok(_) => panic!("an idle error response completed"),
    };
    assert_eq!(error.wire_kind(), Some(WireKind::Transport));
    assert!(
        error.to_string().contains("response body went idle"),
        "{error}"
    );
    server.abort();
}
