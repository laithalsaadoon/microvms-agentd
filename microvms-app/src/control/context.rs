// SPDX-License-Identifier: Apache-2.0
//! A task's build context: the files a Dockerfile may `COPY`, read from a directory the way
//! `docker build` reads one (#221, IMAGE-7).
//!
//! # What enters the artifact
//!
//! Every regular file under the directory that the ignore rules leave in, at its path
//! relative to the root, `/`-separated. The rules are `Dockerfile.dockerignore` when that
//! file exists and `.dockerignore` otherwise — the precedence Docker gives a
//! Dockerfile-specific ignore file, and the artifact's Dockerfile is always named
//! `Dockerfile`. Four names at the root never enter from the context:
//!
//! * `Dockerfile`, because the artifact carries the caller's Dockerfile (usually
//!   [`wrap_dockerfile`](super::artifact::wrap_dockerfile)'s output) in its place;
//! * `.dockerignore` and `Dockerfile.dockerignore`, because the rules have already been
//!   applied, and an ignore file inside the artifact would let the platform's own build
//!   exclude the `agentd` entry — `*` then `!src` is enough;
//! * `agentd`, which is refused rather than skipped: it would replace the daemon.
//!
//! # Links, and other things that are not files
//!
//! A symlink is skipped with a warning naming it. Following it could copy files from
//! outside the context the caller never put there, and the archive cannot carry the link
//! itself. Sockets, FIFOs and devices are skipped the same way. The warnings travel back on
//! [`BuildContext::warnings`] so a harness can log them.
//!
//! # Modes
//!
//! `0o755` for a file with any execute bit and `0o644` for the rest: `COPY` keeps the mode
//! the archive entry carries, a script that loses its execute bit fails in the guest, and
//! two hosts with different umasks must still produce one artifact. On a platform without
//! Unix modes every file is `0o644`.
//!
//! # The agent token has no way in
//!
//! TRAP-5's rule for the shared snapshot holds: nothing here takes a credential. The
//! context is the caller's files, exactly as `docker build` takes them; what the image keeps
//! of them is what the caller's Dockerfile copies.

use crate::error::Error;

/// The artifact's daemon entry, which no context file may take.
pub const DAEMON_ENTRY: &str = "agentd";

/// The artifact's Dockerfile entry, which the caller's Dockerfile fills.
pub const DOCKERFILE_ENTRY: &str = "Dockerfile";

/// The ignore files, in precedence order.
pub const IGNORE_FILES: [&str; 2] = ["Dockerfile.dockerignore", ".dockerignore"];

/// S3's maximum object size for a single `PutObject`, 5 GiB, which is how the artifact is
/// uploaded. A context bigger than that cannot be uploaded at all, so it is refused while it
/// is read rather than after it is zipped in memory.
pub const MAX_CONTEXT_BYTES: u64 = 5 * 1024 * 1024 * 1024;

/// One file of a build context: its path relative to the context root, its mode, and its
/// bytes.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ContextEntry {
    /// The path relative to the context root, `/`-separated, which is also its zip entry name.
    pub name: String,
    /// `0o755` when the file carries any execute bit, `0o644` otherwise.
    pub mode: u32,
    pub bytes: Vec<u8>,
}

/// A build context: the files beside the Dockerfile in the artifact, sorted by name.
///
/// Sorted, so the entries' order is a function of their names alone: the content hash and
/// the zip see the same sequence whatever order the filesystem listed them in.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BuildContext {
    entries: Vec<ContextEntry>,
    warnings: Vec<String>,
}

impl BuildContext {
    /// A context from entries already in memory, sorted by name.
    ///
    /// Each name must be a relative `/`-separated path of non-empty, non-`.`, non-`..`
    /// segments with no backslash, unique, and not `agentd` or `Dockerfile` at the root.
    pub fn from_entries(mut entries: Vec<ContextEntry>) -> Result<Self, Error> {
        for entry in &entries {
            require_entry_name(&entry.name)?;
        }
        entries.sort_by(|a, b| a.name.cmp(&b.name));
        if let Some(pair) = entries.windows(2).find(|pair| pair[0].name == pair[1].name) {
            return Err(Error::invalid_arg(format!(
                "the build context names {:?} twice; a zip entry name is unique.",
                pair[0].name
            )));
        }
        Ok(Self {
            entries,
            warnings: Vec::new(),
        })
    }

    /// The entries, sorted by name.
    pub fn entries(&self) -> &[ContextEntry] {
        &self.entries
    }

    /// What reading the directory skipped, one line each.
    pub fn warnings(&self) -> &[String] {
        &self.warnings
    }

