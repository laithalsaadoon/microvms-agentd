// SPDX-License-Identifier: Apache-2.0
//! Response streaming and per-read deadlines at the local forwarding boundary.

use microvms_app::session::proxy::ProxyAuth;
use microvms_app::testing::CountingMinter;
use microvms_edges::session::forward::{
    ForwardClient, ForwardEvent, ForwardSpec, serve_connection,
};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _, DuplexStream};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

const BOUND: Duration = Duration::from_secs(5);
type RelayTask = JoinHandle<Result<(), microvms_app::error::Error>>;

fn relay(
    endpoint: String,
    timeout: Duration,
    client: Arc<ForwardClient>,
) -> (DuplexStream, RelayTask) {
    relay_observed(endpoint, timeout, client, Arc::new(Mutex::new(Vec::new())))
}

fn relay_observed(
    endpoint: String,
    timeout: Duration,
    client: Arc<ForwardClient>,
    events: Arc<Mutex<Vec<ForwardEvent>>>,
) -> (DuplexStream, RelayTask) {
    let auth = Arc::new(ProxyAuth::with_clock(
        Arc::new(CountingMinter::default()),
        microvms_app::session::DEFAULT_AGENT_PORT,
        Arc::new(microvms_edges::clock::TokioClock::new()),
    ));
    let mut spec = ForwardSpec::new("127.0.0.1:8080".parse().unwrap(), 8080, endpoint);
    spec.exchange_timeout = timeout;
    let (browser, local) = tokio::io::duplex(1024);
    let task = tokio::spawn(async move {
        serve_connection(local, &spec, &auth, &client, |event| {
            events.lock().unwrap().push(event)
        })
        .await
    });
    (browser, task)
}

async fn listener() -> (String, TcpListener) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    (
        format!("http://{}", listener.local_addr().unwrap()),
        listener,
    )
}

async fn through<S: tokio::io::AsyncRead + Unpin>(stream: &mut S, suffix: &[u8]) -> Vec<u8> {
    let mut bytes = Vec::new();
    while !bytes.ends_with(suffix) {
        bytes.push(stream.read_u8().await.expect("response remains open"));
    }
    bytes
}

async fn request(browser: &mut DuplexStream) {
    browser
        .write_all(b"GET /events HTTP/1.1\r\nHost: localhost:8080\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();
}

#[tokio::test]
async fn first_event_arrives_while_the_upstream_body_is_still_open() {
    let (endpoint, listener) = listener().await;
    let (release, released) = tokio::sync::oneshot::channel();
    let upstream = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        through(&mut stream, b"\r\n\r\n").await;
        stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\nb\r\ndata: one\n\n\r\n").await.unwrap();
        released.await.unwrap();
        stream.write_all(b"0\r\n\r\n").await.unwrap();
    });
    let events = Arc::new(Mutex::new(Vec::new()));
    let (mut browser, task) = relay_observed(
        endpoint,
        Duration::from_secs(2),
        Arc::new(ForwardClient::new().unwrap()),
        events.clone(),
    );
    request(&mut browser).await;
    let bytes = tokio::time::timeout(
        Duration::from_secs(1),
        through(&mut browser, b"data: one\n\n"),
    )
    .await
    .expect("first event within one second");
    assert!(bytes.starts_with(b"HTTP/1.1 200 OK\r\n"));
    assert!(!task.is_finished(), "the body is still open");
    assert!(
        events.lock().unwrap().is_empty(),
        "an open body has not completed"
    );
    release.send(()).unwrap();
    let mut tail = Vec::new();
    tokio::time::timeout(BOUND, browser.read_to_end(&mut tail))
        .await
        .unwrap()
        .unwrap();
    assert!(
        tail.ends_with(b"0\r\n\r\n"),
        "stream finishes with complete HTTP framing"
    );
    task.await.unwrap().unwrap();
    assert!(matches!(
        events.lock().unwrap().as_slice(),
        [ForwardEvent::Forwarded {
            status: 200,
            upgraded: false
        }]
    ));
    upstream.await.unwrap();
}

