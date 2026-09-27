// SPDX-License-Identifier: Apache-2.0
//! **CLI-2's manifest half, and the print-macro scan.** The CLI reaches AWS through
//! `microvms-core` and through nothing else, asserted from the manifest here and from the source
//! by the compiler and the ratchet.
//!
//! # What's here
//!
//! A **denylist** of crates that would mean a second path to AWS or HTTP, read out of
//! `cargo metadata`. Dependencies are welcome in this crate: a good, maintained crate beats
//! hand-rolled code, and nothing here polices the manifest's size. What the manifest must never
//! grow is a crate that can open a socket to AWS or sign a request without going through
//! `microvms-core`; those are named below, and `cargo metadata` sees them however they are
//! spelled into the manifest.
//!
//! A **source scan** for print macros, over the shipping code's tokens with test regions taken
//! out.
//!
//! # Where the source half of CLI-2 went
//!
//! This file used to scan the source for control-plane operation names and core's constructors
//! as substrings (#273 retired it). A substring can't tell an alias, a glob import or a
//! fully qualified call from prose, and it read only this crate. Each rule now has the tool that
//! resolves what it names:
//!
//! - A transport call, and a core door outside `src/seam.rs`, is a clippy
//!   `disallowed-methods`/`disallowed-types` entry in `clippy.toml`, which resolves paths the
//!   way the compiler does. The bindings carry the transport bans too.
//! - An operation name as a literal is the ratchet's `operation-literal` rule
//!   (`ratchet/rules/operation-literal.yml`), over the CLI and both bindings.
//!
//! # The print scan matches code, not prose
//!
//! `test_cli.py:269` records writing a check like this the naive way first, as a substring
//! search: it "went red immediately, on a comment explaining *why* a region check is local". A
//! guard that fires on its own documentation gets deleted. So the scan reads the lexer's tokens:
//!
//! 1. **Comments** never reach the token stream.
//! 2. **String literals** are single tokens the walk doesn't enter, because a message that
//!    explains a macro isn't a call to one.
//! 3. **Test regions** are skipped: `src/guards.rs` is test-only, and so is each file's
//!    `mod tests`.
//!
//! (cli.py line numbers resolve at `git show 'c4d396e^:clients/python/src/microvms_agentd/cli.py'`, the retired oracle.)

use std::path::{Path, PathBuf};

/// Crate names that would mean a second path to AWS or to HTTP.
///
/// A denylist of the hazard, not a cap on the manifest: dependencies are welcome here, and
/// nothing polices how many this crate takes. What CLI-2 forbids is a crate that lets a
/// handler reach AWS without going through `microvms-core` — an HTTP client, a signer, a
/// credential chain. A reviewer reading a diff that added `reqwest` sees why it is refused
/// rather than only that a name matched.
const FORBIDDEN: [&str; 12] = [
    "reqwest",
    "hyper",
    "hyper-util",
    "http",
    "aws-config",
    "aws-sdk-s3",
    "aws-sdk-sts",
    "aws-sigv4",
    "aws-credential-types",
    "aws-smithy-runtime",
    "rusoto_core",
    "ureq",
];

/// This crate's package, out of `cargo metadata`.
fn package() -> cargo_metadata::Package {
    let metadata = cargo_metadata::MetadataCommand::new()
        .manifest_path(manifest_path())
        // The whole workspace, because the dependency-direction test in the sibling file needs the
        // other members and building the graph twice is the slow part.
        .exec()
        .expect("cargo metadata runs");
    metadata
        .packages
        .into_iter()
        .find(|package| package.name.as_str() == "microvms-cli")
        .expect("microvms-cli is a workspace member")
}

fn manifest_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml")
}

