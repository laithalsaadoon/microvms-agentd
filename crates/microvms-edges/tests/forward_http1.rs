// SPDX-License-Identifier: Apache-2.0
//! HTTP/1 behavior at the local forwarding boundary.

use microvms_app::session::proxy::ProxyAuth;
use microvms_app::testing::CountingMinter;
use microvms_edges::session::forward::{ForwardClient, ForwardSpec, serve_connection};
use std::{sync::Arc, time::Duration};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _, DuplexStream};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

const BOUND: Duration = Duration::from_secs(5);

type RelayTask = JoinHandle<Result<(), microvms_app::error::Error>>;

fn relay(endpoint: String) -> (DuplexStream, RelayTask, Arc<ProxyAuth>) {
    let auth = Arc::new(ProxyAuth::with_clock(
        Arc::new(CountingMinter::default()),
        microvms_app::session::DEFAULT_AGENT_PORT,
        Arc::new(microvms_edges::clock::TokioClock::new()),
    ));
    let spec = ForwardSpec::new("127.0.0.1:8080".parse().unwrap(), 8080, endpoint);
    let http = ForwardClient::new().unwrap();
    let (browser, local) = tokio::io::duplex(64 * 1024);
    let proxy = auth.clone();
    let task =
        tokio::spawn(async move { serve_connection(local, &spec, &proxy, &http, |_| {}).await });
    (browser, task, auth)
}

async fn head<S: tokio::io::AsyncRead + Unpin>(stream: &mut S) -> Vec<u8> {
    tokio::time::timeout(BOUND, async {
        let mut head = Vec::new();
        while !head.ends_with(b"\r\n\r\n") {
            head.push(stream.read_u8().await.expect("complete head"));
        }
        head
    })
    .await
    .expect("head arrives promptly")
}

async fn upstream(response: Vec<u8>, count: usize) -> (String, JoinHandle<Vec<Vec<u8>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move {
        let mut requests = Vec::new();
        for _ in 0..count {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = head(&mut stream).await;
            let length = String::from_utf8_lossy(&request)
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|length| length.parse::<usize>().ok())
                })
                .unwrap_or(0);
            let mut body = vec![0; length];
            stream.read_exact(&mut body).await.unwrap();
            request.extend(body);
            requests.push(request);
            stream.write_all(&response).await.unwrap();
        }
        requests
    });
    (endpoint, task)
}

async fn reply(browser: &mut DuplexStream, no_body: bool) -> (Vec<u8>, Vec<u8>) {
    let head = head(browser).await;
    let length = if no_body {
        0
    } else {
        String::from_utf8_lossy(&head)
            .lines()
            .find_map(|line| {
                line.to_ascii_lowercase()
                    .strip_prefix("content-length: ")
                    .and_then(|length| length.parse::<usize>().ok())
            })
            .unwrap_or(0)
    };
    let mut body = vec![0; length];
    tokio::time::timeout(BOUND, browser.read_exact(&mut body))
        .await
        .unwrap()
        .unwrap();
    (head, body)
}

