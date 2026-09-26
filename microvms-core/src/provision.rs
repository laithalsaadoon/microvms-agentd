// SPDX-License-Identifier: Apache-2.0
//! Provisioning of the `agentd` daemon binary: one call that returns verified aarch64
//! bytes for the version a client drives (BIND-17 through BIND-20, issue #219).
//!
//! # Why the client stack fetches its own daemon
//!
//! The daemon is this product's own component, versioned in this workspace and shipped
//! from this repository's releases. A harness that bakes it into an image should not need
//! a hard-coded release URL, an ELF check, and a cache of its own; `docker run` takes an
//! image name, and Dagger's CLI provisions its matching-version engine itself. So
//! [`resolve`] answers a request from, in order:
//!
//! 1. a caller-supplied path: [`Request::binary`], else `$MICROVM_AGENTD` (a path, for the
//!    caller who manages the binary themselves);
//! 2. the cache entry for the requested version under the state directory;
//! 3. a fetch of this repository's GitHub release asset for that version.
//!
//! The version defaults to [`crate::VERSION`], the core's own. The daemon and the client
//! share one workspace version, so `v{VERSION}` is the one tag whose protocol this client
//! is proven against; a "latest" fetch would reintroduce exactly the skew the shared
//! version exists to prevent.
//!
//! A refused caller-supplied binary is an error, never a fall-through to the cache or a
//! fetch: a caller who named a binary manages it, and running a different one would run a
//! daemon they did not choose.
//!
//! # What proves a fetch
//!
//! The download goes through a subprocess the caller can see in `ps`, and the CLI's
//! `tests/thinness.rs` is why the policy was written that way: that crate may hold no HTTP
//! client, and the policy moved here unchanged. [`ReleaseFetch`] runs two tools, in
//! preference order:
//!
//! - **`gh`**, because `gh attestation verify` checks the Sigstore attestation the release
//!   workflow published for the asset: provenance, not just integrity. A verification
//!   failure after a successful download is a **hard stop**, never a fall-through: bytes
//!   that exist but do not verify are the one state a fallback must not launder.
//! - **`curl`**, because `gh` refuses to run unauthenticated even against a public
//!   repository. This path checks the download against the release's `SHA256SUMS` asset,
//!   hashed in-process: integrity against corruption, weaker than provenance, and
//!   [`Verification`] says which of the two the caller got. A release with no
//!   `SHA256SUMS` (every tag before v0.5.0) fails closed.
//!
//! A fetch that cannot be verified is an error (BIND-18), never a warning.
//!
//! # What is checked no matter where the bytes came from
//!
//! Every binary this module returns is an aarch64 ELF (BIND-20): MicroVMs are ARM64-only,
//! and a wrong binary baked into an image fails 45 minutes later as a run-hook timeout that
//! says nothing about architecture. Twenty bytes of header now is the whole cost of never
//! finding out.
//!
//! # The cache serves only what it verified
//!
//! An install is write-to-partial-then-rename, so an interrupted download never leaves a
//! truncated binary where the next request reads. Beside each entry sits a digest record
//! written after verification, and a cache hit is served only when the bytes still hash to
//! it (BIND-19). An entry that does not match, or that has no record (one an older client
//! installed), is discarded and fetched again. The record lives in the same directory as
//! the binary, so this catches corruption and a binary copied over the entry, not a writer
//! who replaces both; such a writer could replace a caller-supplied binary just as well.
//!
//! `model/src/provision.rs` checks this policy against the ways it could be written
//! instead, and `tests/features/provision.feature` drives it through a fake release.
//!
//! # The seams
//!
//! [`Fetch`] is the download-and-prove seam the CLI's guards script, and [`Runner`] is the
//! subprocess seam under [`ReleaseFetch`], which the Gherkin runner answers with a fake
//! release. Neither lets a test open a socket to GitHub.

use std::fmt;
use std::path::{Path, PathBuf};

// The ELF checks read bytes, so they're rules with no I/O; this module is where a caller has
// always found them.
pub use microvms_domain::provision::{REQUIRED_ELF_MACHINE, elf_machine, not_aarch64};

use crate::error::{Error, ErrorKind};

/// The repository whose releases carry the daemon asset.
pub const RELEASE_REPO: &str = "laithalsaadoon/microvms-agentd";

/// The release asset's name: a literal, because the README's `--pattern agentd` and the
/// checksum lookup both match it exactly.
pub const ASSET: &str = "agentd";

/// The environment variable naming a caller-managed binary, used when a request carries
/// no [`Request::binary`].
pub const ENV_OVERRIDE: &str = "MICROVM_AGENTD";

/// The digest record's file name, beside the cached binary.
const RECORD: &str = "agentd.verified.json";

/// The longest version string accepted. Release tags are short; a bound keeps a hostile
/// value from becoming a long path component.
const MAX_VERSION_LEN: usize = 64;

/// How a fetched binary's bytes were proven.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verification {
    /// `gh attestation verify`: the bytes carry the release workflow's Sigstore
    /// attestation for this repository.
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

    fn parse(text: &str) -> Option<Self> {
        match text {
            "attestation" => Some(Verification::Attestation),
            "checksum" => Some(Verification::Checksum),
            _ => None,
        }
    }
}

/// How a caller supplied a binary.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Supplier {
    /// [`Request::binary`].
    Argument,
    /// `$MICROVM_AGENTD`.
    Env,
}

impl Supplier {
    pub fn as_str(self) -> &'static str {
        match self {
            Supplier::Argument => "argument",
            Supplier::Env => "env",
        }
    }
}

/// Where a provisioned binary came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Source {
    /// The caller's own binary. Its provenance is the caller's business, so it carries no
    /// [`Verification`]; it is checked for aarch64 like everything else.
    CallerSupplied(Supplier),
    /// The cache entry for the requested version, whose bytes still match the digest
    /// recorded when this verification passed.
    Cache(Verification),
    /// Fetched from the GitHub release during this call.
    Fetched(Verification),
}