/// **No second path to AWS.** The direct dependency set carries none of the crates that
/// could open a socket to AWS or sign a request outside `microvms-core`.
///
/// A denylist, deliberately: dependencies are welcome in this crate, and this test says
/// nothing about how many there are or what they are for. `cargo metadata` is the source
/// rather than the TOML text, because it resolves path dependencies and workspace
/// inheritance — an edge added through a renamed key or a `[target.'cfg(...)']` table is
/// still an edge this sees, and a hand-parsed manifest would check the file instead of the
/// build.
///
/// **Falsification** — add `reqwest = "0.13"` to `microvms-cli/Cargo.toml` and this goes red
/// naming it. Verified; see the packet's guard proofs.
///
/// Redundant once the CLI's allowed set in `arch/placement.toml` is asserted exactly, since
/// every name above is outside it. That happens when #260 clears the CLI's placement drift, and
/// the change that does it should delete this test (#285 asked for the deletion and left it to
/// that change). The sets don't read dev dependencies, which this test does, so that change
/// should also say whether a dev-only HTTP client still needs refusing.
#[test]
fn no_direct_dependency_is_a_second_path_to_aws() {
    let package = package();
    let actual: Vec<String> = package
        .dependencies
        .iter()
        .filter(|dependency| {
            matches!(
                dependency.kind,
                cargo_metadata::DependencyKind::Normal
                    | cargo_metadata::DependencyKind::Development
            )
        })
        .map(|dependency| dependency.name.clone())
        .collect();

    for forbidden in FORBIDDEN {
        assert!(
            !actual.iter().any(|name| name == forbidden),
            "{forbidden} is a direct dependency of the CLI, which gives it a second path to AWS \
             or to HTTP — the requirement CLI-2 is. Every AWS call belongs in microvms-core; if \
             this crate needs something the core does not expose, grow the core's API rather \
             than a parallel transport."
        );
    }
}

/// The files the scan covers, each cut at its test region.
///
/// A file whose own inner attributes include `#![cfg(test)]` is skipped entirely: it does not
/// ship, and `src/guards.rs` is exactly that. It scripts a fake control plane and captures what a
/// command writes, none of which is code a user runs.
///
/// The skip is deliberately keyed on the **inner** attribute (`#![cfg(test)]`, whole file) rather
/// than the outer one (`#[cfg(test)]`, next item), because those are different claims and only the
/// first means "none of this ships".
fn scannable_sources() -> Vec<(PathBuf, String)> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files: Vec<PathBuf> = Vec::new();
    collect_rust_files(&root, &mut files);
    assert!(
        files.len() >= 10,
        "the scan found almost nothing: {files:?}"
    );
    let scanned: Vec<(PathBuf, String)> = files
        .into_iter()
        .filter_map(|path| {
            let text = std::fs::read_to_string(&path).expect("a readable source file");
            if is_whole_file_test_module(&text) {
                return None;
            }
            Some((path, production_region(&text)))
        })
        .collect();
    assert!(
        scanned.len() >= 10,
        "the skip rule excluded too much; only {} files are scanned",
        scanned.len()
    );
    // The file floor proves the walk read files; this proves the token walk reads their code. A
    // walk that returned nothing would pass the scan below, so the seam, whose errors are built
    // with `format!` calls, must still show one. Its Falsification note is on the print-macro
    // scan's doc.
    let seam = scanned
        .iter()
        .find(|(path, _)| path.file_name().is_some_and(|name| name == "seam.rs"));
    assert!(
        seam.is_some_and(|(_, source)| macro_calls(source)
            .iter()
            .any(|(_, name)| name == "format")),
        "the sentinel failed: src/seam.rs {}, so the scan is reading nothing",
        if seam.is_some() {
            "no longer shows a `format!` call to the token walk"
        } else {
            "isn't among the scanned files"
        },
    );
    scanned
}

