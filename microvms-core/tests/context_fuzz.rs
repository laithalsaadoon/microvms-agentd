// SPDX-License-Identifier: Apache-2.0
//! The fuzz harnesses for IMAGE-6, IMAGE-7 and IMAGE-8: the `.dockerignore` matcher, the
//! context-extended content hash, the deterministic artifact, and the artifact key.
//!
//! `bolero::check!` runs each as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under `cargo +nightly bolero test <name> -p microvms-core -T 60s`.
//!
//! # The oracles are independent of the code under test
//!
//! * The matcher is checked against moby's own translation of a pattern into a regular
//!   expression (`patternmatcher.Pattern.compile`), rebuilt here on the `regex` crate. The
//!   matcher under test is hand-written and shares nothing with it. Patterns are drawn from a
//!   grammar that avoids the few spellings where Go's and Rust's regex syntax disagree
//!   (nested `[` and `--` inside a class, `^` outside one).
//! * The no-context hash is checked against the pre-#221 algorithm, restated here, for
//!   arbitrary inputs rather than one pinned vector.
//! * A context built from the same entries in any order must hash and zip identically, and
//!   any single-byte change to an entry must change the hash.

use microvms_core::control::artifact::{
    ProjectFiles, artifact_content_hash, artifact_content_hash_with_context,
    build_artifact_with_context,
};
use microvms_core::control::context::{BuildContext, ContextEntry, IgnoreRules, glob_match};
use microvms_core::control::ensure::artifact_key;

// ── the matcher ─────────────────────────────────────────────────────────────

/// One token of a generated pattern.
#[derive(Debug, bolero::TypeGenerator)]
enum Tok {
    Char(u8),
    Slash,
    Star,
    DoubleStar,
    Question,
    Class {
        negated: bool,
        items: Vec<(u8, bool)>,
    },
    Escaped(u8),
}

/// The characters patterns and paths are drawn from: few, so they collide.
const ALPHABET: &[u8] = b"ab.x_-";

fn pick(byte: u8) -> char {
    ALPHABET[usize::from(byte) % ALPHABET.len()] as char
}

fn class_char(byte: u8) -> char {
    b"abx0"[usize::from(byte) % 4] as char
}

fn render_pattern(tokens: &[Tok]) -> String {
    let mut out = String::new();
    for token in tokens.iter().take(12) {
        match token {
            Tok::Char(b) => out.push(pick(*b)),
            Tok::Slash => out.push('/'),
            Tok::Star => out.push('*'),
            Tok::DoubleStar => out.push_str("**"),
            Tok::Question => out.push('?'),
            Tok::Class { negated, items } => {
                out.push('[');
                if *negated {
                    out.push('^');
                }
                let items: Vec<_> = items.iter().take(3).collect();
                if items.is_empty() {
                    out.push('a');
                }
                for (byte, range) in items {
                    let lo = class_char(*byte);
                    out.push(lo);
                    if *range {
                        out.push('-');
                        out.push('x');
                    }
                }
                out.push(']');
            }
            // Only the characters a pattern means specially: moby hands any other escape to
            // the regex engine as-is (`\a` is a bell there), which is no spelling a caller
            // means, and the matcher reads it as the literal character instead.
            Tok::Escaped(b) => {
                out.push('\\');
                out.push(b"*?[.\\"[usize::from(*b) % 5] as char);
            }
        }
    }
    out
}

fn render_path(segments: &[Vec<u8>]) -> String {
    let segments: Vec<String> = segments
        .iter()
        .take(4)
        .map(|segment| {
            let text: String = segment.iter().take(4).map(|b| pick(*b)).collect();
            if text.is_empty() {
                "a".to_string()
            } else {
                text
            }
        })
        .collect();
    if segments.is_empty() {
        "a".to_string()
    } else {
        segments.join("/")
    }
}

/// `filepath.Clean` plus the leading-`/` strip, as moby applies them to a pattern.
fn clean(pattern: &str) -> String {
    let rooted = pattern.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for part in pattern.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|last| *last != "..") {
                    parts.pop();
                } else if !rooted {
                    parts.push("..");
                }
            }
            other => parts.push(other),
        }
    }
    let joined = parts.join("/");
    if joined.is_empty() {
        if rooted {
            "/".to_string()
        } else {
            ".".to_string()
        }
    } else {
        joined
    }
}

/// moby's `Pattern.compile`, on the `regex` crate.
fn oracle(pattern: &str) -> Option<regex::Regex> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut re = String::from("^");
    let mut exact = true;
    let mut i = 0;
    while i < chars.len() {
        let ch = chars[i];
        i += 1;
        match ch {
            '*' => {
                if chars.get(i) == Some(&'*') {
                    i += 1;
                    if chars.get(i) == Some(&'/') {
                        i += 1;
                    }
                    if i >= chars.len() {
                        re.push_str(".*");
                    } else {
                        re.push_str("(.*/)?");
                    }
                } else {
                    re.push_str("[^/]*");
                }
                exact = false;
            }
            '?' => {
                re.push_str("[^/]");
                exact = false;
            }
            '.' | '+' | '(' | ')' | '|' | '{' | '}' | '$' => {
                re.push('\\');
                re.push(ch);
            }
            '\\' => match chars.get(i) {
                Some(next) => {
                    re.push('\\');
                    re.push(*next);
                    i += 1;
                    exact = false;
                }
                None => re.push_str("\\\\"),
            },
            other => re.push(other),
        }
    }
    let _ = exact;
    re.push('$');
    regex::Regex::new(&re).ok()
}