impl Source {
    /// `"caller-supplied"`, `"cache"`, or `"fetched"`.
    pub fn as_str(self) -> &'static str {
        match self {
            Source::CallerSupplied(_) => "caller-supplied",
            Source::Cache(_) => "cache",
            Source::Fetched(_) => "fetched",
        }
    }

    /// How the bytes were proven: when fetched, or when the cache entry was installed.
    /// `None` for a caller-supplied binary.
    pub fn verification(self) -> Option<Verification> {
        match self {
            Source::CallerSupplied(_) => None,
            Source::Cache(verification) | Source::Fetched(verification) => Some(verification),
        }
    }
}

/// A provisioned daemon binary and how it got here.
#[derive(Clone, PartialEq, Eq)]
pub struct Provisioned {
    /// The binary itself, an aarch64 ELF.
    pub bytes: Vec<u8>,
    /// Where it is on disk: the cache entry, or the caller's path.
    pub path: PathBuf,
    pub source: Source,
    /// The version requested, without a leading `v`. For a caller-supplied binary, the
    /// version asked for, which nothing checks the binary against.
    pub version: String,
    /// The lowercase hex SHA-256 of [`Self::bytes`].
    pub sha256: String,
}

impl Provisioned {
    /// How the bytes were proven, or `None` for a caller-supplied binary.
    pub fn verification(&self) -> Option<Verification> {
        self.source.verification()
    }
}

/// Written by hand so a debug print never dumps megabytes of binary.
impl fmt::Debug for Provisioned {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Provisioned")
            .field("bytes", &format_args!("<{} bytes>", self.bytes.len()))
            .field("path", &self.path)
            .field("source", &self.source)
            .field("version", &self.version)
            .field("sha256", &self.sha256)
            .finish()
    }
}

/// What a caller asks for. Every field is optional; [`Request::default`] asks for the
/// core's own version under the default state directory.
#[derive(Clone, Copy, Debug, Default)]
pub struct Request<'a> {
    /// The release version, with or without a leading `v`. Defaults to [`crate::VERSION`].
    pub version: Option<&'a str>,
    /// The state directory the cache lives under. Defaults to the CLI's
    /// ([`crate::names::default_state_root`]), so every surface shares one cache.
    pub state_dir: Option<&'a Path>,
    /// A binary the caller manages. Outranks `$MICROVM_AGENTD`.
    pub binary: Option<&'a Path>,
}

/// Which step refused, so a caller can attach its own remedies.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// The version is not a release tag.
    InvalidVersion,
    /// The caller's path names nothing, or cannot be read.
    CallerBinary(Supplier),
    /// The binary is not an aarch64 ELF.
    NotAarch64,
    /// The release could not be downloaded, or its bytes could not be verified.
    Fetch,
    /// A filesystem operation under the state directory failed.
    Io,
}

/// A refusal, with the remedies a person or agent can act on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProvisionError {
    pub failure: Failure,
    pub message: String,
    pub remedies: Vec<String>,
}

impl ProvisionError {
    fn new(failure: Failure, message: impl Into<String>) -> Self {
        Self {
            failure,
            message: message.into(),
            remedies: Vec::new(),
        }
    }

    fn remedy(mut self, remedy: impl Into<String>) -> Self {
        self.remedies.push(remedy.into());
        self
    }

    /// The error-taxonomy row: `ERR_INVALID_ARG` for a version that is not a tag,
    /// `ERR_PRECONDITION` for everything else, since no AWS call has been spent.
    pub fn kind(&self) -> ErrorKind {
        match self.failure {
            Failure::InvalidVersion => ErrorKind::InvalidArg,
            _ => ErrorKind::Precondition,
        }
    }
}

impl fmt::Display for ProvisionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ProvisionError {}

/// The taxonomy error a binding raises: the message, then each remedy, since a binding's
/// exception has nowhere else to carry them.
impl From<ProvisionError> for Error {
    fn from(error: ProvisionError) -> Self {
        let mut message = error.message.clone();
        for remedy in &error.remedies {
            message.push_str("\n  - ");
            message.push_str(remedy);
        }
        Error::new(error.kind(), message)
    }
}

/// The download-and-prove seam. The shipped client carries [`SubprocessFetch`]; the CLI's
/// guards script this, which keeps `cargo test` off the network the way its `CoreSeam`
/// keeps it off AWS.
pub trait Fetch {
    /// Download release `tag`'s daemon asset to `dest` and prove the bytes, reporting
    /// progress lines as it goes. The error is prose: the caller owns the remedies.
    fn fetch(
        &self,
        tag: &str,
        dest: &Path,
        progress: &mut dyn FnMut(&str),
    ) -> Result<Verification, String>;
}

/// The subprocess seam under [`ReleaseFetch`]: run an argv, folding a spawn failure and a
/// non-zero exit into one prose reason.
pub trait Runner {
    fn run(&self, argv: &[String]) -> Result<(), String>;
}

/// A borrowed runner runs the same way, so a caller can keep and inspect its own.
impl<R: Runner + ?Sized> Runner for &R {
    fn run(&self, argv: &[String]) -> Result<(), String> {
        (**self).run(argv)
    }
}

/// The shipped [`Runner`]: `std::process::Command`.
#[derive(Clone, Copy, Debug, Default)]
pub struct Subprocess;

impl Runner for Subprocess {
    fn run(&self, argv: &[String]) -> Result<(), String> {
        let output = std::process::Command::new(&argv[0])
            .args(&argv[1..])
            .stdin(std::process::Stdio::null())
            .output()
            .map_err(|error| format!("`{}` did not run: {error}", argv[0]))?;
        if output.status.success() {
            return Ok(());
        }
        let detail = String::from_utf8_lossy(&output.stderr);
        let detail = detail.trim();
        Err(format!(
            "`{}` exited {}: {}",
            argv.join(" "),
            output.status.code().unwrap_or(-1),
            if detail.is_empty() {
                "(no stderr)"
            } else {
                detail
            },
        ))
    }
}

