// SPDX-License-Identifier: Apache-2.0
//! The two calls outside the MicroVMs API that `ensure_image` makes: the caller's account,
//! for the image ARN, and the artifact upload (#221). Not yet implemented.

use futures_util::future::BoxFuture;

use crate::error::{Error, ErrorKind};
use crate::region::Region;

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

/// The real services. Not yet implemented.
#[derive(Debug)]
pub struct SignedBuildServices {
    _region: Region,
}

impl SignedBuildServices {
    /// Not yet implemented.
    pub async fn new(_region: Region) -> Result<Self, Error> {
        Err(Error::new(
            ErrorKind::Unexpected,
            "SignedBuildServices is not implemented yet (#221)",
        ))
    }
}

impl BuildServices for SignedBuildServices {
    fn caller_account(&self) -> BoxFuture<'_, Result<String, Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Unexpected, "not implemented (#221)")) })
    }

    fn put_object<'a>(
        &'a self,
        _bucket: &'a str,
        _key: &'a str,
        _bytes: Vec<u8>,
    ) -> BoxFuture<'a, Result<(), Error>> {
        Box::pin(async { Err(Error::new(ErrorKind::Unexpected, "not implemented (#221)")) })
    }
}
