// SPDX-License-Identifier: Apache-2.0
//! The daemon release fetch against the real GitHub release (BIND-18, #284). Free: it reads
//! a public release and touches no AWS account.
//!
//! Every other provisioning test answers the fetch from memory or from the committed v0.7.0
//! fixture, so nothing local proves today's release still publishes a bundle this verifier
//! accepts. Run it by hand on a machine where `gh` can't help, which is the case #284 is for:
//!
//! ```sh
//! env -u GITHUB_TOKEN -u GH_TOKEN GH_CONFIG_DIR="$(mktemp -d)" \
//!     cargo test -p microvms-core --test live_release -- --ignored --nocapture
//! ```
//!
//! or `mise run live:release`. `MICROVM_RELEASE_VERSION` picks another release; the default
//! is the core's own version, which is the one a client fetches.

use microvms_core::provision::{
    self, ASSET, Bundles, GitHubRelease, HttpsFetch, ReleaseSource, Request, Source, Verification,
};

/// **The core's own release verifies by attestation, with no token and no `gh`.** The
/// bytes also match the release's `SHA256SUMS`, read separately, so the two proofs agree.
#[test]
#[ignore = "needs network: downloads the public GitHub release; no AWS and no credentials"]
fn the_real_release_is_fetched_and_verified_by_attestation() {
    let version = std::env::var("MICROVM_RELEASE_VERSION")
        .unwrap_or_else(|_| microvms_core::VERSION.to_string());
    let state = tempfile::tempdir().expect("a temp dir");
    let request = Request {
        version: Some(&version),
        state_dir: Some(state.path()),
        binary: None,
    };
    // No environment at all: no `$MICROVM_AGENTD` and no `GITHUB_TOKEN`.
    let no_env = |_: &str| None;
    let provisioned = provision::resolve(
        &request,
        &no_env,
        &HttpsFetch::from_env(&no_env),
        &mut |line| eprintln!("PROVISION {line}"),
    )
    .expect("the release fetches and verifies");
    eprintln!(
        "PROVISION source={} verified={:?} sha256={} bytes={}",
        provisioned.source.as_str(),
        provisioned.verification().map(Verification::as_str),
        provisioned.sha256,
        provisioned.bytes.len()
    );
    assert_eq!(
        provisioned.source,
        Source::Fetched(Verification::Attestation)
    );
    assert_eq!(
        provision::elf_machine(&provisioned.bytes),
        Some(provision::REQUIRED_ELF_MACHINE)
    );

    let tag = format!("v{}", version.trim_start_matches('v'));
    let sums = GitHubRelease::new(None)
        .checksums(&tag)
        .expect("the release publishes SHA256SUMS");
    provision::verify_sha256(&sums, ASSET, &provisioned.bytes)
        .expect("the attested bytes match the release's SHA256SUMS");
}

/// **The attestations API answers when the bundle asset is missing, and says "none" for bytes
/// nothing attested.** v0.4.0 predates the `agentd.sigstore.json` asset but its `agentd` is
/// in the API, so this is the one release where the lookup reaches the API with no test-only
/// seam. Two unauthenticated API requests.
#[test]
#[ignore = "needs network: two attestations API requests; no AWS and no credentials"]
fn the_api_answers_for_a_release_without_its_bundle_asset() {
    let release = GitHubRelease::new(None);
    let bytes = release
        .asset("v0.4.0", ASSET)
        .expect("v0.4.0 has an agentd");
    let digest = provision::sha256_hex(&bytes);
    let found = release.attestations("v0.4.0", &digest);
    let Bundles::Published(bundles) = &found else {
        panic!("the API lists v0.4.0's attestation: {found:?}")
    };
    assert!(!bundles.is_empty());
    eprintln!("PROVISION v0.4.0 API bundles={}", bundles.len());

    let found = release.attestations("v0.4.0", &"0".repeat(64));
    assert!(matches!(found, Bundles::Absent(_)), "{found:?}");
}
