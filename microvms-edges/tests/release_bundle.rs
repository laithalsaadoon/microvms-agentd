// SPDX-License-Identifier: Apache-2.0
//! The release workflow's gate (#284): a release's bundle verifies under the Sigstore trusted
//! root this tree embeds, checked before the release is published.
//!
//! `SigstoreVerifier`'s root is fixed when `sigstore-trust-root` is pinned, and a client
//! fetches exactly its own version's daemon. So a bundle logged to a Rekor shard, or signed
//! under a key, that the pinned root predates would make every client of that version refuse
//! its own daemon as if it had been tampered with. `.github/workflows/release.yml` runs this
//! test over the asset and bundle it just produced, with the three variables below set, so
//! that release never publishes. A failure there means bumping the pin, never skipping the
//! step.
//!
//! With none of the variables set it checks the committed v0.7.0 release, which keeps the
//! workflow's path compiled and passing on every `cargo test`.

use microvms_edges::provision::{AttestationVerifier, Signer, SigstoreVerifier};

/// The asset to check, as a path.
const ASSET: &str = "MICROVM_RELEASE_ASSET";
/// Its Sigstore bundle, as a path.
const BUNDLE: &str = "MICROVM_RELEASE_BUNDLE";
/// The tag the release workflow ran for, which the signer identity must name.
const TAG: &str = "MICROVM_RELEASE_TAG";

/// The tag, the asset's bytes and the bundle: from the variables when they're set, the
/// committed release when none is.
fn release() -> (String, Vec<u8>, String) {
    let set = [ASSET, BUNDLE, TAG].map(|name| std::env::var_os(name).map(|value| (name, value)));
    if set.iter().all(Option::is_none) {
        return committed();
    }
    let [Some((_, asset)), Some((_, bundle)), Some((_, tag))] = set else {
        panic!("set all of {ASSET}, {BUNDLE} and {TAG}, or none of them");
    };
    let read = |path: &std::ffi::OsStr| {
        std::fs::read(path).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
    };
    let bundle = String::from_utf8(read(&bundle)).expect("the bundle is UTF-8");
    let tag = tag.into_string().expect("the tag is UTF-8");
    (tag, read(&asset), bundle)
}

/// The committed v0.7.0 release that `src/provision/release.rs`'s tests verify too.
fn committed() -> (String, Vec<u8>, String) {
    use std::io::Read as _;
    let packed = include_bytes!("fixtures/release-v0.7.0/agentd.gz");
    let mut asset = Vec::new();
    flate2::read::GzDecoder::new(&packed[..])
        .read_to_end(&mut asset)
        .expect("the fixture is gzip");
    let bundle = include_str!("fixtures/release-v0.7.0/agentd.sigstore.json");
    ("v0.7.0".to_string(), asset, bundle.to_string())
}

/// **The release's bundle verifies as the CLI built from this tree will verify it.**
#[test]
fn the_release_bundle_verifies_under_the_embedded_trusted_root() {
    let (tag, asset, bundle) = release();
    let verifier = SigstoreVerifier::public_good().expect("the embedded trusted root parses");
    if let Err(refusal) = verifier.verify(&bundle, &asset, &Signer::release(&tag)) {
        panic!(
            "the {tag} bundle doesn't verify under the embedded Sigstore trusted root: \
             {refusal}. If the refusal names a log or key the root doesn't know, bump the \
             sigstore-verify, sigstore-trust-root and sigstore-types pins in \
             microvms-edges/Cargo.toml and tag again. Don't publish this release: every \
             client of this version would refuse its daemon."
        );
    }
}
