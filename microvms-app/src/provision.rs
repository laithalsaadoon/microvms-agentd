// SPDX-License-Identifier: Apache-2.0
//! How a fetched daemon release is proven (BIND-18): which checks, in what order, and which
//! failures stop the fetch.
//!
//! The policy is [`fetch_release`], written against two ports: [`ReleaseSource`] for the
//! release's files and [`AttestationVerifier`] for the Sigstore check. `microvms-edges`
//! implements both (reqwest against GitHub, `sigstore-verify` with its embedded trusted root)
//! and owns the cache and the file writes around them. Here, the order is the whole point:
//!
//! 1. Download the asset.
//! 2. Fetch an attestation bundle for its digest. When one arrives, it must verify against
//!    [`Signer::release`] for the requested tag, or the fetch stops: **the bytes are discarded
//!    and nothing else is tried.** Falling through to the checksum here would launder bytes
//!    that failed provenance into a weaker check that can't see what was wrong with them.
//! 3. When the release answers that it publishes no bundle for these bytes
//!    ([`Bundles::Absent`]), the fetch stops the same way. Every release that ships
//!    `SHA256SUMS` (v0.5.0 on) also ships its bundle, so a missing one never means an older
//!    release: it means the asset isn't the one the release workflow built. Someone who can
//!    edit a release's assets but can't run its workflow would otherwise delete the bundle,
//!    upload their own asset with a matching `SHA256SUMS`, and pass as `checksum`.
//! 4. Only when no answer can be had at all ([`Bundles::Unreachable`]: a transport failure, a
//!    rate limit, a server error), check the bytes against the release's `SHA256SUMS`. A
//!    release with no `SHA256SUMS` (every tag before v0.5.0) fails closed rather than trusting
//!    TLS alone.
//!
//! That's the table `model/src/provision.rs` checks, with "a bundle arrived, or the release
//! answered that it has none" where it says "`gh` downloaded". [`Verification`] tells the
//! caller which proof it got.

/// The repository whose releases carry the daemon asset.
pub const RELEASE_REPO: &str = "laithalsaadoon/microvms-agentd";

/// The release asset's name: a literal, because the README's `--pattern agentd` and the
/// checksum lookup both match it exactly.
pub const ASSET: &str = "agentd";

/// The release asset listing every other asset's SHA-256, in `sha256sum`'s format.
pub const CHECKSUMS: &str = "SHA256SUMS";

/// The workflow that builds and attests the release, relative to the repository root.
pub const RELEASE_WORKFLOW: &str = ".github/workflows/release.yml";

/// The OIDC issuer of a GitHub Actions workflow identity.
pub const ACTIONS_ISSUER: &str = "https://token.actions.githubusercontent.com";

/// The in-toto predicate type `actions/attest-build-provenance` writes, and the one
/// `gh attestation verify` requires by default.
pub const SLSA_PROVENANCE_V1: &str = "https://slsa.dev/provenance/v1";

/// How a fetched binary's bytes were proven.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verification {
    /// The release workflow's Sigstore attestation for these bytes verified: provenance.
    Attestation,
    /// The release's `SHA256SUMS` entry matched, hashed in-process: integrity against a
    /// corrupted or truncated download, not provenance.
    Checksum,
}

impl Verification {
    pub fn as_str(self) -> &'static str {
        match self {
            Verification::Attestation => "attestation",
            Verification::Checksum => "checksum",
        }
    }
}

/// Who must have signed a release's attestation, and what it must attest.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Signer {
    /// The certificate's subject alternative name, matched exactly.
    pub identity: String,
    /// The certificate's OIDC issuer, matched exactly.
    pub issuer: String,
    /// The in-toto statement's `predicateType`, matched exactly.
    pub predicate_type: String,
}

impl Signer {
    /// This repository's release workflow, run for exactly `tag`.
    ///
    /// Stricter than `gh attestation verify --repo`, which accepts any workflow in the
    /// repository at any ref. Pinning the tag ties the bytes to the version asked for, so an
    /// older release's genuine asset and bundle can't answer a request for a newer one.
    ///
    /// The owner is today's name. Releases v0.5.0 and v0.6.0 were signed before the account
    /// was renamed, under a name GitHub now reports as unregistered, so anyone could claim it
    /// and sign as it. Those two tags are refused here, as `gh attestation verify --repo`
    /// refuses them (measured 2026-09-26).
    pub fn release(tag: &str) -> Self {
        Self {
            identity: format!(
                "https://github.com/{RELEASE_REPO}/{RELEASE_WORKFLOW}@refs/tags/{tag}"
            ),
            issuer: ACTIONS_ISSUER.to_string(),
            predicate_type: SLSA_PROVENANCE_V1.to_string(),
        }
    }
}

