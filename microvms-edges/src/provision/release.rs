// SPDX-License-Identifier: Apache-2.0
//! The daemon release over HTTPS, and its attestation checked in-process (BIND-18, #284).
//!
//! [`GitHubRelease`] is the app's [`ReleaseSource`] over reqwest, [`SigstoreVerifier`] its
//! [`AttestationVerifier`] over `sigstore-verify`, [`PolicyFetch`] the [`Fetch`] that runs
//! [`fetch_release`] over a pair of them and writes the proven bytes where the cache asked, and
//! [`HttpsFetch`] that over these two. Nothing here
//! spawns a tool, so a machine with no `gh`, or a `gh` that isn't logged in, still gets an
//! attestation check.
//!
//! # Where a bundle comes from
//!
//! The release asset [`BUNDLE_ASSET`] first: the release workflow uploads the same bundle it
//! publishes to the attestations API (measured 2026-09-25: the two are identical JSON), and a
//! release download isn't counted against the API's rate limit, which is sixty requests an hour
//! for an unauthenticated address. The API by digest is the fallback, with `GITHUB_TOKEN` when
//! it's set.
//!
//! # Which answers mean "there is no bundle"
//!
//! The policy refuses the bytes on [`Bundles::Absent`] and falls back to `SHA256SUMS` on
//! [`Bundles::Unreachable`], so the line between them is where provenance can be stripped.
//! Only a 404 is an answer; every other failure (a transport error, a 403 or 429 rate limit, a
//! 5xx, a body that doesn't parse) is a failure to get one. The lookup is absent when the
//! bundle asset is a 404 and the API has no bundle to give, or when the API itself says it
//! has no attestation for the digest (a 404 or an empty list). It's unreachable only when
//! neither answered.
//!
//! A bundle asset that downloads is the bundle, whatever it holds: one that doesn't parse is
//! refused by the verifier rather than skipped for the API, because otherwise uploading junk
//! in its place would do what deleting it no longer can. Measured 2026-09-26: v0.5.0 on,
//! every release carries both the bundle asset and `SHA256SUMS`, and before that neither, so
//! none of these refusals turns away a release the checksum would have passed.
//!
//! # Why every request gets a runtime of its own
//!
//! [`Fetch`] is synchronous, and the CLI calls it from inside its own tokio runtime, on a worker
//! thread. Starting a runtime there, or dropping one, panics. So each request runs on a scoped
//! thread with a current-thread runtime and a client that live only for that request, which
//! works the same from a runtime worker, a `spawn_blocking` thread, or a plain `main`.

use std::fmt;
use std::future::Future;
use std::path::Path;
use std::time::Duration;

use microvms_app::provision::{
    ASSET, AttestationVerifier, Bundles, CHECKSUMS, RELEASE_REPO, ReleaseSource, Signer,
    fetch_release,
};

use super::{Fetch, Verification};

/// The release asset holding the Sigstore bundle for [`ASSET`], from v0.5.0 on.
pub const BUNDLE_ASSET: &str = "agentd.sigstore.json";

/// The variable whose token raises the attestations API's rate limit.
pub const TOKEN_VARIABLE: &str = "GITHUB_TOKEN";

/// The largest daemon asset accepted. Releases so far are about 2 MiB; a ceiling keeps a
/// hostile or broken server from filling memory.
const ASSET_LIMIT: usize = 64 << 20;

/// The largest `SHA256SUMS` or bundle accepted. Both are a few KiB.
const SMALL_LIMIT: usize = 1 << 20;

/// The largest attestations API response accepted: one bundle per attestation.
const API_LIMIT: usize = 8 << 20;

/// The release's files over HTTPS: the public download URLs for assets, and GitHub's
/// attestations API for a bundle the release doesn't carry.
#[derive(Clone, Default)]
pub struct GitHubRelease {
    token: Option<String>,
}

impl GitHubRelease {
    /// A source that sends `token` to the attestations API. `None`, or an empty token, sends
    /// none: the API answers a public repository without one, at the lower rate limit.
    pub fn new(token: Option<String>) -> Self {
        Self {
            token: token.filter(|token| !token.is_empty()),
        }
    }

    /// A source with [`TOKEN_VARIABLE`]'s value from `env`, when it's set.
    pub fn from_env(env: &dyn Fn(&str) -> Option<String>) -> Self {
        Self::new(env(TOKEN_VARIABLE))
    }

