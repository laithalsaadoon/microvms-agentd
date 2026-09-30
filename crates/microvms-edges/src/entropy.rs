// SPDX-License-Identifier: Apache-2.0
//! The production entropy source.
//!
//! The port is `microvms_app::entropy::Entropy`, and why nonces, agent tokens and identity
//! seeds go through one is written there.

use microvms_app::entropy::Entropy;
use microvms_app::error::{Error, ErrorKind};

/// The production source: the kernel CSPRNG, through `getrandom`.
///
/// `getrandom` reaches the kernel directly. It fails only when the pool is genuinely
/// unavailable, which is why [`Entropy::fill`] can fail at all.
#[derive(Clone, Copy, Debug, Default)]
pub struct OsEntropy;

impl Entropy for OsEntropy {
    fn fill(&self, buf: &mut [u8]) -> Result<(), Error> {
        getrandom::fill(buf).map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("the OS random pool is unavailable: {error}"),
            )
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::*;

    /// The OS source produces distinct draws. One that returned a constant would pass every
    /// shape test and fail TRAP-1 in production.
    #[test]
    fn the_os_source_draws_distinct_bytes() {
        let drawn: HashSet<[u8; 8]> = (0..200)
            .map(|_| {
                let mut bytes = [0_u8; 8];
                OsEntropy.fill(&mut bytes).expect("the pool is available");
                bytes
            })
            .collect();
        assert_eq!(drawn.len(), 200);
    }
}