    /// The context with `warnings` recorded, one line each, for what reading its directory
    /// skipped.
    pub fn with_warnings(mut self, warnings: Vec<String>) -> Self {
        self.warnings = warnings;
        self
    }

    /// The bytes the entries carry, before compression.
    pub fn total_bytes(&self) -> u64 {
        self.entries
            .iter()
            .map(|entry| entry.bytes.len() as u64)
            .sum()
    }
}

fn require_entry_name(name: &str) -> Result<(), Error> {
    let bad = name.is_empty()
        || name.starts_with('/')
        || name.contains('\\')
        || name
            .split('/')
            .any(|segment| segment.is_empty() || segment == "." || segment == "..");
    if bad {
        return Err(Error::invalid_arg(format!(
            "{name:?} is not a build context entry name: it must be a relative path of \
             `/`-separated segments, none empty, `.` or `..`, and no backslash."
        )));
    }
    if name == DAEMON_ENTRY {
        return Err(Error::invalid_arg(format!(
            "the build context has a file named {DAEMON_ENTRY:?} at its root, which would \
             replace the daemon entry the artifact carries. Rename it or exclude it in \
             .dockerignore."
        )));
    }
    if name == DOCKERFILE_ENTRY {
        return Err(Error::invalid_arg(format!(
            "a build context entry named {DOCKERFILE_ENTRY:?} at the root would replace the \
             artifact's Dockerfile; the Dockerfile is passed on its own."
        )));
    }
    Ok(())
}

/// One compiled `.dockerignore` line.
#[derive(Clone, Debug)]
struct Rule {
    exception: bool,
    tokens: Vec<Token>,
}

/// A piece of a compiled glob.
#[derive(Clone, Debug, Eq, PartialEq)]
enum Token {
    Literal(char),
    /// `*`: any run of characters but `/`.
    Star,
    /// `?`: one character but `/`.
    Question,
    /// `[...]`: one character in (or, negated with `^`, not in) the set.
    Class {
        negated: bool,
        ranges: Vec<(char, char)>,
    },
    /// `**` (and `**/`) before more pattern: nothing, or anything ending in `/`.
    AnyDirs,
    /// `**` at the end of the pattern: anything at all.
    AnyRest,
}

/// The patterns of a `.dockerignore` file, applied the way Docker applies them.
///
/// Each line is trimmed; a line starting with `#` is a comment and a blank line is skipped;
/// `!` makes an exception. The pattern is cleaned as Go's `filepath.Clean` cleans it and a
/// leading `/` is dropped, so `/foo`, `./foo` and `foo/` all mean `foo`. A pattern matches a
/// path when it matches the path itself or any of its parent directories, so `node_modules`
/// excludes everything under it; the last line that matches decides, so a later `!` line
/// re-includes.
///
/// The glob is Go's `filepath.Match` plus `**`: `*` and `?` stop at `/`, `[...]` is a
/// character class negated by `^` (not `!`, which is a member), `\` escapes the next
/// character, `**/` matches any number of directories including none, and a trailing `**`
/// matches anything. These are the semantics of moby's `patternmatcher`, which
/// `microvms-core/tests/context_fuzz.rs` checks against a rebuilt copy of its regex
/// translation.
#[derive(Clone, Debug, Default)]
pub struct IgnoreRules {
    rules: Vec<Rule>,
}

impl IgnoreRules {
    /// Parses a `.dockerignore` file's text, refusing a line Docker would refuse: a bare
    /// `!`, an unterminated `[`, or a class range that runs backwards.
    pub fn parse(text: &str) -> Result<Self, Error> {
        let mut rules = Vec::new();
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (exception, pattern) = match line.strip_prefix('!') {
                Some("") => {
                    return Err(Error::invalid_arg(
                        "illegal exclusion pattern \"!\": an exception needs a pattern after \
                         the `!`."
                            .to_string(),
                    ));
                }
                Some(rest) => (true, rest),
                None => (false, line),
            };
            let cleaned = clean(pattern);
            let tokens = compile(&cleaned).map_err(|why| {
                Error::invalid_arg(format!("the ignore pattern {line:?} is malformed: {why}"))
            })?;
            rules.push(Rule { exception, tokens });
        }
        Ok(Self { rules })
    }

    /// Whether `path` — relative to the context root, `/`-separated — is excluded.
    pub fn excludes(&self, path: &str) -> bool {
        let parents: Vec<usize> = path
            .char_indices()
            .filter(|(_, c)| *c == '/')
            .map(|(at, _)| at)
            .collect();
        let mut excluded = false;
        for rule in &self.rules {
            let hit = matches(&rule.tokens, path)
                || parents.iter().any(|at| matches(&rule.tokens, &path[..*at]));
            if hit {
                excluded = !rule.exception;
            }
        }
        excluded
    }

    /// Whether any rule is an exception (`!pattern`), which is when an excluded directory
    /// still has to be walked: an exception could bring something under it back.
    pub fn has_exceptions(&self) -> bool {
        self.rules.iter().any(|rule| rule.exception)
    }
}

