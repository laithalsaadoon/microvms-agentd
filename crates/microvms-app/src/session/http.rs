// SPDX-License-Identifier: Apache-2.0
//! The HTTP seam: one request/response shape, two backends.
//!
//! # Why a trait rather than reqwest directly
//!
//! Everything worth testing about this client sits *above* HTTP — whether both proxy
//! headers went out, whether a mint happened inside the retry path, whether a
//! reconnect resumed at the right byte. reqwest opens real sockets, so none of those
//! are reachable from a test that does not stand up a server, and the two that matter
//! most are only reachable by inspecting a request that was already sent.
//!
//! So [`HttpBackend`] is the seam. Production is `ReqwestBackend` (in `microvms-edges`, and
//! at `microvms_core::session::ReqwestBackend`); a test supplies a
//! recorder that keeps every request head, or a backend that writes bytes at a
//! simulated network. The daemon made the mirror-image choice with
//! `axum::serve::Listener`, and for the same reason: the simulator stays out of the
//! shipping artifact.
//!
//! # Streaming is a separate method
//!
//! [`HttpBackend::send`] collects the whole body, which for an SSE attach means it
//! returns once the command is over — and every property worth checking about a stream
//! is only observable partway through. [`HttpBackend::open_stream`] hands back a chunk
//! source instead.

use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use futures_util::future::BoxFuture;

use crate::error::{Error, WireKind};

/// One request, already fully addressed: absolute URL, every header, whole body.
///
/// Owned rather than borrowed because a backend may need to move it into a spawned
/// task, and a recorder needs to keep it after the call returns.
#[derive(Clone)]
pub struct HttpRequest {
    pub method: &'static str,
    /// Path plus query string. Not a full URL: the base is the backend's, so a
    /// rebound endpoint changes one field rather than every call site.
    pub path: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    /// How long the whole exchange may take. `None` means the backend's default.
    pub timeout: Option<Duration>,
}

impl HttpRequest {
    pub fn new(method: &'static str, path: impl Into<String>) -> Self {
        Self {
            method,
            path: path.into(),
            headers: Vec::new(),
            body: Vec::new(),
            timeout: None,
        }
    }

    /// One header's value, matched case-insensitively.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

/// Names the headers, never their values, and sizes the body rather than printing it.
///
/// Every request on this seam carries `authorization: Bearer <agent token>`, and a
/// streaming attach also carries the minted `X-aws-proxy-auth` — so a derived `Debug` puts
/// two credentials in any log line, error chain, or test-failure message that formats a
/// request. Redacting *all* header values rather than allowlisting the two known ones,
/// because an allowlist is a list somebody has to remember to extend the next time the
/// platform adds a header, and the platform's stated reason for a token *map* is that it
/// may need more than one.
///
/// The body is sized rather than shown for the same reason: an upload body is file
/// contents, a stdin body is base64 of whatever the caller is feeding a child, and neither
/// belongs in a log.
impl fmt::Debug for HttpRequest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let names: Vec<&str> = self.headers.iter().map(|(name, _)| name.as_str()).collect();
        f.debug_struct("HttpRequest")
            .field("method", &self.method)
            .field("path", &self.path)
            .field("headers", &names)
            .field("body_len", &self.body.len())
            .field("timeout", &self.timeout)
            .finish()
    }
}

/// One response, body already collected.
#[derive(Clone, Debug)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: HashMap<String, String>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    /// The typed error for this response, or `Ok` for a 2xx.
    ///
    /// Every non-2xx goes through here, so the status-to-[`WireKind`] table is applied
    /// in exactly one place. A status the daemon never chooses becomes a
    /// [`WireKind::ServerError`]-free plain protocol failure rather than being mapped
    /// to the nearest neighbour — see [`crate::error::WireKind::from_status`] on why
    /// there is no generic 4xx fallback.
    pub fn error_for_status(&self, method: &str, path: &str) -> Result<(), Error> {
        if (200..300).contains(&self.status) {
            return Ok(());
        }
        // Capped at 512 bytes, as the Python does: a detail string is for a human
        // reading a log, and a 256 MB error body in a message is its own incident.
        let detail = String::from_utf8_lossy(&self.body[..self.body.len().min(512)])
            .trim()
            .to_string();
        let message = if detail.is_empty() {
            format!("{method} {path} -> {}", self.status)
        } else {
            format!("{method} {path} -> {}: {detail}", self.status)
        };
        match WireKind::from_status(self.status) {
            Some(wire) => Err(Error::wire(wire, message)),
            // A status outside the daemon's vocabulary. `Protocol` rather than
            // `Retryable`, because nothing says a retry would land differently, and
            // rather than a specific wire kind, because inventing one would be this
            // client claiming to know a meaning the daemon never assigned.
            None => Err(Error::new(crate::error::ErrorKind::Protocol, message)),
        }
    }
}

/// A body arriving in pieces.
///
/// One method, because that is all a cursor-driven stream needs: `Ok(None)` is the end
/// of the body, however it ended. Which *way* it ended is not this trait's business —
/// the protocol answers that with the presence or absence of a terminal `exit` event,
/// which is exactly why the transport is framed.
pub trait ChunkSource: Send {
    fn next_chunk(&mut self) -> BoxFuture<'_, Result<Option<Vec<u8>>, Error>>;
}

