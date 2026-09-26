// SPDX-License-Identifier: Apache-2.0
//! The entropy port: where idempotency nonces, agent tokens and identity seeds come from.
//!
//! # Why a port for randomness
//!
//! Three values a launch mints are random by requirement, and each has a different reason:
//!
//! * the `clientToken` nonce must never repeat across attempts, because a replayed token
//!   wedges an image in `CREATING` for fifteen hours (TRAP-1, [`crate::control::token`]);
//! * the agent token is a bearer credential, so it must be unguessable;
//! * the identity seeds are x25519 secrets.
//!
//! Drawing them through [`Entropy`] rather than calling the OS pool inline keeps the use cases
//! free of that I/O, and lets a test pin exactly which draw became which value.
//!
//! # No weak default
//!
//! Every production control plane gets `OsEntropy` (in `microvms-edges`) unless a caller swaps it, including one built over a
//! caller's transport. A plane with a deterministic source would mint repeating client tokens,
//! which is the exact failure TRAP-1 exists to prevent, so the only way to get one is to ask
//! for it by name.

use std::fmt;

use protocol::identity::SEED_BYTES;

use crate::error::{Error, ErrorKind};
use crate::identity::LaunchIdentity;

/// A source of random bytes.
pub trait Entropy: Send + Sync + fmt::Debug {
    /// Fills `buf` with fresh random bytes.
    ///
    /// Fails only when the source is unavailable. A caller refuses the operation then, never
    /// falls back to a clock: a clock-derived nonce risks the collision the nonce exists to
    /// prevent, and a clock-derived credential is guessable.
    fn fill(&self, buf: &mut [u8]) -> Result<(), Error>;

    /// A fresh identity seed.
    fn seed(&self) -> Result<[u8; SEED_BYTES], Error> {
        let mut seed = [0_u8; SEED_BYTES];
        self.fill(&mut seed)?;
        Ok(seed)
    }
}

/// A launch identity from two fresh seeds drawn from `entropy`.
///
/// An all-zero draw isn't reachable from a working pool. It's still refused, because the
/// protocol crate refuses a zero seed (a shared identity proves nothing), and a launch must
/// never hand out material the daemon would reject at bootstrap. The refusal is the source's
/// fault, not the caller's, so it isn't an invalid argument.
pub fn launch_identity(entropy: &dyn Entropy) -> Result<LaunchIdentity, Error> {
    let vm_seed = entropy.seed()?;
    let host_seed = entropy.seed()?;
    LaunchIdentity::from_seeds(vm_seed, host_seed)
        .map_err(|error| Error::new(ErrorKind::Unexpected, error.to_string()))
}

#[cfg(any(test, feature = "test-support"))]
pub(crate) mod testing {
    //! The deterministic source the tests share.

    use std::sync::atomic::{AtomicU64, Ordering};

    use super::*;

    /// Deterministic bytes, distinct per call, so a test can say which draw became which value.
    ///
    /// Call `n` (from 1) fills byte `i` with byte `i % 8` of `n`, little-endian, XORed with
    /// `i`. The first eight bytes encode `n` one-to-one, so two calls never produce the same
    /// fill, and no fill longer than eight bytes is all zero, so a seed drawn from it passes the
    /// protocol's check.
    #[derive(Debug, Default)]
    pub struct SequenceEntropy {
        calls: AtomicU64,
        unavailable: bool,
    }

    impl SequenceEntropy {
        pub fn new() -> Self {
            Self::default()
        }

        /// A source whose every draw fails, the way an unavailable OS pool does.
        pub fn unavailable() -> Self {
            Self {
                unavailable: true,
                ..Self::default()
            }
        }

        /// The bytes call `n` (from 1) fills a buffer of `len` with.
        pub fn draw(n: u64, len: usize) -> Vec<u8> {
            let counter = n.to_le_bytes();
            (0..len)
                .map(|index| counter[index % counter.len()] ^ (index as u8))
                .collect()
        }

        /// How many fills were asked for, failed ones included.
        pub fn calls(&self) -> u64 {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl Entropy for SequenceEntropy {
        fn fill(&self, buf: &mut [u8]) -> Result<(), Error> {
            let n = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
            if self.unavailable {
                return Err(Error::new(
                    ErrorKind::Unexpected,
                    "the scripted random pool is unavailable",
                ));
            }
            buf.copy_from_slice(&Self::draw(n, buf.len()));
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::testing::SequenceEntropy;
    use super::*;

    /// The scripted source is distinct per call and never all zero, which is what lets a
    /// test's seeds pass the protocol's check.
    #[test]
    fn the_sequence_source_is_distinct_per_call_and_never_zero() {
        let entropy = SequenceEntropy::new();
        let seeds: HashSet<[u8; SEED_BYTES]> = (0..300)
            .map(|_| entropy.seed().expect("scripted"))
            .collect();
        assert_eq!(seeds.len(), 300);
        assert!(seeds.iter().all(|seed| seed.iter().any(|byte| *byte != 0)));
    }

    /// Both seeds of a launch identity come from the source, in order: VM seed, then host.
    ///
    /// **Falsification**: draw the seeds from `OsEntropy` inside `launch_identity` and the VM
    /// seed stops being the first scripted draw.
    #[test]
    fn a_launch_identity_is_the_source_s_next_two_seeds() {
        use base64::Engine as _;
        let entropy = SequenceEntropy::new();
        let identity = launch_identity(&entropy).expect("valid seeds");
        let expected =
            base64::engine::general_purpose::STANDARD.encode(SequenceEntropy::draw(1, SEED_BYTES));
        assert_eq!(identity.seed_field(), expected);
        assert_eq!(entropy.calls(), 2);
    }

    /// An unavailable source refuses the identity; it doesn't panic.
    #[test]
    fn an_unavailable_source_refuses_the_identity() {
        let err = launch_identity(&SequenceEntropy::unavailable()).expect_err("no pool");
        assert_eq!(err.kind(), ErrorKind::Unexpected);
    }
}
