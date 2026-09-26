// SPDX-License-Identifier: Apache-2.0
//! **ARCH-1, ARCH-3, ARCH-4, ARCH-5, ARCH-6, and BIND-1**: the workspace's dependency edges,
//! asserted exactly.
//!
//! Four requirements that are all the same claim from different sides — the CLI depends on core,
//! core depends on neither the CLI nor the bindings, the bindings depend on core and not on the
//! CLI, and nothing a binding needs lives in the CLI. Written as one file because they share a
//! source: `cargo metadata`'s resolved graph.
//!
//! # An exact edge set, not a set of absences
//!
//! `assert!(no edge from A to B)` passes when A has no dependencies at all — which is what a stub
//! crate looks like. So the assertions below are equalities over the edges *between the four
//! crates in question*, which is what makes them fail if `microvms-py` never grows its dependency
//! on core as well as if it grows one on the CLI.
//!
//! # ARCH-5's witness is an absence, and the absence is checkable
//!
//! "Nothing a binding needs lives in the CLI." The usual way to satisfy that is a promise in a doc
//! comment. Here it is a property: `microvms-cli` has no `lib` target, so there is no Rust API for
//! a binding to depend on even if someone wanted to. That is the strongest available form —
//! inexpressible rather than merely forbidden — and this file asserts it from the metadata.
//!
//! # The layers below the adapters
//!
//! `microvms-domain` holds the rules and does no I/O (ARCH-6), and core re-exports it (ARCH-1).
//! Its edges among the workspace's crates are asserted the same way: exactly protocol, and
//! core's include it.
//!
//! # Each crate's allowed set
//!
//! The last tests go past the edges between our crates to every direct dependency of a driving
//! adapter and of the domain, against its set in `arch/placement.toml` (#285). They read the
//! ratchet's files rather than a copy of them. The adapter test covers each adapter the ratchet
//! holds no placement drift for; the domain can't hold any. The domain's dependencies' declared
//! features are asserted too, since a feature can add I/O to a crate the set already allows.

use std::collections::BTreeSet;
use std::path::PathBuf;