    /// [`ReleaseSource::attestations`] over any `get`, so the tests can answer each request
    /// with the status they need. See the module docs for which answers are which.
    fn lookup(&self, tag: &str, sha256: &str, get: &Get<'_>) -> Bundles {
        let asset_reason = match get(&asset_url(tag, BUNDLE_ASSET), None, SMALL_LIMIT) {
            Ok(body) => return Bundles::Published(vec![String::from_utf8_lossy(&body).into()]),
            Err(failure) => failure,
        };
        let (api_absent, api_reason) = match self.api_bundles(sha256, get) {
            Ok(bundles) if !bundles.is_empty() => return Bundles::Published(bundles),
            Ok(_) => (true, "no attestation for the digest".to_string()),
            Err(failure) => (failure.not_found(), failure.message),
        };
        let reason = format!(
            "the {BUNDLE_ASSET} asset: {}; the attestations API: {api_reason}",
            asset_reason.message
        );
        if asset_reason.not_found() || api_absent {
            Bundles::Absent(reason)
        } else {
            Bundles::Unreachable(reason)
        }
    }

    /// The bundles the attestations API lists inline for one digest. A list whose entries
    /// carry no inline bundle is a failure rather than an empty answer: the attestations
    /// exist, and this client can't read them.
    fn api_bundles(&self, sha256: &str, get: &Get<'_>) -> Result<Vec<String>, HttpFailure> {
        let url = attestations_url(sha256);
        let body = get(&url, self.token.as_deref(), API_LIMIT).map_err(|failure| {
            if self.token.is_none() && matches!(failure.status, Some(403 | 429)) {
                HttpFailure {
                    message: format!(
                        "{} (the unauthenticated rate limit; set {TOKEN_VARIABLE} to raise it)",
                        failure.message
                    ),
                    ..failure
                }
            } else {
                failure
            }
        })?;
        parse_attestations(&body).map_err(|message| HttpFailure {
            status: None,
            message,
        })
    }
}

/// One `GET`: the URL, the token for the API, and the body limit.
type Get<'a> = dyn Fn(&str, Option<&str>, usize) -> Result<Vec<u8>, HttpFailure> + 'a;

/// Why a `GET` returned no body.
#[derive(Clone, Debug, PartialEq, Eq)]
struct HttpFailure {
    /// The response status, when a response arrived.
    status: Option<u16>,
    message: String,
}

impl HttpFailure {
    /// The server answered that the resource doesn't exist: the one failure that is an answer.
    fn not_found(&self) -> bool {
        self.status == Some(404)
    }
}

/// The shipped `GET`: [`get`] on a runtime of its own.
fn https_get(url: &str, token: Option<&str>, limit: usize) -> Result<Vec<u8>, HttpFailure> {
    run(get(url, token, limit))
}

/// Written by hand so the token never reaches a log.
impl fmt::Debug for GitHubRelease {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("GitHubRelease")
            .field("token", &self.token.as_ref().map(|_| "<redacted>"))
            .finish()
    }
}

impl ReleaseSource for GitHubRelease {
    fn asset(&self, tag: &str, name: &str) -> Result<Vec<u8>, String> {
        https_get(&asset_url(tag, name), None, ASSET_LIMIT).map_err(|failure| failure.message)
    }

    fn checksums(&self, tag: &str) -> Result<String, String> {
        let body = https_get(&asset_url(tag, CHECKSUMS), None, SMALL_LIMIT)
            .map_err(|failure| failure.message)?;
        String::from_utf8(body).map_err(|_| format!("{CHECKSUMS} is not UTF-8"))
    }

    fn attestations(&self, tag: &str, sha256: &str) -> Bundles {
        self.lookup(tag, sha256, &https_get)
    }
}

/// The public download URL for `asset` on release `tag`. It redirects to GitHub's CDN and
/// needs no token for a public repository.
pub fn asset_url(tag: &str, asset: &str) -> String {
    format!("https://github.com/{RELEASE_REPO}/releases/download/{tag}/{asset}")
}

/// The attestations API's URL for the artifact whose SHA-256 is `sha256`.
pub fn attestations_url(sha256: &str) -> String {
    format!("https://api.github.com/repos/{RELEASE_REPO}/attestations/sha256:{sha256}")
}