/// Whether `path` matches the `.dockerignore` glob `pattern`, taken as written (already
/// cleaned). A malformed pattern matches nothing.
pub fn glob_match(pattern: &str, path: &str) -> bool {
    compile(pattern).is_ok_and(|tokens| matches(&tokens, path))
}

/// Go's `filepath.Clean`, then the leading `/` dropped (unless the pattern is only `/`).
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
    match (joined.is_empty(), rooted) {
        (true, true) => "/".to_string(),
        (true, false) => ".".to_string(),
        (false, _) => joined,
    }
}

fn compile(pattern: &str) -> Result<Vec<Token>, String> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let ch = chars[i];
        i += 1;
        match ch {
            '*' if chars.get(i) == Some(&'*') => {
                i += 1;
                if chars.get(i) == Some(&'/') {
                    i += 1;
                }
                tokens.push(if i >= chars.len() {
                    Token::AnyRest
                } else {
                    Token::AnyDirs
                });
            }
            '*' => tokens.push(Token::Star),
            '?' => tokens.push(Token::Question),
            '\\' => match chars.get(i) {
                Some(next) => {
                    tokens.push(Token::Literal(*next));
                    i += 1;
                }
                None => tokens.push(Token::Literal('\\')),
            },
            '[' => {
                let negated = chars.get(i) == Some(&'^');
                if negated {
                    i += 1;
                }
                let mut ranges = Vec::new();
                let mut closed = false;
                while i < chars.len() {
                    let mut lo = chars[i];
                    i += 1;
                    if lo == ']' && !ranges.is_empty() {
                        closed = true;
                        break;
                    }
                    if lo == '\\' {
                        lo = *chars.get(i).ok_or("a class ends in an escape")?;
                        i += 1;
                    }
                    let mut hi = lo;
                    if chars.get(i) == Some(&'-') && chars.get(i + 1).is_some_and(|c| *c != ']') {
                        hi = chars[i + 1];
                        i += 2;
                        if hi == '\\' {
                            hi = *chars.get(i).ok_or("a class ends in an escape")?;
                            i += 1;
                        }
                        if hi < lo {
                            return Err(format!("the class range {lo}-{hi} runs backwards"));
                        }
                    }
                    ranges.push((lo, hi));
                }
                if !closed {
                    return Err("a `[` is never closed".to_string());
                }
                tokens.push(Token::Class { negated, ranges });
            }
            other => tokens.push(Token::Literal(other)),
        }
    }
    Ok(tokens)
}

/// Backtracking match of compiled tokens against the whole of `path`.
fn matches(tokens: &[Token], path: &str) -> bool {
    let chars: Vec<char> = path.chars().collect();
    let mut memo = std::collections::HashSet::new();
    match_from(tokens, &chars, 0, 0, &mut memo)
}

