// SPDX-License-Identifier: Apache-2.0
//! The adapters port: the production pieces a use case builds for itself partway through.
//!
//! # Why a port for construction
//!
//! Most ports are handed to a use case whole: a [`ControlPlane`](crate::control::ControlPlane)
//! gets its transport, clock and entropy when it's built. Two pieces can't be, because what
//! they're built for isn't known until later. A sandbox learns its VM's endpoint from the
//! launch reply, and only then can it build the HTTP backend its session talks through, and
//! `ensure_image` builds its STS and S3 client on first use, for the plane's region. Building
//! either inline would put reqwest and the credential chain inside the use case, so the use
//! case asks an [`Adapters`] for them instead.
//!
//! `SystemAdapters` (in `microvms-edges`) is the production implementation. A test passes one that hands out
//! fakes, or that refuses, so a session a sandbox builds can't dial a real endpoint by
//! accident.

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use futures_util::future::BoxFuture;

use crate::clock::Clock;
use crate::control::BuildServices;
use crate::error::Error;
use crate::region::Region;
use crate::session::SharedBackend;

/// The production pieces a use case builds once it knows what they're for.
pub trait Adapters: Send + Sync + fmt::Debug {
    /// The HTTP backend a session against `endpoint` sends through, whose non-streaming
    /// requests time out after `timeout`.
    fn http_backend(&self, endpoint: &str, timeout: Duration) -> Result<SharedBackend, Error>;

    /// The STS and S3 calls `ensure_image` makes, with credentials for `region`.
    fn build_services(
        &self,
        region: Region,
    ) -> BoxFuture<'_, Result<Arc<dyn BuildServices>, Error>>;

    /// The clock a session reads when its builder was given none.
    ///
    /// A session a sandbox builds takes the plane's clock instead, so the lifecycle and the
    /// session it hands out read one time source.
    fn clock(&self) -> Arc<dyn Clock>;

    /// Reports a warning no caller is left to receive, such as a `Sandbox` dropped with its VM
    /// still billing.
    ///
    /// A use case can't write to a stream itself, since the driving adapter owns those. It
    /// mustn't panic or block: `Drop` calls it, and a panic there during an unwind aborts.
    fn warn(&self, warning: &str);
}

#[cfg(any(test, feature = "test-support"))]
pub(crate) mod testing {
    //! The adapters the crate's tests wire in when nothing under test should reach a network.

    use super::*;
    use crate::clock::testing::TestClock;
    use crate::error::{ErrorKind, WireKind};
    use crate::session::{HttpBackend, HttpRequest, HttpResponse, OpenStream};

    /// Adapters that build nothing real.
    ///
    /// The HTTP backend answers every request with a transport failure naming it, and the
    /// build services refuse. A test that wants a session to succeed passes its own backend
    /// through `Sandbox::with_session_backend` or `SessionBuilder::with_backend`, which win
    /// over the adapters. A test that sets neither can't reach the endpoint its fake launch
    /// reply named.
    #[derive(Debug, Default)]
    pub struct TestAdapters {
        clock: Arc<TestClock>,
        warnings: std::sync::Mutex<Vec<String>>,
    }

    impl TestAdapters {
        pub fn new() -> Self {
            Self::default()
        }

        /// Adapters whose default session clock is `clock`.
        pub fn with_clock(clock: Arc<TestClock>) -> Self {
            Self {
                clock,
                ..Self::default()
            }
        }

        /// Every warning reported through these adapters, oldest first.
        pub fn warnings(&self) -> Vec<String> {
            self.warnings
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .clone()
        }
    }

    impl Adapters for TestAdapters {
        fn http_backend(&self, endpoint: &str, _timeout: Duration) -> Result<SharedBackend, Error> {
            Ok(Arc::new(Inert {
                endpoint: endpoint.to_string(),
            }))
        }

        fn build_services(
            &self,
            region: Region,
        ) -> BoxFuture<'_, Result<Arc<dyn BuildServices>, Error>> {
            Box::pin(async move {
                Err(Error::new(
                    ErrorKind::Credentials,
                    format!(
                        "the test adapters build no STS or S3 client for {region}; pass one \
                         with Sandbox::with_build_services"
                    ),
                ))
            })
        }

        fn clock(&self) -> Arc<dyn Clock> {
            Arc::clone(&self.clock) as Arc<dyn Clock>
        }

        fn warn(&self, warning: &str) {
            self.warnings
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .push(warning.to_string());
        }
    }

    /// A backend that fails every request, naming the endpoint it wasn't allowed to reach.
    struct Inert {
        endpoint: String,
    }

    impl Inert {
        fn refuse(&self, request: &HttpRequest) -> Error {
            Error::wire(
                WireKind::Transport,
                format!(
                    "{} {} was sent to {} through the test adapters, which reach nothing",
                    request.method, request.path, self.endpoint
                ),
            )
        }
    }

    impl HttpBackend for Inert {
        fn send(&self, request: HttpRequest) -> BoxFuture<'_, Result<HttpResponse, Error>> {
            let error = self.refuse(&request);
            Box::pin(async move { Err(error) })
        }

        fn open_stream(
            &self,
            request: HttpRequest,
            _idle_timeout: Duration,
        ) -> BoxFuture<'_, Result<OpenStream, Error>> {
            let error = self.refuse(&request);
            Box::pin(async move { Err(error) })
        }
    }
}