/// Whether the `#[cfg(test)]` at `index` opens an inline test **module** rather than gating a
/// single item.
///
/// Only a `mod` opens a region. A gated `fn`, `struct`, or `mod x;` declaration does not, and
/// treating one as a cut point is how this test's own first draft was going to exclude eight
/// hundred lines of handler from the scan — the guard reported it, which is the whole point of the
/// `regions <= 1` assertion existing beside the scan rather than being assumed.
fn opens_a_test_region(lines: &[&str], index: usize, line: &str) -> bool {
    if line.trim_start() != "#[cfg(test)]" || line.starts_with(' ') {
        return false;
    }
    lines
        .get(index + 1)
        .map(|next| next.trim_start())
        .is_some_and(|next| next.starts_with("mod ") && next.ends_with('{'))
}

/// Whether the whole file is test-only, by an inner `#![cfg(test)]` attribute.
///
/// Only the file's own inner attributes count, the ones before its first item (`//!` docs lex as
/// `#![doc = ..]`, so they're among them). An inner attribute further down belongs to the module
/// or block it opens, so `mod helpers { #![cfg(test)] }` at the end of a handler file gates that
/// module and nothing else. Matching the line anywhere dropped the whole file from the scan
/// (#273 review).
fn is_whole_file_test_module(text: &str) -> bool {
    use proc_macro2::{Delimiter, TokenTree};
    let mut trees = lex(text).into_iter();
    loop {
        let (
            Some(TokenTree::Punct(hash)),
            Some(TokenTree::Punct(bang)),
            Some(TokenTree::Group(body)),
        ) = (trees.next(), trees.next(), trees.next())
        else {
            return false;
        };
        if hash.as_char() != '#' || bang.as_char() != '!' || body.delimiter() != Delimiter::Bracket
        {
            return false;
        }
        let attribute: String = body.stream().to_string().split_whitespace().collect();
        if attribute == "cfg(test)" {
            return true;
        }
    }
}

/// Every `.rs` file under `dir`, recursively.
fn collect_rust_files(dir: &Path, into: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        if path.is_dir() {
            collect_rust_files(&path, into);
        } else if path.extension().is_some_and(|ext| ext == "rs") {
            into.push(path);
        }
    }
}

/// The part of a source file that ships: everything before its inline test region.
///
/// The test-region cut exists because each file's `mod tests` may print and script fakes. The
/// cut is at the *first* `#[cfg(test)]` at column zero
/// that opens a module, and a separate assertion below pins that each file has at most one.
/// Otherwise a file with an inline `#[cfg(test)]` helper near the top would have almost all of its
/// production code excluded from the scan, silently.
fn production_region(text: &str) -> String {
    let lines: Vec<&str> = text.lines().collect();
    // The first column-zero `#[cfg(test)]` that introduces an inline test *region* — not one that
    // merely gates a `mod x;` declaration.
    //
    // It's the second defect this file's own guard found in it: `main.rs` gates `mod guards;`
    // with `#[cfg(test)]` at line 35, so a naive cut there excluded four hundred lines of
    // dispatcher from the scan and reported a clean pass over the module declarations. A gated
    // `mod x;` doesn't begin a test region: the gated file is its own file, and is either
    // scanned or skipped on its own merits.
    let cut = lines
        .iter()
        .enumerate()
        .find(|(index, line)| opens_a_test_region(&lines, *index, line))
        .map(|(index, _)| index)
        .unwrap_or(usize::MAX);
    lines[..cut.min(lines.len())].join("\n")
}

/// `text` as the compiler's tokens, or a failed test: a file the lexer rejects isn't read as
/// empty.
fn lex(text: &str) -> proc_macro2::TokenStream {
    text.parse().unwrap_or_else(|error| {
        panic!("the scan couldn't lex a source file, and would read nothing from it: {error:?}")
    })
}

