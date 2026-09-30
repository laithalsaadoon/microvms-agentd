// SPDX-License-Identifier: Apache-2.0
//! The host's half of the tunnel identity proof: derivation and the pin.
//!
//! `protocol::identity` carries the wire contract and the full design argument. This module
//! is what a *launcher* holds: the two seeds it generated, the payload fields they become,
//! and the pin it stores to recognize its VM later. Drawing the seeds and building the Noise
//! initiator are `microvms_core::prelude`'s, because both need the OS random pool.
//!
//! # Two seeds, and who learns what
//!
//! At launch the host generates two 32-byte x25519 static secrets:
//!
//! * the **VM seed**, delivered to the daemon in the run-hook payload. After the launch the
//!   host needs only its *public* half (the pin), and the secret half shouldn't outlive the
//!   launch call on the host side.
//! * the **host seed**, which never leaves this process's trust domain. Its public half is
//!   delivered to the daemon, which pins it in return. Holding this secret is what
//!   distinguishes the launching host from anyone else with the agent token.

use protocol::identity::{SEED_BYTES, seed_from_bytes};

use crate::error::Error;

/// One launch's identity material, built by [`LaunchIdentity::from_seeds`].
///
/// Holds both secret halves, so it lives exactly as long as the launch needs it: the payload
/// fields are read out of it before the call, and [`LaunchIdentity::keep`] converts it into
/// the durable [`TunnelIdentity`] — which keeps the host secret and the *public* VM pin, and
/// drops the VM secret on the floor where it belongs.
pub struct LaunchIdentity {
    vm_seed: [u8; SEED_BYTES],
    host_seed: [u8; SEED_BYTES],
}

/// Prints nothing but the fact that material exists. Both fields are private keys, and a
/// derived `Debug` would put them into any log line that formats a launch request.
impl std::fmt::Debug for LaunchIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LaunchIdentity(<two seeds>)")
    }
}

impl LaunchIdentity {
    /// The material for `vm_seed` and `host_seed`, validated the way the daemon validates
    /// them at bootstrap.
    ///
    /// The seeds are the caller's: this crate doesn't read the OS random pool (ARCH-6).
    /// `microvms_core::prelude::LaunchIdentityExt::generate` draws fresh ones, which is what
    /// a launch wants. `seed_from_bytes` is the shared validation the daemon runs at
    /// bootstrap; running it here makes "the host accepted what the daemon will refuse"
    /// unrepresentable.
    pub fn from_seeds(
        vm_seed: [u8; SEED_BYTES],
        host_seed: [u8; SEED_BYTES],
    ) -> Result<Self, Error> {
        seed_from_bytes(&vm_seed)
            .and_then(|_| seed_from_bytes(&host_seed))
            .map_err(|error| Error::invalid_arg(format!("an identity seed: {error}")))?;
        Ok(Self { vm_seed, host_seed })
    }

    /// The payload value under [`protocol::identity::SEED_KEY`]: the VM seed, base64.
    pub fn seed_field(&self) -> String {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(self.vm_seed)
    }

    /// The payload value under [`protocol::identity::HOST_PUBLIC_KEY_KEY`]: the host public
    /// key, base64. The *public* half — the secret never appears in any payload.
    pub fn host_public_field(&self) -> String {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(public_of(&self.host_seed))
    }

    /// Converts into what outlives the launch: the host secret and the VM's public pin.
    ///
    /// The VM's *secret* half ends here. After this call the host can verify its VM forever
    /// and impersonate it never, which is the least-privilege shape for a record that will
    /// sit in a ledger file: a stolen record lets an attacker *check* a VM's identity, not
    /// forge one.
    pub fn keep(self) -> TunnelIdentity {
        TunnelIdentity {
            host_seed: self.host_seed,
            vm_public_key: public_of(&self.vm_seed),
        }
    }
}

/// What a launcher keeps to verify its VM later: its own secret, and the VM's public pin.
#[derive(Clone)]
pub struct TunnelIdentity {
    host_seed: [u8; SEED_BYTES],
    vm_public_key: [u8; SEED_BYTES],
}

/// The pin is printable; the host seed is not.
impl std::fmt::Debug for TunnelIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "TunnelIdentity(vm_public_key: {}, host_seed: <secret>)",
            const_hex::encode(self.vm_public_key)
        )
    }
}

impl TunnelIdentity {
    /// Rebuilds from the two values a ledger record carries.
    ///
    /// `host_seed` is validated with the shared rules; the public key only for length,
    /// because a public key has no all-zero hazard — it is derived, not generated, and the
    /// zero check exists to catch a broken RNG at generation time.
    pub fn from_parts(host_seed: &[u8], vm_public_key: &[u8]) -> Result<Self, Error> {
        let host_seed = seed_from_bytes(host_seed)
            .map_err(|error| Error::invalid_arg(format!("the stored host seed: {error}")))?;
        let vm_public_key: [u8; SEED_BYTES] = vm_public_key.try_into().map_err(|_| {
            Error::invalid_arg(format!(
                "the stored VM public key is {} bytes, and an x25519 public key is exactly \
                 {SEED_BYTES}",
                vm_public_key.len()
            ))
        })?;
        Ok(Self {
            host_seed,
            vm_public_key,
        })
    }

    /// The VM's public key — the pin. Safe to store, print, and compare.
    pub fn vm_public_key(&self) -> [u8; SEED_BYTES] {
        self.vm_public_key
    }

