// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for the parsers daemon provisioning rests on (BIND-17 through
//! BIND-20): the version a caller passes, the release's `SHA256SUMS` body, the digest
//! record beside a cache entry, and the ELF header of a binary.
//!
//! `bolero::check!` runs each as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test provision_fuzz::<name> -p microvms-core -T 60s` (the
//! `provision` job in `.github/workflows/fuzz.yml`).
//!
//! Inputs are built from small alphabets and from header fields rather than taken as raw
//! bytes, so the random inputs of a stable `cargo test` reach `..`, separators, the ELF
//! magic, and both byte orders instead of spending every case on the refusal path.
//!
//! # What it checks
//!
//! * BIND-17: a version is either refused as `ERR_INVALID_ARG` or becomes one plain path
//!   component under the state directory, never a traversal, a separator, or a flag, since
//!   the version reaches the cache path, a `gh` argument, and a URL.
//! * BIND-18: a `SHA256SUMS` body passes the bytes only when its first `agentd` entry is
//!   exactly their digest, and one that names the digest of other bytes never passes.
//! * BIND-19: a digest record vouches for a cache entry only when it is JSON naming exactly
//!   the entry's version and digest; the record an install writes always parses back.
//! * BIND-20: a binary is accepted as aarch64 exactly when its first twenty bytes are an
//!   ELF header whose `e_machine`, read in the header's own byte order, is `0xB7`.

use std::path::{Component, Path};

use crate::ErrorKind;
use crate::provision::{
    REQUIRED_ELF_MACHINE, Verification, cache_path, elf_machine, normalize_version, not_aarch64,
    parse_record, record_json, sha256_hex, verify_sha256,
};

/// The characters a hostile or mistyped version is made of.
const VERSION_ALPHABET: &[u8] = b"v09.+-/\\ a\n*";

#[test]
fn version() {
    bolero::check!().with_type::<Vec<u8>>().for_each(|picks| {
        let version: String = picks
            .iter()
            .map(|pick| VERSION_ALPHABET[*pick as usize % VERSION_ALPHABET.len()] as char)
            .collect();
        match normalize_version(&version) {
            Ok(tag) => {
                assert!(!tag.is_empty() && tag.len() <= 64, "{tag:?}");
                assert!(tag.as_bytes()[0].is_ascii_alphanumeric(), "{tag:?}");
                assert!(!tag.contains(".."), "{tag:?}");
                assert!(
                    tag.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'+' | b'-')),
                    "{tag:?}"
                );
                let root = Path::new("/state");
                let path = cache_path(root, &tag);
                let below = path
                    .strip_prefix(root)
                    .expect("the cache stays under the root");
                assert_eq!(below.components().count(), 3, "{path:?}");
                assert!(
                    below
                        .components()
                        .all(|c| matches!(c, Component::Normal(_))),
                    "{path:?}"
                );
            }
            Err(error) => assert_eq!(error.kind(), ErrorKind::InvalidArg, "{version:?}"),
        }
    });
}

/// One `SHA256SUMS` line: whose digest, under which name, with which separator.
#[derive(Debug, bolero::TypeGenerator)]
struct Line {
    /// 0: these bytes' digest; 1: other bytes'; 2: an uppercase copy of these bytes';
    /// otherwise a short non-hex token.
    digest: u8,
    /// 0: `agentd`; 1: `*agentd`; otherwise another asset.
    name: u8,
    tab: bool,
}

#[test]
fn sums() {
    bolero::check!()
        .with_type::<(Vec<Line>, Vec<u8>)>()
        .for_each(|(lines, bytes)| {
            let digest = sha256_hex(bytes);
            let mut other = bytes.clone();
            other.push(0);
            let mut body = String::new();
            let mut first: Option<bool> = None;
            for line in lines {
                let (hex, matches) = match line.digest % 4 {
                    0 => (digest.clone(), true),
                    1 => (sha256_hex(&other), false),
                    2 => (digest.to_ascii_uppercase(), true),
                    _ => ("zz".to_string(), false),
                };
                let name = match line.name % 3 {
                    0 => "agentd",
                    1 => "*agentd",
                    _ => "microvm-x86_64.tar.gz",
                };
                if name.ends_with("agentd") && first.is_none() {
                    first = Some(matches);
                }
                let separator = if line.tab { "\t" } else { "  " };
                body.push_str(&format!("{hex}{separator}{name}\n"));
            }
            let verdict = verify_sha256(&body, "agentd", bytes);
            assert_eq!(verdict.is_ok(), first == Some(true), "{body:?}");
            // The digest of other bytes never vouches for these.
            let forged = format!("{}  agentd\n", sha256_hex(&other));
            assert!(verify_sha256(&forged, "agentd", bytes).is_err());
        });
}

#[test]
fn record() {
    bolero::check!()
        .with_type::<(u8, bool, bool, u8, Vec<u8>)>()
        .for_each(|(version_pick, right_version, right_digest, how, bytes)| {
            let version = format!("0.{}.0", version_pick % 3);
            let digest = sha256_hex(bytes);
            let mut other = bytes.clone();
            other.push(0);
            let named_version = if *right_version {
                version.clone()
            } else {
                "9.9.9".to_string()
            };
            let named_digest = if *right_digest {
                digest.clone()
            } else {
                sha256_hex(&other)
            };
            let verification = match how % 3 {
                0 => "attestation",
                1 => "checksum",
                _ => "none",
            };
            let text = serde_json::json!({
                "version": named_version,
                "sha256": named_digest,
                "verification": verification,
            })
            .to_string();
            let vouches = parse_record(&text, &version, &digest);
            assert_eq!(
                vouches.map(Verification::as_str),
                (*right_version && *right_digest && verification != "none").then_some(verification),
                "{text}"
            );
            // Truncated or garbled bodies vouch for nothing.
            for cut in 0..text.len() {
                assert_eq!(parse_record(&text[..cut], &version, &digest), None);
            }
            for written_how in [Verification::Attestation, Verification::Checksum] {
                let written = record_json(&version, &digest, written_how);
                assert_eq!(parse_record(&written, &version, &digest), Some(written_how));
            }
        });
}

/// A binary's first bytes, built from the header's fields.
#[derive(Debug, bolero::TypeGenerator)]
struct Binary {
    magic: bool,
    /// `EI_DATA`: 1 is little-endian, anything else is read as big.
    data: u8,
    machine: u16,
    /// Cut the binary to this many bytes when below twenty.
    cut: u8,
    tail: Vec<u8>,
}

#[test]
fn elf() {
    bolero::check!().with_type::<Binary>().for_each(|binary| {
        let mut bytes = vec![0u8; 20];
        if binary.magic {
            bytes[..4].copy_from_slice(b"\x7fELF");
        }
        bytes[5] = binary.data;
        let field = if binary.data == 1 {
            binary.machine.to_le_bytes()
        } else {
            binary.machine.to_be_bytes()
        };
        bytes[18..20].copy_from_slice(&field);
        bytes.extend_from_slice(&binary.tail);
        let whole = (binary.cut as usize) >= 20;
        if !whole {
            bytes.truncate(binary.cut as usize);
        }
        let expected = (binary.magic && whole).then_some(binary.machine);
        assert_eq!(elf_machine(&bytes), expected, "{binary:?}");
        assert_eq!(
            not_aarch64(&bytes).is_none(),
            expected == Some(REQUIRED_ELF_MACHINE),
            "{binary:?}"
        );
    });
}