/// The bundles in an attestations API response, as JSON text for the verifier.
fn parse_attestations(body: &[u8]) -> Result<Vec<String>, String> {
    let response: serde_json::Value = serde_json::from_slice(body)
        .map_err(|error| format!("the response is not JSON: {error}"))?;
    let listed = response["attestations"]
        .as_array()
        .ok_or("the response has no `attestations` list")?;
    let bundles: Vec<String> = listed
        .iter()
        .filter(|attestation| attestation["bundle"].is_object())
        .map(|attestation| attestation["bundle"].to_string())
        .collect();
    if bundles.is_empty() && !listed.is_empty() {
        return Err("the response lists attestations with no inline bundle".to_string());
    }
    Ok(bundles)
}

/// Runs one request to completion on a scoped thread with a runtime of its own. See the module
/// docs for why.
fn run<T: Send>(
    request: impl Future<Output = Result<T, HttpFailure>> + Send,
) -> Result<T, HttpFailure> {
    let failure = |message: String| HttpFailure {
        status: None,
        message,
    };
    std::thread::scope(|scope| {
        scope
            .spawn(|| {
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .map_err(|error| {
                        failure(format!(
                            "could not start a runtime for the download: {error}"
                        ))
                    })?
                    .block_on(request)
            })
            .join()
            .unwrap_or_else(|_| Err(failure("the download thread panicked".to_string())))
    })
}

