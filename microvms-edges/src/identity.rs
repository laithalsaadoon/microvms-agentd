// SPDX-License-Identifier: Apache-2.0
//! The host's Noise initiator for the tunnel identity proof.
//!
//! The identity, its seeds and its pin are `microvms_domain::identity`, and the design argument
//! is `protocol::identity`'s. The initiator is here because building one draws an ephemeral key
//! from the OS random pool. `microvms_core::prelude::TunnelIdentityExt::initiator` is this under
//! the method name it had.

use microvms_app::error::{Error, ErrorKind};
use microvms_app::identity::TunnelIdentity;

/// The handshake state [`initiator`] returns, named here so a caller needn't depend on `snow`.
pub use snow::HandshakeState;

/// A Noise initiator bound to `identity`: proves the host, verifies the pin.
///
/// Built per connection. A `HandshakeState` carries nonces and must never be reused.
pub fn initiator(identity: &TunnelIdentity) -> Result<HandshakeState, Error> {
    // The builder borrows both keys until `build_initiator`, so they need a home that
    // outlives the chain.
    let (host_seed, vm_public_key) = (identity.host_seed(), identity.vm_public_key());
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
