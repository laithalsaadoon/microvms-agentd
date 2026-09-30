// SPDX-License-Identifier: Apache-2.0
//! The two calls outside the MicroVMs API that `ensure_image` makes: the caller's account,
//! for the image ARN, and the artifact upload (#221, IMAGE-8).
//!
//! [`BuildServices`] is the port. `Sandbox::with_build_services` takes one, a test records
//! the calls and answers them, and production gets `SignedBuildServices` (in
//! `microvms-edges`, re-exported as `microvms_core::control::SignedBuildServices`) through the
//! plane's adapters on the first `ensure_image`. The account comes from STS because
//! `GetMicrovmImage` takes an ARN and the service rejects a bare name; `GetCallerIdentity`
//! answers the account for any identity and needs no permission to make.

use futures_util::future::BoxFuture;

use crate::error::Error;

/// The seam `ensure_image` reaches STS and S3 through.
pub trait BuildServices: Send + Sync {
    /// The caller's twelve-digit account id.
    fn caller_account(&self) -> BoxFuture<'_, Result<String, Error>>;

    /// Puts `bytes` at `s3://<bucket>/<key>`.
    fn put_object<'a>(
        &'a self,
        bucket: &'a str,
        key: &'a str,
        bytes: Vec<u8>,
    ) -> BoxFuture<'a, Result<(), Error>>;
}
