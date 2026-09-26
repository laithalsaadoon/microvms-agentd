// SPDX-License-Identifier: Apache-2.0
//! The session's production pieces: the reqwest backend and the sockets.
//!
//! [`forward`] is the port forwarder's listener and its 403-vs-502 diagnostic. [`tunnel`] is
//! the WebSocket client that carries raw TCP to the daemon's relay, and [`shell`] is the
//! interactive shell over the same kind of socket. Each builds on the session's
//! `ProxyAuth` in `microvms-app`, which mints the tokens they present.

pub mod forward;
pub mod http;
pub mod shell;
pub mod tunnel;

pub use forward::{
    DEFAULT_EXCHANGE_TIMEOUT, ForwardClient, ForwardEvent, ForwardSpec, forwards_request_header,
    refusal_explanation, upstream_url,
};
pub use http::ReqwestBackend;
pub use tunnel::{TUNNEL_CHUNK_BYTES, TunnelEnd, explain_close, relay_connection, tunnel_url};