/// What a lookup of the attestation bundles for one digest found. Each reason is prose; the
/// policy adds what it means.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Bundles {
    /// The bundles (Sigstore bundle JSON) published for the digest. An empty list is read as
    /// [`Bundles::Absent`].
    Published(Vec<String>),
    /// The release answered, and it publishes no bundle for the digest. This refuses the
    /// bytes, so a source returns it only for an answer the release itself gave (a
    /// "not found"), never for a failure to get one.
    Absent(String),
    /// No answer could be had: a transport failure, a rate limit, a server error. The only
    /// case the checksum is consulted in.
    Unreachable(String),
}

/// Where a release's files come from. The error is prose; the policy adds what it means.
pub trait ReleaseSource {
    /// The bytes of release `tag`'s asset `name`.
    fn asset(&self, tag: &str, name: &str) -> Result<Vec<u8>, String>;

    /// The body of release `tag`'s [`CHECKSUMS`] asset.
    fn checksums(&self, tag: &str) -> Result<String, String>;

    /// Every attestation bundle published for the asset whose SHA-256 is `sha256`, lowercase
    /// hex, on release `tag`, or why there are none.
    fn attestations(&self, tag: &str, sha256: &str) -> Bundles;
}

/// The Sigstore check over one bundle.
pub trait AttestationVerifier {
    /// Whether `bundle` proves `artifact` was attested by `signer`: the certificate chain, the
    /// transparency log evidence, the signature over the artifact's digest, the certificate
    /// identity and issuer, and the predicate type.
    fn verify(&self, bundle: &str, artifact: &[u8], signer: &Signer) -> Result<(), String>;
}

/// The asset's bytes, and how they were proven.
#[derive(Clone, PartialEq, Eq)]
pub struct Fetched {
    pub bytes: Vec<u8>,
    pub verification: Verification,
}

/// Written by hand so a debug print never dumps megabytes of binary.
impl std::fmt::Debug for Fetched {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Fetched")
            .field("bytes", &format_args!("<{} bytes>", self.bytes.len()))
            .field("verification", &self.verification)
            .finish()
    }
}

/// Release `tag`'s [`ASSET`], proven by attestation or, when no answer about its bundle can
/// be had, by checksum. See the module docs for the order and why it's that order.
pub fn fetch_release(
    source: &dyn ReleaseSource,
    verifier: &dyn AttestationVerifier,
    tag: &str,
    progress: &mut dyn FnMut(&str),
) -> Result<Fetched, String> {
    let bytes = source.asset(tag, ASSET).map_err(|reason| {
        format!("could not download {ASSET} {tag} from {RELEASE_REPO}: {reason}")
    })?;
    let digest = sha256_hex(&bytes);

    let unavailable = match source.attestations(tag, &digest) {
        Bundles::Published(bundles) if !bundles.is_empty() => {
            progress(&format!(
                "downloaded {ASSET} {tag}; verifying its attestation"
            ));
            let signer = Signer::release(tag);
            // A digest can carry more than one attestation; one from the release workflow
            // is the proof.
            let mut refusals = Vec::new();
            for bundle in &bundles {
                match verifier.verify(bundle, &bytes, &signer) {
                    Ok(()) => {
                        return Ok(Fetched {
                            bytes,
                            verification: Verification::Attestation,
                        });
                    }
                    Err(reason) => refusals.push(reason),
                }
            }
            drop(bytes);
            return Err(format!(
                "the attestation for {ASSET} {tag} did not verify: {}. The bytes were \
                 discarded; do not retry with verification off.",
                refusals.join("; ")
            ));
        }
        Bundles::Published(_) => {
            return Err(absent(tag, bytes, "the lookup listed no bundles"));
        }
        Bundles::Absent(reason) => return Err(absent(tag, bytes, &reason)),
        Bundles::Unreachable(reason) => reason,
    };

    progress(&format!(
        "downloaded {ASSET} {tag}, but GitHub couldn't be reached for its attestation bundle \
         ({unavailable}); checking {CHECKSUMS}"
    ));
    let sums = source.checksums(tag).map_err(|reason| {
        format!(
            "downloaded {ASSET} {tag}, but could neither fetch an attestation for it \
             ({unavailable}) nor the release's {CHECKSUMS} ({reason}). An unverified download \
             proves nothing about the bytes, so this fails closed. Releases before v0.5.0 ship \
             no {CHECKSUMS}; for those, download and verify manually."
        )
    })?;
    verify_sha256(&sums, ASSET, &bytes)?;
    progress(&format!("{CHECKSUMS} entry for {ASSET} {tag} matched"));
    Ok(Fetched {
        bytes,
        verification: Verification::Checksum,
    })
}

