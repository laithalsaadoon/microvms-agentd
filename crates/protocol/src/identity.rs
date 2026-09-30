// SPDX-License-Identifier: Apache-2.0
//! The tunnel's identity proof: the seed's wire contract, the Noise pattern, and the pin.
//!
//! Here rather than on either side for ARCH-2's reason, and the stakes are higher than for
//! the close codes: the host derives a public key from a seed and the daemon derives what
//! must be the *same* public key from the *same* seed. A derivation spelled twice is a
//! derivation that can disagree, and a disagreement surfaces as a handshake that fails with
//! no way to tell a wrong pin from a wrong derivation.
//!
//! Following this crate's own rule (see the crate docs), the base64 *encoding* is not here:
//! the seed travels as a `String` field and each side encodes with the shared `base64` crate,
//! exactly as the exec payloads do. What is here is the part that must agree — the key names,
//! the length, the pattern, and which seeds are refused.
//!
//! # What this proves, and what platform-layer mTLS cannot
//!
//! TLS terminates at the AWS-managed endpoint proxy and the service model has no
//! client-certificate parameter anywhere: `CreateMicrovmAuthToken` takes
//! `microvmIdentifier`, `allowedPorts`, and `expirationInMinutes`, and nothing else. So there
//! is no way to present a certificate to the *VM* through the platform, and endpoint auth is
//! the JWE alone.
//!
//! The JWE is scoped to one `microvmId` and the proxy enforces that scope, so an
//! authenticated connection already binds to VM identity **as attested by AWS**. A caller who
//! trusts the proxy needs nothing here. This module is for the caller who wants that binding
//! without trusting the proxy, and it works because we control both endpoints: the proof
//! terminates in the daemon rather than at the proxy.
//!
//! # Noise KK rather than TLS, on a measured constraint
//!
//! Issue #70's layer 3 called for rustls with a per-VM certificate pinned by SPKI. That is
//! not available to the daemon, and the reason is the shipping target rather than a
//! preference. `agentd` ships as a static `aarch64-unknown-linux-musl` binary, and **both**
//! rustls crypto providers compile C: `aws-lc-sys` and `ring` each drive `cc-rs`, which looks
//! for `aarch64-linux-musl-gcc`. Measured 2026-08-29 on the devbox, and `.github/workflows/ci.yml`
//! installs `gcc-aarch64-linux-gnu` — a *gnu* cross compiler, not the musl one `cc-rs` asks
//! for. Both providers fail the daemon's release build with `error occurred in cc-rs: failed
//! to find tool "aarch64-linux-musl-gcc"`, so putting rustls in the daemon would trade a
//! working shipping artifact for an identity feature.
//!
//! Noise KK gives the same three properties with no PKI and no C:
//!
//! * **Mutual authentication.** `KK` means both statics are known to both sides before the
//!   handshake, which is exactly the situation here — the host generated the seed, so it
//!   knows the daemon's key, and the daemon is told the host's key in the same payload. Each
//!   side proves possession of its own static, so the daemon learns the peer is the launching
//!   host rather than another token holder.
//! * **Pinning by construction.** There is no name, no CA, and no chain to get wrong: the
//!   remote static key *is* the pin. A wrong pin cannot be "accepted anyway" by a verifier
//!   that forgot a check, because the key is mixed into the handshake hash and a mismatch
//!   fails decryption. That is a stronger position than a custom certificate verifier, which
//!   is code that can be written wrong.
//! * **Confidentiality past the proxy.** ChaCha20-Poly1305 over the whole stream, so the
//!   bytes the relay carries are opaque to the endpoint that carries them.
//!
//! `snow` is the crate, at 26M downloads the most-used Noise implementation in the ecosystem,
//! and it cross-compiles to the shipping target with **no C compiler** — measured, with
//! `default-features = false`, which is load-bearing: snow's `std` feature reads `ring/std`
//! rather than `ring?/std`, so enabling it force-enables the very C dependency the daemon
//! cannot build.
//!
//! # The end of stream (#342)
//!
//! Every relayed chunk rides one Noise transport message, and so does the end of a stream: a
//! side that has sent its last byte sends one more message with an empty plaintext. A relayed
//! chunk is never empty, so the empty message is unambiguous, and it authenticates under the
//! session's keys like any other. It also counts: each transport message decrypts only at its
//! own position (snow's nonce is a counter), so the end of stream proves every message before
//! it arrived, in order, with none added. That's why it carries no byte counts.
//!
//! The WebSocket close frame that follows it stays plaintext, since it has to reach a caller
//! whose handshake failed. After the handshake it's a diagnosis and never the verdict: anything
//! on the path can send a close frame or drop the connection, and only the end of stream says a
//! stream finished rather than being cut short.
//!
//! Each side offers the end of stream in its handshake message's payload
//! ([`HANDSHAKE_PAYLOAD`]), and a side holds its peer to it only when the peer offered it. The
//! payloads are inside the handshake's encryption and its hash, so an on-path party can't strip
//! the offer the way it could strip a query flag. An older peer sends an empty payload and no
//! end of stream, and it reads the newer peer's end of stream as a message with nothing in it,
//! so a release that predates the end of stream and one that sends it still work together in
//! either order. `docs/PROTOCOL.md` has what each side does without one.
//!
//! # The honest limit, restated here because this is where a reader will look
//!
//! This proves the far end is **`agentd` in the VM the caller launched**. It does not prove
//! the VM is uncompromised: the workload runs as uid 0 and can in principle `ptrace` the
//! daemon and read the derived key out of its memory. Same class as the other entries on
//! `docs/TRUST.md`'s unenforced list. It is documented rather than overclaimed.

