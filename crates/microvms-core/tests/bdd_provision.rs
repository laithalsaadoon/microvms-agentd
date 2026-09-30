// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for BIND-17 through BIND-20: `microvms_core::provision`
//! against a fake GitHub release.
//!
//! The scenarios live in `tests/features/provision.feature`, tagged with the requirement
//! each one verifies; this file is their step definitions and runner. It is a
//! `harness = false` test, so `cargo test` runs it on every CI system, and it writes a JUnit
//! report when `CUCUMBER_JUNIT` names a file (give that path absolutely).
//!
//! # The fake release sits at the release seam
//!
//! [`provision::PolicyFetch`] runs the shipped verification policy,
//! [`provision::fetch_release`], over a [`provision::ReleaseSource`] and a
//! [`provision::AttestationVerifier`]; the shipped fetch is the same over GitHub and
//! `sigstore-verify`. [`FakeRelease`] is both ports from memory: it serves the asset or
//! refuses it, publishes an attestation bundle that verifies or one that doesn't, answers that
//! it has none, or can't be reached for one, and serves, omits, or corrupts `SHA256SUMS`. So a scenario exercises the real policy,
//! including which step runs after which failure, and nothing opens a socket to GitHub.
//!
//! Only `provision.feature` runs here, named by file rather than by directory, so another
//! feature file added under `tests/features/` never runs against these step definitions.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use cucumber::{World, WriterExt, cli, given, then, when, writer};
use microvms_core::provision::{
    self, AttestationVerifier, Bundles, PolicyFetch, ReleaseSource, Request, Signer,
};

/// The e_machine of an aarch64 ELF.
const AARCH64: u16 = 0xB7;
/// The e_machine of an x86_64 ELF.
const X86_64: u16 = 0x3E;

/// A little-endian ELF header for `machine`, padded so two binaries differ by `tail`.
fn elf(machine: u16, tail: &[u8]) -> Vec<u8> {
    let mut bytes = vec![0u8; 20];
    bytes[..4].copy_from_slice(b"\x7fELF");
    bytes[5] = 1;
    bytes[18..20].copy_from_slice(&machine.to_le_bytes());
    bytes.extend_from_slice(tail);
    bytes
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest as _, Sha256};
    const_hex::encode(Sha256::digest(bytes))
}

/// What the release's `SHA256SUMS` asset holds.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
enum Sums {
    /// A matching entry for `agentd`.
    #[default]
    Matching,
    /// No `SHA256SUMS` asset on the release.
    Missing,
    /// An entry whose digest is not the asset's.
    Mismatched,
    /// Entries for other assets only.
    WithoutAgentd,
}

/// What an attestation lookup for the asset's digest finds.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
enum Bundle {
    /// A bundle the release workflow signed for these bytes.
    #[default]
    Verifying,
    /// A bundle that doesn't verify for these bytes.
    Refused,
    /// Neither the release asset nor the attestations API answers.
    Unavailable,
    /// The release answers that it publishes no bundle for these bytes.
    Absent,
}

/// The fake release's behavior, and a log of every operation it was asked for, with its tag.
#[derive(Debug, Default)]
struct ReleaseState {
    asset: Vec<u8>,
    /// The asset downloads.
    downloads: bool,
    bundle: Bundle,
    sums: Sums,
    calls: Vec<(&'static str, String)>,
}

/// A [`ReleaseSource`] and an [`AttestationVerifier`] answered from a [`ReleaseState`].
#[derive(Clone, Debug, Default)]
struct FakeRelease(Arc<Mutex<ReleaseState>>);

impl FakeRelease {
    fn state(&self) -> std::sync::MutexGuard<'_, ReleaseState> {
        self.0.lock().expect("the release lock")
    }
}

/// The bundle text the fake publishes for a digest, so the verifier can tell whether it was
/// handed the bundle for the bytes it's checking.
fn bundle_for(sha256: &str) -> String {
    format!("{{\"bundle for\": \"{sha256}\"}}")
}

impl ReleaseSource for FakeRelease {
    fn asset(&self, tag: &str, name: &str) -> Result<Vec<u8>, String> {
        let mut state = self.state();
        state.calls.push(("asset", tag.to_string()));
        assert_eq!(name, provision::ASSET);
        if !state.downloads {
            return Err("GET https://github.com/...: error sending request: dns error".into());
        }
        Ok(state.asset.clone())
    }

