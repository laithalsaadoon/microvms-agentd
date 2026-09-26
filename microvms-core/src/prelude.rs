// SPDX-License-Identifier: Apache-2.0
//! The I/O that `microvms-domain`'s types leave out, under the method names they had.
//!
//! The domain can't read the clock or the OS random pool (ARCH-6), so the methods that did
//! moved here as extension traits. With `use microvms_core::prelude::*;` in scope, a call
//! written against 0.10, such as `CalendarDate::today_utc()`, compiles unchanged: a path to a
//! type's associated function also finds the methods of traits in scope.

use protocol::identity::SEED_BYTES;

use crate::cost::CalendarDate;
use crate::error::{Error, ErrorKind};
use crate::identity::{LaunchIdentity, TunnelIdentity};
use crate::names::NameRecord;

/// [`CalendarDate`]'s clock read.
pub trait CalendarDateExt {
    /// Today, UTC, from the system clock.
    ///
    /// Falls back to the epoch if the clock is set before 1970 or after 9999, which is a
    /// machine whose age arithmetic is already meaningless. That's what 0.10 did, and the
    /// signature can't grow an error without breaking the callers the prelude exists for.
    fn today_utc() -> CalendarDate;
}

impl CalendarDateExt for CalendarDate {
    fn today_utc() -> CalendarDate {
        let seconds = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |since| since.as_secs());
        CalendarDate::from_unix_secs(seconds).unwrap_or(CalendarDate::from_ymd(1970, 1, 1))
    }
}

/// [`NameRecord`]'s timestamp.
pub trait NameRecordExt: Sized {
    /// A record for a VM this process can already address, stamped with the current time.
    ///
    /// Refuses an illegal name or an empty id, endpoint, or token: a record missing any of
    /// them resolves to a VM nobody can adopt.
    fn new(
        name: impl Into<String>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        region: impl Into<String>,
    ) -> Result<Self, Error>;
}

impl NameRecordExt for NameRecord {
    fn new(
        name: impl Into<String>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        region: impl Into<String>,
    ) -> Result<Self, Error> {
        let at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|since| since.as_secs())
            .unwrap_or(0);
        NameRecord::new_at(name, microvm_id, endpoint, agent_token, region, at)
    }
}

/// [`LaunchIdentity`]'s seeds, from the OS random pool.
pub trait LaunchIdentityExt: Sized {
    /// Fresh material from the OS random pool.
    ///
    /// An all-zero draw isn't reachable from a working pool. It's still refused, because
    /// the protocol crate refuses a zero seed (a shared identity proves nothing), and this
    /// constructor must never hand out material the daemon would reject at bootstrap.
    fn generate() -> Result<Self, Error>;
}

impl LaunchIdentityExt for LaunchIdentity {
    fn generate() -> Result<Self, Error> {
        let mut vm_seed = [0_u8; SEED_BYTES];
        let mut host_seed = [0_u8; SEED_BYTES];
        getrandom::fill(&mut vm_seed)
            .and_then(|()| getrandom::fill(&mut host_seed))
            .map_err(|error| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("the OS random pool is unavailable: {error}"),
                )
            })?;
        // A refusal here is the pool's fault, not the caller's, so it isn't an invalid
        // argument.
        LaunchIdentity::from_seeds(vm_seed, host_seed)
            .map_err(|error| Error::new(ErrorKind::Unexpected, error.to_string()))
    }
}

/// [`TunnelIdentity`]'s Noise initiator, which draws its ephemeral key from the OS pool.
pub trait TunnelIdentityExt {
    /// A Noise initiator bound to this identity: proves the host, verifies the pin.
    ///
    /// Built per connection. A `HandshakeState` carries nonces and must never be reused.
    fn initiator(&self) -> Result<snow::HandshakeState, Error>;
}

impl TunnelIdentityExt for TunnelIdentity {
    fn initiator(&self) -> Result<snow::HandshakeState, Error> {
        // The builder borrows both keys until `build_initiator`, so they need a home that
        // outlives the chain.
        let (host_seed, vm_public_key) = (self.host_seed(), self.vm_public_key());
        snow::Builder::new(
            protocol::identity::NOISE_PATTERN
                .parse()
                .expect("the pattern is a compile-time constant the protocol crate tests"),
        )
        .local_private_key(&host_seed)
        .and_then(|builder| builder.remote_public_key(&vm_public_key))
        .and_then(|builder| builder.build_initiator())
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not build the identity initiator: {error}"),
            )
        })
    }
}
