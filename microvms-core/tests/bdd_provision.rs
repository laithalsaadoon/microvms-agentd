// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for BIND-17 through BIND-20: `microvms_core::provision`
//! against a fake GitHub release.
//!
//! The scenarios live in `tests/features/provision.feature`, tagged with the requirement
//! each one verifies; this file is their step definitions and runner. It is a
//! `harness = false` test, so `cargo test` runs it on every CI system, and it writes a JUnit
//! report when `CUCUMBER_JUNIT` names a file (give that path absolutely).
//!
//! # The fake release sits at the subprocess seam
//!
//! [`provision::ReleaseFetch`] is the shipped verification policy: `gh release download`,
//! then `gh attestation verify`, or `curl` and the release's `SHA256SUMS` when `gh` cannot
//! download. It runs those tools through a [`provision::Runner`], and [`FakeRelease`] is a
//! runner that answers the same argv from memory: it writes the asset where `--output`
//! says, accepts or refuses the attestation, and serves, omits, or corrupts `SHA256SUMS`.
//! So a scenario exercises the real policy, including which tool runs after which failure,
//! and nothing opens a socket to GitHub.
//!
//! Only `provision.feature` runs here, named by file rather than by directory, so another
//! feature file added under `tests/features/` never runs against these step definitions.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use cucumber::{World, WriterExt, cli, given, then, when, writer};
use microvms_core::provision::{self, ReleaseFetch, Request, Runner};

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

/// What the release's `SHA256SUMS` asset holds for `curl`.
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

/// The fake release's behavior, and a log of every argv it was asked to run.
#[derive(Debug, Default)]
struct ReleaseState {
    asset: Vec<u8>,
    /// `gh` can download (installed and authenticated).
    gh_downloads: bool,
    /// `gh attestation verify` accepts the download.
    attested: bool,
    /// `curl` can download.
    curl_downloads: bool,
    sums: Sums,
    calls: Vec<Vec<String>>,
}

/// A [`Runner`] that answers `gh` and `curl` argv from a [`ReleaseState`].
#[derive(Clone, Debug, Default)]
struct FakeRelease(Arc<Mutex<ReleaseState>>);

fn flag<'a>(argv: &'a [String], name: &str) -> &'a str {
    let at = argv
        .iter()
        .position(|arg| arg == name)
        .unwrap_or_else(|| panic!("{name} in {argv:?}"));
    &argv[at + 1]
}

impl Runner for FakeRelease {
    fn run(&self, argv: &[String]) -> Result<(), String> {
        let mut state = self.0.lock().expect("the release lock");
        state.calls.push(argv.to_vec());
        let words: Vec<&str> = argv.iter().map(String::as_str).collect();
        match words.as_slice() {
            ["gh", "release", "download", ..] => {
                if !state.gh_downloads {
                    return Err(
                        "`gh` exited 4: To get started with GitHub CLI, please run: gh auth login"
                            .into(),
                    );
                }
                std::fs::write(flag(argv, "--output"), &state.asset).map_err(|e| e.to_string())
            }
            ["gh", "attestation", "verify", ..] => {
                if state.attested {
                    Ok(())
                } else {
                    Err("`gh` exited 1: no matching attestations found".into())
                }
            }
            ["curl", .., url] => {
                if !state.curl_downloads {
                    return Err("`curl` exited 6: Could not resolve host: github.com".into());
                }
                let dest = flag(argv, "--output");
                if url.ends_with("/agentd") {
                    return std::fs::write(dest, &state.asset).map_err(|e| e.to_string());
                }
                assert!(url.ends_with("/SHA256SUMS"), "unexpected curl {url}");
                let body = match state.sums {
                    Sums::Missing => {
                        return Err(
                            "`curl` exited 22: The requested URL returned error: 404".into()
                        );
                    }
                    Sums::Matching => format!("{}  agentd\n", sha256_hex(&state.asset)),
                    Sums::Mismatched => format!("{}  agentd\n", sha256_hex(b"something else")),
                    Sums::WithoutAgentd => {
                        format!("{}  microvm-x86_64.tar.gz\n", sha256_hex(b"x"))
                    }
                };
                std::fs::write(dest, body).map_err(|e| e.to_string())
            }
            other => panic!("the fake release does not run {other:?}"),
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
        self.release.0.lock().expect("the release lock")
    }

    fn provision(&mut self, version: Option<&str>, binary: Option<&Path>) {
        let env = self.env.clone();
        let lookup = move |name: &str| env.get(name).cloned();
        let fetch = ReleaseFetch(self.release.clone());
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
            .filter(|argv| {
                (argv[0] == "gh" && argv[1] == "release")
                    || (argv[0] == "curl"
                        && argv.last().is_some_and(|url| url.ends_with("/agentd")))
            })
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
    gh: bool,
    attested: bool,
    curl: bool,
    sums: Sums,
) {
    let mut state = world.state();
    state.asset = asset;
    state.gh_downloads = gh;
    state.attested = attested;
    state.curl_downloads = curl;
    state.sums = sums;
}

#[given("a release whose agentd is an aarch64 ELF that gh can attest")]
fn arm_attested(world: &mut Provisioning) {
    configure(
        world,
        elf(AARCH64, b"release"),
        true,
        true,
        true,
        Sums::Matching,
    );
}

#[given("a release whose agentd is an x86_64 ELF that gh can attest")]
fn x86_attested(world: &mut Provisioning) {
    configure(
        world,
        elf(X86_64, b"release"),
        true,
        true,
        true,
        Sums::Matching,
    );
}

#[given(
    "a release whose agentd is an aarch64 ELF that only curl can fetch, with a matching SHA256SUMS"
)]
fn arm_curl_only(world: &mut Provisioning) {
    configure(
        world,
        elf(AARCH64, b"release"),
        false,
        false,
        true,
        Sums::Matching,
    );
}

#[given(regex = r"^a release where (.+)$")]
fn broken_release(world: &mut Provisioning, what: String) {
    let asset = elf(AARCH64, b"release");
    match what.as_str() {
        "gh downloads bytes its attestation refuses" => {
            configure(world, asset, true, false, true, Sums::Matching)
        }
        "only curl can fetch and SHA256SUMS is gone" => {
            configure(world, asset, false, false, true, Sums::Missing)
        }
        "only curl can fetch and SHA256SUMS differs" => {
            configure(world, asset, false, false, true, Sums::Mismatched)
        }
        "only curl can fetch and SHA256SUMS omits it" => {
            configure(world, asset, false, false, true, Sums::WithoutAgentd)
        }
        "neither gh nor curl can download" => {
            configure(world, asset, false, false, false, Sums::Matching)
        }
        other => panic!("no release behaves as {other:?}"),
    }
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
    let asked = world.state().calls.iter().any(|argv| argv.contains(&tag));
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
    let asked = world.state().calls.iter().any(|argv| argv.contains(&tag));
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

#[then("curl was never run")]
fn no_curl(world: &mut Provisioning) {
    let calls = world.state().calls.clone();
    assert!(calls.iter().all(|argv| argv[0] != "curl"), "{calls:#?}");
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