    fn checksums(&self, tag: &str) -> Result<String, String> {
        let mut state = self.state();
        state.calls.push(("checksums", tag.to_string()));
        Ok(match state.sums {
            Sums::Missing => return Err("HTTP 404".into()),
            Sums::Matching => format!("{}  agentd\n", sha256_hex(&state.asset)),
            Sums::Mismatched => format!("{}  agentd\n", sha256_hex(b"something else")),
            Sums::WithoutAgentd => format!("{}  microvm-x86_64.tar.gz\n", sha256_hex(b"x")),
        })
    }

    fn attestations(&self, tag: &str, sha256: &str) -> Bundles {
        let mut state = self.state();
        state.calls.push(("attestations", tag.to_string()));
        match state.bundle {
            Bundle::Unavailable => {
                Bundles::Unreachable("HTTP 403 (the unauthenticated rate limit)".into())
            }
            Bundle::Absent => Bundles::Absent("HTTP 404; the attestations API: HTTP 404".into()),
            Bundle::Verifying | Bundle::Refused => Bundles::Published(vec![bundle_for(sha256)]),
        }
    }
}

impl AttestationVerifier for FakeRelease {
    fn verify(&self, bundle: &str, artifact: &[u8], signer: &Signer) -> Result<(), String> {
        let state = self.state();
        let (_, tag) = state
            .calls
            .iter()
            .rev()
            .find(|(operation, _)| *operation == "asset")
            .expect("the asset was asked for");
        // The policy must ask for the release workflow at the tag it downloaded.
        assert_eq!(signer, &Signer::release(tag));
        if state.bundle == Bundle::Verifying && bundle == bundle_for(&sha256_hex(artifact)) {
            Ok(())
        } else {
            Err("certificate identity mismatch".into())
        }
    }
}

#[derive(Debug, World)]
#[world(init = Self::new)]
struct Provisioning {
    state_dir: tempfile::TempDir,
    release: FakeRelease,
    env: HashMap<String, String>,
    /// The caller-supplied binary, and the bytes written there.
    binary: Option<(PathBuf, Vec<u8>)>,
    outcome: Option<Result<provision::Provisioned, microvms_core::Error>>,
}

impl Provisioning {
    fn new() -> Self {
        Self {
            state_dir: tempfile::tempdir().expect("a temp dir"),
            release: FakeRelease::default(),
            env: HashMap::new(),
            binary: None,
            outcome: None,
        }
    }

    fn state(&self) -> std::sync::MutexGuard<'_, ReleaseState> {
        self.release.state()
    }

    fn provision(&mut self, version: Option<&str>, binary: Option<&Path>) {
        let env = self.env.clone();
        let lookup = move |name: &str| env.get(name).cloned();
        let fetch = PolicyFetch {
            source: self.release.clone(),
            verifier: self.release.clone(),
        };
        let request = Request {
            version,
            state_dir: Some(self.state_dir.path()),
            binary,
        };
        let outcome =
            provision::resolve(&request, &lookup, &fetch, &mut |_| {}).map_err(Into::into);
        self.outcome = Some(outcome);
    }

    fn served(&self) -> &provision::Provisioned {
        match &self.outcome {
            Some(Ok(provisioned)) => provisioned,
            Some(Err(error)) => panic!("provisioning failed: {error}"),
            None => panic!("nothing was provisioned"),
        }
    }

    fn failure(&self) -> &microvms_core::Error {
        match &self.outcome {
            Some(Err(error)) => error,
            Some(Ok(provisioned)) => panic!("provisioning succeeded: {:?}", provisioned.source),
            None => panic!("nothing was provisioned"),
        }
    }

    fn downloads(&self) -> usize {
        self.state()
            .calls
            .iter()
            .filter(|(operation, _)| *operation == "asset")
            .count()
    }

    fn supply(&mut self, name: &str, bytes: Vec<u8>) -> PathBuf {
        let path = self.state_dir.path().join(name);
        std::fs::write(&path, &bytes).expect("writes the caller's binary");
        self.binary = Some((path.clone(), bytes));
        path
    }
}

fn configure(
    world: &mut Provisioning,
    asset: Vec<u8>,
    downloads: bool,
    bundle: Bundle,
    sums: Sums,
) {
    let mut state = world.state();
    state.asset = asset;
    state.downloads = downloads;
    state.bundle = bundle;
    state.sums = sums;
}

#[given("a release whose agentd is an aarch64 ELF with a verifying attestation")]
fn arm_attested(world: &mut Provisioning) {
    let asset = elf(AARCH64, b"release");
    configure(world, asset, true, Bundle::Verifying, Sums::Matching);
}