/// The verification policy over a [`Runner`]: `gh` for provenance, `curl` and
/// `SHA256SUMS` for integrity when `gh` cannot download. See the module docs.
#[derive(Clone, Copy, Debug, Default)]
pub struct ReleaseFetch<R>(pub R);

impl<R: Runner> Fetch for ReleaseFetch<R> {
    fn fetch(
        &self,
        tag: &str,
        dest: &Path,
        progress: &mut dyn FnMut(&str),
    ) -> Result<Verification, String> {
        let runner = &self.0;
        // `gh` first. Any failure to *download* falls through to curl: `gh` refuses to run
        // unauthenticated even against a public repository, and that refusal must not cost
        // an unauthenticated machine the feature.
        match runner.run(&gh_download_args(tag, dest)) {
            Ok(()) => {
                progress(&format!(
                    "downloaded {ASSET} {tag} via gh; verifying provenance"
                ));
                // A verification failure after a successful download is the one hard stop:
                // falling through to curl here would launder bytes that failed provenance
                // into a weaker check that cannot see what was wrong with them.
                return match runner.run(&gh_verify_args(dest)) {
                    Ok(()) => Ok(Verification::Attestation),
                    Err(reason) => {
                        let _ = std::fs::remove_file(dest);
                        Err(format!(
                            "`gh attestation verify` refused the downloaded asset: {reason}. \
                             The bytes were discarded; do not retry with verification off."
                        ))
                    }
                };
            }
            Err(gh_reason) => {
                progress(&format!("gh could not download ({gh_reason}); trying curl"));
            }
        }

        runner
            .run(&curl_args(&asset_url(tag, ASSET), dest))
            .map_err(|curl_reason| {
                format!(
                    "neither tool could download {ASSET} {tag} from {RELEASE_REPO}: gh and \
                     curl both failed, most recently: {curl_reason}"
                )
            })?;
        // Integrity, fail-closed: a release without SHA256SUMS (every tag before v0.5.0)
        // refuses rather than trusting TLS alone.
        let sums_dest = dest.with_extension("sums");
        let sums = runner
            .run(&curl_args(&asset_url(tag, "SHA256SUMS"), &sums_dest))
            .and_then(|()| std::fs::read_to_string(&sums_dest).map_err(|error| error.to_string()));
        let _ = std::fs::remove_file(&sums_dest);
        let sums = sums.map_err(|reason| {
            let _ = std::fs::remove_file(dest);
            format!(
                "downloaded {ASSET} {tag} via curl, but could not fetch the release's \
                 SHA256SUMS to verify it ({reason}). curl alone proves nothing about the \
                 bytes, so this fails closed. Releases before v0.5.0 ship no SHA256SUMS; \
                 for those, authenticate `gh` and retry, or download and verify manually."
            )
        })?;
        let bytes = std::fs::read(dest).map_err(|error| error.to_string())?;
        if let Err(reason) = verify_sha256(&sums, ASSET, &bytes) {
            let _ = std::fs::remove_file(dest);
            return Err(reason);
        }
        progress(&format!(
            "downloaded {ASSET} {tag} via curl; SHA256SUMS entry matched"
        ));
        Ok(Verification::Checksum)
    }
}

/// The shipped fetcher: [`ReleaseFetch`] over real subprocesses.
#[derive(Clone, Copy, Debug, Default)]
pub struct SubprocessFetch;

impl Fetch for SubprocessFetch {
    fn fetch(
        &self,
        tag: &str,
        dest: &Path,
        progress: &mut dyn FnMut(&str),
    ) -> Result<Verification, String> {
        ReleaseFetch(Subprocess).fetch(tag, dest, progress)
    }
}

/// The public download URL for `asset` on release `tag`: what curl gets, since it cannot
/// speak the release API without a token.
fn asset_url(tag: &str, asset: &str) -> String {
    format!("https://github.com/{RELEASE_REPO}/releases/download/{tag}/{asset}")
}

/// `gh release download` argv, spelled as the README's manual command.
fn gh_download_args(tag: &str, dest: &Path) -> Vec<String> {
    vec![
        "gh".into(),
        "release".into(),
        "download".into(),
        tag.into(),
        "--repo".into(),
        RELEASE_REPO.into(),
        "--pattern".into(),
        ASSET.into(),
        "--output".into(),
        dest.display().to_string(),
        "--clobber".into(),
    ]
}

/// `gh attestation verify` argv: the provenance check the install docs tell people to run.
fn gh_verify_args(dest: &Path) -> Vec<String> {
    vec![
        "gh".into(),
        "attestation".into(),
        "verify".into(),
        dest.display().to_string(),
        "--repo".into(),
        RELEASE_REPO.into(),
    ]
}

/// curl argv: fail on HTTP errors, follow the release redirect to the CDN, HTTPS only, and
/// a ceiling so a stalled transfer is an error rather than a hang.
fn curl_args(url: &str, dest: &Path) -> Vec<String> {
    vec![
        "curl".into(),
        "-sSfL".into(),
        "--proto".into(),
        "=https".into(),
        "--max-time".into(),
        "300".into(),
        "--output".into(),
        dest.display().to_string(),
        url.into(),
    ]
}

/// Checks `bytes` against `asset`'s entry in a `SHA256SUMS` body (`<64 hex>  <name>` per
/// line, sha256sum's own format). In-process, because `sha256sum` the tool does not exist
/// on Windows.
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