/// The Noise handshake pattern, spelled once for both sides.
///
/// `KK` because both static keys are known in advance, which is the whole shape of this
/// problem: the host *generated* the daemon's key, so pre-knowledge is not an assumption to
/// arrange but the starting condition. The alternatives are worse fits — `XX` transmits the
/// statics during the handshake, which is what you want when the peers have never met, and it
/// would let a responder present *any* key and be believed until a later check caught it.
/// Under `KK` a wrong key fails the handshake itself.
///
/// `25519_ChaChaPoly_BLAKE2s`: all three have pure-Rust implementations in snow's default
/// resolver, which is what keeps the daemon's build free of a C compiler. AES-GCM would pull
/// that constraint back in on some targets for no security gain here.
pub const NOISE_PATTERN: &str = "Noise_KK_25519_ChaChaPoly_BLAKE2s";

/// The length of a seed, a static secret, and a public key. All 32 bytes, on x25519.
///
/// One constant rather than three, because they are the same number for the same reason: an
/// x25519 static secret *is* 32 bytes of seed, so the derivation is a type change rather than
/// a KDF. That is why this design needs no HKDF step and no rejection loop — the thing the
/// platform can carry is already the thing the handshake needs.
pub const SEED_BYTES: usize = 32;

/// The payload key carrying the VM's own seed.
///
/// Named beside the pattern so a reader finds the whole wire contract in one file, and
/// spelled as a constant because it appears in the host's composition and the daemon's parse
/// — a literal in each place is a typo that fails as "the VM has no identity" rather than as
/// a compile error.
pub const SEED_KEY: &str = "identity_seed";

/// The payload key carrying the host's public key, which the daemon pins in return.
///
/// The other half of mutual authentication. Without it the daemon would accept a handshake
/// from anyone holding the agent token, and the token is a bearer credential the proxy
/// transports on every request — so "knows the token" is a weaker claim than "holds the
/// launching host's private key".
pub const HOST_PUBLIC_KEY_KEY: &str = "identity_host_public_key";

/// How many bytes the identity material adds to the run-hook payload.
///
/// Two 32-byte values, each base64'd to 44 characters, plus the JSON keys, quotes, colons,
/// and commas. Stated as a constant because the payload shares one measured 4096-byte ceiling
/// with the agent token and the launch environment, and a caller near that ceiling needs the
/// number rather than a warning. `microvms-core` asserts the real composition against it.
pub const IDENTITY_PAYLOAD_BYTES: usize = 137;

/// The handshake payload's flag for the end of stream: the sender sends one, and checks the
/// peer's when the peer offered it too.
pub const END_OF_STREAM: u8 = 0b0000_0001;

/// The payload each side writes into its handshake message: the flags it offers.
///
/// One byte of flags, so a later release can offer something more in another bit. The
/// initiator's first message and the responder's reply both carry it, and both are encrypted
/// under keys only the two pinned statics can derive, so the offer can't be forged or stripped.
pub const HANDSHAKE_PAYLOAD: [u8; 1] = [END_OF_STREAM];