#[given("a release whose agentd is an x86_64 ELF with a verifying attestation")]
fn x86_attested(world: &mut Provisioning) {
    let asset = elf(X86_64, b"release");
    configure(world, asset, true, Bundle::Verifying, Sums::Matching);
}

#[given(
    "a release whose agentd is an aarch64 ELF with no attestation to fetch, and a matching SHA256SUMS"
)]
fn arm_checksum_only(world: &mut Provisioning) {
    let asset = elf(AARCH64, b"release");
    configure(world, asset, true, Bundle::Unavailable, Sums::Matching);
}

#[given(regex = r"^a release where (.+)$")]
fn broken_release(world: &mut Provisioning, what: String) {
    let asset = elf(AARCH64, b"release");
    let (downloads, bundle, sums) = match what.as_str() {
        "the attestation refuses the downloaded bytes" => (true, Bundle::Refused, Sums::Matching),
        "the release publishes no attestation for the downloaded bytes" => {
            (true, Bundle::Absent, Sums::Matching)
        }
        "no attestation can be fetched and SHA256SUMS is gone" => {
            (true, Bundle::Unavailable, Sums::Missing)
        }
        "no attestation can be fetched and SHA256SUMS differs" => {
            (true, Bundle::Unavailable, Sums::Mismatched)
        }
        "no attestation can be fetched and SHA256SUMS omits it" => {
            (true, Bundle::Unavailable, Sums::WithoutAgentd)
        }
        "the asset cannot be downloaded" => (false, Bundle::Verifying, Sums::Matching),
        other => panic!("no release behaves as {other:?}"),
    };
    configure(world, asset, downloads, bundle, sums);
}

#[given("the cache already holds the core's own version")]
fn cache_holds_core(world: &mut Provisioning) {
    world.provision(None, None);
    assert_eq!(world.served().source.as_str(), "fetched");
}

#[given("the cached binary is overwritten with other bytes")]
fn tamper(world: &mut Provisioning) {
    let path = provision::cache_path(world.state_dir.path(), microvms_core::VERSION);
    std::fs::write(&path, elf(AARCH64, b"changed after it was verified")).expect("overwrites");
}

#[given("the cache holds a binary for the core's own version with no digest record")]
fn legacy_entry(world: &mut Provisioning) {
    let path = provision::cache_path(world.state_dir.path(), microvms_core::VERSION);
    std::fs::create_dir_all(path.parent().expect("a parent")).expect("mkdir");
    std::fs::write(&path, elf(AARCH64, b"an older client installed this")).expect("writes");
}

#[given("the caller supplies an aarch64 ELF binary")]
fn caller_arm(world: &mut Provisioning) {
    world.supply("my-agentd", elf(AARCH64, b"caller"));
}

#[given(regex = r"^the caller supplies (an x86_64 ELF binary|a shell script|a path that is gone)$")]
fn caller_bad(world: &mut Provisioning, what: String) {
    match what.as_str() {
        "an x86_64 ELF binary" => {
            world.supply("my-agentd", elf(X86_64, b"caller"));
        }
        "a shell script" => {
            world.supply("my-agentd", b"#!/bin/sh\nexec agentd\n".to_vec());
        }
        _ => world.binary = Some((world.state_dir.path().join("gone"), Vec::new())),
    }
}

#[given(regex = r"^MICROVM_AGENTD names an? (aarch64|x86_64) ELF binary$")]
fn env_binary(world: &mut Provisioning, arch: String) {
    let machine = if arch == "aarch64" { AARCH64 } else { X86_64 };
    let path = world.supply("env-agentd", elf(machine, b"env"));
    world.binary = None;
    world.env.insert(
        provision::ENV_OVERRIDE.to_string(),
        path.display().to_string(),
    );
}

#[when("I provision agentd with no version")]
fn provision_default(world: &mut Provisioning) {
    world.provision(None, None);
}

#[when(expr = "I provision agentd version {string}")]
fn provision_version(world: &mut Provisioning, version: String) {
    world.provision(Some(&version), None);
}

#[when("I provision agentd with the caller's binary")]
fn provision_caller(world: &mut Provisioning) {
    let path = world.binary.as_ref().expect("a caller's binary").0.clone();
    world.provision(None, Some(&path));
}

#[then(expr = "the binary came from {string}, verified by {string}")]
fn came_from_verified(world: &mut Provisioning, source: String, verification: String) {
    let served = world.served();
    assert_eq!(served.source.as_str(), source);
    assert_eq!(
        served.verification().map(|v| v.as_str()),
        Some(verification.as_str())
    );
    // What a caller reads is what the cache holds, and it is an aarch64 ELF.
    assert_eq!(std::fs::read(&served.path).expect("reads"), served.bytes);
    assert_eq!(provision::elf_machine(&served.bytes), Some(AARCH64));
    assert_eq!(served.sha256, sha256_hex(&served.bytes));
}