/// A version as a release tag spells it, without the leading `v`, or a refusal.
///
/// The version becomes a path component of the cache and an argument to `gh` and a URL, so
/// it must be a plain tag: ASCII letters, digits, `.`, `+`, `-`, starting with a letter or
/// digit, with no `..`. That rules out every traversal and every flag.
pub fn normalize_version(version: &str) -> Result<String, ProvisionError> {
    let bare = version.strip_prefix('v').unwrap_or(version);
    let plain = !bare.is_empty()
        && bare.len() <= MAX_VERSION_LEN
        && bare.as_bytes()[0].is_ascii_alphanumeric()
        && !bare.contains("..")
        && bare
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'-'));
    if plain {
        return Ok(bare.to_string());
    }
    Err(ProvisionError::new(
        Failure::InvalidVersion,
        format!(
            "{version:?} is not a release version: expected a tag such as {} or v{}, made of \
             letters, digits, `.`, `+`, and `-`",
            crate::VERSION,
            crate::VERSION
        ),
    )
    .remedy("omit the version to provision the core's own, which is the one it is proven against"))
}

/// The cache path for `version`, under `state_dir`.
///
/// Versioned by directory rather than by file name, so the binary keeps the name the
/// Dockerfile stanza and every error message call it.
pub fn cache_path(state_dir: &Path, version: &str) -> PathBuf {
    state_dir
        .join("agentd")
        .join(format!("v{version}"))
        .join(ASSET)
}

/// The digest record beside [`cache_path`].
fn record_path(state_dir: &Path, version: &str) -> PathBuf {
    state_dir
        .join("agentd")
        .join(format!("v{version}"))
        .join(RECORD)
}

/// The record's body: the version, the digest of the bytes verified, and how.
pub(crate) fn record_json(version: &str, sha256: &str, verification: Verification) -> String {
    serde_json::json!({
        "version": version,
        "sha256": sha256,
        "verification": verification.as_str(),
    })
    .to_string()
}

/// How the cache entry for `version` was verified, if its record exists, names this
/// version, and matches `sha256`.
fn recorded(state_dir: &Path, version: &str, sha256: &str) -> Option<Verification> {
    let text = std::fs::read_to_string(record_path(state_dir, version)).ok()?;
    parse_record(&text, version, sha256)
}

/// How a record body says the entry was verified, if it names `version` and `sha256`
/// exactly.
pub(crate) fn parse_record(text: &str, version: &str, sha256: &str) -> Option<Verification> {
    let record: serde_json::Value = serde_json::from_str(text).ok()?;
    let matches =
        record["version"].as_str() == Some(version) && record["sha256"].as_str() == Some(sha256);
    matches
        .then(|| Verification::parse(record["verification"].as_str()?))
        .flatten()
}

/// A filesystem failure under the state directory.
fn io_error(path: &Path, verb: &str, error: &std::io::Error) -> ProvisionError {
    ProvisionError::new(
        Failure::Io,
        format!("could not {verb} {}: {error}", path.display()),
    )
    .remedy("the failure is on this machine's filesystem; the platform was not involved")
}

/// A failed fetch, with every way out named: the manual download, the override variable,
/// and the self-build. This is the error a fresh machine with no `gh` and no network sees,
/// so it carries the whole story rather than pointing at a doc.
fn fetch_error(tag: &str, reason: &str) -> ProvisionError {
    ProvisionError::new(
        Failure::Fetch,
        format!("could not provision the agentd daemon binary for {tag}: {reason}"),
    )
    .remedy(format!(
        "manual download: `gh release download {tag} --repo {RELEASE_REPO} --pattern {ASSET}` \
         (then `gh attestation verify {ASSET} --repo {RELEASE_REPO}`), and pass its path as \
         the binary or ${ENV_OVERRIDE}"
    ))
    .remedy(
        "a self-built daemon works too: cargo build --release -p agentd --target \
         aarch64-unknown-linux-musl",
    )
}

/// Answers `request` from the caller's binary, the cache, or the release, in that order.
///
/// `env` supplies `$MICROVM_AGENTD` and, when [`Request::state_dir`] is `None`, the
/// variables [`crate::names::default_state_root`] reads. `fetch` is the release seam;
/// `progress` receives one line per step.
pub fn resolve(
    request: &Request<'_>,
    env: &dyn Fn(&str) -> Option<String>,
    fetch: &dyn Fetch,
    progress: &mut dyn FnMut(&str),
) -> Result<Provisioned, ProvisionError> {
    let version = normalize_version(request.version.unwrap_or(crate::VERSION))?;

    // The caller's binary first: a caller who named one manages it, and a cache hit that
    // silently outranked it would run a daemon they did not choose.
    let supplied = match request.binary {
        Some(path) => Some((path.to_path_buf(), Supplier::Argument)),
        None => env(ENV_OVERRIDE).map(|path| (PathBuf::from(path), Supplier::Env)),
    };
    if let Some((path, supplier)) = supplied {
        return caller_supplied(path, supplier, version, progress);
    }

    let state_dir = match request.state_dir {
        Some(dir) => dir.to_path_buf(),
        None => crate::names::default_state_root(env),
    };
    let cached = cache_path(&state_dir, &version);
    if let Some(hit) = cache_hit(&state_dir, &cached, &version, progress)? {
        return Ok(hit);
    }
    fetched(&state_dir, &cached, version, fetch, progress)
}