/// Whether a peer's handshake payload offers the end of stream.
///
/// An empty payload is a peer that predates it. A flag or a byte this release doesn't know is
/// ignored rather than refused, so a later release's payload still reads here.
pub fn offers_end_of_stream(payload: &[u8]) -> bool {
    payload
        .first()
        .is_some_and(|flags| flags & END_OF_STREAM != 0)
}

/// Whether a transport message's plaintext is the end of stream.
///
/// The one message with nothing in it: neither side relays an empty read, so an empty
/// plaintext can't be a chunk of the stream.
pub fn is_end_of_stream(plaintext: &[u8]) -> bool {
    plaintext.is_empty()
}

/// Why some bytes are not a usable identity seed.
///
/// Named variants rather than a string for the same reason [`super::hook::RunHookError`] has
/// them: this material is secret, and a message that quoted what it rejected would put key
/// material into a log line and a response body.
#[derive(Debug, Eq, PartialEq)]
pub enum SeedError {
    /// Not exactly [`SEED_BYTES`] bytes. Carries what was found, because a truncated payload
    /// and a padded one are different bugs on the composition side.
    WrongLength(usize),
    /// Every byte is zero. A caller that produced it has a broken random source, and
    /// accepting it would install an identity every other broken caller shares.
    AllZero,
}

impl std::fmt::Display for SeedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SeedError::WrongLength(found) => write!(
                f,
                "the identity seed is {found} bytes, and an x25519 static secret is exactly \
                 {SEED_BYTES}. A seed of another length is a different derivation, not a \
                 shorter one. It travels as standard base64 beside the agent token in the \
                 run-hook payload."
            ),
            SeedError::AllZero => f.write_str(
                "the identity seed is all zero bytes, which every caller with a broken random \
                 source would produce identically. Refused rather than installed: a shared \
                 identity proves nothing about which VM answered.",
            ),
        }
    }
}

impl std::error::Error for SeedError {}