#[then(expr = "the binary came from {string}, with no verification")]
fn came_from_unverified(world: &mut Provisioning, source: String) {
    let served = world.served();
    assert_eq!(served.source.as_str(), source);
    assert_eq!(served.verification(), None);
}

#[then("the binary is the core's own version")]
fn core_version(world: &mut Provisioning) {
    let served = world.served();
    assert_eq!(served.version, microvms_core::VERSION);
    let tag = format!("v{}", microvms_core::VERSION);
    let asked = world.state().calls.iter().any(|(_, asked)| *asked == tag);
    assert!(asked, "the release was never asked for {tag}");
}

#[then("the binary is the caller's bytes")]
fn caller_bytes(world: &mut Provisioning) {
    let (path, bytes) = world.binary.clone().expect("a caller's binary");
    let served = world.served();
    assert_eq!(served.bytes, bytes);
    assert_eq!(served.path, path);
}

#[then("the binary is the release's bytes")]
fn release_bytes(world: &mut Provisioning) {
    let asset = world.state().asset.clone();
    assert_eq!(world.served().bytes, asset);
}

#[then(expr = "the release was downloaded {int} time(s)")]
fn downloaded(world: &mut Provisioning, times: usize) {
    assert_eq!(world.downloads(), times, "{:#?}", world.state().calls);
}

#[then(expr = "the release was asked for tag {string}")]
fn asked_for(world: &mut Provisioning, tag: String) {
    let asked = world.state().calls.iter().any(|(_, asked)| *asked == tag);
    assert!(asked, "{:#?}", world.state().calls);
    assert_eq!(world.served().version, tag.trim_start_matches('v'));
}

#[then(expr = "provisioning failed with {string}")]
fn failed_with(world: &mut Provisioning, code: String) {
    assert_eq!(world.failure().code(), code, "{}", world.failure());
}

#[then(expr = "the failure mentions {string}")]
fn failure_mentions(world: &mut Provisioning, detail: String) {
    let message = world.failure().to_string();
    assert!(message.contains(&detail), "{message}");
}

#[then("nothing is cached")]
fn nothing_cached(world: &mut Provisioning) {
    let dir = world.state_dir.path().join("agentd");
    let mut left = Vec::new();
    if dir.exists() {
        for entry in walk(&dir) {
            left.push(entry);
        }
    }
    assert!(left.is_empty(), "the cache holds {left:?}");
}

fn walk(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in std::fs::read_dir(dir).expect("reads the cache dir") {
        let path = entry.expect("an entry").path();
        if path.is_dir() {
            files.extend(walk(&path));
        } else {
            files.push(path);
        }
    }
    files
}

#[then("SHA256SUMS was never fetched")]
fn no_checksums(world: &mut Provisioning) {
    let calls = world.state().calls.clone();
    assert!(
        calls.iter().all(|(operation, _)| *operation != "checksums"),
        "{calls:#?}"
    );
}

/// libtest flags `cargo test` may pass, which cucumber's own CLI would refuse.
const LIBTEST_FLAGS: [&str; 6] = [
    "--exact",
    "--nocapture",
    "--quiet",
    "--test-threads",
    "--color",
    "--ignored",
];

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let feature = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/features/provision.feature"
    );
    // A libtest filter naming something else selects nothing here, as libtest would.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_provision".contains(filter))
    {
        return;
    }
    let libtest = !filters.is_empty()
        || args
            .iter()
            .any(|arg| LIBTEST_FLAGS.iter().any(|flag| arg.starts_with(flag)));
    macro_rules! run {
        ($cucumber:expr) => {
            if libtest {
                $cucumber
                    .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
                    .run_and_exit(feature)
                    .await
            } else {
                $cucumber.run_and_exit(feature).await
            }
        };
    }
    let cucumber = Provisioning::cucumber().max_concurrent_scenarios(8);
    match std::env::var_os("CUCUMBER_JUNIT") {
        Some(path) => {
            let report = std::fs::File::create(&path).expect("the JUnit report file");
            run!(
                cucumber.with_writer(
                    writer::Basic::raw(std::io::stdout(), writer::Coloring::Never, 0)
                        .summarized()
                        .tee::<Provisioning, _>(writer::JUnit::for_tee(report, 0))
                        .normalized(),
                )
            )
        }
        None => run!(cucumber),
    }
}
