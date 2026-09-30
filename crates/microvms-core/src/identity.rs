// SPDX-License-Identifier: Apache-2.0
//! The host's half of the tunnel identity proof: seed generation, derivation, and the pin.
//!
//! `protocol::identity` carries the wire contract and the full design argument (why Noise KK
//! rather than rustls — a measured cross-compile constraint on the daemon's shipping target —
//! and the honest ptrace limit). This module is what a *launcher* holds: the two seeds it
//! generated, the payload fields they become, and the pin it stores to recognise its VM later.
//!
//! # Two seeds, and who learns what
//!
//! At launch the host generates two 32-byte x25519 static secrets:
//!
//! * the **VM seed**, delivered to the daemon in the run-hook payload. After the launch the
//!   host needs only its *public* half — the pin — and the secret half should not outlive the
//!   launch call on the host side.
//! * the **host seed**, which never leaves this process's trust domain. Its public half is
//!   delivered to the daemon, which pins it in return. Holding this secret is what
//!   distinguishes the launching host from anyone else with the agent token.
//!
//! Mutual authentication falls out: the client proves it holds the host secret, the daemon
//! proves it holds the VM seed, and each side pinned the other's public half before the first
//! byte moved.

pub use microvms_domain::identity::*;

// Seed generation and the Noise initiator draw from the OS random pool, so they're
// `crate::prelude::LaunchIdentityExt` and `TunnelIdentityExt`; the rest is the domain's.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prelude::*;

    #[test]
    fn generated_material_is_fresh_and_valid() {
        let first = LaunchIdentity::generate().expect("the pool is available");
        let second = LaunchIdentity::generate().expect("the pool is available");
        // Distinct across calls — the property a seeded identity exists to provide.
        assert_ne!(first.seed_field(), second.seed_field());
        assert_ne!(first.host_public_field(), second.host_public_field());
        // And the fields are the size the payload budget accounts for: 44 chars of base64.
        assert_eq!(first.seed_field().len(), 44);
        assert_eq!(first.host_public_field().len(), 44);
    }

    /// The client's derivation agrees with the daemon's, end to end in one process.
    ///
    /// The two sides derive independently — `microvms-domain` with `x25519-dalek` directly, the
    /// daemon through the same crate but its own code path — and this is the assertion that
    /// the payload fields this module emits produce a daemon whose handshake this module's
    /// initiator completes.
    #[test]
    fn the_initiator_completes_against_a_responder_built_from_the_payload_fields() {
        use base64::Engine as _;
        let b64 = base64::engine::general_purpose::STANDARD;

        let launch = LaunchIdentity::from_seeds([7_u8; 32], [9_u8; 32]).expect("valid seeds");
        // What the daemon receives: the two payload fields, decoded its way.
        let vm_seed_bytes: [u8; 32] = b64
            .decode(launch.seed_field())
            .expect("valid base64")
            .try_into()
            .expect("32 bytes");
        let host_public_bytes: [u8; 32] = b64
            .decode(launch.host_public_field())
            .expect("valid base64")
            .try_into()
            .expect("32 bytes");

        let mut responder =
            snow::Builder::new(protocol::identity::NOISE_PATTERN.parse().expect("parses"))
                .local_private_key(&vm_seed_bytes)
                .expect("a 32-byte secret")
                .remote_public_key(&host_public_bytes)
                .expect("a 32-byte key")
                .build_responder()
                .expect("builds");

        let kept = launch.keep();
        let mut initiator = kept.initiator().expect("builds");

        let mut buffer = [0_u8; 1024];
        let mut scratch = [0_u8; 1024];
        let written = initiator.write_message(&[], &mut buffer).expect("writes");
        responder
            .read_message(&buffer[..written], &mut scratch)
            .expect("the daemon accepts the launching host");
        let written = responder.write_message(&[], &mut buffer).expect("writes");
        initiator
            .read_message(&buffer[..written], &mut scratch)
            .expect("the host accepts its own VM");

        // Both reach transport mode: the handshake genuinely completed.
        let mut host_transport = initiator.into_transport_mode().expect("transport");
        let mut vm_transport = responder.into_transport_mode().expect("transport");
        let sent = host_transport
            .write_message(b"ping", &mut buffer)
            .expect("encrypts");
        let received = vm_transport
            .read_message(&buffer[..sent], &mut scratch)
            .expect("decrypts");
        assert_eq!(&scratch[..received], b"ping");
    }
}