/// A streaming response: the head, and the body still to arrive.
///
/// Named rather than spelled inline at [`HttpBackend::open_stream`], because the two
/// halves are one thing — the status has to be readable before the first body byte, so a
/// backend cannot hand back one without the other.
pub type OpenStream = (HttpResponse, Box<dyn ChunkSource>);

/// The seam. Production and simulated transports implement this.
pub trait HttpBackend: Send + Sync {
    /// Sends one request and collects the whole body, whatever the status.
    ///
    /// Deliberately does not fail on a status: a conformance suite asserts on 401 and
    /// 409 as expected outcomes, and a client that could only reach them through an
    /// error would be a client that cannot test the protocol.
    fn send(&self, request: HttpRequest) -> BoxFuture<'_, Result<HttpResponse, Error>>;

    /// Opens a streaming response, reading the status before any body byte.
    ///
    /// Status first so a 404 on an unknown exec id surfaces as `NotFound` rather than
    /// as an empty stream. `idle_timeout` bounds *silence* rather than duration: an SSE
    /// body is idle by design between chunks, so the useful bound is how long a gap may
    /// last. Without one, a half-open connection — the failure a NAT or proxy timeout
    /// produces, where no FIN ever arrives — hangs forever and the reconnect logic
    /// never runs.
    fn open_stream(
        &self,
        request: HttpRequest,
        idle_timeout: Duration,
    ) -> BoxFuture<'_, Result<OpenStream, Error>>;
}

/// A backend behind an `Arc`, which is what a [`crate::session::Session`] holds.
pub type SharedBackend = Arc<dyn HttpBackend>;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorKind;

    /// Each status the daemon chooses becomes its own variant, and an unmapped 4xx
    /// becomes none of them.
    #[test]
    fn a_response_status_becomes_the_wire_kind_the_daemon_meant() {
        let response = |status: u16| HttpResponse {
            status,
            headers: HashMap::new(),
            body: b"{\"error\":\"unknown_exec\",\"detail\":\"e1\"}".to_vec(),
        };
        assert!(response(200).error_for_status("GET", "/v1/health").is_ok());

        let err = response(404)
            .error_for_status("GET", "/v1/exec/e1")
            .expect_err("404 is an error");
        assert_eq!(err.wire_kind(), Some(WireKind::NotFound));
        assert!(
            err.to_string().contains("unknown_exec"),
            "the daemon's detail must survive into the message: {err}"
        );

        let err = response(429)
            .error_for_status("GET", "/v1/health")
            .expect_err("an unmapped status is still an error");
        assert_eq!(err.kind(), ErrorKind::Protocol);
        assert_eq!(
            err.wire_kind(),
            None,
            "a status the daemon never chooses must not be given an invented meaning"
        );
    }

    /// A body long enough to be its own problem is truncated in the message.
    #[test]
    fn an_enormous_error_body_is_capped_in_the_message() {
        let response = HttpResponse {
            status: 500,
            headers: HashMap::new(),
            body: vec![b'x'; 100_000],
        };
        let err = response
            .error_for_status("PUT", "/v1/fs/file")
            .expect_err("500 is an error");
        assert!(err.to_string().len() < 700, "{}", err.to_string().len());
    }

    /// Header lookup on a request is case-insensitive, since a caller may spell a
    /// header either way and the proxy compares insensitively.
    #[test]
    fn a_request_header_is_found_whatever_case_it_was_set_in() {
        let mut request = HttpRequest::new("GET", "/v1/health");
        request
            .headers
            .push(("X-Aws-Proxy-Port".into(), "9000".into()));
        assert_eq!(request.header("x-aws-proxy-port"), Some("9000"));
        assert_eq!(request.header("authorization"), None);
    }

    /// A request's `Debug` names its headers and never their values, so neither the agent
    /// token nor the minted proxy token reaches a log line or a test-failure message.
    ///
    /// **Falsification** — restore `#[derive(Debug)]` on [`HttpRequest`] and both value
    /// assertions fail; drop the body from the redaction and the last one fails.
    #[test]
    fn a_request_debug_prints_header_names_and_never_their_values() {
        let mut request = HttpRequest::new("POST", "/v1/exec/e1/stdin");
        request.headers.push((
            "authorization".into(),
            "Bearer super-secret-agent-token".into(),
        ));
        request
            .headers
            .push(("X-aws-proxy-auth".into(), "eyJhbGciOi-secret-jwe".into()));
        request.body = b"secret-body-bytes".to_vec();
        request.timeout = Some(Duration::from_secs(7));

        let rendered = format!("{request:?}");
        assert!(rendered.contains("authorization"), "{rendered}");
        assert!(rendered.contains("X-aws-proxy-auth"), "{rendered}");
        assert!(rendered.contains("/v1/exec/e1/stdin"), "{rendered}");
        assert!(rendered.contains("POST"), "{rendered}");
        assert!(rendered.contains('7'), "the timeout survives: {rendered}");
        assert!(
            rendered.contains("body_len"),
            "the body's size is diagnostic: {rendered}"
        );
        assert!(
            !rendered.contains("super-secret-agent-token"),
            "the agent token reached a Debug string: {rendered}"
        );
        assert!(
            !rendered.contains("eyJhbGciOi-secret-jwe"),
            "the minted proxy token reached a Debug string: {rendered}"
        );
        assert!(
            !rendered.contains("secret-body-bytes"),
            "the body reached a Debug string: {rendered}"
        );
    }
}