/// Checks decoded bytes into a seed, refusing the two shapes that are not one.
///
/// Takes bytes rather than a base64 string on purpose: this crate carries no encoder (crate
/// docs), so each side decodes with the shared `base64` crate and hands the result here. That
/// keeps one *validation* for both sides, which is the half that has to agree — a host that
/// accepted a seed the daemon refuses would report a launch as identity-capable and then fail
/// every handshake against it.
pub fn seed_from_bytes(bytes: &[u8]) -> Result<[u8; SEED_BYTES], SeedError> {
    if bytes.len() != SEED_BYTES {
        return Err(SeedError::WrongLength(bytes.len()));
    }
    // A zero seed is a working x25519 secret and a useless identity, so it is refused here
    // rather than at the handshake, where the failure would read as a wrong pin.
    if bytes.iter().all(|byte| *byte == 0) {
        return Err(SeedError::AllZero);
    }
    let mut seed = [0_u8; SEED_BYTES];
    seed.copy_from_slice(bytes);
    Ok(seed)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The pattern is `KK` and nothing else, because the choice *is* the security property.
    ///
    /// `XX` would transmit the statics during the handshake, which is the pattern for peers
    /// that have never met — and it would let a responder present any key and be believed
    /// until a later check caught it. An edit to this string is a change of threat model, so
    /// it is pinned rather than merely used.
    #[test]
    fn the_pattern_is_mutually_authenticated_and_pure_rust() {
        assert_eq!(NOISE_PATTERN, "Noise_KK_25519_ChaChaPoly_BLAKE2s");
        assert!(
            NOISE_PATTERN.starts_with("Noise_KK_"),
            "only KK pre-knows both statics, which is what makes the pin unforgeable"
        );
        // ChaChaPoly and BLAKE2s both have pure-Rust implementations in snow's default
        // resolver. That is what keeps the daemon's aarch64-musl build free of a C compiler,
        // which is the constraint that ruled rustls out in the first place.
        assert!(NOISE_PATTERN.contains("ChaChaPoly"), "{NOISE_PATTERN}");
        assert!(NOISE_PATTERN.contains("BLAKE2s"), "{NOISE_PATTERN}");
    }

    #[test]
    fn a_seed_of_the_right_length_is_accepted_byte_for_byte() {
        let mut ascending = [0_u8; SEED_BYTES];
        for (index, byte) in ascending.iter_mut().enumerate() {
            *byte = index as u8;
        }
        assert_eq!(seed_from_bytes(&ascending).expect("accepted"), ascending);
    }

    /// A seed one byte short and one byte long are both refused, and the message says which.
    ///
    /// The pair matters because a truncated payload and a padded one are different bugs on the
    /// composition side, and a single "bad seed" message would send a reader to the wrong one.
    #[test]
    fn a_seed_of_the_wrong_length_reports_the_length_it_found() {
        assert_eq!(
            seed_from_bytes(&[7_u8; 31]),
            Err(SeedError::WrongLength(31))
        );
        assert_eq!(
            seed_from_bytes(&[7_u8; 33]),
            Err(SeedError::WrongLength(33))
        );
        assert_eq!(seed_from_bytes(&[]), Err(SeedError::WrongLength(0)));
        assert!(
            SeedError::WrongLength(31).to_string().contains("31"),
            "the found length is the diagnosis"
        );
    }

    /// A zero seed decodes fine and is refused anyway, because it is a shared identity.
    #[test]
    fn an_all_zero_seed_is_refused() {
        assert_eq!(
            seed_from_bytes(&[0_u8; SEED_BYTES]),
            Err(SeedError::AllZero)
        );
        // One non-zero byte is enough to be a real seed: the refusal is about a broken RNG
        // producing an all-zero buffer, not about weak-looking keys in general.
        let mut nearly = [0_u8; SEED_BYTES];
        nearly[SEED_BYTES - 1] = 1;
        assert!(seed_from_bytes(&nearly).is_ok());
    }

    /// No refusal may carry the material it rejected.
    ///
    /// The payload this validates sits beside the agent token, and `docs/TRUST.md` promises
    /// no secret reaches a log line or a response body.
    #[test]
    fn a_refusal_never_quotes_the_bytes_it_rejected() {
        let secret = [0xAB_u8; 31];
        let message = seed_from_bytes(&secret).expect_err("refused").to_string();
        assert!(!message.contains("171"), "{message}");
        assert!(!message.contains("ab"), "{message}");
        assert!(!message.contains("AB"), "{message}");
    }

    /// The two payload keys are distinct, and neither collides with the payload's other keys.
    ///
    /// A collision would make one value overwrite the other in the payload object, and the
    /// symptom would be a failed handshake rather than anything naming a key.
    #[test]
    fn the_payload_keys_are_distinct_from_each_other_and_from_the_token() {
        assert_ne!(SEED_KEY, HOST_PUBLIC_KEY_KEY);
        for key in [SEED_KEY, HOST_PUBLIC_KEY_KEY] {
            assert_ne!(key, "agent_token");
            assert_ne!(key, "env");
        }
    }

    /// The offer reads from the flags byte alone, and an older peer's empty payload offers
    /// nothing.
    ///
    /// The empty payload is the skew case: every release before #342 wrote one, and reading it
    /// as an offer would hold that peer to an end of stream it never sends, failing every
    /// tunnel to an older image. A later release's extra flag or byte must not turn the offer
    /// off either.
    #[test]
    fn the_end_of_stream_is_offered_by_its_flag_and_not_by_an_empty_payload() {
        assert!(offers_end_of_stream(&HANDSHAKE_PAYLOAD));
        assert!(!offers_end_of_stream(&[]));
        assert!(!offers_end_of_stream(&[0b0000_0010]));
        assert!(offers_end_of_stream(&[END_OF_STREAM | 0b1000_0000, 0xff]));
        assert!(is_end_of_stream(&[]));
        assert!(!is_end_of_stream(&[0]));
    }

    /// The identity payload budget is what a caller near the ceiling needs, so it is asserted
    /// against the real composition rather than trusted as a comment.
    ///
    /// The number is checked here against its two 44-character base64 values and JSON framing;
    /// `microvms-core` asserts the same constant against the payload it actually builds. Two
    /// independent checks of one number, because the ceiling it is spent against is measured
    /// and hard — 4096 bytes, where 4097 fails.
    #[test]
    fn the_identity_payload_budget_matches_its_parts() {
        // Two keys, two quoted 44-char base64 values, two colons, two commas, four quote pairs.
        let value_chars = 44 * 2;
        let key_chars = SEED_KEY.len() + HOST_PUBLIC_KEY_KEY.len();
        // Per pair: quotes around the key (2), a colon (1), quotes around the value (2), and a
        // leading comma (1) — six characters of framing each.
        let framing = 6 * 2;
        assert_eq!(
            IDENTITY_PAYLOAD_BYTES,
            value_chars + key_chars + framing,
            "the stated budget must equal the bytes the two halves really cost"
        );
    }
}