/// `GET url`, failing on any status but success and on a body over `limit` bytes.
///
/// HTTPS only, redirects included, as `curl --proto =https` enforced before this. `token`
/// goes only to the API, the one host whose rate limit it raises: a release download needs
/// none, and it would otherwise follow the redirect to the CDN.
async fn get(url: &str, token: Option<&str>, limit: usize) -> Result<Vec<u8>, HttpFailure> {
    let failure = |message: String| HttpFailure {
        status: None,
        message,
    };
    let client = reqwest::Client::builder()
        .https_only(true)
        // The API refuses a request without a User-Agent.
        .user_agent(concat!("microvms-edges/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(Duration::from_secs(30))
        // The ceiling curl had: a stalled transfer is an error rather than a hang.
        .timeout(Duration::from_secs(300))
        .build()
        .map_err(|error| failure(format!("could not build an HTTP client: {}", chain(&error))))?;
    let mut request = client.get(url);
    if url.starts_with("https://api.github.com/") {
        request = request
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28");
        if let Some(token) = token {
            request = request.bearer_auth(token);
        }
    }
    let mut response = request
        .send()
        .await
        .map_err(|error| failure(format!("GET {url}: {}", chain(&error))))?;
    let status = response.status();
    if !status.is_success() {
        return Err(HttpFailure {
            status: Some(status.as_u16()),
            message: format!("GET {url}: HTTP {}", status.as_u16()),
        });
    }
    if response
        .content_length()
        .is_some_and(|length| length > limit as u64)
    {
        return Err(failure(format!(
            "GET {url}: the body is over {limit} bytes"
        )));
    }
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|error| failure(format!("GET {url}: {}", chain(&error))))?
    {
        if body.len() + chunk.len() > limit {
            return Err(failure(format!(
                "GET {url}: the body is over {limit} bytes"
            )));
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

/// An error and its causes on one line. reqwest's own message stops at "error sending request",
/// and the cause (a DNS failure, a refused connection, a timeout) is what a reader acts on.
fn chain(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(": ");
        text.push_str(&cause.to_string());
        source = cause.source();
    }
    text
}

/// The Sigstore check, in-process, against the public-good trusted root `sigstore-trust-root`
/// embeds.
///
/// It checks everything `gh attestation verify` does: the certificate chain to Fulcio, the
/// certificate transparency SCT, the Rekor inclusion proof, checkpoint and SET, the
/// certificate's validity at the logged time, the DSSE signature, and the in-toto subject
/// against the artifact's digest. Then the signer: the certificate identity and issuer through
/// the policy, and the statement's predicate type after it.
///
/// # The root is as old as this build
///
/// The root is the one `sigstore-trust-root` embedded when this crate's pin was last bumped,
/// and nothing refreshes it at run time (`tuf` is off; see `Cargo.toml`). A bundle logged to a
/// Rekor shard or signed under a key newer than the root is refused like a tampered one. Each
/// client fetches its own version's daemon, so the release workflow is where that would
/// surface: it verifies the bundle it just produced with this type before it publishes
/// anything (`tests/release_bundle.rs`), and a refusal there means bumping the pin, not
/// turning a check off.
pub struct SigstoreVerifier {
    verifier: sigstore_verify::Verifier,
}

impl SigstoreVerifier {
    /// A verifier over the embedded public-good trusted root. Fails only if that root doesn't
    /// parse, which a test below would have caught first.
    pub fn public_good() -> Result<Self, String> {
        let root = sigstore_trust_root::TrustedRoot::from_json(
            sigstore_trust_root::SIGSTORE_PRODUCTION_TRUSTED_ROOT,
        )
        .map_err(|error| format!("the embedded Sigstore trusted root: {error}"))?;
        let verifier = sigstore_verify::Verifier::new(&root)
            .map_err(|error| format!("the embedded Sigstore trusted root: {error}"))?;
        Ok(Self { verifier })
    }
}

impl fmt::Debug for SigstoreVerifier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("SigstoreVerifier(public-good)")
    }
}

impl AttestationVerifier for SigstoreVerifier {
    fn verify(&self, bundle: &str, artifact: &[u8], signer: &Signer) -> Result<(), String> {
        let bundle = sigstore_types::Bundle::from_json(bundle)
            .map_err(|error| format!("the bundle doesn't parse: {error}"))?;
        // Never `skip_tlog_unsafe`, `skip_sct` or `skip_certificate_chain`: the log evidence is
        // what proves the short-lived certificate signed inside its ten-minute window.
        let policy = sigstore_verify::VerificationPolicy::new(&signer.identity, &signer.issuer);
        self.verifier
            .verify(artifact, &bundle, &policy)
            .map_err(|error| error.to_string())?;
        // `sigstore-verify` binds the subject but not the predicate, and `gh` checks it: a
        // bundle the release workflow signed over some other statement isn't provenance.
        let sigstore_types::SignatureContent::DsseEnvelope(envelope) = &bundle.content else {
            return Err("the bundle signs a message, not an in-toto attestation".to_string());
        };
        let statement: sigstore_types::Statement =
            serde_json::from_slice(&envelope.decode_payload())
                .map_err(|error| format!("the attested statement doesn't parse: {error}"))?;
        if statement.predicate_type != signer.predicate_type {
            return Err(format!(
                "predicate type mismatch: expected {}, got {}",
                signer.predicate_type, statement.predicate_type
            ));
        }
        Ok(())
    }
}

/// A [`Fetch`] that runs [`fetch_release`] over any source and verifier, writing the proven
/// bytes to the destination the cache gave it, and nothing when the policy refuses.
/// [`HttpsFetch`] is this over GitHub and the public-good root; a test answers with a release
/// of its own.
#[derive(Clone, Debug, Default)]
pub struct PolicyFetch<S, V> {
    pub source: S,
    pub verifier: V,
}

impl<S: ReleaseSource, V: AttestationVerifier> Fetch for PolicyFetch<S, V> {
    fn fetch(
        &self,
        tag: &str,
        dest: &Path,
        progress: &mut dyn FnMut(&str),
    ) -> Result<Verification, String> {
        let fetched = fetch_release(&self.source, &self.verifier, tag, progress)?;
        std::fs::write(dest, &fetched.bytes)
            .map_err(|error| format!("could not write {ASSET} to {}: {error}", dest.display()))?;
        Ok(fetched.verification)
    }
}

/// The shipped [`Fetch`]: [`PolicyFetch`] over [`GitHubRelease`] and [`SigstoreVerifier`].
#[derive(Clone, Debug, Default)]
pub struct HttpsFetch {
    release: GitHubRelease,
}

impl HttpsFetch {
    /// A fetch that sends [`TOKEN_VARIABLE`]'s value from `env` to the attestations API.
    pub fn from_env(env: &dyn Fn(&str) -> Option<String>) -> Self {
        Self {
            release: GitHubRelease::from_env(env),
        }
    }
}

impl Fetch for HttpsFetch {
    fn fetch(
        &self,
        tag: &str,
        dest: &Path,
        progress: &mut dyn FnMut(&str),
    ) -> Result<Verification, String> {
        PolicyFetch {
            source: self.release.clone(),
            verifier: SigstoreVerifier::public_good()?,
        }
        .fetch(tag, dest, progress)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use microvms_app::provision::{ReleaseSource, sha256_hex};

    // A real release, committed: the v0.7.0 daemon (the smallest attested `agentd` signed
    // under the repository's current owner), its bundle, and its SHA256SUMS, downloaded
    // 2026-09-26 from the release's public URLs. `tests/release_bundle.rs` reads the same files.
    const TAG: &str = "v0.7.0";
    const BUNDLE: &str = include_str!("../../tests/fixtures/release-v0.7.0/agentd.sigstore.json");
    const SUMS: &str = include_str!("../../tests/fixtures/release-v0.7.0/SHA256SUMS");

    /// The asset's bytes. It's committed gzipped so the tree holds no executable: OpenSSF
    /// Scorecard's Binary-Artifacts check counts every ELF file against the score, and a
    /// gzip stream isn't one.
    fn agentd() -> &'static [u8] {
        static AGENTD: std::sync::LazyLock<Vec<u8>> = std::sync::LazyLock::new(|| {
            use std::io::Read as _;
            let packed = include_bytes!("../../tests/fixtures/release-v0.7.0/agentd.gz");
            let mut bytes = Vec::new();
            flate2::read::GzDecoder::new(&packed[..])
                .read_to_end(&mut bytes)
                .expect("the fixture is gzip");
            bytes
        });
        &AGENTD
    }

    fn verifier() -> SigstoreVerifier {
        SigstoreVerifier::public_good().expect("the embedded trusted root parses")
    }

    /// The fixture with one bit of one byte flipped.
    fn flipped() -> Vec<u8> {
        let mut bytes = agentd().to_vec();
        bytes[1_000_000] ^= 0x01;
        bytes
    }

    /// The bundle with one field of its first transparency log entry changed by `edit`.
    fn tampered(edit: impl FnOnce(&mut serde_json::Value)) -> String {
        let mut bundle: serde_json::Value =
            serde_json::from_str(BUNDLE).expect("the fixture parses");
        edit(&mut bundle["verificationMaterial"]["tlogEntries"][0]);
        bundle.to_string()
    }

    /// Base64 `text` with one bit in its middle byte flipped.
    fn flip_base64(value: &mut serde_json::Value) {
        use base64::Engine as _;
        let engine = base64::engine::general_purpose::STANDARD;
        let mut bytes = engine
            .decode(value.as_str().expect("a base64 string"))
            .expect("valid base64");
        let middle = bytes.len() / 2;
        bytes[middle] ^= 0x01;
        *value = serde_json::Value::String(engine.encode(bytes));
    }

    /// **A real release's asset verifies against its committed bundle** (#284): the positive
    /// half, without which every refusal below could be a verifier that refuses everything.
    #[test]
    fn the_committed_release_verifies_against_its_bundle() {
        verifier()
            .verify(BUNDLE, agentd(), &Signer::release(TAG))
            .expect("the v0.7.0 release asset is attested by its release workflow");
        assert_eq!(
            sha256_hex(agentd()),
            "b281856000f58e6306bcaca73362e4d77260324a51f9b7ba3de563cf102dbe76",
            "the fixture is the published asset"
        );
    }

    /// **One flipped byte in the asset is refused** (BIND-18, #284).
    #[test]
    fn one_flipped_byte_in_the_asset_is_refused() {
        let refusal = verifier()
            .verify(BUNDLE, &flipped(), &Signer::release(TAG))
            .expect_err("changed bytes must not verify");
        assert!(refusal.contains("does not match any subject"), "{refusal}");
    }

    /// **A signer other than the release workflow at the requested tag is refused** (BIND-18): the
    /// same bundle, checked against another tag (a downgrade), another workflow, and the
    /// repository's former owner.
    #[test]
    fn another_signer_identity_is_refused() {
        let release = Signer::release(TAG);
        let others = [
            Signer::release("v0.8.0"),
            Signer {
                identity: release.identity.replace("release.yml", "ci.yml"),
                ..release.clone()
            },
            Signer {
                identity: release.identity.replace("laithalsaadoon", "theagenticguy"),
                ..release.clone()
            },
        ];
        for signer in others {
            let refusal = verifier()
                .verify(BUNDLE, agentd(), &signer)
                .expect_err(&signer.identity);
            assert!(refusal.contains("identity mismatch"), "{refusal}");
        }
    }

    /// Another OIDC issuer is refused.
    #[test]
    fn another_issuer_is_refused() {
        let signer = Signer {
            issuer: "https://accounts.google.com".into(),
            ..Signer::release(TAG)
        };
        let refusal = verifier()
            .verify(BUNDLE, agentd(), &signer)
            .expect_err("another issuer must refuse");
        assert!(refusal.contains("issuer mismatch"), "{refusal}");
    }

    /// A statement of another predicate type is refused, as `gh` refuses it.
    #[test]
    fn another_predicate_type_is_refused() {
        let signer = Signer {
            predicate_type: "https://spdx.dev/Document".into(),
            ..Signer::release(TAG)
        };
        let refusal = verifier()
            .verify(BUNDLE, agentd(), &signer)
            .expect_err("another predicate type must refuse");
        assert!(refusal.contains("predicate type mismatch"), "{refusal}");
    }

    /// **Tampered transparency log evidence is refused.** The reason this crate and not
    /// `sigstore` 0.14, which accepted each of these (measured 2026-09-25). The first three
    /// cases are the ones only the inclusion proof check sees, so they're what fails if the
    /// policy ever turns on `skip_tlog_unsafe`; the SET and the integrated time it signs are
    /// checked either way.
    #[test]
    fn tampered_transparency_evidence_is_refused() {
        let cases = [
            (
                "proof hash",
                tampered(|entry| flip_base64(&mut entry["inclusionProof"]["hashes"][0])),
            ),
            (
                "checkpoint signature",
                tampered(|entry| {
                    let envelope = entry["inclusionProof"]["checkpoint"]["envelope"]
                        .as_str()
                        .expect("a checkpoint")
                        .to_string();
                    let (head, signature) = envelope.trim_end().rsplit_once(' ').expect("signed");
                    let mut signature = serde_json::Value::String(signature.to_string());
                    flip_base64(&mut signature);
                    entry["inclusionProof"]["checkpoint"]["envelope"] = serde_json::Value::String(
                        format!("{head} {}\n", signature.as_str().expect("a string")),
                    );
                }),
            ),
            (
                "proof log index",
                tampered(|entry| {
                    let index: u64 = entry["inclusionProof"]["logIndex"]
                        .as_str()
                        .expect("a string")
                        .parse()
                        .expect("a number");
                    entry["inclusionProof"]["logIndex"] =
                        serde_json::Value::String((index + 1).to_string());
                }),
            ),
            (
                "proof root hash",
                tampered(|entry| flip_base64(&mut entry["inclusionProof"]["rootHash"])),
            ),
            (
                "SET",
                tampered(|entry| {
                    flip_base64(&mut entry["inclusionPromise"]["signedEntryTimestamp"])
                }),
            ),
            (
                "integrated time",
                tampered(|entry| {
                    let time: u64 = entry["integratedTime"]
                        .as_str()
                        .expect("a string")
                        .parse()
                        .expect("a number");
                    entry["integratedTime"] = serde_json::Value::String((time + 60).to_string());
                }),
            ),
        ];
        for (what, bundle) in cases {
            verifier()
                .verify(&bundle, agentd(), &Signer::release(TAG))
                .expect_err(what);
        }
    }

    /// A bundle that isn't one is a refusal, not a panic.
    #[test]
    fn a_bundle_that_does_not_parse_is_refused() {
        let refusal = verifier()
            .verify("{\"not\": \"a bundle\"}", agentd(), &Signer::release(TAG))
            .expect_err("must refuse");
        assert!(refusal.contains("doesn't parse"), "{refusal}");
    }

    /// The committed release served from memory, with or without its bundle.
    struct Fixture {
        asset: Vec<u8>,
        bundle: Option<Bundle>,
    }

    /// What the fixture's lookup answers when it doesn't serve the bundle.
    #[derive(Clone, Copy, Debug)]
    enum Bundle {
        Unreachable,
        Absent,
    }

    impl ReleaseSource for Fixture {
        fn asset(&self, tag: &str, name: &str) -> Result<Vec<u8>, String> {
            assert_eq!((tag, name), (TAG, ASSET));
            Ok(self.asset.clone())
        }

        fn checksums(&self, _: &str) -> Result<String, String> {
            Ok(SUMS.to_string())
        }

        fn attestations(&self, _: &str, _: &str) -> Bundles {
            match self.bundle {
                None => Bundles::Published(vec![BUNDLE.to_string()]),
                Some(Bundle::Unreachable) => Bundles::Unreachable("HTTP 403".to_string()),
                Some(Bundle::Absent) => Bundles::Absent("HTTP 404".to_string()),
            }
        }
    }

    /// The policy over the real verifier and the committed release: attestation when the
    /// bundle is there, the checksum when it can't be reached, a refusal when the release says
    /// it has none, and a refusal for flipped bytes either way.
    #[test]
    fn the_policy_over_the_committed_release() {
        let fetch = |asset: Vec<u8>, bundle: Option<Bundle>| {
            fetch_release(&Fixture { asset, bundle }, &verifier(), TAG, &mut |_| {})
        };
        let attested = fetch(agentd().to_vec(), None).expect("attested");
        assert_eq!(attested.verification, Verification::Attestation);
        assert_eq!(attested.bytes, agentd());
        let checked = fetch(agentd().to_vec(), Some(Bundle::Unreachable)).expect("checked");
        assert_eq!(checked.verification, Verification::Checksum);
        let refusal = fetch(agentd().to_vec(), Some(Bundle::Absent))
            .expect_err("a release that says it has no bundle is refused");
        assert!(refusal.contains("publishes no attestation"), "{refusal}");

        let refusal = fetch(flipped(), None).expect_err("flipped bytes, bundle there");
        assert!(refusal.contains("discarded"), "{refusal}");
        let refusal = fetch(flipped(), Some(Bundle::Unreachable)).expect_err("flipped, no bundle");
        assert!(refusal.contains("SHA256 mismatch"), "{refusal}");
    }

    /// What [`GitHubRelease::lookup`]'s scripted `GET` answers for one URL.
    #[derive(Clone, Copy, Debug)]
    enum Answer {
        /// 200 with the committed bundle, whole or wrapped as the API lists it.
        Bundle,
        /// 200 with an empty attestations list.
        EmptyList,
        /// 200 with junk.
        Junk,
        Status(u16),
        /// No response at all.
        Transport,
    }

    /// The lookup over a `GET` that answers the bundle asset with `asset` and the API with
    /// `api`, and the token each request carried.
    fn lookup(token: Option<&str>, asset: Answer, api: Answer) -> (Bundles, Vec<Option<String>>) {
        let tokens = std::cell::RefCell::new(Vec::new());
        let get = |url: &str, token: Option<&str>, _: usize| {
            tokens.borrow_mut().push(token.map(str::to_string));
            let (answer, from_api) = if url == asset_url(TAG, BUNDLE_ASSET) {
                (asset, false)
            } else {
                assert_eq!(url, attestations_url("abc"), "only the two lookups run");
                (api, true)
            };
            let bundle: serde_json::Value = serde_json::from_str(BUNDLE).expect("parses");
            match answer {
                Answer::Bundle if from_api => Ok(serde_json::json!({
                    "attestations": [{ "bundle": bundle }]
                })
                .to_string()
                .into_bytes()),
                Answer::Bundle => Ok(BUNDLE.as_bytes().to_vec()),
                Answer::EmptyList => Ok(br#"{"attestations": []}"#.to_vec()),
                Answer::Junk => Ok(b"\xff<html>".to_vec()),
                Answer::Status(status) => Err(HttpFailure {
                    status: Some(status),
                    message: format!("GET {url}: HTTP {status}"),
                }),
                Answer::Transport => Err(HttpFailure {
                    status: None,
                    message: format!("GET {url}: error sending request: dns error"),
                }),
            }
        };
        let found = GitHubRelease::new(token.map(str::to_string)).lookup(TAG, "abc", &get);
        (found, tokens.into_inner())
    }

    /// **Only a "not found" is an answer** (BIND-18, #284). Deleting the bundle asset, or
    /// replacing it with junk, refuses the bytes; a rate limit, a server error or a failed
    /// connection is the only road to the checksum; and the API's bundle is used when the
    /// asset's can't be had.
    #[test]
    fn a_missing_bundle_is_absent_and_a_failed_request_is_unreachable() {
        use Answer::*;
        let published = |found: &Bundles| matches!(found, Bundles::Published(b) if b.len() == 1);
        let absent = |found: &Bundles| matches!(found, Bundles::Absent(_));
        let unreachable = |found: &Bundles| matches!(found, Bundles::Unreachable(_));
        type Row<'a> = (Answer, Answer, &'a dyn Fn(&Bundles) -> bool);
        let rows: [Row<'_>; 14] = [
            (Bundle, Status(500), &published),
            // A bundle asset that downloads is the bundle, junk included: the verifier refuses
            // junk, and skipping it for the API would let junk stand in for a deleted bundle.
            (Junk, Status(404), &published),
            (Junk, Bundle, &published),
            (Status(404), Bundle, &published),
            (Status(503), Bundle, &published),
            (Status(404), Status(404), &absent),
            (Status(404), EmptyList, &absent),
            (Status(404), Status(403), &absent),
            (Status(404), Transport, &absent),
            (Transport, Status(404), &absent),
            (Status(403), EmptyList, &absent),
            (Status(403), Status(429), &unreachable),
            (Transport, Status(502), &unreachable),
            (Status(500), Junk, &unreachable),
        ];
        for (asset, api, expected) in rows {
            let (found, _) = lookup(None, asset, api);
            assert!(expected(&found), "asset {asset:?}, API {api:?}: {found:?}");
        }
        let (found, _) = lookup(None, Status(404), Bundle);
        let Bundles::Published(bundles) = found else {
            panic!("{found:?}")
        };
        verifier()
            .verify(&bundles[0], agentd(), &Signer::release(TAG))
            .expect("the API's bundle verifies like the asset's");
    }

    /// The token goes to the API and nowhere else, and a rate limit without one says to set
    /// it.
    #[test]
    fn the_token_goes_only_to_the_api() {
        let (_, tokens) = lookup(Some("ghp_secret"), Answer::Status(404), Answer::Bundle);
        assert_eq!(tokens, [None, Some("ghp_secret".to_string())]);
        let (found, _) = lookup(None, Answer::Transport, Answer::Status(403));
        let Bundles::Unreachable(reason) = found else {
            panic!("{found:?}")
        };
        assert!(reason.contains("set GITHUB_TOKEN"), "{reason}");
        let (found, _) = lookup(Some("t"), Answer::Transport, Answer::Status(403));
        assert!(
            !format!("{found:?}").contains("set GITHUB_TOKEN"),
            "{found:?}"
        );
    }

    /// The API's response shape: a list of attestations, each carrying its bundle inline.
    #[test]
    fn the_api_response_yields_each_bundle() {
        let bundle: serde_json::Value = serde_json::from_str(BUNDLE).expect("parses");
        let body = serde_json::json!({
            "attestations": [
                { "bundle": bundle, "repository_id": 1 },
                { "bundle_url": "https://example.invalid/only-a-url" },
            ]
        });
        let only_urls = r#"{"attestations": [{"bundle_url": "https://example.invalid/x"}]}"#;
        assert!(parse_attestations(only_urls.as_bytes()).is_err());
        let bundles = parse_attestations(body.to_string().as_bytes()).expect("parses");
        assert_eq!(bundles.len(), 1);
        verifier()
            .verify(&bundles[0], agentd(), &Signer::release(TAG))
            .expect("the API's bundle verifies like the asset's");

        assert_eq!(
            parse_attestations(br#"{"attestations": []}"#).expect("parses"),
            Vec::<String>::new()
        );
        assert!(parse_attestations(br#"{"message": "Not Found"}"#).is_err());
        assert!(parse_attestations(b"<html>").is_err());
    }

    /// The URLs the source reads, spelled out so a change to either is a visible diff.
    #[test]
    fn the_urls_are_the_public_release_and_the_attestations_api() {
        assert_eq!(
            asset_url("v0.7.0", BUNDLE_ASSET),
            "https://github.com/laithalsaadoon/microvms-agentd/releases/download/v0.7.0/\
             agentd.sigstore.json"
        );
        assert_eq!(
            attestations_url("abc"),
            "https://api.github.com/repos/laithalsaadoon/microvms-agentd/attestations/sha256:abc"
        );
    }

    /// The token comes from `GITHUB_TOKEN`, an empty one is none, and neither appears in a
    /// debug print.
    #[test]
    fn the_token_is_read_from_the_environment_and_never_printed() {
        let with =
            GitHubRelease::from_env(&|name| (name == TOKEN_VARIABLE).then(|| "ghp_secret".into()));
        assert_eq!(with.token.as_deref(), Some("ghp_secret"));
        let printed = format!("{:?}", HttpsFetch { release: with });
        assert!(!printed.contains("ghp_secret"), "{printed}");
        assert!(printed.contains("redacted"), "{printed}");
        assert_eq!(
            GitHubRelease::from_env(&|_| Some(String::new())).token,
            None
        );
        assert_eq!(GitHubRelease::from_env(&|_| None).token, None);
    }

    /// A request runs from inside a runtime too, which is where the CLI calls it from. A
    /// runtime started on a runtime's own thread would panic here instead of answering.
    #[tokio::test]
    async fn a_request_runs_from_inside_a_runtime() {
        let answer = run(async { Ok::<_, HttpFailure>(7) });
        assert_eq!(answer, Ok(7));
    }
}