    /// The host's secret half, for a ledger record.
    ///
    /// Exposed because the CLI's registry must persist it (0600, beside the agent token it
    /// already holds) for `tunnel --name` to verify in a later process. Callers other than a
    /// persistence layer have no business with it — the initiator below is the way to *use*
    /// the identity.
    pub fn host_seed(&self) -> [u8; SEED_BYTES] {
        self.host_seed
    }

    /// The host secret in its wire spelling — standard base64 — for a registry record.
    ///
    /// Here rather than in each consumer, so the CLI (whose manifest deliberately carries no
    /// encoding crates) and the record it writes cannot disagree with
    /// [`TunnelIdentity::from_encoded_parts`] about alphabet or padding.
    pub fn host_seed_base64(&self) -> String {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(self.host_seed)
    }

    /// The pin in its wire spelling.
    pub fn vm_public_key_base64(&self) -> String {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(self.vm_public_key)
    }

    /// [`TunnelIdentity::from_parts`], from the base64 spellings a record or a flag carries.
    pub fn from_encoded_parts(host_seed: &str, vm_public_key: &str) -> Result<Self, Error> {
        use base64::Engine as _;
        let b64 = base64::engine::general_purpose::STANDARD;
        let host = b64.decode(host_seed).map_err(|_| {
            Error::invalid_arg(
                "the identity host seed is not valid base64. It is the identityHostSeed \
                 value from `run --identity`'s envelope, or the registry record's \
                 identityHostSeed field."
                    .to_string(),
            )
        })?;
        let vm = b64.decode(vm_public_key).map_err(|_| {
            Error::invalid_arg(
                "the identity VM public key is not valid base64. It is the \
                 identityVmPublicKey value from `run --identity`'s envelope."
                    .to_string(),
            )
        })?;
        Self::from_parts(&host, &vm)
    }
}

/// The x25519 public half of a 32-byte static secret.
fn public_of(seed: &[u8; SEED_BYTES]) -> [u8; SEED_BYTES] {
    *x25519_dalek::PublicKey::from(&x25519_dalek::StaticSecret::from(*seed)).as_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn launch(vm_seed: [u8; SEED_BYTES], host_seed: [u8; SEED_BYTES]) -> LaunchIdentity {
        LaunchIdentity::from_seeds(vm_seed, host_seed).expect("valid seeds")
    }

    /// A zero seed is a working x25519 secret and a useless identity, and the daemon refuses
    /// it at bootstrap, so the host refuses it first.
    #[test]
    fn an_all_zero_seed_is_refused_on_either_side() {
        assert!(LaunchIdentity::from_seeds([0_u8; SEED_BYTES], [9_u8; SEED_BYTES]).is_err());
        let error = LaunchIdentity::from_seeds([7_u8; SEED_BYTES], [0_u8; SEED_BYTES])
            .expect_err("a zero host seed is refused");
        assert_eq!(error.kind(), crate::error::ErrorKind::InvalidArg);
    }

    /// `keep` retains exactly the two values that verify and drops the one that impersonates.
    #[test]
    fn keeping_an_identity_drops_the_vm_secret() {
        let vm_seed = [7_u8; SEED_BYTES];
        let host_seed = [9_u8; SEED_BYTES];
        let kept = launch(vm_seed, host_seed).keep();

        assert_eq!(kept.vm_public_key(), public_of(&vm_seed));
        assert_eq!(kept.host_seed(), host_seed);
        // The pin is the *public* key: a record holding the seed itself would let anyone who
        // read the file impersonate the VM to its own launcher.
        assert_ne!(kept.vm_public_key(), vm_seed);
    }

    #[test]
    fn a_kept_identity_round_trips_through_its_stored_parts() {
        let kept = launch([7_u8; 32], [9_u8; 32]).keep();
        let restored = TunnelIdentity::from_parts(&kept.host_seed(), &kept.vm_public_key())
            .expect("what was stored restores");
        assert_eq!(restored.vm_public_key(), kept.vm_public_key());
        assert_eq!(restored.host_seed(), kept.host_seed());
    }

    #[test]
    fn stored_parts_of_the_wrong_shape_are_refused_with_the_length() {
        let error = TunnelIdentity::from_parts(&[1_u8; 16], &[2_u8; 32])
            .expect_err("a short host seed is refused");
        assert!(error.to_string().contains("16"), "{error}");

        let error = TunnelIdentity::from_parts(&[1_u8; 32], &[2_u8; 31])
            .expect_err("a short public key is refused");
        assert!(error.to_string().contains("31"), "{error}");
    }

    /// The debug renderings never contain a secret, in hex or in base64.
    #[test]
    fn debug_prints_the_pin_and_never_a_secret() {
        let launch = launch([0x5A_u8; 32], [0x3C_u8; 32]);
        let rendered = format!("{launch:?}");
        assert!(!rendered.to_lowercase().contains("5a5a"), "{rendered}");
        assert!(!rendered.to_lowercase().contains("3c3c"), "{rendered}");

        let kept = launch.keep();
        let rendered = format!("{kept:?}");
        // The pin is there — it is what a human correlates against `NameRecord` — and the
        // host seed is not.
        assert!(
            rendered.contains(&const_hex::encode(kept.vm_public_key())),
            "{rendered}"
        );
        assert!(!rendered.to_lowercase().contains("3c3c3c"), "{rendered}");
    }
}