fn caller_supplied(
    path: PathBuf,
    supplier: Supplier,
    version: String,
    progress: &mut dyn FnMut(&str),
) -> Result<Provisioned, ProvisionError> {
    let named = match supplier {
        Supplier::Argument => format!("the agentd binary {}", path.display()),
        Supplier::Env => format!("${ENV_OVERRIDE} names {}, which", path.display()),
    };
    let refuse = |failure: Failure, detail: String| {
        let error = ProvisionError::new(failure, format!("{named} {detail}"));
        match supplier {
            Supplier::Argument => error,
            Supplier::Env => error
                .remedy(format!(
                    "unset {ENV_OVERRIDE} to provision the release asset instead"
                ))
                .remedy("or point it at a real aarch64 agentd binary"),
        }
    };
    if !path.exists() {
        return Err(refuse(
            Failure::CallerBinary(supplier),
            "does not exist. A caller-supplied binary short-circuits provisioning, so a \
             stale path blocks the fetch that would otherwise have worked."
                .to_string(),
        ));
    }
    let bytes = std::fs::read(&path).map_err(|error| {
        refuse(
            Failure::CallerBinary(supplier),
            format!("could not be read: {error}"),
        )
    })?;
    if let Some(why) = not_aarch64(&bytes) {
        return Err(refuse(
            Failure::NotAarch64,
            format!(
                "is {why}. MicroVMs are ARM64-only, and a wrong-architecture daemon fails as \
                 a run-hook timeout 45 minutes into a build."
            ),
        )
        .remedy("build one: cargo build --release -p agentd --target aarch64-unknown-linux-musl"));
    }
    match supplier {
        Supplier::Argument => progress(&format!("using the caller's agentd: {}", path.display())),
        Supplier::Env => progress(&format!("using ${ENV_OVERRIDE}: {}", path.display())),
    }
    Ok(Provisioned {
        sha256: sha256_hex(&bytes),
        bytes,
        path,
        source: Source::CallerSupplied(supplier),
        version,
    })
}

/// The cache entry for `version`, when its bytes still match its record; otherwise the
/// entry is discarded and `None` sends the caller to a fetch (BIND-19).
fn cache_hit(
    state_dir: &Path,
    cached: &Path,
    version: &str,
    progress: &mut dyn FnMut(&str),
) -> Result<Option<Provisioned>, ProvisionError> {
    let bytes = match std::fs::read(cached) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error(cached, "read", &error)),
    };
    let sha256 = sha256_hex(&bytes);
    match recorded(state_dir, version, &sha256) {
        Some(verification) if not_aarch64(&bytes).is_none() => {
            progress(&format!(
                "using cached agentd v{version}: {}",
                cached.display()
            ));
            Ok(Some(Provisioned {
                bytes,
                path: cached.to_path_buf(),
                source: Source::Cache(verification),
                version: version.to_string(),
                sha256,
            }))
        }
        _ => {
            progress(&format!(
                "cached agentd v{version} at {} does not match the digest recorded when it \
                 was verified; discarding it and fetching the release again",
                cached.display()
            ));
            for path in [cached.to_path_buf(), record_path(state_dir, version)] {
                match std::fs::remove_file(&path) {
                    Ok(()) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    Err(error) => return Err(io_error(&path, "discard", &error)),
                }
            }
            Ok(None)
        }
    }
}

fn fetched(
    state_dir: &Path,
    cached: &Path,
    version: String,
    fetch: &dyn Fetch,
    progress: &mut dyn FnMut(&str),
) -> Result<Provisioned, ProvisionError> {
    let tag = format!("v{version}");
    progress(&format!(
        "no agentd given; fetching the release asset for {tag}"
    ));
    let dir = cached.parent().expect("the cache path has a parent");
    std::fs::create_dir_all(dir).map_err(|error| io_error(dir, "create", &error))?;
    // A partial name in the same directory, so the final `rename` is atomic on the same
    // filesystem: an interrupted download leaves a `.partial` nothing trusts, never a
    // truncated `agentd` the next request reads.
    let partial = dir.join(format!(".{ASSET}.partial-{}", std::process::id()));
    let verification = match fetch.fetch(&tag, &partial, progress) {
        Ok(verification) => verification,
        Err(reason) => {
            let _ = std::fs::remove_file(&partial);
            return Err(fetch_error(&tag, &reason));
        }
    };
    let bytes = std::fs::read(&partial).map_err(|error| {
        let _ = std::fs::remove_file(&partial);
        io_error(&partial, "read", &error)
    })?;
    if let Some(why) = not_aarch64(&bytes) {
        let _ = std::fs::remove_file(&partial);
        return Err(ProvisionError::new(
            Failure::NotAarch64,
            format!(
                "the fetched {tag} asset is {why}, so it was discarded rather than cached: a \
                 wrong-architecture daemon fails as a run-hook timeout 45 minutes into a build."
            ),
        )
        .remedy("this is a release-asset defect worth reporting, not a local mistake"));
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        std::fs::set_permissions(&partial, std::fs::Permissions::from_mode(0o755))
            .map_err(|error| io_error(&partial, "chmod", &error))?;
    }
    std::fs::rename(&partial, cached).map_err(|error| io_error(cached, "install", &error))?;
    // The record after the binary: a crash between the two leaves a binary with no record,
    // which the next request discards and fetches again rather than trusts.
    let sha256 = sha256_hex(&bytes);
    let record = record_path(state_dir, &version);
    let record_partial = dir.join(format!(".{RECORD}.partial-{}", std::process::id()));
    std::fs::write(
        &record_partial,
        record_json(&version, &sha256, verification),
    )
    .and_then(|()| std::fs::rename(&record_partial, &record))
    .map_err(|error| {
        let _ = std::fs::remove_file(&record_partial);
        io_error(&record, "record", &error)
    })?;
    progress(&format!(
        "fetched and verified agentd {tag} ({}); cached at {}",
        verification.as_str(),
        cached.display()
    ));
    Ok(Provisioned {
        bytes,
        path: cached.to_path_buf(),
        source: Source::Fetched(verification),
        version,
        sha256,
    })
}

/// **The one call.** The daemon binary for `version` (default: [`crate::VERSION`]) under
/// `state_dir` (default: the CLI's), from `$MICROVM_AGENTD`, the cache, or a verified fetch
/// through `gh` or `curl`.
///
/// Blocking: a fetch runs subprocesses and can take seconds.
pub fn agentd(version: Option<&str>, state_dir: Option<&Path>) -> Result<Provisioned, Error> {
    agentd_with(&Request {
        version,
        state_dir,
        binary: None,
    })
}