fn oracle_matches_or_parent(re: &regex::Regex, path: &str) -> bool {
    if re.is_match(path) {
        return true;
    }
    let parts: Vec<&str> = path.split('/').collect();
    (1..parts.len()).any(|end| re.is_match(&parts[..end].join("/")))
}

#[derive(Debug, bolero::TypeGenerator)]
struct GlobCase {
    rules: Vec<(bool, Vec<Tok>)>,
    path: Vec<Vec<u8>>,
    /// Put the path under a directory the first pattern names, so a pattern matching a
    /// parent directory — `node_modules` excluding `node_modules/x` — is exercised often
    /// rather than by luck.
    nest: bool,
}

/// A path the pattern's literal reading names: every wildcard and class read as `a`.
fn literalize(pattern: &str) -> String {
    let mut out = String::new();
    let mut chars = pattern.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '*' | '?' => out.push('a'),
            '\\' => out.extend(chars.next()),
            '[' => {
                for inner in chars.by_ref() {
                    if inner == ']' {
                        break;
                    }
                }
                out.push('a');
            }
            other => out.push(other),
        }
    }
    let cleaned = clean(&out);
    if cleaned == "." || cleaned == "/" {
        "a".to_string()
    } else {
        cleaned
    }
}

#[test]
fn ignore_rules_agree_with_mobys_regex_translation() {
    bolero::check!()
        .with_type::<GlobCase>()
        .for_each(|case: &GlobCase| {
            let mut path = render_path(&case.path);
            if case.nest
                && let Some((_, tokens)) = case.rules.first()
            {
                let parent = literalize(&render_pattern(tokens));
                if !parent.split('/').any(|segment| segment == "..") {
                    path = format!("{parent}/{path}");
                }
            }
            let mut text = String::new();
            let mut compiled = Vec::new();
            for (exception, tokens) in case.rules.iter().take(4) {
                let pattern = render_pattern(tokens);
                if pattern.is_empty() {
                    continue;
                }
                let Some(re) = oracle(&clean(&pattern)) else {
                    // A pattern the translation cannot compile is one Docker refuses too.
                    let line = if *exception {
                        format!("!{pattern}")
                    } else {
                        pattern
                    };
                    assert!(
                        IgnoreRules::parse(&format!("{line}\n")).is_err(),
                        "IMAGE-7: {line:?} is malformed but parsed"
                    );
                    return;
                };
                assert_eq!(
                    glob_match(&clean(&pattern), &path),
                    re.is_match(&path),
                    "IMAGE-7: {pattern:?} against {path:?}"
                );
                text.push_str(if *exception { "!" } else { "" });
                text.push_str(&pattern);
                text.push('\n');
                compiled.push((*exception, re));
            }
            let rules = IgnoreRules::parse(&text).expect("every line compiled in the oracle");
            let mut excluded = false;
            for (exception, re) in &compiled {
                if oracle_matches_or_parent(re, &path) {
                    excluded = !exception;
                }
            }
            assert_eq!(
                rules.excludes(&path),
                excluded,
                "IMAGE-7: the last matching line decides for {path:?} under\n{text}"
            );
        });
}

// ── the hash and the artifact ───────────────────────────────────────────────

/// The algorithm `artifact_content_hash` implemented before #221, restated.
fn legacy_hash(binary: &[u8], dockerfile: &str, project: Option<&ProjectFiles>) -> String {
    use sha2::{Digest as _, Sha256};
    let mut hasher = Sha256::new();
    hasher.update((binary.len() as u64).to_be_bytes());
    hasher.update(binary);
    hasher.update((dockerfile.len() as u64).to_be_bytes());
    hasher.update(dockerfile.as_bytes());
    if let Some(project) = project {
        for (name, bytes) in [
            (project.ecosystem.manifest_name(), &project.manifest),
            (project.ecosystem.lockfile_name(), &project.lockfile),
        ] {
            hasher.update((name.len() as u64).to_be_bytes());
            hasher.update(name.as_bytes());
            hasher.update((bytes.len() as u64).to_be_bytes());
            hasher.update(bytes);
        }
    }
    const_hex::encode(hasher.finalize())
}

#[derive(Debug, bolero::TypeGenerator)]
struct HashCase {
    binary: Vec<u8>,
    dockerfile: String,
    project: Option<(u8, Vec<u8>, Vec<u8>)>,
    entries: Vec<(Vec<u8>, bool, Vec<u8>)>,
    flip: (u8, u8),
    /// Change the entry's mode instead of a byte of it.
    flip_mode: bool,
    rotate: u8,
}