fn match_from(
    tokens: &[Token],
    path: &[char],
    t: usize,
    p: usize,
    failed: &mut std::collections::HashSet<(usize, usize)>,
) -> bool {
    if failed.contains(&(t, p)) {
        return false;
    }
    let result = match tokens.get(t) {
        None => p == path.len(),
        Some(Token::AnyRest) => true,
        Some(Token::Literal(c)) => {
            path.get(p) == Some(c) && match_from(tokens, path, t + 1, p + 1, failed)
        }
        Some(Token::Question) => {
            path.get(p).is_some_and(|c| *c != '/') && match_from(tokens, path, t + 1, p + 1, failed)
        }
        Some(Token::Class { negated, ranges }) => {
            path.get(p)
                .is_some_and(|c| ranges.iter().any(|(lo, hi)| lo <= c && c <= hi) != *negated)
                && match_from(tokens, path, t + 1, p + 1, failed)
        }
        Some(Token::Star) => {
            let mut end = p;
            loop {
                if match_from(tokens, path, t + 1, end, failed) {
                    break true;
                }
                if end >= path.len() || path[end] == '/' {
                    break false;
                }
                end += 1;
            }
        }
        Some(Token::AnyDirs) => {
            match_from(tokens, path, t + 1, p, failed)
                || (p..path.len())
                    .filter(|at| path[*at] == '/')
                    .any(|at| match_from(tokens, path, t + 1, at + 1, failed))
        }
    };
    if !result {
        failed.insert((t, p));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorKind;

    fn names(context: &BuildContext) -> Vec<&str> {
        context
            .entries()
            .iter()
            .map(|entry| entry.name.as_str())
            .collect()
    }

    /// **IMAGE-7, the glob.** `*` and `?` stay inside one path segment, `**` crosses any
    /// number of them (zero included), a character class matches one character, and a
    /// backslash escapes. The matcher is anchored at both ends.
    #[test]
    fn globs_follow_dockerignore_semantics() {
        for (pattern, path, matched) in [
            ("*.pyc", "x.pyc", true),
            ("*.pyc", "dir/x.pyc", false),
            ("**/*.pyc", "dir/x.pyc", true),
            ("**/*.pyc", "x.pyc", true),
            ("**/*.pyc", "a/b/c/x.pyc", true),
            ("dir/**", "dir/a/b", true),
            ("dir/**/x", "dir/x", true),
            ("dir/**/x", "dir/a/b/x", true),
            ("d?r", "dir", true),
            ("d?r", "d/r", false),
            ("[a-c]at", "bat", true),
            ("[a-c]at", "rat", false),
            ("[^a-c]at", "rat", true),
            // Go's `filepath.Match`, which Docker follows, negates with `^` only: `!` is a
            // member of the class.
            ("[!a-c]at", "bat", true),
            ("[!a-c]at", "!at", true),
            ("[!a-c]at", "rat", false),
            ("\\*", "*", true),
            ("\\*", "x", false),
            ("node_modules", "node_modules", true),
            ("node_modules", "node_modules2", false),
            ("a*b", "ab", true),
            ("a*b", "a/b", false),
        ] {
            assert_eq!(
                glob_match(pattern, path),
                matched,
                "IMAGE-7: {pattern:?} against {path:?}"
            );
        }
    }

    /// **IMAGE-7, the rules.** Comments and blank lines are skipped, a leading `/` and `.`
    /// segments are cleaned away, a pattern that matches a parent directory excludes what is
    /// under it, and the last matching line wins — so `!` re-includes.
    #[test]
    fn the_last_matching_rule_decides_and_parents_count() {
        let rules = IgnoreRules::parse(
            "# comment\n\n  *.log  \n/build\n./cache/\nnode_modules\n!node_modules/keep.js\n\
             docs/**\n!docs/README.md\nsecrets/*\n",
        )
        .expect("valid patterns");
        for (path, excluded) in [
            ("app.log", true),
            ("sub/app.log", false),
            ("build", true),
            ("build/out.o", true),
            ("src/build", false),
            ("cache/x", true),
            ("node_modules/left-pad/index.js", true),
            ("node_modules/keep.js", false),
            ("docs/guide.md", true),
            ("docs/README.md", false),
            ("secrets/key", true),
            ("secrets", false),
            ("src/main.py", false),
            ("# comment", false),
        ] {
            assert_eq!(rules.excludes(path), excluded, "IMAGE-7: {path:?}");
        }
    }

    /// **IMAGE-7, a malformed file.** A pattern Docker would refuse — an unterminated class,
    /// a bare `!` — is refused naming the line, rather than read as something else: a
    /// misread ignore file ships files the caller meant to exclude.
    #[test]
    fn a_malformed_ignore_file_is_refused_naming_the_pattern() {
        for (text, named) in [
            ("src/[abc\n", "src/[abc"),
            ("!\n", "!"),
            ("[z-a]\n", "[z-a]"),
        ] {
            let error = IgnoreRules::parse(text).expect_err(named);
            assert_eq!(error.kind(), ErrorKind::InvalidArg);
            assert!(error.to_string().contains(named), "{error}");
        }
    }

    /// **IMAGE-7, entries in memory.** Sorted by name whatever the order given; names that
    /// escape the root, are absolute, repeat, or take the daemon's or the Dockerfile's place
    /// are refused.
    #[test]
    fn entries_in_memory_are_sorted_and_names_are_checked() {
        let entry = |name: &str| ContextEntry {
            name: name.to_string(),
            mode: 0o644,
            bytes: name.as_bytes().to_vec(),
        };
        let context =
            BuildContext::from_entries(vec![entry("b/x"), entry("a"), entry("b/a")]).expect("ok");
        assert_eq!(names(&context), ["a", "b/a", "b/x"]);
        for bad in [
            "../up",
            "/abs",
            "a//b",
            "a/./b",
            "",
            "agentd",
            "Dockerfile",
            "a\\b",
        ] {
            BuildContext::from_entries(vec![entry(bad)])
                .expect_err(&format!("IMAGE-7: {bad:?} is not a context entry name"));
        }
        BuildContext::from_entries(vec![entry("a"), entry("a")]).expect_err("a repeat");
    }
}
