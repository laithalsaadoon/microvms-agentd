// SPDX-License-Identifier: Apache-2.0
//! A task's build context: the files a Dockerfile may `COPY`, read from a directory the way
//! `docker build` reads one (#221). Not yet implemented.

use std::path::Path;

use crate::error::{Error, ErrorKind};

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

/// A build context. Not yet implemented.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BuildContext {
    entries: Vec<ContextEntry>,
    warnings: Vec<String>,
}

impl BuildContext {
    /// Reads `dir` as a build context. Not yet implemented.
    pub fn from_dir(_dir: impl AsRef<Path>) -> Result<Self, Error> {
        Err(Error::new(
            ErrorKind::Unexpected,
            "BuildContext::from_dir is not implemented yet (#221)",
        ))
    }

    /// A context from entries already in memory. Not yet implemented.
    pub fn from_entries(_entries: Vec<ContextEntry>) -> Result<Self, Error> {
        Err(Error::new(
            ErrorKind::Unexpected,
            "BuildContext::from_entries is not implemented yet (#221)",
        ))
    }

    /// The entries, sorted by name.
    pub fn entries(&self) -> &[ContextEntry] {
        &self.entries
    }

    /// What reading the directory skipped, one line each.
    pub fn warnings(&self) -> &[String] {
        &self.warnings
    }
}

/// The patterns of a `.dockerignore` file. Not yet implemented.
#[derive(Clone, Debug, Default)]
pub struct IgnoreRules {
    _patterns: Vec<(bool, String)>,
}

impl IgnoreRules {
    /// Parses a `.dockerignore` file's text. Not yet implemented.
    pub fn parse(_text: &str) -> Result<Self, Error> {
        Ok(Self::default())
    }

    /// Whether `path` is excluded. Not yet implemented.
    pub fn excludes(&self, _path: &str) -> bool {
        false
    }
}

/// Whether `path` matches the `.dockerignore` glob `pattern`. Not yet implemented.
pub fn glob_match(_pattern: &str, _path: &str) -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A scratch directory under the system temp dir, removed on drop.
    struct Scratch(std::path::PathBuf);

    impl Scratch {
        fn new(label: &str) -> Self {
            let mut bytes = [0u8; 8];
            getrandom::fill(&mut bytes).expect("randomness");
            let dir = std::env::temp_dir().join(format!(
                "microvms-context-{label}-{}",
                const_hex::encode(bytes)
            ));
            std::fs::create_dir_all(&dir).expect("scratch dir");
            Self(dir)
        }

        fn write(&self, name: &str, bytes: &[u8]) -> &Self {
            let path = self.0.join(name);
            std::fs::create_dir_all(path.parent().expect("a parent")).expect("dirs");
            std::fs::write(path, bytes).expect("write");
            self
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

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

    /// **IMAGE-7, the walk.** Every regular file the rules leave in, sorted by name, with
    /// the execute bit carried as `0o755` and everything else `0o644`; the root `Dockerfile`
    /// and ignore files are left out because the artifact carries the wrapped Dockerfile, and
    /// an ignore file inside the artifact would let the platform's own build exclude the
    /// daemon entry.
    #[cfg(unix)]
    #[test]
    fn a_directory_becomes_its_included_files_sorted_with_modes() {
        use std::os::unix::fs::PermissionsExt as _;

        let scratch = Scratch::new("walk");
        scratch
            .write("Dockerfile", b"FROM x\n")
            .write(".dockerignore", b"*.log\nsecret.txt\n")
            .write("secret.txt", b"do not ship")
            .write("app/run.sh", b"#!/bin/sh\necho hi\n")
            .write("app/data.txt", b"data")
            .write("debug.log", b"noise")
            .write("sub/Dockerfile", b"FROM nested\n")
            .write("zeta.txt", b"z");
        let run = scratch.0.join("app/run.sh");
        std::fs::set_permissions(&run, std::fs::Permissions::from_mode(0o750)).expect("chmod");

        let context = BuildContext::from_dir(&scratch.0).expect("a readable context");
        assert_eq!(
            names(&context),
            ["app/data.txt", "app/run.sh", "sub/Dockerfile", "zeta.txt"],
            "IMAGE-7"
        );
        let mode = |name: &str| {
            context
                .entries()
                .iter()
                .find(|entry| entry.name == name)
                .expect("present")
                .mode
        };
        assert_eq!(mode("app/run.sh"), 0o755);
        assert_eq!(mode("app/data.txt"), 0o644);
        assert!(context.warnings().is_empty(), "{:?}", context.warnings());
    }

    /// **IMAGE-7, the precedence.** `Dockerfile.dockerignore` beside the Dockerfile replaces
    /// `.dockerignore` entirely, as Docker's Dockerfile-specific ignore file does.
    #[test]
    fn the_dockerfile_specific_ignore_file_takes_precedence() {
        let scratch = Scratch::new("precedence");
        scratch
            .write(".dockerignore", b"a.txt\n")
            .write("Dockerfile.dockerignore", b"b.txt\n")
            .write("a.txt", b"a")
            .write("b.txt", b"b");
        let context = BuildContext::from_dir(&scratch.0).expect("readable");
        assert_eq!(names(&context), ["a.txt"], "IMAGE-7");
    }

    /// **IMAGE-7, the links.** A symlink cannot ride in the artifact, so it is skipped and
    /// named in a warning rather than followed (a link out of the context would copy files
    /// the caller never put there) or silently dropped.
    #[cfg(unix)]
    #[test]
    fn a_symlink_is_skipped_with_a_warning_naming_it() {
        let scratch = Scratch::new("links");
        scratch.write("real.txt", b"r");
        std::os::unix::fs::symlink("real.txt", scratch.0.join("link.txt")).expect("symlink");
        std::os::unix::fs::symlink("/etc", scratch.0.join("etc")).expect("symlink");
        let context = BuildContext::from_dir(&scratch.0).expect("readable");
        assert_eq!(names(&context), ["real.txt"]);
        let warnings = context.warnings().join("\n");
        assert!(warnings.contains("link.txt"), "IMAGE-7: {warnings}");
        assert!(warnings.contains("etc"), "IMAGE-7: {warnings}");
        assert!(warnings.contains("symlink"), "{warnings}");
    }

    /// **IMAGE-7, the collision.** A context file named `agentd` at the root would replace
    /// the daemon entry, so it is refused naming the file.
    #[test]
    fn a_context_file_named_like_the_daemon_is_refused() {
        let scratch = Scratch::new("collide");
        scratch.write("agentd", b"not the daemon");
        let error = BuildContext::from_dir(&scratch.0).expect_err("collides");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("agentd"), "{error}");

        let error = BuildContext::from_dir(scratch.0.join("missing")).expect_err("no dir");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
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