#[tokio::test]
async fn successful_reads_keep_a_stream_open_past_the_exchange_timeout() {
    let (endpoint, listener) = listener().await;
    let upstream = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        through(&mut stream, b"\r\n\r\n").await;
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            .await
            .unwrap();
        for _ in 0..8 {
            stream.write_all(b"1\r\nx\r\n").await.unwrap();
            tokio::time::sleep(Duration::from_millis(150)).await;
        }
        stream.write_all(b"0\r\n\r\n").await.unwrap();
    });
    let (mut browser, task) = relay(
        endpoint,
        Duration::from_secs(1),
        Arc::new(ForwardClient::new().unwrap()),
    );
    request(&mut browser).await;
    let mut bytes = Vec::new();
    tokio::time::timeout(BOUND, browser.read_to_end(&mut bytes))
        .await
        .unwrap()
        .unwrap();
    assert!(bytes.starts_with(b"HTTP/1.1 200 OK"));
    assert_eq!(bytes.iter().filter(|byte| **byte == b'x').count(), 8);
    assert!(bytes.ends_with(b"0\r\n\r\n"));
    task.await.unwrap().unwrap();
    upstream.await.unwrap();
}

#[tokio::test]
async fn response_head_and_body_gaps_each_have_a_deadline() {
    for send_head in [false, true] {
        let (endpoint, listener) = listener().await;
        let upstream = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            through(&mut stream, b"\r\n\r\n").await;
            if send_head {
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n")
                    .await
                    .unwrap();
            }
            std::future::pending::<()>().await;
        });
        let (mut browser, task) = relay(
            endpoint,
            Duration::from_millis(200),
            Arc::new(ForwardClient::new().unwrap()),
        );
        request(&mut browser).await;
        let mut bytes = Vec::new();
        tokio::time::timeout(BOUND, browser.read_to_end(&mut bytes))
            .await
            .expect("idle exchange closes")
            .unwrap();
        assert_eq!(bytes.starts_with(b"HTTP/1.1 200 OK"), send_head);
        assert!(
            !bytes.ends_with(b"0\r\n\r\n"),
            "a failed body must not look complete"
        );
        assert!(task.await.unwrap().is_err());
        upstream.abort();
    }
}

#[tokio::test]
async fn replacing_a_custom_timeout_keeps_active_clients_independent() {
    let (endpoint, listener) = listener().await;
    let upstream = tokio::spawn(async move {
        let mut exchanges = tokio::task::JoinSet::new();
        for _ in 0..2 {
            let (mut stream, _) = listener.accept().await.unwrap();
            exchanges.spawn(async move {
                through(&mut stream, b"\r\n\r\n").await;
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n")
                    .await
                    .unwrap();
                tokio::time::sleep(Duration::from_millis(400)).await;
                let _ = stream.write_all(b"ok").await;
            });
        }
        while exchanges.join_next().await.is_some() {}
    });
    let client = Arc::new(ForwardClient::new().unwrap());
    let (mut long, long_task) = relay(endpoint.clone(), Duration::from_secs(2), client.clone());
    request(&mut long).await;
    tokio::time::timeout(BOUND, through(&mut long, b"\r\n\r\n"))
        .await
        .unwrap();
    let (mut short, short_task) = relay(endpoint, Duration::from_millis(100), client);
    request(&mut short).await;
    let mut short_body = Vec::new();
    tokio::time::timeout(BOUND, short.read_to_end(&mut short_body))
        .await
        .unwrap()
        .unwrap();
    assert!(!short_body.ends_with(b"ok"));
    assert!(short_task.await.unwrap().is_err());
    let mut long_body = Vec::new();
    tokio::time::timeout(BOUND, long.read_to_end(&mut long_body))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(long_body, b"ok");
    long_task.await.unwrap().unwrap();
    upstream.await.unwrap();
}