fn project_of(project: &Option<(u8, Vec<u8>, Vec<u8>)>) -> Option<ProjectFiles> {
    project
        .as_ref()
        .map(|(which, manifest, lockfile)| ProjectFiles {
            ecosystem: microvms_core::control::Ecosystem::ALL[usize::from(*which) % 3],
            manifest: manifest.clone(),
            lockfile: lockfile.clone(),
        })
}

#[test]
fn the_context_hash_extends_the_legacy_one_and_follows_every_entry() {
    bolero::check!()
        .with_type::<HashCase>()
        .for_each(|case: &HashCase| {
            let project = project_of(&case.project);
            // IMAGE-6: with no context, the digest is the pre-#221 one, for any inputs.
            let legacy = legacy_hash(&case.binary, &case.dockerfile, project.as_ref());
            assert_eq!(
                artifact_content_hash_with_context(
                    &case.binary,
                    &case.dockerfile,
                    project.as_ref(),
                    None
                ),
                legacy
            );
            assert_eq!(
                artifact_content_hash(&case.binary, &case.dockerfile, project.as_ref()),
                legacy
            );

            // Unique names, drawn from `a`..`z` so none collides with an ecosystem file.
            let mut entries: Vec<ContextEntry> = Vec::new();
            for (name, exec, bytes) in case.entries.iter().take(6) {
                let name: String = name
                    .iter()
                    .take(6)
                    .map(|b| (b'a' + b % 26) as char)
                    .collect();
                let name = format!("ctx/{name}e");
                if entries.iter().any(|entry| entry.name == name) {
                    continue;
                }
                entries.push(ContextEntry {
                    name,
                    mode: if *exec { 0o755 } else { 0o644 },
                    bytes: bytes.clone(),
                });
            }
            if entries.is_empty() {
                return;
            }
            let context = BuildContext::from_entries(entries.clone()).expect("valid names");
            let mut rotated = entries.clone();
            let turns = usize::from(case.rotate) % rotated.len();
            rotated.rotate_left(turns);
            let same = BuildContext::from_entries(rotated).expect("valid names");
            let hash = |context: &BuildContext| {
                artifact_content_hash_with_context(
                    &case.binary,
                    &case.dockerfile,
                    project.as_ref(),
                    Some(context),
                )
            };
            assert_eq!(
                hash(&context),
                hash(&same),
                "IMAGE-6: read order is not identity"
            );
            assert_ne!(hash(&context), legacy, "IMAGE-6: a context is identity");

            // IMAGE-7: equal inputs zip to equal bytes.
            let zip = |context: &BuildContext| {
                build_artifact_with_context(
                    &case.binary,
                    &case.dockerfile,
                    project.as_ref(),
                    Some(context),
                )
                .expect("zips")
            };
            assert_eq!(
                zip(&context),
                zip(&same),
                "IMAGE-7: one set of inputs, one object"
            );

            // Any single-byte change to an entry is a new identity.
            let mut changed = entries.clone();
            let target = usize::from(case.flip.0) % changed.len();
            let entry = &mut changed[target];
            if case.flip_mode {
                entry.mode = if entry.mode == 0o755 { 0o644 } else { 0o755 };
            } else if entry.bytes.is_empty() {
                entry.bytes.push(case.flip.1);
            } else {
                let at = usize::from(case.flip.1) % entry.bytes.len();
                entry.bytes[at] ^= 0x01;
            }
            let changed = BuildContext::from_entries(changed).expect("valid names");
            assert_ne!(
                hash(&context),
                hash(&changed),
                "IMAGE-6: every byte and the mode count"
            );
        });
}

// ── the key ─────────────────────────────────────────────────────────────────

#[derive(Debug, bolero::TypeGenerator)]
struct KeyCase {
    prefix: Option<Vec<u8>>,
    name: Vec<u8>,
}

#[test]
fn the_artifact_key_is_the_prefix_the_name_and_artifact_zip() {
    bolero::check!()
        .with_type::<KeyCase>()
        .for_each(|case: &KeyCase| {
            let name: String = case
                .name
                .iter()
                .take(20)
                .map(|b| b"abc-_0"[usize::from(*b) % 6] as char)
                .collect();
            if name.is_empty() {
                return;
            }
            let prefix: Option<String> = case.prefix.as_ref().map(|bytes| {
                bytes
                    .iter()
                    .take(20)
                    .map(|b| b"ab/_."[usize::from(*b) % 5] as char)
                    .collect()
            });
            let key = artifact_key(prefix.as_deref(), &name);
            assert!(
                key.ends_with(&format!("{name}/artifact.zip")),
                "IMAGE-8: {key:?}"
            );
            assert!(!key.starts_with('/'), "IMAGE-8: {key:?}");
            assert!(!key.contains("//"), "IMAGE-8: {key:?}");
            let trimmed = prefix.as_deref().unwrap_or("").trim_matches('/');
            let expected = if trimmed.is_empty() {
                format!("{name}/artifact.zip")
            } else {
                format!("{trimmed}/{name}/artifact.zip")
            };
            if !trimmed.contains("//") {
                assert_eq!(key, expected, "IMAGE-8");
            }
        });
}