/// The refusal for a download the release publishes no attestation for. It takes the bytes
/// so they're gone before the caller sees the error.
fn absent(tag: &str, bytes: Vec<u8>, reason: &str) -> String {
    drop(bytes);
    format!(
        "release {tag} publishes no attestation for the {ASSET} it served ({reason}). Every \
         release that ships {CHECKSUMS} also publishes its attestation, so {CHECKSUMS} was not \
         consulted and the bytes were discarded: treat this as a replaced asset until shown \
         otherwise. Releases before v0.5.0 carry neither; for those, download and verify \
         manually."
    )
}

/// Checks `bytes` against `asset`'s entry in a `SHA256SUMS` body (`<64 hex>  <name>` per
/// line, sha256sum's own format). In-process, because `sha256sum` the tool doesn't exist on
/// Windows.
pub fn verify_sha256(sums: &str, asset: &str, bytes: &[u8]) -> Result<(), String> {
    let expected = sums
        .lines()
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let digest = parts.next()?;
            let name = parts.next()?;
            // sha256sum marks a binary-mode entry with a leading `*`.
            (name.trim_start_matches('*') == asset).then(|| digest.to_ascii_lowercase())
        })
        .next()
        .ok_or_else(|| {
            format!(
                "the release's SHA256SUMS has no entry for {asset}, so nothing to verify against"
            )
        })?;
    let actual = sha256_hex(bytes);
    if actual == expected {
        return Ok(());
    }
    Err(format!(
        "SHA256 mismatch for {asset}: the release says {expected}, the download hashed to \
         {actual}. The bytes were not installed; retry, and if it repeats, treat the \
         mismatch as the finding rather than the obstacle."
    ))
}

