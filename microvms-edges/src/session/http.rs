// SPDX-License-Identifier: Apache-2.0
//! The production HTTP backend: one pooled reqwest client.
//!
//! [`HttpBackend`] and its request and response shapes are `microvms_app::session::http`, and
//! why a session talks through a trait rather than reqwest directly is written there.

use std::collections::HashMap;
use std::fmt;
use std::time::Duration;

use futures_util::future::BoxFuture;

use microvms_app::error::{Error, WireKind};
use microvms_app::session::http::{
    ChunkSource, HttpBackend, HttpRequest, HttpResponse, OpenStream,
};

/// The production backend: one pooled reqwest client.
///
/// Pooled rather than a client per request, because the daemon drains a bounded prefix
/// of a rejected body specifically so pooled connections keep working, and throwing the
/// pool away per request discards that.
pub struct ReqwestBackend {
    client: reqwest::Client,
    base_url: String,
    timeout: Duration,
}

impl ReqwestBackend {
    /// A backend rooted at `base_url`.
    ///
    /// A bare host is read as `https`. The endpoint the platform hands back is a
    /// hostname, and defaulting to plain HTTP there would send a bearer token in
    /// clear text on the strength of a missing prefix.
    pub fn new(base_url: &str, timeout: Duration) -> Result<Self, Error> {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .map_err(|err| {
                Error::new(
                    microvms_app::error::ErrorKind::Unexpected,
                    format!("could not build an HTTP client: {err}"),
                )
                .with_source(err)
            })?;
        Ok(Self {
            client,
            base_url: normalize_base_url(base_url),
            timeout,
        })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base_url)
    }

    fn build(&self, request: &HttpRequest) -> reqwest::RequestBuilder {
        let mut builder = self
            .client
            .request(
                reqwest::Method::from_bytes(request.method.as_bytes())
                    .unwrap_or(reqwest::Method::GET),
                self.url(&request.path),
            )
            .timeout(request.timeout.unwrap_or(self.timeout));
        for (name, value) in &request.headers {
            builder = builder.header(name, value);
        }
        if !request.body.is_empty() {
            builder = builder.body(request.body.clone());
        }
        builder
    }
}

/// A bare host becomes `https://`, and a trailing slash is dropped so a path can be
/// concatenated without producing a double one.
fn normalize_base_url(base_url: &str) -> String {
    let with_scheme = if base_url.starts_with("http://") || base_url.starts_with("https://") {
        base_url.to_string()
    } else {
        format!("https://{base_url}")
    };
    with_scheme.trim_end_matches('/').to_string()
}

/// A transport failure. Retryable, because it says nothing about the daemon's state.
fn transport_error(method: &str, path: &str, err: reqwest::Error) -> Error {
    Error::wire(
        WireKind::Transport,
        format!("{method} {path} failed on the wire: {err}"),
    )
    .with_source(err)
}

fn collect_headers(response: &reqwest::Response) -> HashMap<String, String> {
    response
        .headers()
        .iter()
        .map(|(name, value)| {
            (
                name.as_str().to_string(),
                String::from_utf8_lossy(value.as_bytes()).into_owned(),
            )
        })
        .collect()
}

impl HttpBackend for ReqwestBackend {
    fn send(&self, request: HttpRequest) -> BoxFuture<'_, Result<HttpResponse, Error>> {
        Box::pin(async move {
            let response = self
                .build(&request)
                .send()
                .await
                .map_err(|err| transport_error(request.method, &request.path, err))?;
            let status = response.status().as_u16();
            let headers = collect_headers(&response);
            let body = response
                .bytes()
                .await
                .map_err(|err| transport_error(request.method, &request.path, err))?
                .to_vec();
            Ok(HttpResponse {
                status,
                headers,
                body,
            })
        })
    }

    fn open_stream(
        &self,
        request: HttpRequest,
        idle_timeout: Duration,
    ) -> BoxFuture<'_, Result<OpenStream, Error>> {
        Box::pin(async move {
            let response = self
                .build(&request)
                // No overall timeout on a stream: the bound is `idle_timeout`, applied
                // per chunk below. An overall one would cut a healthy long-running
                // command off mid-output.
                .timeout(Duration::MAX)
                .send()
                .await
                .map_err(|err| transport_error(request.method, &request.path, err))?;
            let status = response.status().as_u16();
            let headers = collect_headers(&response);

            // The status is read before any body byte. On a failure the body is
            // collected so the typed error carries the daemon's detail string.
            if !(200..300).contains(&status) {
                let body = response
                    .bytes()
                    .await
                    .map_err(|err| transport_error(request.method, &request.path, err))?
                    .to_vec();
                return Ok((
                    HttpResponse {
                        status,
                        headers,
                        body,
                    },
                    Box::new(EmptyChunks) as Box<dyn ChunkSource>,
                ));
            }

            let head = HttpResponse {
                status,
                headers,
                body: Vec::new(),
            };
            let chunks = ReqwestChunks {
                response: Some(response),
                idle_timeout,
                method: request.method,
                path: request.path,
            };
            Ok((head, Box::new(chunks) as Box<dyn ChunkSource>))
        })
    }
}

/// No body at all, for the failure path where the head already carries everything.
struct EmptyChunks;

impl ChunkSource for EmptyChunks {
    fn next_chunk(&mut self) -> BoxFuture<'_, Result<Option<Vec<u8>>, Error>> {
        Box::pin(async { Ok(None) })
    }
}

struct ReqwestChunks {
    response: Option<reqwest::Response>,
    idle_timeout: Duration,
    method: &'static str,
    path: String,
}

impl ChunkSource for ReqwestChunks {
    fn next_chunk(&mut self) -> BoxFuture<'_, Result<Option<Vec<u8>>, Error>> {
        Box::pin(async move {
            let Some(response) = self.response.as_mut() else {
                return Ok(None);
            };
            match tokio::time::timeout(self.idle_timeout, response.chunk()).await {
                Ok(Ok(Some(bytes))) => Ok(Some(bytes.to_vec())),
                Ok(Ok(None)) => {
                    self.response = None;
                    Ok(None)
                }
                Ok(Err(err)) => {
                    self.response = None;
                    Err(transport_error(self.method, &self.path, err))
                }
                Err(_) => {
                    self.response = None;
                    // Retryable by construction: silence past the keepalive interval
                    // means the connection is dead, and the exec is untouched.
                    Err(Error::wire(
                        WireKind::Transport,
                        format!(
                            "{} {} went silent for {}s, longer than the keepalive \
                             interval, so the connection is treated as dead",
                            self.method,
                            self.path,
                            self.idle_timeout.as_secs()
                        ),
                    ))
                }
            }
        })
    }
}

impl fmt::Debug for ReqwestBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ReqwestBackend")
            .field("base_url", &self.base_url)
            .field("timeout", &self.timeout)
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A bare host is `https`, because the platform hands back a hostname and plain
    /// HTTP there would put a bearer token on the wire in clear text.
    #[test]
    fn a_bare_endpoint_host_is_read_as_https() {
        assert_eq!(
            normalize_base_url("vm-abc.microvms.aws"),
            "https://vm-abc.microvms.aws"
        );
        assert_eq!(
            normalize_base_url("http://127.0.0.1:9000"),
            "http://127.0.0.1:9000",
            "an explicit scheme is honoured, so a local daemon is still reachable"
        );
        assert_eq!(
            normalize_base_url("https://host/"),
            "https://host",
            "a trailing slash would produce a double one on every path"
        );
    }
}