/// The workspace's resolved metadata.
fn metadata() -> cargo_metadata::Metadata {
    cargo_metadata::MetadataCommand::new()
        .manifest_path(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
        .exec()
        .expect("cargo metadata runs")
}

/// The four crates whose edges these requirements are about.
const IN_QUESTION: [&str; 4] = [
    "microvms-cli",
    "microvms-core",
    "microvms-py",
    "microvms-js",
];

/// The direct dependencies of `name` that are among [`IN_QUESTION`].
fn edges_among_ours(metadata: &cargo_metadata::Metadata, name: &str) -> BTreeSet<String> {
    let package = metadata
        .packages
        .iter()
        .find(|package| package.name.as_str() == name)
        .unwrap_or_else(|| panic!("{name} is a workspace member"));
    package
        .dependencies
        .iter()
        .map(|dependency| dependency.name.clone())
        .filter(|dependency| IN_QUESTION.contains(&dependency.as_str()))
        .collect()
}

fn set(names: &[&str]) -> BTreeSet<String> {
    names.iter().map(|name| (*name).to_string()).collect()
}

/// **ARCH-3 and ARCH-4.** The CLI depends on core; core depends on neither the CLI nor a binding.
///
/// The second half is the one that matters architecturally: a library that depended on its own CLI
/// would make every consumer of the library — including both bindings — carry clap, ratatui, and a
/// tokio multi-thread runtime. The first half is asserted as an equality so a CLI that stopped
/// depending on core (by reimplementing it) fails here too.
#[test]
fn the_cli_depends_on_core_and_core_depends_on_neither_the_cli_nor_a_binding() {
    let metadata = metadata();
    assert_eq!(
        edges_among_ours(&metadata, "microvms-cli"),
        set(&["microvms-core"]),
        "the CLI must depend on microvms-core and on no other crate of ours"
    );
    assert_eq!(
        edges_among_ours(&metadata, "microvms-core"),
        BTreeSet::new(),
        "microvms-core must depend on none of the CLI or the bindings: a library that depended on \
         its own CLI would make every library consumer carry clap and a runtime"
    );
}

/// **BIND-1.** Each binding depends on core and **not** on the CLI.
///
/// Asserted as an equality per binding, so it fails in both directions: a binding that grew an
/// edge to the CLI fails, and a binding that has no edge to core at all — which is what a stub
/// looks like — fails too. That second half is why this is not a pair of `assert!(!contains)`
/// calls: those pass against a crate with an empty dependency list, which is exactly the state the
/// bindings are in until their own task lands.
///
/// The bindings are another task's (T-W3-8), so this test is *expected* to be the thing that tells
/// that task it is not finished — and the message says so, rather than reading as a failure of
/// this one.
#[test]
fn each_binding_depends_on_core_and_never_on_the_cli() {
    let metadata = metadata();
    for binding in ["microvms-py", "microvms-js"] {
        let edges = edges_among_ours(&metadata, binding);
        assert!(
            !edges.contains("microvms-cli"),
            "{binding} depends on microvms-cli. BIND-1 and ARCH-5 say nothing a binding needs \
             lives in the CLI, and the CLI has no lib target to depend on — so this edge cannot \
             even compile, which means the manifest is wrong rather than the code."
        );
        assert_eq!(
            edges,
            set(&["microvms-core"]),
            "{binding} must depend on microvms-core (and on no other crate of ours). If this is \
             failing with an empty set, the binding is still T-W1-1's dependency-free stub and \
             T-W3-8 has not landed — which is what this assertion is here to say."
        );
    }
}

/// **ARCH-5's witness.** `microvms-cli` has no library target, so there is nothing for a binding to
/// depend on.
///
/// The strongest available form of "nothing a binding needs lives here": not a rule, but an
/// absence the compiler enforces. A `lib` target added later — even an empty one — would make the
/// edge BIND-1 forbids *possible*, and this is what catches that at the moment it becomes possible
/// rather than at the moment someone uses it.
///
/// **Falsification** — add `src/lib.rs` to this crate and it goes red naming the target. Verified;
/// see the packet's guard proofs.
#[test]
fn the_cli_exports_no_library_target_at_all() {
    let metadata = metadata();
    let package = metadata
        .packages
        .iter()
        .find(|package| package.name.as_str() == "microvms-cli")
        .expect("a workspace member");

    let targets: Vec<(&str, Vec<String>)> = package
        .targets
        .iter()
        .map(|target| {
            (
                target.name.as_str(),
                target.kind.iter().map(|k| k.to_string()).collect(),
            )
        })
        .collect();

    let library: Vec<&(&str, Vec<String>)> = targets
        .iter()
        .filter(|(_, kinds)| {
            kinds.iter().any(|kind| {
                matches!(
                    kind.as_str(),
                    "lib" | "rlib" | "dylib" | "cdylib" | "staticlib" | "proc-macro"
                )
            })
        })
        .collect();
    assert!(
        library.is_empty(),
        "microvms-cli grew a library target ({library:?}). ARCH-5's witness is that it has none: a \
         binding cannot need a type from a crate that exports nothing, and the absence is what \
         makes that a property rather than a promise. Test-only code that needs to be reachable \
         belongs in `src/guards.rs` under cfg(test)."
    );

    // And exactly one binary, named `microvm`, so the crate is what it claims to be.
    let binaries: Vec<&str> = targets
        .iter()
        .filter(|(_, kinds)| kinds.iter().any(|kind| kind == "bin"))
        .map(|(name, _)| *name)
        .collect();
    assert_eq!(binaries, ["microvm"], "{targets:?}");
}

/// The direct dependencies of `name` that are workspace members, of every kind.
///
/// Every kind, dev included: a test-only edge from the domain onto core would still let the
/// domain's tests lean on I/O the domain itself refuses, and it's the one cycle cargo allows.
fn edges_among_members(metadata: &cargo_metadata::Metadata, name: &str) -> BTreeSet<String> {
    let members: BTreeSet<String> = metadata
        .workspace_members
        .iter()
        .filter_map(|id| metadata.packages.iter().find(|package| package.id == *id))
        .map(|package| package.name.to_string())
        .collect();
    let package = metadata
        .packages
        .iter()
        .find(|package| package.name.as_str() == name)
        .unwrap_or_else(|| panic!("{name} is a workspace member"));
    package
        .dependencies
        .iter()
        .map(|dependency| dependency.name.clone())
        .filter(|dependency| members.contains(dependency))
        .collect()
}

/// **ARCH-1 and ARCH-6.** The domain depends on protocol and on no other crate of ours, and
/// core depends on the domain.
///
/// The domain's half is what keeps it below everything: an edge onto core would hand it the
/// control plane, the clock and the random pool through core's own dependencies. The core half
/// is ARCH-1's re-export: a core that stopped depending on the domain would have copied it.
///
/// **Falsification**: add `microvms-core = { path = "../microvms-core" }` to the domain's
/// `[dev-dependencies]` and this goes red naming the edge. (A normal dependency is a cycle,
/// which fails `cargo metadata` before any test runs.)
#[test]
fn the_domain_depends_only_on_protocol_and_core_depends_on_the_domain() {
    let metadata = metadata();
    assert_eq!(
        edges_among_members(&metadata, "microvms-domain"),
        set(&["microvms-protocol"]),
        "microvms-domain must depend on microvms-protocol and on no other crate of ours. It \
         performs no I/O (ARCH-6), and every other crate here does some or reaches one that does."
    );
    let core = edges_among_members(&metadata, "microvms-core");
    assert!(
        core.contains("microvms-domain"),
        "microvms-core must depend on microvms-domain and re-export it (ARCH-1); its edges are \
         {core:?}"
    );
}

/// The workspace's member list is the crates the architecture describes.
///
/// Pinned because the requirements above are equalities over a *known* set: a seventh crate that
/// depended on the CLI would satisfy every assertion here while violating ARCH-5, since nothing
/// would have looked at it. This is the assertion that makes the set known.
#[test]
fn the_workspace_members_are_the_crates_the_architecture_names() {
    let metadata = metadata();
    let mut members: Vec<String> = metadata
        .workspace_members
        .iter()
        .filter_map(|id| {
            metadata
                .packages
                .iter()
                .find(|package| package.id == *id)
                .map(|package| package.name.to_string())
        })
        .collect();
    members.sort();
    assert_eq!(
        members,
        [
            "agentd",
            // `model/`'s *package* is `agentd-model`; the directory name is not the crate name,
            // which is exactly why this list is read out of the metadata rather than off `ls`.
            "agentd-model",
            "microvms-cli",
            "microvms-core",
            "microvms-domain",
            "microvms-js",
            // `protocol/`'s *package* is `microvms-protocol` — the bare name is taken on
            // crates.io. Dependents rename it back to `protocol`, so this is the one place in
            // the workspace where the registry name is visible.
            "microvms-protocol",
            "microvms-py",
        ],
        "a workspace member appeared or vanished. The dependency-direction assertions above are \
         equalities over the four crates ARCH-3/4/5 name, so a new member that depended on the \
         CLI would pass all of them — this is what makes the set known."
    );
}

/// Nothing in the workspace depends on `microvms-cli`.
///
/// The general form of BIND-1, and it catches the case the per-binding test cannot: `agentd`, or
/// `model`, or a crate added later growing an edge to the CLI. There is deliberately no exception
/// list — a crate that needs something from the CLI needs it moved into core instead, which is the
/// kickoff's own rule stated the other way round.
#[test]
fn no_workspace_crate_depends_on_the_cli() {
    let metadata = metadata();
    for package in &metadata.packages {
        if !metadata.workspace_members.contains(&package.id) {
            continue;
        }
        if package.name.as_str() == "microvms-cli" {
            continue;
        }
        let depends = package
            .dependencies
            .iter()
            .any(|dependency| dependency.name == "microvms-cli");
        assert!(
            !depends,
            "{} depends on microvms-cli. Nothing a consumer needs lives in the CLI — if something \
             does, it belongs in microvms-core (ARCH-5).",
            package.name
        );
    }
}

/// A crate's direct dependencies of one kind, by package name. A dependency repeated per
/// target is one edge.
fn direct(
    metadata: &cargo_metadata::Metadata,
    name: &str,
    kind: cargo_metadata::DependencyKind,
) -> BTreeSet<String> {
    let package = metadata
        .packages
        .iter()
        .find(|package| package.name.as_str() == name)
        .unwrap_or_else(|| panic!("{name} is a workspace member"));
    package
        .dependencies
        .iter()
        .filter(|dependency| dependency.kind == kind)
        .map(|dependency| dependency.name.clone())
        .collect()
}

/// A file under the repository root, read as text.
fn repo_file(path: &str) -> String {
    let full = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join(path);
    std::fs::read_to_string(&full).unwrap_or_else(|error| panic!("{}: {error}", full.display()))
}

/// What a crate with a set in `arch/placement.toml` may depend on directly, for one kind
/// (`normal` or `build`).
///
/// Its set in `arch/placement.toml`, plus each crate a placement decision in
/// `ratchet/drift.json` records for it. Both files are the ratchet's, read here rather than
/// copied, so the two checks can't disagree about what's allowed. A decision is the only way
/// a crate joins an existing set: the ratchet refuses a set that grows.
fn allowed(crate_name: &str, kind: &str) -> BTreeSet<String> {
    let sets: toml::Table = repo_file("arch/placement.toml")
        .parse()
        .expect("arch/placement.toml is TOML");
    let table = sets
        .get(crate_name)
        .and_then(toml::Value::as_table)
        .unwrap_or_else(|| panic!("arch/placement.toml has no [{crate_name}] table"));
    let mut allowed: BTreeSet<String> = table
        .get(kind)
        .and_then(toml::Value::as_array)
        .map(|names| {
            names
                .iter()
                .map(|name| name.as_str().expect("a crate name").to_string())
                .collect()
        })
        .unwrap_or_default();

    // The ratchet keys a build dependency with a suffix, so a normal decision never covers a
    // build edge or the other way round.
    let suffix = if kind == "build" { " (build)" } else { "" };
    let prefix = format!("{crate_name} -> ");
    for decision in placement_records("decisions") {
        if let Some(dependency) = decision
            .strip_prefix(&prefix)
            .and_then(|rest| rest.strip_suffix(suffix))
            .filter(|dependency| !dependency.contains(' '))
        {
            allowed.insert(dependency.to_string());
        }
    }
    allowed
}

/// The keys of the placement `entries` or `decisions` in `ratchet/drift.json`.
fn placement_records(list: &str) -> Vec<String> {
    let drift: serde_json::Value =
        serde_json::from_str(&repo_file("ratchet/drift.json")).expect("drift.json is JSON");
    drift[list]
        .as_array()
        .unwrap_or_else(|| panic!("drift.json has a {list} list"))
        .iter()
        .filter(|record| record["category"] == "placement")
        .map(|record| record["key"].as_str().expect("a key").to_string())
        .collect()
}

/// The driving adapters. `arch/placement.toml` also holds the domain's set, which isn't an
/// adapter's.
const ADAPTERS: [&str; 3] = ["microvms-cli", "microvms-py", "microvms-js"];

/// The driving adapters the ratchet holds no placement drift for. Their sets are exact.
///
/// Read from the drift file rather than listed, so an adapter joins the moment its last entry
/// is fixed and `mise run ratchet:update` removes it: `microvms-cli` joins when #260 moves
/// directory sync (`tar`, `globset`, `sha2`, `const-hex`) into core. Until then the ratchet
/// asserts the CLI's surplus, entry by entry.
fn exact_adapters() -> Vec<String> {
    let sets: toml::Table = repo_file("arch/placement.toml")
        .parse()
        .expect("arch/placement.toml is TOML");
    let drifting: BTreeSet<String> = placement_records("entries")
        .iter()
        .filter_map(|key| key.split(" -> ").next().map(str::to_string))
        .collect();
    for adapter in ADAPTERS {
        assert!(
            sets.contains_key(adapter),
            "arch/placement.toml has no [{adapter}] table"
        );
    }
    ADAPTERS
        .iter()
        .filter(|name| !drifting.contains(**name))
        .map(|name| (*name).to_string())
        .collect()
}

/// **The driving-adapter contract.** Each adapter the ratchet holds no placement drift for
/// depends directly on exactly its allowed set, normal and build.
///
/// Exact both ways, like the edge assertions above: a crate added to a binding's manifest
/// fails, and so does a listed crate the binding no longer uses, since a stale entry is a set
/// that allows more than the crate needs. Dev dependencies are out: they never ship.
///
/// This is stricter than the ratchet, which it doesn't replace. The ratchet still collects all
/// three adapters, and only it can hold the CLI's remaining drift as entries; this test covers
/// an adapter once that drift is gone, and it's the one that says a set has gone stale.
///
/// **Falsification**: add `globset = "0.4"` to `microvms-py/Cargo.toml` and this goes red
/// naming it (the ratchet fails too, as new placement drift). Delete `napi-build` from
/// `microvms-js`'s build dependencies and it goes red on the stale set entry.
#[test]
fn each_exact_adapter_depends_on_exactly_its_allowed_set() {
    let exact = exact_adapters();
    for binding in ["microvms-py", "microvms-js"] {
        assert!(
            exact.contains(&binding.to_string()),
            "{binding} has placement entries in ratchet/drift.json, so its set isn't asserted \
             exactly. The bindings' sets have been exact since #285, and the ratchet refuses a new \
             entry, so the file was edited around the check. Fix the dependency instead."
        );
    }

    let metadata = metadata();
    for adapter in &exact {
        for (kind, cargo_kind) in [
            ("normal", cargo_metadata::DependencyKind::Normal),
            ("build", cargo_metadata::DependencyKind::Build),
        ] {
            let actual = direct(&metadata, adapter, cargo_kind);
            let allowed = allowed(adapter, kind);
            let added: Vec<&String> = actual.difference(&allowed).collect();
            let stale: Vec<&String> = allowed.difference(&actual).collect();
            assert!(
                added.is_empty() && stale.is_empty(),
                "{adapter}'s direct {kind} dependencies differ from its set in \
                 arch/placement.toml. Outside the set: {added:?}. Listed but unused: {stale:?}. \
                 An adapter parses input, converts types, bridges to the host, and renders \
                 output (AGENTS.md, Architecture); a crate doing other work belongs in a lower \
                 layer. A crate listed but unused comes out of the set, or out of \
                 ratchet/drift.json when a placement decision allows it."
            );
        }
    }
}

/// **ARCH-6.** The domain depends directly on exactly its set in `arch/placement.toml`, normal
/// and build, and `ratchet/drift.json` holds nothing for it.
///
/// The set is how the domain's I/O ban reaches past `std`: tokio, getrandom and reqwest can't be
/// named in its clippy.toml one method at a time, and they don't need to be when they can't be
/// dependencies. Exact both ways, like the adapters' sets: a crate added fails, and so does a
/// listed crate the domain no longer uses. The ratchet reads the same table, so a crate added
/// here is new placement drift there too, but the domain gets no entry and no decision: a
/// domain that needs I/O needs a port.
///
/// **Falsification**: add `tokio = "1"` to the domain's `[dependencies]` and this goes red
/// naming it. Delete `jiff` from `arch/placement.toml`'s domain set and it goes red on the crate
/// outside the set.
#[test]
fn the_domain_depends_on_exactly_its_allowed_set() {
    let records: Vec<String> = placement_records("entries")
        .into_iter()
        .chain(placement_records("decisions"))
        .filter(|key| key.starts_with("microvms-domain -> "))
        .collect();
    assert!(
        records.is_empty(),
        "ratchet/drift.json records placement for microvms-domain: {records:?}. The domain does \
         no I/O (ARCH-6), so a dependency outside its set isn't drift to count; it's a port to \
         add in microvms-core."
    );

    let metadata = metadata();
    for (kind, cargo_kind) in [
        ("normal", cargo_metadata::DependencyKind::Normal),
        ("build", cargo_metadata::DependencyKind::Build),
    ] {
        let actual = direct(&metadata, "microvms-domain", cargo_kind);
        let allowed = allowed("microvms-domain", kind);
        let added: Vec<&String> = actual.difference(&allowed).collect();
        let stale: Vec<&String> = allowed.difference(&actual).collect();
        assert!(
            added.is_empty() && stale.is_empty(),
            "microvms-domain's direct {kind} dependencies differ from its set in \
             arch/placement.toml. Outside the set: {added:?}. Listed but unused: {stale:?}. The \
             domain computes and encodes and does no I/O (ARCH-6): a crate that reaches the \
             network, files, a subprocess, the clock or entropy belongs in microvms-core behind \
             a port. A crate listed but unused comes out of the set."
        );
    }
}

/// What each of the domain's normal dependencies declares: whether it takes the crate's default
/// features, and exactly which others it turns on.
///
/// Listed here rather than in `arch/placement.toml` because the ratchet counts crates, not
/// features, and its schema takes only the two name lists. The allowed set is by name, and a
/// name says nothing about features: x25519-dalek's `getrandom` adds `StaticSecret::random`,
/// and jiff's defaults add `tz-system`, which reads `TZ` and `/etc/localtime`. Neither changes
/// a crate name, so neither would fail the set above.
const DOMAIN_FEATURES: [(&str, bool, &[&str]); 10] = [
    ("base64", true, &[]),
    ("const-hex", true, &[]),
    ("jiff", false, &["std"]),
    ("microvms-protocol", true, &[]),
    ("rust_decimal", true, &["serde-with-str"]),
    ("rust_decimal_macros", true, &[]),
    ("serde", true, &["derive"]),
    ("serde_json", true, &[]),
    ("thiserror", true, &[]),
    ("x25519-dalek", false, &["static_secrets"]),
];

/// **ARCH-6.** Each of the domain's normal dependencies declares exactly the features
/// [`DOMAIN_FEATURES`] records for it, in every table it appears in.
///
/// Every declaration is read, not one per name, so a second entry under
/// `[target.'cfg(unix)'.dependencies]` that turns a feature on fails too. This covers what the
/// domain's own manifest asks for. Another member can still turn a feature on through
/// workspace unification (agentd also depends on x25519-dalek 3), which is why the domain's
/// `clippy.toml` bans the methods those features add as well.
///
/// **Falsification**: set x25519-dalek's features to `["static_secrets", "getrandom"]`, or
/// drop jiff's `default-features = false`, and this goes red naming the crate.
#[test]
fn the_domain_declares_exactly_the_features_it_is_allowed() {
    let metadata = metadata();
    let package = metadata
        .packages
        .iter()
        .find(|package| package.name.as_str() == "microvms-domain")
        .expect("a workspace member");
    let recorded: std::collections::BTreeMap<&str, (bool, BTreeSet<String>)> = DOMAIN_FEATURES
        .iter()
        .map(|(name, defaults, features)| (*name, (*defaults, set(features))))
        .collect();
    let normal: Vec<&cargo_metadata::Dependency> = package
        .dependencies
        .iter()
        .filter(|dependency| dependency.kind == cargo_metadata::DependencyKind::Normal)
        .collect();
    for dependency in &normal {
        let declared = (
            dependency.uses_default_features,
            dependency
                .features
                .iter()
                .cloned()
                .collect::<BTreeSet<String>>(),
        );
        let target = dependency.target.as_ref().map_or_else(
            || "[dependencies]".to_string(),
            |target| format!("{target}"),
        );
        let Some(allowed) = recorded.get(dependency.name.as_str()) else {
            panic!(
                "microvms-domain depends on {} (in {target}), which DOMAIN_FEATURES doesn't \
                 record. Record its default-features setting and features once it's in the \
                 domain's set in arch/placement.toml.",
                dependency.name
            );
        };
        assert_eq!(
            &declared, allowed,
            "microvms-domain's {} (in {target}) declares default features {} and features {:?}; \
             DOMAIN_FEATURES allows default features {} and {:?}. A feature can bring the clock, \
             the filesystem or the OS random pool into a crate the set already allows (ARCH-6). \
             If the new feature does no I/O, record it here with the reason.",
            dependency.name, declared.0, declared.1, allowed.0, allowed.1
        );
    }
    let names: BTreeSet<&str> = normal
        .iter()
        .map(|dependency| dependency.name.as_str())
        .collect();
    let stale: Vec<&&str> = recorded
        .keys()
        .filter(|name| !names.contains(**name))
        .collect();
    assert!(
        stale.is_empty(),
        "DOMAIN_FEATURES records {stale:?}, which microvms-domain no longer depends on. Take \
         the row out."
    );
}
