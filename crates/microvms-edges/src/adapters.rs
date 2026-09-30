// SPDX-License-Identifier: Apache-2.0
//! The production [`Adapters`]: reqwest for sessions, SigV4 for STS and S3, tokio's clock.

use std::sync::Arc;
use std::time::Duration;

use futures_util::future::BoxFuture;

use microvms_app::adapters::Adapters;
use microvms_app::clock::Clock;
use microvms_app::control::BuildServices;
use microvms_app::error::Error;
use microvms_app::region::Region;
use microvms_app::session::SharedBackend;

use crate::clock::TokioClock;
use crate::control::services::SignedBuildServices;
use crate::session::http::ReqwestBackend;

/// The adapters every production control plane carries.
///
/// Holds nothing. Each call builds its piece fresh, the way the use cases did inline before
/// they went through this port, so the credential chain resolves when it's first needed
/// rather than when the plane is built.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemAdapters;

impl Adapters for SystemAdapters {
    fn http_backend(&self, endpoint: &str, timeout: Duration) -> Result<SharedBackend, Error> {
        Ok(Arc::new(ReqwestBackend::new(endpoint, timeout)?))
    }

    fn build_services(
        &self,
        region: Region,
    ) -> BoxFuture<'_, Result<Arc<dyn BuildServices>, Error>> {
        Box::pin(async move {
            Ok(Arc::new(SignedBuildServices::new(region).await?) as Arc<dyn BuildServices>)
        })
    }

    fn clock(&self) -> Arc<dyn Clock> {
        Arc::new(TokioClock::new())
    }

    /// A plain write to stderr rather than a log macro, because the client has no logging
    /// dependency, and taking one on to warn about a leak would be a dependency for a
    /// diagnostic. Not `eprintln!`: it panics when stderr's reader has gone, and a panic in
    /// `drop` during an unwind aborts the process (CLI-7). The error is ignored; there is
    /// nowhere left to report it.
    fn warn(&self, warning: &str) {
        use std::io::Write as _;
        let _ = writeln!(std::io::stderr(), "{warning}");
    }
}
