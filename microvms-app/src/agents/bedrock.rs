// SPDX-License-Identifier: Apache-2.0
//! A Bedrock bearer token, and how long it lives (AGENT-4).
//!
//! Minting one signs a SigV4 query presign with the caller's AWS credentials, which reaches
//! the credential chain, so the minter is in `microvms-edges` (at
//! `microvms_core::agents::bedrock::mint`). This module holds what the agent helpers carry.
//!
//! # The token is a credential
//!
//! [`BearerToken`]'s `Debug` prints its length and nothing else, per
//! `.erpaval/solutions/best-practices/credential-structs-never-derive-debug.md`. It
//! reaches the guest as a file over the authenticated channel and never as an argv
//! element or an env var on the wire.

use std::time::{Duration, SystemTime};

/// The reference implementation's `TOKEN_DURATION`: the default and the ceiling.
pub const MAX_LIFETIME: Duration = Duration::from_secs(43_200);

/// A minted bearer token. Opaque; `Debug` shows only the length.
#[derive(Clone, PartialEq, Eq)]
pub struct BearerToken(String);

impl BearerToken {
    /// Wraps a minted token's text. The minter builds one; so can a test that needs a token
    /// shaped value without signing anything.
    pub fn new(token: String) -> Self {
        Self(token)
    }

    /// The token text, for writing into the guest's environment file.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for BearerToken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "BearerToken(<{} bytes>)", self.0.len())
    }
}

/// A token and when it stops working.
#[derive(Clone, Debug)]
pub struct Minted {
    pub token: BearerToken,
    /// The earlier of the presign expiry and the known signing credential expiry.
    /// With credentials lacking expiry metadata, this remains only an upper bound.
    pub expires_at: SystemTime,
}