/// The lowercase hex SHA-256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest as _, Sha256};
    const_hex::encode(Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    /// What a scripted release's attestation lookup answers.
    #[derive(Clone, Copy, Debug)]
    enum Bundle {
        /// Neither the release asset nor the API answered.
        Unavailable,
        /// A bundle arrived and verifies.
        Attested,
        /// A bundle arrived and doesn't verify.
        Unattested,
        /// The release answered that it publishes no bundle for the digest.
        Absent,
        /// The lookup answered with an empty list.
        Empty,
    }

    /// A release played from a table, recording which operations ran.
    struct Scripted {
        downloads: bool,
        bundle: Bundle,
        /// `None`: no `SHA256SUMS` on the release; `Some(true)`: matching; `Some(false)`: not.
        sums: Option<bool>,
        asset: Vec<u8>,
        ran: RefCell<Vec<&'static str>>,
    }

    impl Scripted {
        fn new(bundle: Bundle, sums: Option<bool>) -> Self {
            Self {
                downloads: true,
                bundle,
                sums,
                asset: b"\x7fELF release bytes".to_vec(),
                ran: RefCell::new(Vec::new()),
            }
        }

        fn ran(&self) -> Vec<&'static str> {
            self.ran.borrow().clone()
        }
    }

    impl ReleaseSource for Scripted {
        fn asset(&self, _: &str, name: &str) -> Result<Vec<u8>, String> {
            self.ran.borrow_mut().push("asset");
            assert_eq!(name, ASSET);
            if self.downloads {
                Ok(self.asset.clone())
            } else {
                Err("connection refused".into())
            }
        }

        fn checksums(&self, _: &str) -> Result<String, String> {
            self.ran.borrow_mut().push("checksums");
            let digest = match self.sums {
                None => return Err("HTTP 404".into()),
                Some(true) => sha256_hex(&self.asset),
                Some(false) => sha256_hex(b"other bytes"),
            };
            Ok(format!("{digest}  agentd\n"))
        }

        fn attestations(&self, _: &str, sha256: &str) -> Bundles {
            self.ran.borrow_mut().push("attestations");
            assert_eq!(
                sha256,
                sha256_hex(&self.asset),
                "looked up by the asset's digest"
            );
            match self.bundle {
                Bundle::Unavailable => Bundles::Unreachable("HTTP 403: rate limited".into()),
                Bundle::Attested => Bundles::Published(vec!["good".into()]),
                Bundle::Unattested => Bundles::Published(vec!["bad".into()]),
                Bundle::Absent => Bundles::Absent("HTTP 404".into()),
                Bundle::Empty => Bundles::Published(Vec::new()),
            }
        }
    }

    /// Accepts the bundle spelled `good` for the release signer of `v9.9.9`, and nothing else.
    struct Verifier;

    impl AttestationVerifier for Verifier {
        fn verify(&self, bundle: &str, _: &[u8], signer: &Signer) -> Result<(), String> {
            assert_eq!(signer, &Signer::release("v9.9.9"));
            match bundle {
                "good" => Ok(()),
                _ => Err("identity mismatch".into()),
            }
        }
    }

    fn fetch(source: &Scripted) -> Result<Fetched, String> {
        fetch_release(source, &Verifier, "v9.9.9", &mut |_| {})
    }

    /// **An attestation that doesn't verify discards the bytes and stops** (BIND-18): no
    /// bytes come back, and the checksum is never consulted, even though it would match.
    #[test]
    fn an_attestation_failure_discards_the_bytes_and_stops() {
        let source = Scripted::new(Bundle::Unattested, Some(true));
        let refusal = fetch(&source).expect_err("bytes that failed provenance must not be served");
        assert!(refusal.contains("discarded"), "{refusal}");
        assert!(refusal.contains("identity mismatch"), "{refusal}");
        assert_eq!(
            source.ran(),
            ["asset", "attestations"],
            "a refused attestation must not fall through to the checksum"
        );
    }

    /// **A release that answers it has no attestation for the bytes refuses them without the
    /// checksum**, even one that would match: that's what a replaced asset with a rewritten
    /// `SHA256SUMS` and a deleted bundle looks like. An empty list reads the same.
    #[test]
    fn a_release_that_publishes_no_attestation_refuses_without_the_checksum() {
        for bundle in [Bundle::Absent, Bundle::Empty] {
            let source = Scripted::new(bundle, Some(true));
            let refusal = fetch(&source).expect_err("an unattested asset must not be served");
            assert!(refusal.contains("publishes no attestation"), "{refusal}");
            assert!(refusal.contains("discarded"), "{refusal}");
            assert_eq!(
                source.ran(),
                ["asset", "attestations"],
                "{bundle:?}: a missing attestation must not fall through to the checksum"
            );
        }
    }

    /// **A release with no `SHA256SUMS` fails closed** when no bundle could be had.
    #[test]
    fn a_missing_sha256sums_fails_closed() {
        let source = Scripted::new(Bundle::Unavailable, None);
        let refusal = fetch(&source).expect_err("an unverifiable download must refuse");
        assert!(refusal.contains("fails closed"), "{refusal}");
        assert!(refusal.contains("rate limited"), "{refusal}");
        assert!(refusal.contains("HTTP 404"), "{refusal}");
    }

    /// **A checksum mismatch discards the bytes.**
    #[test]
    fn a_checksum_mismatch_discards_the_bytes() {
        let source = Scripted::new(Bundle::Unavailable, Some(false));
        let refusal = fetch(&source).expect_err("a mismatch must refuse");
        assert!(refusal.contains("SHA256 mismatch"), "{refusal}");
        assert!(refusal.contains("not installed"), "{refusal}");
    }

    /// One verifying bundle among several is the proof: the API lists every attestation for
    /// a digest, and another workflow's is no reason to refuse the release workflow's.
    #[test]
    fn one_verifying_bundle_among_several_is_enough() {
        struct Several(Scripted);
        impl ReleaseSource for Several {
            fn asset(&self, tag: &str, name: &str) -> Result<Vec<u8>, String> {
                self.0.asset(tag, name)
            }
            fn checksums(&self, tag: &str) -> Result<String, String> {
                self.0.checksums(tag)
            }
            fn attestations(&self, _: &str, _: &str) -> Bundles {
                Bundles::Published(vec!["bad".into(), "good".into()])
            }
        }
        let source = Several(Scripted::new(Bundle::Attested, None));
        let fetched = fetch_release(&source, &Verifier, "v9.9.9", &mut |_| {}).expect("verifies");
        assert_eq!(fetched.verification, Verification::Attestation);
        assert_eq!(fetched.bytes, source.0.asset);
    }

    /// The signer is the release workflow at exactly the requested tag, from GitHub Actions,
    /// attesting SLSA provenance.
    #[test]
    fn the_signer_is_the_release_workflow_at_the_requested_tag() {
        assert_eq!(
            Signer::release("v0.10.0"),
            Signer {
                identity: "https://github.com/laithalsaadoon/microvms-agentd/.github/workflows/\
                           release.yml@refs/tags/v0.10.0"
                    .into(),
                issuer: "https://token.actions.githubusercontent.com".into(),
                predicate_type: "https://slsa.dev/provenance/v1".into(),
            }
        );
    }

    /// **The verification table**, the literal mirror of `the_verification_table` in
    /// `model/src/provision.rs` (BIND-18), with "a bundle arrived, or the release answered it
    /// has none" in place of "`gh` downloaded": attestation when a bundle verifies, refusal
    /// without the checksum when one arrives and doesn't or the release says there is none
    /// (the model's `Unattested`), and otherwise only a matching `SHA256SUMS` passes. A failed
    /// download refuses whatever the rest would say.
    #[test]
    fn the_verification_table_matches_the_model() {
        // (downloads, bundle, sums) → verdict; `None` is refused.
        type Row = (bool, Bundle, Option<bool>, Option<Verification>);
        let rows: [Row; 16] = [
            (true, Bundle::Absent, Some(true), None),
            (true, Bundle::Absent, None, None),
            (true, Bundle::Empty, Some(true), None),
            (false, Bundle::Absent, Some(true), None),
            (
                true,
                Bundle::Attested,
                Some(true),
                Some(Verification::Attestation),
            ),
            (
                true,
                Bundle::Attested,
                Some(false),
                Some(Verification::Attestation),
            ),
            (
                true,
                Bundle::Attested,
                None,
                Some(Verification::Attestation),
            ),
            (true, Bundle::Unattested, Some(true), None),
            (true, Bundle::Unattested, None, None),
            (
                true,
                Bundle::Unavailable,
                Some(true),
                Some(Verification::Checksum),
            ),
            (true, Bundle::Unavailable, Some(false), None),
            (true, Bundle::Unavailable, None, None),
            (false, Bundle::Attested, Some(true), None),
            (false, Bundle::Unattested, Some(true), None),
            (false, Bundle::Unavailable, Some(true), None),
            (false, Bundle::Unavailable, None, None),
        ];
        for (downloads, bundle, sums, expected) in rows {
            let source = Scripted {
                downloads,
                ..Scripted::new(bundle, sums)
            };
            let fetched = fetch(&source);
            let row = (downloads, bundle, sums);
            assert_eq!(
                fetched.as_ref().ok().map(|fetched| fetched.verification),
                expected,
                "{row:?}: {fetched:?}"
            );
            if let Ok(fetched) = fetched {
                assert_eq!(fetched.bytes, source.asset, "{row:?}");
            }
            if !downloads {
                assert_eq!(source.ran(), ["asset"], "{row:?}: nothing to verify");
            }
            if matches!(bundle, Bundle::Absent | Bundle::Empty | Bundle::Unattested) {
                assert!(!source.ran().contains(&"checksums"), "{row:?}: laundered");
            }
        }
    }

    /// The SHA256SUMS check: a matching entry passes, a mismatch refuses with both digests,
    /// and a missing entry refuses rather than passing vacuously.
    #[test]
    fn the_checksum_verification_matches_mismatches_and_refuses_a_missing_entry() {
        let digest = sha256_hex(b"agentd-bytes");
        let sums = format!("{digest}  agentd\nother  microvm-x86_64.tar.gz\n");
        assert!(verify_sha256(&sums, "agentd", b"agentd-bytes").is_ok());
        let starred = format!("{digest} *agentd\n");
        assert!(verify_sha256(&starred, "agentd", b"agentd-bytes").is_ok());

        let mismatch = verify_sha256(&sums, "agentd", b"tampered").expect_err("must refuse");
        assert!(mismatch.contains(&digest), "{mismatch}");
        assert!(mismatch.contains("not installed"), "{mismatch}");

        let missing =
            verify_sha256("abc  something-else\n", "agentd", b"x").expect_err("must refuse");
        assert!(missing.contains("no entry"), "{missing}");
    }
}
