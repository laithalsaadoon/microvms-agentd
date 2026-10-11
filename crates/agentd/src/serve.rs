// SPDX-License-Identifier: Apache-2.0
//! Serving the router over real or simulated sockets with bounded connections.

use std::fmt::Debug;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use axum::Router;
use axum::serve::Listener;
use hyper_util::rt::{TokioIo, TokioTimer};
use hyper_util::service::TowerToHyperService;
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

const MAX_CONNECTIONS: usize = 128;
const HEADER_TIMEOUT: Duration = Duration::from_secs(5);

/// Serves `app` on `listener` until the process is asked to stop.
///
/// Connection admission happens before reading headers or collecting a body.
/// HTTP upgrades keep their permit until the upgraded socket itself is dropped.
pub async fn serve<L>(mut listener: L, app: Router) -> std::io::Result<()>
where
    L: Listener + Send + 'static,
    L::Addr: Debug,
{
    let capacity = Arc::new(tokio::sync::Semaphore::new(MAX_CONNECTIONS));
    let mut connections = tokio::task::JoinSet::new();
    let (shutdown, _) = tokio::sync::watch::channel(false);
    let signal = shutdown_signal();
    tokio::pin!(signal);
    loop {
        while connections.try_join_next().is_some() {}
        tokio::select! {
            _ = &mut signal => break,
            _ = connections.join_next(), if !connections.is_empty() => {},
            (socket, _) = listener.accept() => {
                let Ok(permit) = capacity.clone().try_acquire_owned() else {
                    // Close excess connections without allocating a connection task.
                    drop(socket);
                    continue;
                };
                let service = TowerToHyperService::new(app.clone());
                let mut stopping = shutdown.subscribe();
                connections.spawn(async move {
                    let io = TokioIo::new(BoundedIo::new(socket, permit));
                    let mut builder = hyper::server::conn::http1::Builder::new();
                    builder.timer(TokioTimer::new())
                        .header_read_timeout(HEADER_TIMEOUT)
                        .max_buf_size(64 * 1024);
                    let connection = builder.serve_connection(io, service).with_upgrades();
                    tokio::pin!(connection);
                    tokio::select! {
                        result = &mut connection => {
                            if let Err(error) = result {
                                tracing::debug!(%error, "HTTP connection ended");
                            }
                        }
                        _ = stopping.changed() => {
                            connection.as_mut().graceful_shutdown();
                            let _ = connection.await;
                        }
                    }
                });
            }
        }
    }
    let _ = shutdown.send(true);
    while connections.join_next().await.is_some() {}
    Ok(())
}

/// The permit stays with the socket through an HTTP upgrade, so a tunnel counts
/// toward connection admission until its I/O is dropped.
struct BoundedIo<T> {
    inner: T,
    _permit: tokio::sync::OwnedSemaphorePermit,
}

impl<T> BoundedIo<T> {
    fn new(inner: T, permit: tokio::sync::OwnedSemaphorePermit) -> Self {
        Self {
            inner,
            _permit: permit,
        }
    }
}

impl<T: AsyncRead + Unpin> AsyncRead for BoundedIo<T> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_read(cx, buffer)
    }
}

impl<T: AsyncWrite + Unpin> AsyncWrite for BoundedIo<T> {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.inner).poll_write(cx, data)
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

/// Resolves when the process receives a shutdown signal.
///
/// `/terminate` acknowledges its lifecycle handler; it does not resolve this
/// future. A process signal starts graceful shutdown so in-flight requests can
/// drain.
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(sig) => sig,
            Err(err) => {
                tracing::warn!(%err, "cannot listen for SIGTERM; graceful shutdown disabled");
                std::future::pending::<()>().await;
                return;
            }
        };
        tokio::select! {
            _ = term.recv() => tracing::info!("SIGTERM received; draining"),
            result = tokio::signal::ctrl_c() => match result {
                Ok(()) => tracing::info!("interrupt received; draining"),
                Err(err) => tracing::warn!(%err, "interrupt listener failed"),
            },
        }
    }

    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