/// [`agentd`] with a caller-supplied binary as well: the bindings' entry point.
pub fn agentd_with(request: &Request<'_>) -> Result<Provisioned, Error> {
    resolve(request, &crate::env::process, &SubprocessFetch, &mut |_| {}).map_err(Error::from)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};

    /// A scripted fetch: writes `bytes` to the destination and counts invocations.
    struct Scripted {
        bytes: Vec<u8>,
        verification: Verification,
        calls: Cell<usize>,
    }

    impl Scripted {
        fn elf() -> Self {
            Self::with(elf_header(REQUIRED_ELF_MACHINE))
        }

        fn with(bytes: Vec<u8>) -> Self {
            Self {
                bytes,
                verification: Verification::Attestation,
                calls: Cell::new(0),
            }
        }
    }

    impl Fetch for Scripted {
        fn fetch(
            &self,
            _: &str,
            dest: &Path,
            _: &mut dyn FnMut(&str),
        ) -> Result<Verification, String> {
            self.calls.set(self.calls.get() + 1);
            std::fs::write(dest, &self.bytes).expect("writes");
            Ok(self.verification)
        }
    }

    /// A fetch that always fails, for the error-path assertions.
    struct Failing;

    impl Fetch for Failing {
        fn fetch(
            &self,
            _: &str,
            _: &Path,
            _: &mut dyn FnMut(&str),
        ) -> Result<Verification, String> {
            Err("no network in tests".into())
        }
    }

    /// A fetch that must not run.
    struct Panicking;

    impl Fetch for Panicking {
        fn fetch(
            &self,
            tag: &str,
            _: &Path,
            _: &mut dyn FnMut(&str),
        ) -> Result<Verification, String> {
            panic!("this request must not fetch (asked for {tag})");
        }
    }

    /// A 20-byte little-endian ELF header for `machine`.
    fn elf_header(machine: u16) -> Vec<u8> {
        let mut header = vec![0u8; 20];
        header[..4].copy_from_slice(b"\x7fELF");
        header[5] = 1;
        header[18..20].copy_from_slice(&machine.to_le_bytes());
        header
    }

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn request(dir: &Path) -> Request<'_> {
        Request {
            version: Some("9.9.9"),
            state_dir: Some(dir),
            binary: None,
        }
    }

    /// **A cache miss fetches once; the next request is a cache hit** (BIND-17). The
    /// property the module exists for: one download per version per machine.
    #[test]
    fn a_cache_miss_fetches_once_and_the_next_request_reads_the_cache() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let fetch = Scripted::elf();
        let first = resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("resolves");
        assert_eq!(first.source, Source::Fetched(Verification::Attestation));
        assert_eq!(fetch.calls.get(), 1);
        assert_eq!(first.bytes, std::fs::read(&first.path).expect("reads"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt as _;
            let mode = std::fs::metadata(&first.path)
                .expect("stat")
                .permissions()
                .mode();
            assert_eq!(
                mode & 0o111,
                0o111,
                "the installed binary must be executable"
            );
        }

        let second = resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("resolves");
        assert_eq!(second.source, Source::Cache(Verification::Attestation));
        assert_eq!(fetch.calls.get(), 1, "a cache hit must not fetch again");
        assert_eq!(second.path, first.path);
        assert_eq!(second.sha256, first.sha256);
    }

    /// The version defaults to the core's own, with or without a `v` (BIND-17).
    #[test]
    fn the_version_defaults_to_the_cores_own_and_accepts_a_leading_v() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let fetch = Scripted::elf();
        let default = Request {
            state_dir: Some(dir.path()),
            ..Request::default()
        };
        let served = resolve(&default, &no_env, &fetch, &mut |_| {}).expect("resolves");
        assert_eq!(served.version, crate::VERSION);
        assert_eq!(served.path, cache_path(dir.path(), crate::VERSION));
        let tagged = format!("v{}", crate::VERSION);
        let again = Request {
            version: Some(&tagged),
            ..default
        };
        let hit = resolve(&again, &no_env, &Panicking, &mut |_| {}).expect("resolves");
        assert_eq!(hit.source.as_str(), "cache");
    }

    /// **The caller's binary outranks the cache and never fetches** (BIND-17), and the
    /// argument outranks `$MICROVM_AGENTD`.
    #[test]
    fn a_caller_binary_outranks_the_environment_and_the_cache() {
        let dir = tempfile::tempdir().expect("a temp dir");
        resolve(&request(dir.path()), &no_env, &Scripted::elf(), &mut |_| {}).expect("caches");
        let own = dir.path().join("my-agentd");
        let mut bytes = elf_header(REQUIRED_ELF_MACHINE);
        bytes.extend_from_slice(b"caller-managed");
        std::fs::write(&own, &bytes).expect("writes");
        let env_path = dir.path().join("env-agentd");
        std::fs::write(&env_path, elf_header(REQUIRED_ELF_MACHINE)).expect("writes");
        let env_str = env_path.display().to_string();
        let env = move |name: &str| (name == ENV_OVERRIDE).then(|| env_str.clone());

        let from_env = resolve(&request(dir.path()), &env, &Panicking, &mut |_| {}).expect("env");
        assert_eq!(from_env.source, Source::CallerSupplied(Supplier::Env));
        assert_eq!(from_env.path, env_path);
        assert_eq!(from_env.verification(), None);

        let with_binary = Request {
            binary: Some(&own),
            ..request(dir.path())
        };
        let from_arg = resolve(&with_binary, &env, &Panicking, &mut |_| {}).expect("argument");
        assert_eq!(from_arg.source, Source::CallerSupplied(Supplier::Argument));
        assert_eq!(from_arg.bytes, bytes);
    }

    /// A `$MICROVM_AGENTD` pointing at nothing is an error naming the variable, not a
    /// fall-through to a fetch the caller opted out of.
    #[test]
    fn a_stale_environment_override_is_an_error_rather_than_a_fallthrough() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let env = |name: &str| (name == ENV_OVERRIDE).then(|| "/definitely/not/here".to_string());
        let failure = resolve(&request(dir.path()), &env, &Panicking, &mut |_| {})
            .expect_err("a stale override must refuse");
        assert_eq!(failure.failure, Failure::CallerBinary(Supplier::Env));
        assert_eq!(failure.kind(), ErrorKind::Precondition);
        assert!(
            failure.message.contains(ENV_OVERRIDE),
            "{}",
            failure.message
        );
        assert!(failure.remedies.iter().any(|r| r.contains("unset")));
    }

    /// **A caller-supplied binary that is not an aarch64 ELF is refused** (BIND-20), from
    /// either supplier, and never falls through to a fetch.
    #[test]
    fn a_caller_binary_that_is_not_aarch64_is_refused() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let x86 = dir.path().join("x86");
        std::fs::write(&x86, elf_header(0x3E)).expect("writes");
        let script = dir.path().join("script");
        std::fs::write(&script, b"#!/bin/sh\n").expect("writes");
        for (path, detail) in [(&x86, "ELF machine 0x3e"), (&script, "not an ELF")] {
            let with_binary = Request {
                binary: Some(path),
                ..request(dir.path())
            };
            let failure =
                resolve(&with_binary, &no_env, &Panicking, &mut |_| {}).expect_err("must refuse");
            assert_eq!(failure.failure, Failure::NotAarch64);
            assert!(failure.message.contains(detail), "{}", failure.message);
        }
        let x86_str = x86.display().to_string();
        let env = move |name: &str| (name == ENV_OVERRIDE).then(|| x86_str.clone());
        let failure =
            resolve(&request(dir.path()), &env, &Panicking, &mut |_| {}).expect_err("must refuse");
        assert!(
            failure.message.contains(ENV_OVERRIDE),
            "{}",
            failure.message
        );
    }

    /// **Fetched bytes that are not an aarch64 ELF are discarded, and the cache stays
    /// empty** (BIND-20). Caching them would turn one bad download into a persistent
    /// run-hook-timeout mystery on every later request.
    #[test]
    fn a_fetched_non_arm_binary_is_refused_and_not_cached() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let failure = resolve(
            &request(dir.path()),
            &no_env,
            &Scripted::with(elf_header(0x3E)),
            &mut |_| {},
        )
        .expect_err("an x86 asset must refuse");
        assert!(failure.message.contains("0x3e"), "{}", failure.message);
        assert!(
            !cache_path(dir.path(), "9.9.9").exists(),
            "nothing may be cached"
        );

        let failure = resolve(
            &request(dir.path()),
            &no_env,
            &Scripted::with(b"#!/bin/sh".to_vec()),
            &mut |_| {},
        )
        .expect_err("a non-ELF asset must refuse");
        assert!(
            failure.message.contains("not an ELF"),
            "{}",
            failure.message
        );
        let left: Vec<_> = std::fs::read_dir(cache_path(dir.path(), "9.9.9").parent().unwrap())
            .expect("the version dir exists")
            .collect();
        assert!(left.is_empty(), "no partial file may survive: {left:?}");
    }

    /// A failed fetch carries the tag and every way out (BIND-18).
    #[test]
    fn a_failed_fetch_names_the_tag_and_every_way_out() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let failure =
            resolve(&request(dir.path()), &no_env, &Failing, &mut |_| {}).expect_err("no network");
        assert_eq!(failure.failure, Failure::Fetch);
        assert!(failure.message.contains("v9.9.9"), "{}", failure.message);
        let remedies = failure.remedies.join("\n");
        assert!(remedies.contains("gh release download"), "{remedies}");
        assert!(remedies.contains(ENV_OVERRIDE), "{remedies}");
        assert!(
            remedies.contains("cargo build --release -p agentd"),
            "{remedies}"
        );
        // The binding form carries them in its one message.
        let error = Error::from(failure);
        assert_eq!(error.code(), "ERR_PRECONDITION");
        assert!(error.to_string().contains("gh release download"), "{error}");
    }

    /// **A cache entry that no longer matches its record is fetched again** (BIND-19), and
    /// so is one with no record at all.
    #[test]
    fn a_changed_or_unrecorded_cache_entry_is_fetched_again() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let fetch = Scripted::elf();
        resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("caches");
        let cached = cache_path(dir.path(), "9.9.9");
        let mut changed = elf_header(REQUIRED_ELF_MACHINE);
        changed.extend_from_slice(b"changed");
        std::fs::write(&cached, &changed).expect("overwrites");
        let mut lines = Vec::new();
        let again = resolve(&request(dir.path()), &no_env, &fetch, &mut |line| {
            lines.push(line.to_string())
        })
        .expect("refetches");
        assert_eq!(again.source.as_str(), "fetched");
        assert_eq!(fetch.calls.get(), 2);
        assert_eq!(again.bytes, fetch.bytes);
        assert!(
            lines.iter().any(|line| line.contains("does not match")),
            "{lines:?}"
        );

        std::fs::remove_file(record_path(dir.path(), "9.9.9")).expect("removes the record");
        let unrecorded =
            resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("refetches");
        assert_eq!(unrecorded.source.as_str(), "fetched");
        assert_eq!(fetch.calls.get(), 3);
    }

    /// A record for another version, or a garbled one, is no record (BIND-17, BIND-19).
    #[test]
    fn a_record_for_another_version_does_not_vouch_for_this_one() {
        let dir = tempfile::tempdir().expect("a temp dir");
        let fetch = Scripted::elf();
        resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("caches");
        let digest = sha256_hex(&fetch.bytes);
        let record = record_path(dir.path(), "9.9.9");
        std::fs::write(
            &record,
            record_json("1.0.0", &digest, Verification::Attestation),
        )
        .expect("writes");
        resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("refetches");
        assert_eq!(fetch.calls.get(), 2);
        std::fs::write(&record, "{not json").expect("writes");
        resolve(&request(dir.path()), &no_env, &fetch, &mut |_| {}).expect("refetches");
        assert_eq!(fetch.calls.get(), 3);
    }

    /// Versions that are not plain tags are refused before anything touches the disk.
    #[test]
    fn a_version_that_is_not_a_tag_is_refused() {
        for bad in [
            "", "v", "../x", "1.0/../x", ".hidden", "-rf", "1..2", "a b", "1\n",
        ] {
            let failure = normalize_version(bad).expect_err(bad);
            assert_eq!(failure.kind(), ErrorKind::InvalidArg, "{bad:?}");
        }
        assert_eq!(normalize_version("v0.9.0").unwrap(), "0.9.0");
        assert_eq!(
            normalize_version("1.2.3-rc.1+build").unwrap(),
            "1.2.3-rc.1+build"
        );
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

    /// The argv builders spell the exact commands the docs teach, so the two cannot drift.
    #[test]
    fn the_subprocess_argv_matches_the_documented_manual_commands() {
        let dest = Path::new("/tmp/agentd");
        assert_eq!(
            gh_download_args("v0.5.0", dest).join(" "),
            "gh release download v0.5.0 --repo laithalsaadoon/microvms-agentd \
             --pattern agentd --output /tmp/agentd --clobber"
        );
        assert_eq!(
            gh_verify_args(dest).join(" "),
            "gh attestation verify /tmp/agentd --repo laithalsaadoon/microvms-agentd"
        );
        let curl = curl_args(&asset_url("v0.5.0", ASSET), dest).join(" ");
        assert!(
            curl.starts_with("curl -sSfL --proto =https --max-time 300"),
            "{curl}"
        );
        assert!(
            curl.ends_with(
                "https://github.com/laithalsaadoon/microvms-agentd/releases/download/v0.5.0/agentd"
            ),
            "{curl}"
        );
    }

    /// A [`Runner`] that plays the release from a table: which tools download, whether the
    /// attestation passes, and what `SHA256SUMS` says.
    struct Table {
        gh: bool,
        attested: bool,
        curl: bool,
        /// `None`: no SHA256SUMS on the release; `Some(true)`: matching; `Some(false)`: not.
        sums: Option<bool>,
        asset: Vec<u8>,
        ran: RefCell<Vec<String>>,
    }

    impl Runner for Table {
        fn run(&self, argv: &[String]) -> Result<(), String> {
            self.ran.borrow_mut().push(argv[..2].join(" "));
            let output = || {
                let at = argv.iter().position(|a| a == "--output").expect("--output");
                PathBuf::from(&argv[at + 1])
            };
            match (argv[0].as_str(), argv[1].as_str()) {
                ("gh", "release") if self.gh => {
                    std::fs::write(output(), &self.asset).map_err(|e| e.to_string())
                }
                ("gh", "attestation") if self.attested => Ok(()),
                ("curl", _) if self.curl => {
                    let url = argv.last().expect("a url");
                    if url.ends_with("/agentd") {
                        return std::fs::write(output(), &self.asset).map_err(|e| e.to_string());
                    }
                    let digest = match self.sums {
                        None => return Err("404".into()),
                        Some(true) => sha256_hex(&self.asset),
                        Some(false) => sha256_hex(b"other"),
                    };
                    std::fs::write(output(), format!("{digest}  agentd\n"))
                        .map_err(|e| e.to_string())
                }
                _ => Err(format!("{} refused", argv[0])),
            }
        }
    }

    /// **The verification table** — the literal mirror of `the_verification_table` in
    /// `model/src/provision.rs` (BIND-18): attestation when `gh` downloads and attests,
    /// refusal without `curl` when it downloads and does not, and otherwise `curl` with
    /// only a matching `SHA256SUMS` passing. A refused fetch leaves no file behind.
    #[test]
    fn the_verification_table_matches_the_model() {
        // (gh downloads, attested, curl downloads, sums) → verdict; `None` is refused.
        type Row = (bool, bool, bool, Option<bool>, Option<Verification>);
        let rows: [Row; 10] = [
            (
                true,
                true,
                true,
                Some(true),
                Some(Verification::Attestation),
            ),
            (true, true, false, None, Some(Verification::Attestation)),
            (true, false, true, Some(true), None),
            (true, false, false, None, None),
            (false, false, true, Some(true), Some(Verification::Checksum)),
            (false, false, true, Some(false), None),
            (false, false, true, None, None),
            (false, false, false, Some(true), None),
            (false, true, true, Some(true), Some(Verification::Checksum)),
            (false, true, true, None, None),
        ];
        let dir = tempfile::tempdir().expect("a temp dir");
        for (gh, attested, curl, sums, expected) in rows {
            let table = Table {
                gh,
                attested,
                curl,
                sums,
                asset: elf_header(REQUIRED_ELF_MACHINE),
                ran: RefCell::new(Vec::new()),
            };
            let dest = dir.path().join("partial");
            let verdict = ReleaseFetch(&table)
                .fetch("v9.9.9", &dest, &mut |_| {})
                .ok();
            let row = (gh, attested, curl, sums);
            assert_eq!(verdict, expected, "{row:?}");
            if verdict.is_none() {
                assert!(!dest.exists(), "a refused fetch left bytes behind: {row:?}");
            }
            if gh && !attested {
                let ran = table.ran.borrow();
                assert!(
                    ran.iter().all(|tool| !tool.starts_with("curl")),
                    "a refused attestation must not fall through to curl: {ran:?}"
                );
            }
            let _ = std::fs::remove_file(&dest);
        }
    }
}