/// Each macro call in `source`, as its 1-based line and the macro's name.
///
/// Read from the tokens `proc-macro2` lexes, the compiler's token rules as a library, because
/// every hand-rolled reading of this source got a corner wrong: a `//` inside a literal, a `\"`
/// escape, and last a `'"'` char literal, which a quote tracker took for an opening delimiter.
/// That one inverted the rest of the file, so code read as a string and strings read as code
/// (#277 review). A lexer has no such state to lose. Then a text match on the stripped source
/// (`print!(`) passed `print! (..)`, `println! {..}` and `eprintln![..]` (#273 review); a call
/// is the macro's name and a `!` token, whatever follows.
///
/// # Why a macro named in a literal or a comment isn't a call
///
/// A message that *names* a print macro, to say why a write goes through `Output` instead, isn't
/// a write. A guard that demanded its deletion is a guard someone deletes instead, which is
/// `test_cli.py:269`'s lesson exactly. Comments never reach the token stream, a doc comment
/// arrives as a `#[doc = "..."]` whose text is a literal, and a literal is one token, so none of
/// them is walked into.
fn macro_calls(source: &str) -> Vec<(usize, String)> {
    use proc_macro2::TokenTree;
    fn walk(stream: proc_macro2::TokenStream, found: &mut Vec<(usize, String)>) {
        let mut trees = stream.into_iter().peekable();
        while let Some(tree) = trees.next() {
            match tree {
                TokenTree::Ident(ident) => {
                    if matches!(trees.peek(), Some(TokenTree::Punct(bang)) if bang.as_char() == '!')
                    {
                        found.push((ident.span().start().line, ident.to_string()));
                    }
                }
                TokenTree::Group(group) => walk(group.stream(), found),
                TokenTree::Punct(_) | TokenTree::Literal(_) => {}
            }
        }
    }
    let mut found = Vec::new();
    walk(lex(source), &mut found);
    found
}

/// The macros that write to stdout or stderr and panic when the reader has gone.
const PRINT_MACROS: [&str; 4] = ["print", "println", "eprint", "eprintln"];

/// The token walk keeps code that follows a quote inside a char literal or a raw string, reads a
/// print macro however it's spelled, and doesn't read one named in a literal or a comment.
///
/// A `'"'` read as a string delimiter flips a hand-rolled scanner's state: the code after it is
/// skipped and the file's next real string is read as code. The scanner this file used to have
/// did exactly that, and the source scan passed a `ControlPlane::new` placed in `suspend` after a
/// `let _quote = '"';` (#277 review).
///
/// **Falsification**: 2026-09-26, against the hand-rolled scanner this failed. 2026-09-27: a walk
/// that doesn't enter groups fails it, finding none of the calls in the function body; restored
/// after.
#[test]
fn the_scan_reads_a_quote_in_a_char_literal_as_a_literal() {
    let source = r##"fn suspend() {
    let _quote = '"';
    print! ("a");
    let message = "println!(\"in a literal\")"; // and eprintln!("in a comment")
    let _raw = r#"a raw string with a " quote, then eprint!("x")"#;
    println! {"b"};
    eprintln!["c"];
    std::eprint!("d");
}
"##;
    let calls = macro_calls(source);
    assert_eq!(
        calls,
        vec![
            (3, "print".to_string()),
            (6, "println".to_string()),
            (7, "eprintln".to_string()),
            (8, "eprint".to_string()),
        ],
        "the walk misread a call, a literal or a comment in:\n{source}"
    );
}

/// An inner `#![cfg(test)]` skips a file only when it's among the file's own attributes.
///
/// **Falsification**: 2026-09-27. With the skip matching the line anywhere in the file (the
/// version before), the nested-module assertion fails; restored after. Under that version a
/// `print!` in `suspend` beside a `mod scratch_helpers { #![cfg(test)] }` passed the print scan.
#[test]
fn only_a_file_level_cfg_test_skips_a_file() {
    let nested = "// SPDX-License-Identifier: Apache-2.0\n//! A handler.\nfn suspend() {}\nmod scratch_helpers {\n    #![cfg(test)]\n}\n";
    assert!(
        !is_whole_file_test_module(nested),
        "an inner attribute on a nested module took the whole file out of the scan"
    );
    let whole = "// SPDX-License-Identifier: Apache-2.0\n//! Test-only guards.\n#![allow(dead_code)]\n#![cfg(test)]\n\nuse std::sync::Arc;\n";
    assert!(
        is_whole_file_test_module(whole),
        "a file-level #![cfg(test)] after the module docs wasn't read as test-only"
    );
    assert!(
        !is_whole_file_test_module(""),
        "an empty file isn't test-only"
    );
}