#[tokio::test]
async fn keep_alive_forwards_each_request_on_the_same_connection() {
    let (endpoint, upstream) = upstream(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".to_vec(),
        2,
    )
    .await;
    let (mut browser, task, _) = relay(endpoint);
    for target in ["/first", "/second"] {
        browser
            .write_all(format!("GET {target} HTTP/1.1\r\nHost: localhost:8080\r\n\r\n").as_bytes())
            .await
            .unwrap();
        let (head, body) = reply(&mut browser, false).await;
        assert!(head.starts_with(b"HTTP/1.1 200 OK"));
        assert_eq!(body, b"ok");
    }
    let requests = upstream.await.unwrap();
    assert!(requests[0].starts_with(b"GET /first "));
    assert!(requests[1].starts_with(b"GET /second "));
    drop(browser);
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn chunked_requests_are_decoded_and_forwarded_without_truncation() {
    let (endpoint, upstream) = upstream(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok".to_vec(),
        1,
    )
    .await;
    let (mut browser, task, _) = relay(endpoint);
    browser.write_all(b"POST /submit HTTP/1.1\r\nHost: localhost:8080\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\npay\r\n4\r\nload\r\n0\r\n\r\n").await.unwrap();
    let (head, body) = reply(&mut browser, false).await;
    assert!(head.starts_with(b"HTTP/1.1 200 OK"));
    assert_eq!(body, b"ok");
    let requests = upstream.await.unwrap();
    assert!(requests[0].ends_with(b"\r\n\r\npayload"));
    assert!(
        !String::from_utf8_lossy(&requests[0])
            .to_lowercase()
            .contains("transfer-encoding:")
    );
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn head_keeps_the_get_length_and_204_has_no_content_length() {
    for (method, response, expected) in [
        (
            "HEAD",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n".as_slice(),
            Some("5"),
        ),
        (
            "DELETE",
            b"HTTP/1.1 204 No Content\r\n\r\n".as_slice(),
            None,
        ),
    ] {
        let (endpoint, upstream) = upstream(response.to_vec(), 1).await;
        let (mut browser, task, _) = relay(endpoint);
        browser
            .write_all(
                format!("{method} / HTTP/1.1\r\nHost: localhost:8080\r\nConnection: close\r\n\r\n")
                    .as_bytes(),
            )
            .await
            .unwrap();
        let (head, _) = reply(&mut browser, true).await;
        let text = String::from_utf8(head).unwrap().to_lowercase();
        let length = text
            .lines()
            .find_map(|line| line.strip_prefix("content-length: "));
        assert_eq!(length, expected, "{text}");
        if method == "DELETE" {
            assert!(text.starts_with("http/1.1 204 no content"));
        }
        let mut tail = Vec::new();
        browser.read_to_end(&mut tail).await.unwrap();
        assert!(tail.is_empty());
        task.await.unwrap().unwrap();
        upstream.await.unwrap();
    }
}

#[tokio::test]
async fn non_utf8_header_octets_survive_both_hops() {
    let (endpoint, upstream) = upstream(
        b"HTTP/1.1 200 OK\r\nx-bin: caf\xe9\r\nContent-Length: 2\r\n\r\nok".to_vec(),
        1,
    )
    .await;
    let (mut browser, task, _) = relay(endpoint);
    browser.write_all(b"GET / HTTP/1.1\r\nHost: localhost:8080\r\nx-bin: caf\xe9\r\nConnection: close\r\n\r\n").await.unwrap();
    let (head, body) = reply(&mut browser, false).await;
    assert!(
        head.windows(b"x-bin: caf\xe9".len())
            .any(|part| part == b"x-bin: caf\xe9")
    );
    assert_eq!(body, b"ok");
    let requests = upstream.await.unwrap();
    assert!(
        requests[0]
            .windows(b"x-bin: caf\xe9".len())
            .any(|part| part == b"x-bin: caf\xe9")
    );
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn conflicting_content_lengths_answer_400_before_minting() {
    let (mut browser, task, auth) = relay("http://127.0.0.1:1".into());
    browser.write_all(b"POST / HTTP/1.1\r\nHost: localhost:8080\r\nContent-Length: 3\r\nContent-Length: 7\r\n\r\npayload").await.unwrap();
    assert!(
        head(&mut browser)
            .await
            .starts_with(b"HTTP/1.1 400 Bad Request")
    );
    assert_eq!(auth.mint_count(), 0);
    assert!(task.await.unwrap().is_err());
}

#[tokio::test]
async fn host_validation_precedes_body_collection_and_large_bodies_are_refused() {
    for (host, status) in [("example.com:8080", 403), ("localhost:8080", 413)] {
        let (mut browser, task, auth) = relay("http://127.0.0.1:1".into());
        browser
            .write_all(
                format!("POST / HTTP/1.1\r\nHost: {host}\r\nContent-Length: 16777217\r\n\r\n")
                    .as_bytes(),
            )
            .await
            .unwrap();
        assert!(
            head(&mut browser)
                .await
                .starts_with(format!("HTTP/1.1 {status} ").as_bytes())
        );
        assert_eq!(auth.mint_count(), 0);
        drop(browser);
        task.await.unwrap().unwrap();
    }
}

#[tokio::test]
async fn chunked_request_limit_is_enforced_while_reading() {
    let (browser, task, auth) = relay("http://127.0.0.1:1".into());
    let (mut reader, mut writer) = tokio::io::split(browser);
    let sending = tokio::spawn(async move {
        writer.write_all(b"POST / HTTP/1.1\r\nHost: localhost:8080\r\nTransfer-Encoding: chunked\r\n\r\n1000001\r\n").await.unwrap();
        let _ = writer.write_all(&vec![b'x'; (16 << 20) + 1]).await;
    });
    assert!(
        head(&mut reader)
            .await
            .starts_with(b"HTTP/1.1 413 Payload Too Large")
    );
    assert_eq!(auth.mint_count(), 0);
    sending.abort();
    drop(reader);
    task.await.unwrap().unwrap();
}

#[tokio::test(start_paused = true)]
async fn idle_connections_and_incomplete_bodies_have_deadlines() {
    let (mut browser, task, auth) = relay("http://127.0.0.1:1".into());
    tokio::task::yield_now().await;
    tokio::time::advance(Duration::from_secs(6)).await;
    assert!(
        tokio::time::timeout(BOUND, task)
            .await
            .expect("idle connection closes")
            .unwrap()
            .is_err()
    );
    assert_eq!(auth.mint_count(), 0);
    let mut tail = Vec::new();
    browser.read_to_end(&mut tail).await.unwrap();

    let (mut browser, task, auth) = relay("http://127.0.0.1:1".into());
    browser
        .write_all(b"POST / HTTP/1.1\r\nHost: localhost:8080\r\nContent-Length: 2\r\n\r\nx")
        .await
        .unwrap();
    tokio::task::yield_now().await;
    tokio::time::advance(Duration::from_secs(6)).await;
    assert!(
        head(&mut browser)
            .await
            .starts_with(b"HTTP/1.1 408 Request Timeout")
    );
    assert_eq!(auth.mint_count(), 0);
    drop(browser);
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn request_body_at_the_limit_is_accepted() {
    let (endpoint, upstream) = upstream(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok".to_vec(),
        1,
    )
    .await;
    let (mut browser, task, _) = relay(endpoint);
    let size = 16 << 20;
    browser.write_all(format!("POST / HTTP/1.1\r\nHost: localhost:8080\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
    browser.write_all(&vec![b'x'; size]).await.unwrap();
    let (head, body) = reply(&mut browser, false).await;
    assert!(head.starts_with(b"HTTP/1.1 200 OK"));
    assert_eq!(body, b"ok");
    assert!(upstream.await.unwrap()[0].ends_with(&vec![b'x'; size]));
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn connection_nominated_headers_stay_on_their_own_hop() {
    let (endpoint, upstream) = upstream(b"HTTP/1.1 200 OK\r\nConnection: x-origin, close\r\nx-origin: private\r\nx-end-to-end: public\r\nContent-Length: 2\r\n\r\nok".to_vec(), 1).await;
    let (mut browser, task, _) = relay(endpoint);
    browser.write_all(b"GET / HTTP/1.1\r\nHost: localhost:8080\r\nConnection: x-local, close\r\nx-local: private\r\nx-end-to-end: public\r\n\r\n").await.unwrap();
    let (head, body) = reply(&mut browser, false).await;
    assert_eq!(body, b"ok");
    let head = String::from_utf8(head).unwrap();
    assert!(!head.contains("x-origin:"));
    assert!(head.contains("x-end-to-end: public"));
    let request = String::from_utf8(upstream.await.unwrap().remove(0)).unwrap();
    assert!(!request.contains("x-local:"));
    assert!(request.contains("x-end-to-end: public"));
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn upgrade_read_ahead_is_spliced_without_losing_bytes() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let guest = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        head(&mut stream).await;
        stream
            .write_all(
                b"HTTP/1.1 101 Switching Protocols\r\nConnection: upgrade\r\nUpgrade: test\r\n\r\n",
            )
            .await
            .unwrap();
        let mut bytes = [0; 5];
        tokio::time::timeout(BOUND, stream.read_exact(&mut bytes))
            .await
            .unwrap()
            .unwrap();
        stream.write_all(b"PONG").await.unwrap();
        bytes
    });
    let (mut browser, task, _) = relay(endpoint);
    browser.write_all(b"GET / HTTP/1.1\r\nHost: localhost:8080\r\nConnection: upgrade\r\nUpgrade: test\r\n\r\nPING!").await.unwrap();
    assert!(
        head(&mut browser)
            .await
            .starts_with(b"HTTP/1.1 101 Switching Protocols")
    );
    let mut answer = [0; 4];
    tokio::time::timeout(BOUND, browser.read_exact(&mut answer))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(&answer, b"PONG");
    assert_eq!(&guest.await.unwrap(), b"PING!");
    drop(browser);
    task.await.unwrap().unwrap();
}