/// Each shipping file opens at most one inline test region, so the scan's cut cannot hide code.
///
/// Without this the scan is quietly defeatable: a second `#[cfg(test)] mod` placed near the top of
/// a file would exclude everything after it, and the guard would report a clean pass over three
/// lines of a four-hundred-line module. The `kept` ratio below is the same worry from the other
/// side — it fails if the one region starts so early that most of the file is outside the scan.
#[test]
fn the_scan_cut_cannot_hide_production_code() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    collect_rust_files(&root, &mut files);
    for path in files {
        let text = std::fs::read_to_string(&path).expect("readable");
        if is_whole_file_test_module(&text) {
            continue;
        }
        let lines: Vec<&str> = text.lines().collect();
        let regions = lines
            .iter()
            .enumerate()
            .filter(|(index, line)| opens_a_test_region(&lines, *index, line))
            .count();
        assert!(
            regions <= 1,
            "{} opens {regions} inline test regions; the thinness scan cuts at the first, so a \
             second would exclude shipping code from the scan without saying so. Put every \
             test-only helper inside the one `mod tests`.",
            path.display(),
        );
        // And the scan really did keep most of the file, so the cut is not silently swallowing it.
        let kept = production_region(&text).lines().count();
        let total = lines.len();
        if regions == 1 && total > 50 {
            assert!(
                kept * 4 > total,
                "{} keeps only {kept} of {total} lines in the scanned region — the cut is in the \
                 wrong place",
                path.display(),
            );
        }
    }
}

/// No print macro anywhere in the CLI's production code, `main.rs` and `envelope.rs` included.
///
/// Two requirements rest on it. CLI-4's structural half: "exactly one envelope on stdout" is only
/// enforceable if there is one place that writes to stdout, `Output`. CLI-7: `print!` and
/// `eprintln!` panic when their stream's reader has gone, which is how
/// `microvm keepalive --help | head` exited 101 (#216); `Output` records the closed reader
/// instead. Clap's help and `constants --emit-json` go through `Output::raw`.
///
/// A guard on the *shape* of the code rather than on its behaviour, beside the crate-level
/// `clippy::print_stdout`/`print_stderr` deny in `main.rs`: the lint catches a new macro at
/// compile time, and this test catches one even where someone relaxed the lint.
///
/// **Falsification** — 2026-09-24. Restoring `print!("{error}")` for clap's help in `main.rs`
/// turned this red (and the `@CLI-7` help scenarios in `tests/features/closed_output.feature`);
/// restored after. Making the token walk record nothing turns it red with the seam sentinel's
/// message in `scannable_sources`, and a `let _quote = '"';` before a `print!` on the same line
/// still turns it red (under the hand-rolled scanner it passed). 2026-09-27: `print! (..)`,
/// `println! {..}` and a nested `#![cfg(test)]` beside a `print!` each turn it red (the substring
/// match and the any-line skip passed all three). All are registry entries.
#[test]
fn no_production_code_writes_with_a_print_macro() {
    for (path, source) in scannable_sources() {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        for (line, macro_name) in macro_calls(&source) {
            assert!(
                !PRINT_MACROS.contains(&macro_name.as_str()),
                "{name}:{line} writes with {macro_name}!. Every write goes through Output: a print \
                 macro panics on a closed reader (CLI-7), and a stray stdout write breaks the \
                 one-envelope parse (CLI-4).",
            );
        }
    }
}
