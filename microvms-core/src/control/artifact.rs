// SPDX-License-Identifier: Apache-2.0
//! The build artifact: a zip of the daemon binary, a Dockerfile, and — when the caller
//! passes [`ProjectFiles`] — the project's dependency files.
//!
//! What `CreateMicrovmImage`'s `codeArtifact.uri` points at (`sandbox.py:523`
//! `build_artifact`). Two entries always, both carrying a measured constraint, plus an
//! ecosystem-named manifest/lockfile pair when the image bakes an environment layer (#74).
//!
//! # The execute bit has to be in the zip entry
//!
//! A build that copies a non-executable binary produces an image whose `CMD` fails, and
//! the failure surfaces as a **run-hook timeout** — which says nothing about permissions,
//! and sends the reader to look at the daemon's startup path instead of at the archive.
//! So the `agentd` entry sets mode `0o755` explicitly rather than inheriting whatever the
//! host file had.
//!
//! # The agent token is never in here (TRAP-5's other half)
//!
//! The artifact becomes a **shared image snapshot**: every MicroVM launched from the image
//! sees the same bytes. A per-VM secret in there is a per-VM secret shared with every VM.
//! So the token travels through `runHookPayload` at launch instead, and this function has
//! no parameter that could carry one — see [`crate::control::microvm`]. The test at the
//! bottom of this file scans the produced zip's raw bytes for a token value to prove the
//! path stays closed, which is a byte scan rather than an API review because the leak
//! would be a *value* appearing somewhere, not a parameter being declared.

use std::io::Write as _;
use std::time::Duration;

use crate::error::{Error, ErrorKind};

/// The Dockerfile entry's name, which the build looks for by convention.
const DOCKERFILE_ENTRY: &str = "Dockerfile";

/// The daemon entry's name, matching the `CMD ["/agentd"]` the Dockerfile sets.
const AGENTD_ENTRY: &str = "agentd";

/// The mode the daemon entry carries.
///
/// See the module docs: a non-executable binary here becomes a run-hook timeout later.
const AGENTD_MODE: u32 = 0o755;

/// An ecosystem whose dependency files may enter the build artifact (#74).
///
/// The variant fixes **both entry names**. That is the TRAP-5 half of the design: the
/// artifact is a shared image snapshot, so what may enter it is an allowlist of
/// dependency-defining files, not a caller-named list — there is no field anywhere in this
/// module a caller could put `.env` or a credentials file's name into. The byte-scan test
/// at the bottom of this file covers the entries this type admits.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Ecosystem {
    /// Python under uv: `pyproject.toml` + `uv.lock`.
    Uv,
    /// Node under npm: `package.json` + `package-lock.json`.
    Npm,
    /// Rust under cargo: `Cargo.toml` + `Cargo.lock`.
    Cargo,
}

impl Ecosystem {
    /// Every ecosystem, in detection order.
    pub const ALL: [Self; 3] = [Self::Uv, Self::Npm, Self::Cargo];

    /// The manifest's file name, which is also its zip entry name.
    pub fn manifest_name(self) -> &'static str {
        match self {
            Self::Uv => "pyproject.toml",
            Self::Npm => "package.json",
            Self::Cargo => "Cargo.toml",
        }
    }

    /// The lockfile's file name, which is also its zip entry name — and the file the
    /// environment layer is keyed on (#74).
    pub fn lockfile_name(self) -> &'static str {
        match self {
            Self::Uv => "uv.lock",
            Self::Npm => "package-lock.json",
            Self::Cargo => "Cargo.lock",
        }
    }

    /// The whole `RUN` line that installs the environment layer: toolchain bootstrap on
    /// the managed al2023 base, then the lockfile-faithful install.
    ///
    /// # The install commands are the lockfile-faithful spellings
    ///
    /// `uv sync --locked`, `npm ci`, and `cargo fetch --locked` each **refuse** a lockfile
    /// that disagrees with its manifest rather than quietly re-resolving — which is what
    /// keeps the baked layer the thing the content hash says it is. `uv sync` without
    /// `--locked`, `npm install`, or a bare `cargo fetch` would install *something* and
    /// the hash would then name an environment the build did not produce.
    ///
    /// # The bootstrap packages are measured, the full build is not yet
    ///
    /// The managed base is al2023-minimal and ships no toolchain, so each line installs
    /// its own. Package names and the binaries they land were verified against the
    /// Amazon Linux 2023 repos (2026-08-31): `python3.12-pip` puts `pip3.12` on PATH,
    /// `nodejs22-npm` puts `npm` and `node` on PATH, `cargo` puts `cargo` on PATH.
    /// `install_weak_deps=0` keeps the layer minimal. A full in-guest build of each line
    /// is the live-conformance scenario #74's acceptance defers to — see the issue.
    ///
    /// Cargo is the one that needs a stub: `cargo fetch` refuses a package with no
    /// targets, so the line creates an empty `src/main.rs` first. The working tree synced
    /// at launch lands over it.
    pub fn install_run_line(self) -> &'static str {
        match self {
            Self::Uv => {
                "RUN dnf -y --setopt=install_weak_deps=0 install python3.12 python3.12-pip \
                 && dnf clean all && pip3.12 install --no-cache-dir uv && uv sync --locked"
            }
            Self::Npm => {
                "RUN dnf -y --setopt=install_weak_deps=0 install nodejs22-npm \
                 && dnf clean all && npm ci"
            }
            Self::Cargo => {
                "RUN dnf -y --setopt=install_weak_deps=0 install cargo \
                 && dnf clean all && mkdir -p src && touch src/main.rs \
                 && cargo fetch --locked"
            }
        }
    }
}

/// A project's dependency files: the lockfile plus the manifest that owns it.
///
/// Bytes rather than paths, for the reason [`build_artifact`]'s `binary` is bytes: the
/// caller owns the read, and this module keeps no filesystem behaviour to stub in a test.
/// The entry names come from [`Ecosystem`], never from the caller — see the type's docs
/// for why that is a TRAP-5 property rather than a convenience.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectFiles {
    /// Which ecosystem's pair this is, fixing both entry names.
    pub ecosystem: Ecosystem,
    /// The manifest's bytes (`pyproject.toml`, `package.json`, `Cargo.toml`).
    pub manifest: Vec<u8>,
    /// The lockfile's bytes (`uv.lock`, `package-lock.json`, `Cargo.lock`).
    pub lockfile: Vec<u8>,
}

/// Zips `binary` with `dockerfile` — and, when given, the project's dependency files —
/// into the bytes `codeArtifact.uri` will point at.
///
/// `binary` is the daemon's bytes rather than a path, so the caller owns the read and this
/// function has no filesystem behaviour to stub in a test. `project` adds exactly two more
/// entries, named by its [`Ecosystem`], at the archive root beside the Dockerfile — which
/// is the build context root, so a `COPY pyproject.toml uv.lock …` in the Dockerfile finds
/// them.
pub fn build_artifact(
    binary: &[u8],
    dockerfile: &str,
    project: Option<&ProjectFiles>,
) -> Result<Vec<u8>, Error> {
    use zip::write::{SimpleFileOptions, ZipWriter};

    let mut writer = ZipWriter::new(std::io::Cursor::new(Vec::new()));
    let deflated =
        SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);

    let mut write_entry = |name: &str, bytes: &[u8], options: SimpleFileOptions| {
        writer
            .start_file(name, options)
            .and_then(|()| writer.write_all(bytes).map_err(Into::into))
            .map_err(|error| {
                Error::new(
                    ErrorKind::Unexpected,
                    format!("could not add {name} to the build artifact: {error}"),
                )
            })
    };

    write_entry(DOCKERFILE_ENTRY, dockerfile.as_bytes(), deflated)?;
    write_entry(AGENTD_ENTRY, binary, deflated.unix_permissions(AGENTD_MODE))?;
    if let Some(project) = project {
        write_entry(
            project.ecosystem.manifest_name(),
            &project.manifest,
            deflated,
        )?;
        write_entry(
            project.ecosystem.lockfile_name(),
            &project.lockfile,
            deflated,
        )?;
    }

    let bytes = writer
        .finish()
        .map_err(|error| {
            Error::new(
                ErrorKind::Unexpected,
                format!("could not finish the build artifact: {error}"),
            )
        })?
        .into_inner();
    Ok(bytes)
}

/// The sha256 of the artifact's inputs, as lowercase hex.
///
/// # What is hashed, and why not the zip
///
/// The **inputs** — the daemon binary's bytes, the Dockerfile text, and the project
/// files when the artifact carries them — rather than the bytes [`build_artifact`]
/// produces. The zip is a container: its byte identity depends on the `zip` crate's
/// version, its compression level, and its header defaults, so an upgraded dependency
/// would silently change every image name and orphan every reuse. The inputs are what a
/// build actually consumes, and two builds with equal inputs produce interchangeable
/// images — which is the property content-addressed reuse rests on.
///
/// Each input is length-prefixed before hashing, so `(binary="ab", dockerfile="c")` and
/// `(binary="a", dockerfile="bc")` are different hashes rather than one concatenation.
/// A project file contributes its entry **name and bytes**, both length-prefixed, so the
/// same bytes under a different ecosystem — `Cargo.lock` and `package-lock.json` holding
/// identical text — are different identities too.
///
/// # `None` is the historical digest, deliberately
///
/// With no project files the digest stream is byte-identical to what this function
/// produced before #74, so every projectless image name in every account survives the
/// change — the alternative orphans exactly the reuse this hash exists to serve. The
/// pinned vector in the tests is what holds that fixed. This is #74's key: two projects
/// with identical lockfiles share an image, and a lockfile edit is a new name and a
/// fresh build.
pub fn artifact_content_hash(
    binary: &[u8],
    dockerfile: &str,
    project: Option<&ProjectFiles>,
) -> String {
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

/// A base image: the platform ARN, the Dockerfile `FROM` that pairs with it, and whether
/// it declares a `WORKDIR`.
///
/// All three together because the first two **must** agree and used to be able to
/// disagree: the Python client's `DEFAULT_BASE_IMAGE` named the managed base for
/// `baseImageArn` while `default_dockerfile` hardcoded an unrelated registry literal in
/// its `FROM`, so changing either left the other pointing somewhere else
/// (`sandbox.py:410-444`). Pairing them means a caller selects one thing and both fields
/// follow.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BaseImage {
    /// Goes into `baseImageArn` — the platform's managed base, not a registry ref.
    pub name: String,
    /// Goes into the Dockerfile `FROM` — the registry ref measured alongside `name`.
    pub docker_ref: String,
    /// What `docker inspect` reports for `WorkingDir`. Empty means it declares none.
    ///
    /// A field rather than a lookup because a caller with a purpose-built image is the
    /// only one who can say what their image declares, and this client cannot read it
    /// without pulling the manifest.
    pub working_dir: String,
}

impl BaseImage {
    /// The managed base every `docs/PLATFORM.md` measurement from 2026-08-06 onward used,
    /// paired with the registry ref those same builds used as `FROM`.
    ///
    /// `working_dir` is empty, and that is measured rather than assumed:
    /// `al2023-minimal`, `python:3.12-slim`, and `node:20-slim` all leave `WorkingDir`
    /// empty (2026-08-05), which is what makes [`require_workdir`] necessary.
    pub fn al2023() -> Self {
        Self {
            name: "al2023-1".to_string(),
            docker_ref: "public.ecr.aws/amazonlinux/amazonlinux:2023-minimal".to_string(),
            working_dir: String::new(),
        }
    }

    /// The base image a task Dockerfile pairs with: the managed base's `name` (so
    /// `baseImageArn` is unchanged) and the Dockerfile's own first `FROM` as `docker_ref`
    /// (IMAGE-4).
    ///
    /// # The pairing the other way round
    ///
    /// [`require_matching_from`] refuses a Dockerfile whose first `FROM` is not the base's
    /// `docker_ref`. That is the right guard where this client derives the Dockerfile from
    /// the base; a task Dockerfile chooses its own `FROM`, so the pairing has to run from the
    /// Dockerfile to the base, and every harness wrote the same inversion by hand (#220).
    /// Here the ref is taken whole — `--platform` and `AS` decoration dropped by
    /// [`dockerfile_from_ref`], a digest pin kept — so the guard's comparison is the ref
    /// against itself and it passes by construction.
    ///
    /// `working_dir` is empty: this cannot read the `FROM` image's `WorkingDir` without
    /// pulling its manifest, and empty is what every measured public base declares (see
    /// [`BaseImage::al2023`]). A task that relies on one sets `WORKDIR`, which
    /// [`require_workdir`] reads from the Dockerfile.
    ///
    /// Refuses a Dockerfile with no `FROM`: there is no ref to take.
    pub fn from_dockerfile(dockerfile: &str) -> Result<Self, Error> {
        match dockerfile_from_ref(dockerfile) {
            Some(docker_ref) => Ok(Self {
                docker_ref: docker_ref.to_string(),
                ..Self::al2023()
            }),
            None => Err(Error::invalid_arg(
                "the Dockerfile has no FROM, so there is no image reference to pair the \
                 managed base with. Derive the base from the Dockerfile the task builds \
                 from."
                    .to_string(),
            )),
        }
    }

    /// The `baseImageArn` for this base in `region`.
    ///
    /// # `microvm-image:<name>`, with a colon, and it is not just the managed base
    ///
    /// This was the only place in the repo building the colon form; the fakes and the
    /// encoding test all used `microvm-image/<name>`, so the repo held two beliefs about the
    /// shape of the identifier every image call takes. The model's `TaggableResource` pattern
    /// admits only `(capacity-provider|network-connector|microvm-image):[a-zA-Z0-9-_]+`, and
    /// a live read settled it: `ListMicrovmImages` in us-east-1 returns
    /// `arn:aws:lambda:us-east-1:<account>:microvm-image:coding-agents-on-bedrock` for a
    /// customer image, and `GetMicrovmImage` accepts exactly that (measured 2026-08-15).
    ///
    /// So the colon is right for **both** the managed base and a customer image; there is no
    /// two-form rule, and this comment used to imply there was. The slash form fails as
    /// `AccessDeniedException` rather than as a validation error, because IAM evaluates the
    /// malformed ARN as a resource with no matching policy — a permissions message for a
    /// resource that exists.
    pub fn arn(&self, region: &crate::region::Region) -> String {
        format!(
            "arn:aws:lambda:{}:aws:microvm-image:{}",
            region.as_str(),
            self.name
        )
    }
}

impl Default for BaseImage {
    fn default() -> Self {
        Self::al2023()
    }
}

/// Where the environment layer lives when the caller named no workdir of their own.
///
/// A constant rather than `/` because the install commands resolve everything against the
/// current directory — `uv sync` writes `.venv` beside the manifest, `npm ci` writes
/// `node_modules` — and the working tree synced at launch has to land in the same place
/// for the layer to be *its* environment.
pub const DEFAULT_PROJECT_WORKDIR: &str = "/project";

/// A Dockerfile that makes the daemon the container `CMD`.
///
/// `ENTRYPOINT []` plus `CMD ["/agentd"]` is the deployment invariant the trust boundary
/// rests on: it is what guarantees no task workload runs before the platform's run hook
/// lands. It is also what makes an omitted `cwd` inherit the image `WORKDIR`, since the
/// daemon's own cwd is the image's.
///
/// The `FROM` is derived from `base` rather than written here, so it cannot disagree with
/// the `baseImageArn` the create call sends.
///
/// `project` bakes the environment layer (#74): the manifest+lockfile pair is copied into
/// the working directory — `workdir` when given, [`DEFAULT_PROJECT_WORKDIR`] otherwise —
/// and the ecosystem's [`Ecosystem::install_run_line`] installs from the lockfile. Without
/// it the derived Dockerfile installs nothing, which was the measured state this closes:
/// every launch then pays dependency installation inside the guest, the 31–48% of launch
/// time `docs/STRATEGY.md` attributes to environment init.
///
/// The invariant is *unenforced*: a base image that starts its own background process
/// before bootstrap breaks it, and enforcing that belongs to whoever builds the image
/// (`docs/PROTOCOL.md`, "Trust boundary").
pub fn default_dockerfile(
    port: u16,
    workdir: Option<&str>,
    base: &BaseImage,
    project: Option<Ecosystem>,
) -> String {
    format!(
        "FROM {}\n{}",
        base.docker_ref,
        agentd_stanza(port, workdir, project)
    )
}

/// The agentd stanza: every line [`default_dockerfile`] writes after its `FROM`, and every
/// line [`wrap_dockerfile`] appends to a task Dockerfile (IMAGE-1).
///
/// # One source, and why it is this function rather than a constant
///
/// Two copies of these lines drift: a harness that carried the stanza as a string literal
/// of its own (#220) had to track every change here by hand, and the day one side gained a
/// line the other did not, the image a task built and the image the default build produced
/// stopped agreeing about how the daemon starts. So both callers render this one function,
/// and `wrapping_a_bare_from_is_the_default_dockerfile_minus_nothing` holds them equal:
/// `wrap_dockerfile("FROM x\n")` is the default Dockerfile for a base whose ref is `x`.
///
/// The text is byte-for-byte what `default_dockerfile` emitted before the split, which is
/// what keeps every content-addressed image name (`build --reuse`, `AgentVm::image_name`)
/// derived from the default Dockerfile valid: the name hashes the Dockerfile text.
fn agentd_stanza(port: u16, workdir: Option<&str>, project: Option<Ecosystem>) -> String {
    let mut lines = vec![
        "COPY agentd /agentd".to_string(),
        "RUN chmod 0755 /agentd".to_string(),
    ];
    let workdir = match (workdir.filter(|dir| !dir.is_empty()), project) {
        (Some(dir), _) => Some(dir),
        // The layer needs a directory to live in even when the caller named none.
        (None, Some(_)) => Some(DEFAULT_PROJECT_WORKDIR),
        (None, None) => None,
    };
    if let Some(workdir) = workdir {
        lines.push(format!("RUN mkdir -p {workdir}"));
        lines.push(format!("WORKDIR {workdir}"));
    }
    if let Some(ecosystem) = project {
        // Relative destination, resolving against the WORKDIR set above — the same
        // directory the install command runs in.
        lines.push(format!(
            "COPY {} {} ./",
            ecosystem.manifest_name(),
            ecosystem.lockfile_name(),
        ));
        lines.push(ecosystem.install_run_line().to_string());
    }
    lines.extend([
        format!("ENV AGENTD_PORT={port}"),
        "ENV AGENTD_LOG=info".to_string(),
        format!("EXPOSE {port}"),
        "ENTRYPOINT []".to_string(),
        r#"CMD ["/agentd"]"#.to_string(),
        String::new(),
    ]);
    lines.join("\n")
}

/// What [`wrap_dockerfile`] needs besides the task text.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WrapOptions {
    /// The agent port the stanza's `ENV AGENTD_PORT` and `EXPOSE` name. It must be the port
    /// the create call sends as `hooks.port` — [`ControlPlane::port`](super::ControlPlane::port)
    /// — which [`require_matching_agentd_port`] checks at the create call.
    pub port: u16,
    /// A working directory for the stanza to create and set, exactly as
    /// [`default_dockerfile`] writes one. One absolute path; `Some("")` reads as `None`, the
    /// same way the default generator reads it.
    pub workdir: Option<String>,
    /// The caller relies on the image `WORKDIR` — an exec with no `cwd` runs there — so one
    /// must be declared: by the task Dockerfile or by [`WrapOptions::workdir`]. Off by
    /// default, because a task with no `WORKDIR` runs its commands in `/` under Docker too,
    /// and a harness reproducing that has nothing to refuse.
    pub inherit_workdir: bool,
}

impl Default for WrapOptions {
    fn default() -> Self {
        Self {
            port: super::DEFAULT_AGENT_PORT,
            workdir: None,
            inherit_workdir: false,
        }
    }
}

/// A task Dockerfile with the agentd stanza appended: the one call a harness needs to go
/// from the Dockerfile a task brings to one this client can build (#220).
///
/// The result is the task text verbatim, a newline if it lacked one, `USER root` when the
/// task's last `USER` is anyone else, and then [`default_dockerfile`]'s stanza for
/// `opts.port` and `opts.workdir` — the same function, so the two cannot drift (IMAGE-1).
/// The stanza's `ENTRYPOINT []` and `CMD ["/agentd"]` are the result's last instructions,
/// so whatever entrypoint, command, or port the task set, the daemon is the container's
/// process and listens where the hooks are called (IMAGE-2). Pair it with
/// [`BaseImage::from_dockerfile`] for the create call's `FROM` guard (IMAGE-4).
///
/// # `USER root`, only when the task changed user
///
/// The daemon demotes each exec to the user it names, which takes root; a task that ends on
/// `USER app` would otherwise start the daemon as `app`. The line is written only when the
/// last `USER` is not root, so a task that never changes user wraps to exactly the default
/// stanza — writing it unconditionally would make the default Dockerfile and the wrap of a
/// bare `FROM` two different texts (`model/src/wrap.rs`, `AlwaysUserRoot`).
///
/// # What is refused, and why each is a refusal (IMAGE-3)
///
/// Each is a Dockerfile the build would accept and the guest would then fail on, one build
/// cycle later and naming none of the cause:
///
/// * **No `FROM`.** Nothing to append to, and no base to derive.
/// * **An unfinished last instruction.** A trailing line continuation — in the escape
///   character the `# escape=` directive selects, across the blank and comment lines Docker
///   skips inside one — joins the stanza's `COPY agentd /agentd` into the task's last
///   instruction; an unterminated heredoc swallows the whole stanza as its body. Either way
///   the image has no daemon and the build fails as a run-hook timeout.
/// * **A keepalive at or over the client's stream idle timeout**, for the reason
///   [`require_keepalive_under_idle_timeout`] gives.
/// * **A port of 0, or a workdir that is not one absolute path.** A relative `WORKDIR`
///   resolves against whatever the task left, whitespace splits `RUN mkdir -p` into several
///   directories, and a line break writes an instruction the caller never saw.
/// * **`inherit_workdir` with no `WORKDIR` anywhere**, for the reason [`require_workdir`]
///   gives.
///
/// The scan reads instructions the way the Dockerfile parser does for these questions —
/// escape directive, continuations, comments, heredoc bodies — and no further: it finds a
/// Dockerfile that cannot take the stanza, it does not validate one.
pub fn wrap_dockerfile(task: &str, opts: &WrapOptions) -> Result<String, Error> {
    if dockerfile_from_ref(task).is_none() {
        return Err(Error::invalid_arg(
            "the task Dockerfile has no FROM, so there is no image to append the agentd \
             stanza to and no base image to pair with it. Wrap the Dockerfile the task \
             builds from, or pass `FROM <image>` for a task that names only an image."
                .to_string(),
        ));
    }
    let scan = Instructions::scan(task);
    if let Some(terminator) = scan.open_heredoc {
        return Err(Error::invalid_arg(format!(
            "the task Dockerfile ends inside a heredoc that is never terminated (no line \
             {terminator:?} closes it), so every line appended after it — the whole agentd \
             stanza — would become the heredoc's body. The image would build with no daemon \
             in it and fail as a run-hook timeout. Terminate the heredoc."
        )));
    }
    if scan.continued {
        return Err(Error::invalid_arg(format!(
            "the task Dockerfile's last instruction ends with a line continuation ({:?}), so \
             the first line appended after it — the agentd stanza's `COPY agentd /agentd` — \
             would be joined into that instruction instead of running. The image would build \
             with no daemon in it and fail as a run-hook timeout. Finish the instruction, or \
             drop the trailing {:?}.",
            scan.escape, scan.escape,
        )));
    }
    require_keepalive_under_idle_timeout(crate::session::exec::DEFAULT_STREAM_IDLE_TIMEOUT, task)?;
    super::require_valid_port("port", opts.port)?;
    let workdir = opts.workdir.as_deref().filter(|dir| !dir.is_empty());
    if let Some(dir) = workdir
        && (!dir.starts_with('/') || dir.chars().any(|c| c.is_whitespace() || c.is_control()))
    {
        return Err(Error::invalid_arg(format!(
            "the workdir {dir:?} is not one absolute path. The stanza writes it as \
             `RUN mkdir -p {dir}` and `WORKDIR {dir}`: a relative path resolves against \
             whatever WORKDIR the task left, whitespace splits the mkdir into several \
             directories, and a line break writes an instruction nobody reviewed. Pass an \
             absolute path such as /workspace."
        )));
    }
    if opts.inherit_workdir && workdir.is_none() && !dockerfile_declares_workdir(task) {
        return Err(Error::invalid_arg(
            "inherit_workdir was requested but the task Dockerfile sets no WORKDIR and no \
             workdir option was given, so there is nothing to inherit: every exec with no cwd \
             would run in `/`, and every relative path in it would resolve there. Pass a \
             workdir, or set WORKDIR in the task Dockerfile."
                .to_string(),
        ));
    }

    let stanza = agentd_stanza(opts.port, workdir, None);
    let mut wrapped = String::with_capacity(task.len() + stanza.len() + 16);
    wrapped.push_str(task);
    if !task.ends_with('\n') {
        wrapped.push('\n');
    }
    if scan
        .last_user
        .as_deref()
        .is_some_and(|user| !is_root_user(user))
    {
        wrapped.push_str("USER root\n");
    }
    wrapped.push_str(&stanza);
    Ok(wrapped)
}

/// Whether a `USER` value names root: `root` or `0`, with no group or a root group.
///
/// Anything else is not root, including a value built from a variable — the scan cannot
/// expand `${TASK_USER}`, and restoring root for a value that turns out to be root costs one
/// redundant line.
fn is_root_user(value: &str) -> bool {
    let is_root = |part: &str| part == "root" || part == "0";
    match value.split_once(':') {
        Some((user, group)) => is_root(user) && is_root(group),
        None => is_root(value),
    }
}

/// Whether any line of `dockerfile` is a `WORKDIR` with an argument.
fn dockerfile_declares_workdir(dockerfile: &str) -> bool {
    dockerfile.lines().any(|line| {
        let mut words = line.split_whitespace();
        words
            .next()
            .is_some_and(|first| first.eq_ignore_ascii_case("WORKDIR"))
            && words.next().is_some()
    })
}

/// What [`wrap_dockerfile`] needs to know about how a Dockerfile's instructions end.
struct Instructions {
    /// The escape character: `\` unless an `# escape=` directive chose a backtick.
    escape: char,
    /// The text ends inside a line continuation.
    continued: bool,
    /// The terminator of a heredoc the text never closes.
    open_heredoc: Option<String>,
    /// The value of the last `USER` instruction.
    last_user: Option<String>,
}

impl Instructions {
    /// Reads the parser directives, then every logical instruction: continuation lines
    /// joined, blank and comment lines skipped (inside a continuation too, as the parser
    /// does), heredoc bodies consumed up to their terminators.
    fn scan(dockerfile: &str) -> Self {
        let escape = escape_directive(dockerfile);
        let mut heredocs: std::collections::VecDeque<(String, bool)> = Default::default();
        let mut pending = String::new();
        let mut continued = false;
        let mut last_user = None;
        for line in dockerfile.lines() {
            if let Some((terminator, strip_tabs)) = heredocs.front() {
                let candidate = if *strip_tabs {
                    line.trim_start_matches('\t')
                } else {
                    line
                };
                if candidate.trim_end_matches('\r') == terminator {
                    heredocs.pop_front();
                }
                continue;
            }
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            let body = line.trim_end();
            if let Some(joined) = body.strip_suffix(escape) {
                pending.push_str(joined);
                pending.push(' ');
                continued = true;
                continue;
            }
            continued = false;
            pending.push_str(body);
            let instruction = std::mem::take(&mut pending);
            let mut words = instruction.split_whitespace();
            let Some(keyword) = words.next() else {
                continue;
            };
            if keyword.eq_ignore_ascii_case("USER") {
                last_user = words.next().map(str::to_string);
            } else if ["RUN", "COPY", "ADD"]
                .iter()
                .any(|name| keyword.eq_ignore_ascii_case(name))
            {
                heredocs.extend(words.filter_map(heredoc_marker));
            }
        }
        Self {
            escape,
            continued,
            open_heredoc: heredocs.pop_front().map(|(terminator, _)| terminator),
            last_user,
        }
    }
}

/// The escape character a Dockerfile's `# escape=` parser directive selects, or `\`.
///
/// Directives are only read at the very top: the first line that is not one — an
/// instruction, a blank line, or an ordinary comment — ends them.
fn escape_directive(dockerfile: &str) -> char {
    for line in dockerfile.lines() {
        let Some(directive) = line.trim().strip_prefix('#') else {
            break;
        };
        let Some((key, value)) = directive.split_once('=') else {
            break;
        };
        let key = key.trim().to_ascii_lowercase();
        if key == "escape" {
            return match value.trim() {
                "`" => '`',
                _ => '\\',
            };
        }
        if !["syntax", "check"].contains(&key.as_str()) {
            break;
        }
    }
    '\\'
}

/// The terminator a heredoc marker word opens, and whether it strips leading tabs (`<<-`).
///
/// `<<EOF`, `<<-EOF`, `<<"EOF"`, `<<'EOF'`; not the here-string `<<<`, and not a bare `<<`.
fn heredoc_marker(word: &str) -> Option<(String, bool)> {
    let rest = word.strip_prefix("<<")?;
    if rest.starts_with('<') {
        return None;
    }
    let (rest, strip_tabs) = match rest.strip_prefix('-') {
        Some(rest) => (rest, true),
        None => (rest, false),
    };
    let name = rest
        .strip_prefix('"')
        .and_then(|name| name.strip_suffix('"'))
        .or_else(|| {
            rest.strip_prefix('\'')
                .and_then(|name| name.strip_suffix('\''))
        })
        .unwrap_or(rest);
    let valid = name
        .chars()
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic() || first == '_')
        && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_');
    valid.then(|| (name.to_string(), strip_tabs))
}

/// The image ref in a Dockerfile's first `FROM`, or `None` when it has none.
///
/// Deliberately loose on whitespace and case, and it ignores `--platform=` and `AS name`
/// decoration, because the check exists to catch a base that disagrees rather than to
/// validate Dockerfile syntax.
///
/// Split-and-scan rather than a regex: the pattern is "first token after FROM on a line
/// whose first word is FROM", which `split_whitespace` states more directly than a
/// pattern string would. (`regex-lite` is available if this ever grows.)
pub fn dockerfile_from_ref(dockerfile: &str) -> Option<&str> {
    for line in dockerfile.lines() {
        let mut words = line.split_whitespace();
        let Some(first) = words.next() else { continue };
        if !first.eq_ignore_ascii_case("FROM") {
            continue;
        }
        // Skip flag decoration such as `--platform=linux/arm64`.
        return words.find(|word| !word.starts_with("--"));
    }
    None
}

/// Rejects working-directory inheritance when nothing declares one.
///
/// Measured 2026-08-05: `al2023-minimal`, `python:3.12-slim`, and `node:20-slim` all leave
/// `WorkingDir` empty, so "inherit the image WORKDIR" inherits `/` and every relative path
/// in the caller's commands resolves somewhere they did not mean.
///
/// Rejected rather than warned because the symptom appears in the **guest, one build cycle
/// later**, as commands running in the wrong directory rather than as anything about
/// `WORKDIR`.
pub fn require_workdir(base: &BaseImage, dockerfile: Option<&str>) -> Result<(), Error> {
    if !base.working_dir.is_empty() {
        return Ok(());
    }
    let declares_workdir = dockerfile.is_some_and(|text| {
        text.lines().any(|line| {
            let mut words = line.split_whitespace();
            words
                .next()
                .is_some_and(|first| first.eq_ignore_ascii_case("WORKDIR"))
                && words.next().is_some()
        })
    });
    if declares_workdir {
        return Ok(());
    }
    Err(Error::invalid_arg(format!(
        "inherit_workdir was requested but base image {:?} declares no WorkingDir and the \
         Dockerfile sets none. Most public ARM64 base images leave it empty \
         (docs/PLATFORM.md, 'Most public ARM64 base images have no WORKDIR'), so there is \
         nothing to inherit and every relative path would resolve against `/`. Pass a workdir \
         to default_dockerfile, or set WORKDIR in your own Dockerfile.",
        base.name
    )))
}

/// The port in a Dockerfile's last `ENV AGENTD_PORT=`, or `None` when it sets none.
///
/// Last rather than first: a later `ENV` of the same name wins at build time, so the last
/// one is what the daemon reads. Accepts `ENV AGENTD_PORT=9000` and the legacy
/// `ENV AGENTD_PORT 9000` spelling, since both set the variable and a guard that only
/// understands one form passes the file it cannot parse.
///
/// Split-and-scan for the same reason [`dockerfile_from_ref`] is: `split_whitespace`
/// states the pattern more directly than a regex string would here.
pub fn dockerfile_agentd_port(dockerfile: &str) -> Option<u16> {
    let mut found = None;
    for line in dockerfile.lines() {
        let mut words = line.split_whitespace();
        let Some(first) = words.next() else { continue };
        if !first.eq_ignore_ascii_case("ENV") {
            continue;
        }
        let Some(assignment) = words.next() else {
            continue;
        };
        let value = match assignment.split_once('=') {
            Some(("AGENTD_PORT", value)) => value,
            Some(_) => continue,
            // `ENV AGENTD_PORT 9000`: the value is the next word.
            None if assignment == "AGENTD_PORT" => match words.next() {
                Some(value) => value,
                None => continue,
            },
            None => continue,
        };
        // An unparseable value is not this guard's business: the daemon keeps its default
        // for one (`agentd/src/config.rs:118`), so there is no disagreement to report.
        if let Ok(port) = value.parse() {
            found = Some(port);
        }
    }
    found
}

/// Rejects a Dockerfile whose `AGENTD_PORT` disagrees with the port the create call sends
/// as `hooks.port`.
///
/// The two are set independently — `hooks.port` comes from `ControlPlane::port`, the
/// variable from the caller's own Dockerfile — and the platform calls its build-time
/// `ready`/`validate` hooks on the port in the create call. A guest listening elsewhere
/// answers nothing, so the build fails.
///
/// Rejected rather than warned because the failure points away from its cause: the docker
/// build succeeds, the log group holds a clean build *and* the daemon's own "agentd
/// listening" line, and the image still lands in `CREATE_FAILED`. Neither
/// `GetMicrovmImage` nor the build log names the port — only
/// `GetMicrovmImageVersion`'s `hooks.port` does, compared by hand against the Dockerfile.
///
/// A Dockerfile that sets no `AGENTD_PORT` is checked against
/// [`DEFAULT_AGENT_PORT`](crate::control::DEFAULT_AGENT_PORT) rather than passed, because
/// silence is not neutral: `Config::from_env` keeps its own default for an unset variable
/// (`agentd/src/config.rs:118`, default `agentd/src/config.rs:84`), so the guest listens on
/// 9000 whether or not the Dockerfile mentions a port. Against a client that changed its
/// port, the absent variable produces exactly the measured failure — and produces it for a
/// caller who never typed a port anywhere, which is the harder version to diagnose.
pub fn require_matching_agentd_port(port: u16, dockerfile: &str) -> Result<(), Error> {
    // An unparseable value lands here too, and belongs here: the daemon warns and keeps the
    // same default (`agentd/src/config.rs:174-179`), so both spellings of "the Dockerfile
    // named no usable port" have the same consequence in the guest.
    let Some(found) = dockerfile_agentd_port(dockerfile) else {
        if port == super::DEFAULT_AGENT_PORT {
            return Ok(());
        }
        return Err(Error::invalid_arg(format!(
            "this client sends hooks.port={port} but the Dockerfile sets no usable \
             AGENTD_PORT, so the daemon will listen on its own default of {default}. These \
             must agree: the platform calls the build-time ready/validate hooks on the port \
             in the create call, and a daemon listening on {default} answers none of them — \
             the docker build succeeds, the daemon logs that it is listening, and the image \
             still fails with CREATE_FAILED naming no port. Add ENV AGENTD_PORT={port} to \
             the Dockerfile, or build it with default_dockerfile, which derives the value \
             from the same port.",
            default = super::DEFAULT_AGENT_PORT,
        )));
    };
    if found == port {
        return Ok(());
    }
    Err(Error::invalid_arg(format!(
        "the Dockerfile sets ENV AGENTD_PORT={found} but this client sends hooks.port={port}. \
         These must agree: the platform calls the build-time ready/validate hooks on the port \
         in the create call, and a daemon listening on {found} answers none of them — the \
         docker build succeeds, the daemon logs that it is listening, and the image still \
         fails with CREATE_FAILED naming no port. Set ENV AGENTD_PORT={port} in the \
         Dockerfile, or build it with default_dockerfile, which derives the value from the \
         same port."
    )))
}

/// The seconds in a Dockerfile's last `ENV AGENTD_SSE_KEEPALIVE_SECS=`, or `None` when it
/// sets none.
///
/// Same shape and same reasoning as [`dockerfile_agentd_port`]: last assignment wins, both
/// `ENV` spellings, and an unparseable value reads as absent because the daemon warns and
/// keeps its default for one (`agentd/src/config.rs:174-179`).
pub fn dockerfile_env_u64(dockerfile: &str, key: &str) -> Option<u64> {
    let mut found = None;
    for line in dockerfile.lines() {
        let mut words = line.split_whitespace();
        let Some(first) = words.next() else { continue };
        if !first.eq_ignore_ascii_case("ENV") {
            continue;
        }
        let Some(assignment) = words.next() else {
            continue;
        };
        let value = match assignment.split_once('=') {
            Some((name, value)) if name == key => value,
            Some(_) => continue,
            None if assignment == key => match words.next() {
                Some(value) => value,
                None => continue,
            },
            None => continue,
        };
        if let Ok(parsed) = value.parse() {
            found = Some(parsed);
        }
    }
    found
}

/// Rejects a Dockerfile whose `AGENTD_SSE_KEEPALIVE_SECS` is not shorter than the client's
/// stream idle timeout.
///
/// The fourth pair of this shape, found by sweeping for the other three. The daemon's SSE
/// keepalive interval and the client's tolerance for silence are set in different
/// repositories of truth — the interval in the caller's Dockerfile
/// (`agentd/src/config.rs:139`, default 15s at `:95`), the tolerance in
/// [`DEFAULT_STREAM_IDLE_TIMEOUT`](crate::session::exec::DEFAULT_STREAM_IDLE_TIMEOUT),
/// which is 60s *because* it is four times that 15. Raise the interval past the tolerance
/// and every attached stream treats a healthy connection as dead, reconnects
/// `max_reconnects` times, and fails.
///
/// The failure names both numbers and misattributes one of them: the client reports that the
/// stream "went silent for 60s, longer than the keepalive interval"
/// (`microvms-core/src/session/http.rs:383-387`), where 60 is its own timeout and the
/// keepalive interval is the number it does not know. So a reader is told the keepalive was
/// exceeded by a message that prints the wrong value for it, one build cycle after the
/// Dockerfile that caused it.
///
/// Equality is refused, not just excess: an interval exactly equal to the timeout races.
pub fn require_keepalive_under_idle_timeout(
    idle_timeout: Duration,
    dockerfile: &str,
) -> Result<(), Error> {
    let Some(secs) = dockerfile_env_u64(dockerfile, "AGENTD_SSE_KEEPALIVE_SECS") else {
        // Absent is safe here, unlike the port: the daemon's default of 15s is already
        // under every idle timeout this client will use, and a client that shortens its
        // timeout below 15s is not something a Dockerfile can be blamed for.
        return Ok(());
    };
    if secs < idle_timeout.as_secs() {
        return Ok(());
    }
    Err(Error::invalid_arg(format!(
        "the Dockerfile sets ENV AGENTD_SSE_KEEPALIVE_SECS={secs} but this client treats a \
         stream as dead after {timeout}s of silence. The keepalive must be shorter: the \
         daemon sends nothing between events except that keepalive, so an interval of \
         {secs}s makes a healthy stream look dead, and every attach reconnects until it \
         gives up. The error it raises then reports the client's own {timeout}s as though it \
         were the keepalive interval, so it names neither the Dockerfile nor {secs}. Leave \
         the variable unset for the daemon's 15s default, or keep it under {timeout}.",
        timeout = idle_timeout.as_secs(),
    )))
}

/// The value of a Dockerfile's last `ENTRYPOINT`, or `None` when it sets none.
///
/// Last rather than first for the reason [`dockerfile_agentd_port`] gives: a later
/// instruction of the same name wins at build time, so the last one is what the container
/// runs. The value is the raw rest of the line — this scan finds a disagreement, it does
/// not parse Dockerfile syntax.
pub fn dockerfile_entrypoint(dockerfile: &str) -> Option<&str> {
    last_instruction_value(dockerfile, "ENTRYPOINT")
}

/// The value of a Dockerfile's last `CMD`, or `None` when it sets none.
pub fn dockerfile_cmd(dockerfile: &str) -> Option<&str> {
    last_instruction_value(dockerfile, "CMD")
}

/// The rest of the last line whose first word is `keyword`, trimmed, or `None`.
fn last_instruction_value<'a>(dockerfile: &'a str, keyword: &str) -> Option<&'a str> {
    let mut found = None;
    for line in dockerfile.lines() {
        let trimmed = line.trim_start();
        let Some(first) = trimmed.split_whitespace().next() else {
            continue;
        };
        if first.eq_ignore_ascii_case(keyword) {
            found = Some(trimmed[first.len()..].trim());
        }
    }
    found
}

/// Whether an instruction value is the empty exec form — `[]`, with any spacing.
fn is_empty_exec_form(value: &str) -> bool {
    let mut meaningful = value.chars().filter(|c| !c.is_whitespace());
    meaningful.next() == Some('[') && meaningful.next() == Some(']') && meaningful.next().is_none()
}

/// Rejects a Dockerfile whose image would never run the daemon: no `CMD`, or an
/// `ENTRYPOINT` that swallows it.
///
/// The artifact unconditionally carries the daemon as entry `agentd` ([`build_artifact`]),
/// and the deployment invariant is `ENTRYPOINT []` plus `CMD ["/agentd"]` — see
/// [`default_dockerfile`]. A caller Dockerfile with no `CMD` runs the base's own default
/// instead, and a non-empty `ENTRYPOINT` turns any `CMD` into that entrypoint's arguments
/// rather than the process. Either way the daemon never starts, the build-time
/// `ready`/`validate` hooks go unanswered, and the image lands in `CREATE_FAILED` — or the
/// launch dies as a run-hook timeout — with nothing naming `CMD`, `ENTRYPOINT`, or the
/// artifact entry.
///
/// Weak-form on purpose: it does **not** check that the `CMD` names a path anything was
/// copied to. That would be Dockerfile interpretation rather than agreement checking, and
/// the two mistakes people actually make are the two refused here. The unenforceable half —
/// a base image that starts its own background process before bootstrap — stays with
/// whoever builds the image (`docs/PROTOCOL.md`, "Trust boundary").
pub fn require_daemon_cmd(dockerfile: &str) -> Result<(), Error> {
    if let Some(entrypoint) = dockerfile_entrypoint(dockerfile)
        && !is_empty_exec_form(entrypoint)
    {
        return Err(Error::invalid_arg(format!(
            "the Dockerfile sets ENTRYPOINT {entrypoint}, which makes any CMD its arguments \
             rather than the container's process — so the daemon this client uploads never \
             starts. The build succeeds anyway: the ready/validate hooks just go unanswered \
             and the image fails as CREATE_FAILED, or the launch dies as a run-hook timeout, \
             and neither symptom names ENTRYPOINT. Set `ENTRYPOINT []` alongside \
             `CMD [\"/agentd\"]`, or build with default_dockerfile, which sets both."
        )));
    }
    match dockerfile_cmd(dockerfile) {
        Some(cmd) if !cmd.is_empty() && !is_empty_exec_form(cmd) => Ok(()),
        _ => Err(Error::invalid_arg(
            "the Dockerfile has no CMD, so the container runs the base image's default \
             process and the daemon this client uploads never starts. The build succeeds \
             anyway: the ready/validate hooks just go unanswered and the image fails as \
             CREATE_FAILED, or the launch dies as a run-hook timeout, and neither symptom \
             names CMD. Add `ENTRYPOINT []` and `CMD [\"/agentd\"]`, or build with \
             default_dockerfile, which sets both."
                .to_string(),
        )),
    }
}

/// Rejects a caller Dockerfile that never mentions the lockfile the request carries.
///
/// The project files enter the artifact unconditionally once the request holds them, so a
/// custom Dockerfile that ignores them builds cleanly and produces an image with **no
/// environment layer** — and the symptom appears at launch, as every dependency
/// installing inside the guest, with nothing anywhere naming the `COPY` that was never
/// written. That is the silent-degradation shape: the caller asked for the layer, paid
/// the build, and got an image indistinguishable from one that never asked.
///
/// Weak-form on purpose, like [`require_daemon_cmd`]: a substring test for the lockfile's
/// name rather than parsing `COPY` syntax, because the check exists to catch a Dockerfile
/// that plainly ignores the files, not to validate how it consumes them.
pub fn require_project_install(project: &ProjectFiles, dockerfile: &str) -> Result<(), Error> {
    let lockfile = project.ecosystem.lockfile_name();
    if dockerfile.contains(lockfile) {
        return Ok(());
    }
    Err(Error::invalid_arg(format!(
        "the request carries project files ({lockfile} and {manifest}) but the Dockerfile \
         never mentions {lockfile}, so the image would bake no environment layer: the build \
         succeeds, and every launch still installs dependencies inside the guest with \
         nothing naming the COPY that was never written. Copy the pair and install from \
         the lockfile ({install}), or build with default_dockerfile, which derives both \
         lines — or drop the project files if the layer is not wanted.",
        manifest = project.ecosystem.manifest_name(),
        install = match project.ecosystem {
            Ecosystem::Uv => "uv sync --locked",
            Ecosystem::Npm => "npm ci",
            Ecosystem::Cargo => "cargo fetch --locked",
        },
    )))
}

/// Rejects a Dockerfile whose `FROM` is not the selected base image.
///
/// The build runs the Dockerfile *on top of* the base named in `baseImageArn`, so the two
/// disagreeing produces an image built from something other than the platform base whose
/// behaviour every `docs/PLATFORM.md` measurement describes — and nothing in the result
/// says so.
pub fn require_matching_from(base: &BaseImage, dockerfile: &str) -> Result<(), Error> {
    let Some(found) = dockerfile_from_ref(dockerfile) else {
        // No FROM at all is not this check's business: the build will say so, and a
        // Dockerfile validator is not what this is.
        return Ok(());
    };
    if found == base.docker_ref {
        return Ok(());
    }
    // The digest-pinned spelling of the same ref: `<docker_ref>@sha256:<64 hex>`. A digest
    // names one specific manifest of the ref this check already accepts, so it is a stricter
    // statement of agreement rather than a disagreement — a supply-chain-conscious caller
    // pins exactly this way, and refusing it would force them to un-pin to pass. The tag
    // must still be present and identical: `alpine@sha256:...` against a docker_ref of
    // `alpine:3` is a different claim and stays refused.
    if let Some(digest) = found
        .strip_prefix(base.docker_ref.as_str())
        .and_then(|rest| rest.strip_prefix("@sha256:"))
        && digest.len() == 64
        && digest.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Ok(());
    }
    Err(Error::invalid_arg(format!(
        "the Dockerfile's FROM is {found:?} but base image {:?} pairs with {:?}. These must \
         agree: baseImageArn and the FROM select the same base, and a mismatch builds against \
         a base none of the measured platform behaviour applies to. Use default_dockerfile \
         with this base, or pass a BaseImage whose docker_ref matches.",
        base.name, base.docker_ref
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULT_AGENT_PORT_FOR_TESTS: u16 = crate::control::DEFAULT_AGENT_PORT;

    /// The archive holds exactly the two entries the build expects, named as the build
    /// looks for them.
    #[test]
    fn the_artifact_holds_a_dockerfile_and_the_daemon() {
        let bytes = build_artifact(b"\x7fELF fake daemon", "FROM scratch\n", None).expect("zips");
        let mut archive =
            zip::ZipArchive::new(std::io::Cursor::new(bytes)).expect("a readable zip");

        let mut names: Vec<String> = (0..archive.len())
            .map(|index| archive.by_index(index).expect("entry").name().to_string())
            .collect();
        names.sort();
        assert_eq!(names, ["Dockerfile", "agentd"]);
    }

    /// The daemon entry carries mode 0755, and the Dockerfile entry does not have to.
    ///
    /// Read back out of the archive rather than asserted on the constant, because the
    /// constant being right and the zip entry carrying it are two different facts — and it
    /// is the second one that turns into a run-hook timeout.
    ///
    /// The permission bits are masked out of the returned mode: `zip` reports the whole
    /// Unix mode, so the entry reads as `0o100755` — `S_IFREG | 0o755`. Comparing the
    /// unmasked value against `0o755` fails while the archive is perfectly correct, which
    /// is a test that would have sent someone to debug the writer.
    #[test]
    fn the_daemon_entry_carries_the_execute_bit() {
        let bytes = build_artifact(b"binary", "FROM scratch\n", None).expect("zips");
        let mut archive =
            zip::ZipArchive::new(std::io::Cursor::new(bytes)).expect("a readable zip");
        let mode = archive
            .by_name("agentd")
            .expect("the daemon entry")
            .unix_mode()
            .expect("a mode was recorded");
        assert_eq!(
            mode & 0o777,
            AGENTD_MODE,
            "a non-executable binary surfaces as a run-hook timeout, not a permission error \
             (mode was {mode:o})"
        );
        assert!(
            mode & 0o111 != 0,
            "the execute bit is the whole point: {mode:o}"
        );
    }

    /// The bytes round-trip. A zip that holds a truncated or re-encoded binary produces an
    /// image whose CMD fails for a reason no message names.
    #[test]
    fn the_daemon_bytes_survive_the_round_trip() {
        use std::io::Read as _;

        // Deliberately includes a NUL and a high byte, which is what a real ELF has and
        // what a text-mode write would mangle.
        let binary: Vec<u8> = (0u8..=255).cycle().take(4096).collect();
        let bytes = build_artifact(&binary, "FROM scratch\n", None).expect("zips");
        let mut archive =
            zip::ZipArchive::new(std::io::Cursor::new(bytes)).expect("a readable zip");
        let mut read = Vec::new();
        archive
            .by_name("agentd")
            .expect("entry")
            .read_to_end(&mut read)
            .expect("reads");
        assert_eq!(read, binary);
    }

    /// **AC-2-3, the byte-scan guard.** The agent token must not appear anywhere in the
    /// artifact's raw bytes — including the project-file entries #74 added.
    ///
    /// A byte scan rather than an API review, because the leak this guards against is a
    /// *value* turning up somewhere — in the Dockerfile as an `ENV`, in a baked config
    /// file, in a stray argument — not a parameter being declared. Scanning the compressed
    /// bytes and the decompressed entries both, since deflate would hide a literal from a
    /// naive scan of the archive. The artifact under scan carries a full project-file
    /// pair, and the entry count is asserted so the scan cannot silently sweep fewer
    /// members than the artifact holds.
    ///
    /// **Falsification** — add the token to the Dockerfile (`ENV AGENT_TOKEN=…`, the
    /// plausible mistake) and the decompressed scan fails. Re-run for the #74 members
    /// (2026-08-31): append the token to the lockfile entry inside `build_artifact` and
    /// the per-entry scan fails naming `uv.lock`; it was restored.
    #[test]
    fn the_artifact_never_carries_the_agent_token() {
        use std::io::Read as _;

        let token = "s3cr3t-agent-token-do-not-bake-me";
        let dockerfile = default_dockerfile(9000, Some("/opt/work"), &BaseImage::al2023(), None);
        assert!(
            !dockerfile.contains(token),
            "the default Dockerfile must not mention a token"
        );

        let project = ProjectFiles {
            ecosystem: Ecosystem::Uv,
            manifest: b"[project]\nname = \"demo\"\n".to_vec(),
            lockfile: b"version = 1\n".to_vec(),
        };
        let bytes = build_artifact(b"binary", &dockerfile, Some(&project)).expect("zips");
        assert!(
            !bytes
                .windows(token.len())
                .any(|window| window == token.as_bytes()),
            "the token appears in the artifact's raw bytes"
        );

        let mut archive =
            zip::ZipArchive::new(std::io::Cursor::new(bytes)).expect("a readable zip");
        assert_eq!(
            archive.len(),
            4,
            "the scan below must sweep every member this artifact can hold"
        );
        for index in 0..archive.len() {
            let mut entry = archive.by_index(index).expect("entry");
            let name = entry.name().to_string();
            let mut content = Vec::new();
            entry.read_to_end(&mut content).expect("reads");
            assert!(
                !content
                    .windows(token.len())
                    .any(|window| window == token.as_bytes()),
                "the token appears inside {name} — the image snapshot is shared by every VM \
                 launched from it, so a token here is a token shared with every VM"
            );
        }
    }

    /// **#74, the build-context half.** A project's manifest and lockfile enter the
    /// archive under their ecosystem's fixed names, at the root beside the Dockerfile —
    /// the build context root, where the generated `COPY` finds them — and their bytes
    /// survive the round trip.
    #[test]
    fn project_files_enter_the_artifact_under_their_ecosystem_names() {
        use std::io::Read as _;

        for (ecosystem, manifest_name, lockfile_name) in [
            (Ecosystem::Uv, "pyproject.toml", "uv.lock"),
            (Ecosystem::Npm, "package.json", "package-lock.json"),
            (Ecosystem::Cargo, "Cargo.toml", "Cargo.lock"),
        ] {
            let project = ProjectFiles {
                ecosystem,
                manifest: b"manifest-bytes".to_vec(),
                lockfile: b"lockfile-bytes".to_vec(),
            };
            let bytes = build_artifact(b"binary", "FROM scratch\n", Some(&project)).expect("zips");
            let mut archive =
                zip::ZipArchive::new(std::io::Cursor::new(bytes)).expect("a readable zip");

            let mut names: Vec<String> = (0..archive.len())
                .map(|index| archive.by_index(index).expect("entry").name().to_string())
                .collect();
            names.sort();
            let mut expected = vec![
                "Dockerfile".to_string(),
                "agentd".to_string(),
                manifest_name.to_string(),
                lockfile_name.to_string(),
            ];
            expected.sort();
            assert_eq!(names, expected, "{ecosystem:?}");

            let mut lockfile = Vec::new();
            archive
                .by_name(lockfile_name)
                .expect("the lockfile entry")
                .read_to_end(&mut lockfile)
                .expect("reads");
            assert_eq!(lockfile, b"lockfile-bytes", "{ecosystem:?}");
        }
    }

    /// **#74's TRAP-5 design guard, the compile surface.** [`ProjectFiles`] has no field
    /// that could carry an entry *name* — the names come from the [`Ecosystem`] variant —
    /// so a caller cannot put `.env` or a credentials file into the shared snapshot
    /// through this type. Asserted by destructuring, the same shape as TRAP-1's
    /// `no_request_type_carries_a_caller_supplied_client_token`: a `name`/`path` field
    /// added later fails to compile this test.
    #[test]
    fn project_files_carry_no_caller_named_entry() {
        let ProjectFiles {
            ecosystem: _,
            manifest: _,
            lockfile: _,
        } = ProjectFiles {
            ecosystem: Ecosystem::Npm,
            manifest: Vec::new(),
            lockfile: Vec::new(),
        };
    }

    /// The content hash is a pure function of the two inputs: equal inputs agree, either
    /// input changing changes it, and the boundary between the two inputs is part of the
    /// identity.
    ///
    /// The boundary case is the one worth spelling out: without length prefixes,
    /// `("ab", "c")` and `("a", "bc")` hash the same concatenation — and a Dockerfile
    /// edit could then collide with a binary edit, serving a stale image for changed
    /// inputs, which is the exact hazard the hash exists to close.
    #[test]
    fn the_content_hash_follows_the_inputs_and_only_the_inputs() {
        let hash = artifact_content_hash(b"binary-bytes", "FROM scratch\n", None);
        assert_eq!(
            hash,
            artifact_content_hash(b"binary-bytes", "FROM scratch\n", None)
        );
        assert_eq!(hash.len(), 64, "sha256 as lowercase hex");
        assert!(
            hash.chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        );

        assert_ne!(
            hash,
            artifact_content_hash(b"other-bytes", "FROM scratch\n", None)
        );
        assert_ne!(
            hash,
            artifact_content_hash(b"binary-bytes", "FROM scratch\nRUN true\n", None)
        );
        assert_ne!(
            artifact_content_hash(b"ab", "c", None),
            artifact_content_hash(b"a", "bc", None),
            "the input boundary is part of the identity"
        );

        // The projectless digest is pinned to the exact value this function produced
        // before #74. Every image name `build --reuse` ever derived embeds this digest's
        // first twelve characters, so changing the no-project stream silently orphans
        // every existing reuse — the module docs call that hazard out for the zip's own
        // bytes, and it applies to the algorithm the same way. Recomputed independently
        // (Python hashlib, 2026-08-31) rather than pasted from this function's output.
        assert_eq!(
            hash, "90a0ef925a4696b0c0f142241b2c714e63f40071d0d02951e26cb2a1d8bb24fc",
            "the no-project digest must stay what it was before #74"
        );
    }

    /// **#74 step 3, the key.** The project files are part of the artifact identity:
    /// identical pairs agree, a lockfile edit is a new identity (and so a fresh image
    /// name and a fresh build), and the entry names count — the same bytes under a
    /// different ecosystem are a different identity, because the Dockerfile that consumes
    /// them installs a different environment.
    ///
    /// **Falsification** — run 2026-08-31 and again 2026-09-02: the lockfile's
    /// `hasher.update` lines were dropped from `artifact_content_hash` and the
    /// lockfile-edit assertion went red (two different lockfiles, one hash — the
    /// stale-reuse hazard verbatim); restored.
    #[test]
    fn the_content_hash_keys_on_the_lockfile() {
        let project = |ecosystem, lockfile: &[u8]| ProjectFiles {
            ecosystem,
            manifest: b"[project]".to_vec(),
            lockfile: lockfile.to_vec(),
        };
        let hash = artifact_content_hash(
            b"binary",
            "FROM scratch\n",
            Some(&project(Ecosystem::Uv, b"version = 1")),
        );
        assert_eq!(
            hash,
            artifact_content_hash(
                b"binary",
                "FROM scratch\n",
                Some(&project(Ecosystem::Uv, b"version = 1")),
            ),
            "identical dependency files share an image"
        );
        assert_ne!(
            hash,
            artifact_content_hash(
                b"binary",
                "FROM scratch\n",
                Some(&project(Ecosystem::Uv, b"version = 2")),
            ),
            "a lockfile edit is a new image name and a fresh build"
        );
        assert_ne!(
            hash,
            artifact_content_hash(b"binary", "FROM scratch\n", None),
            "carrying a layer is a different identity from carrying none"
        );
        assert_ne!(
            artifact_content_hash(
                b"binary",
                "FROM scratch\n",
                Some(&project(Ecosystem::Npm, b"version = 1")),
            ),
            hash,
            "the entry names count: the same bytes under another ecosystem install a \
             different environment"
        );
    }

    /// The Dockerfile's `FROM` comes from the base image, so the two cannot disagree.
    #[test]
    fn the_default_dockerfile_derives_its_from_from_the_base_image() {
        let base = BaseImage::al2023();
        let dockerfile = default_dockerfile(9000, None, &base, None);
        assert_eq!(
            dockerfile_from_ref(&dockerfile),
            Some(base.docker_ref.as_str())
        );
        assert!(dockerfile.contains(r#"CMD ["/agentd"]"#));
        assert!(
            dockerfile.contains("ENTRYPOINT []"),
            "the trust boundary rests on this line"
        );
        assert!(dockerfile.contains("ENV AGENTD_PORT=9000"));
    }

    /// A workdir is written as both a `mkdir` and a `WORKDIR`, and is absent when none was
    /// asked for. An empty string counts as none, since that is what an unset value looks
    /// like coming from a CLI flag.
    #[test]
    fn a_workdir_is_created_and_set_or_absent_entirely() {
        let base = BaseImage::al2023();
        let with = default_dockerfile(9000, Some("/opt/baked-workdir"), &base, None);
        assert!(with.contains("RUN mkdir -p /opt/baked-workdir"));
        assert!(with.contains("WORKDIR /opt/baked-workdir"));

        for none in [None, Some("")] {
            let without = default_dockerfile(9000, none, &base, None);
            assert!(!without.contains("WORKDIR"), "{without}");
        }
    }

    /// **#74 step 2, the install.** A project Dockerfile copies the ecosystem's pair into
    /// the working directory and installs from the lockfile with the lockfile-faithful
    /// spelling — `uv sync --locked`, `npm ci`, `cargo fetch --locked` — where the
    /// projectless Dockerfile installs nothing (the measured pre-#74 state).
    ///
    /// **Falsification** — run 2026-08-31: the `COPY` line was dropped from
    /// `default_dockerfile` and the copies-its-pair assertion went red for all three
    /// ecosystems; restored.
    #[test]
    fn a_project_dockerfile_installs_from_the_lockfile_it_can_see() {
        let base = BaseImage::al2023();
        for (ecosystem, install) in [
            (Ecosystem::Uv, "uv sync --locked"),
            (Ecosystem::Npm, "npm ci"),
            (Ecosystem::Cargo, "cargo fetch --locked"),
        ] {
            let dockerfile = default_dockerfile(9000, None, &base, Some(ecosystem));
            assert!(
                dockerfile.contains(&format!(
                    "COPY {} {} ./",
                    ecosystem.manifest_name(),
                    ecosystem.lockfile_name(),
                )),
                "copies its pair: {dockerfile}"
            );
            assert!(dockerfile.contains(install), "{dockerfile}");
            // The layer needs a directory even when the caller named none, because the
            // install resolves everything against the current directory.
            assert!(
                dockerfile.contains(&format!("WORKDIR {DEFAULT_PROJECT_WORKDIR}")),
                "{dockerfile}"
            );

            // A caller-named workdir is where the layer lives instead.
            let placed = default_dockerfile(9000, Some("/srv/app"), &base, Some(ecosystem));
            assert!(placed.contains("WORKDIR /srv/app"), "{placed}");
            assert!(!placed.contains(DEFAULT_PROJECT_WORKDIR), "{placed}");
        }

        let without = default_dockerfile(9000, None, &base, None);
        for install in ["uv sync", "npm ci", "cargo fetch", "COPY pyproject"] {
            assert!(!without.contains(install), "{without}");
        }
    }

    /// The project Dockerfile passes every guard the plain one does: the `FROM` matches
    /// its base, the port agrees, the daemon stays the `CMD`, and the pair it copies
    /// satisfies [`require_project_install`]. A derived Dockerfile that failed its own
    /// preflight would refuse every `--project` build at the door.
    #[test]
    fn the_project_dockerfile_passes_its_own_guards() {
        let base = BaseImage::al2023();
        for ecosystem in Ecosystem::ALL {
            let dockerfile = default_dockerfile(9000, None, &base, Some(ecosystem));
            require_matching_from(&base, &dockerfile).expect("its FROM is its base");
            require_matching_agentd_port(9000, &dockerfile).expect("its port agrees");
            require_daemon_cmd(&dockerfile).expect("the daemon is still the CMD");
            let project = ProjectFiles {
                ecosystem,
                manifest: Vec::new(),
                lockfile: Vec::new(),
            };
            require_project_install(&project, &dockerfile)
                .expect("it copies the pair it was derived for");
        }
    }

    /// **#74's silent-degradation guard.** A caller Dockerfile that never mentions the
    /// lockfile bakes no environment layer while building cleanly — the symptom is every
    /// launch still installing dependencies, with nothing naming the missing `COPY` — so
    /// it is refused up front, naming the lockfile, the install command, and both ways
    /// out.
    ///
    /// **Falsification** — run 2026-08-31: the `contains` check was inverted and both
    /// halves went red (the ignoring Dockerfile passed, the copying one was refused);
    /// restored.
    #[test]
    fn a_dockerfile_that_ignores_the_project_files_is_refused() {
        let project = ProjectFiles {
            ecosystem: Ecosystem::Npm,
            manifest: b"{}".to_vec(),
            lockfile: b"{}".to_vec(),
        };
        let error = require_project_install(
            &project,
            "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n\
             COPY agentd /agentd\n\
             ENTRYPOINT []\nCMD [\"/agentd\"]\n",
        )
        .expect_err("no mention of the lockfile bakes no layer");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("package-lock.json"), "{message}");
        assert!(message.contains("npm ci"), "{message}");
        assert!(message.contains("no environment layer"), "{message}");

        require_project_install(
            &project,
            "FROM x\nCOPY package.json package-lock.json ./\nRUN npm ci\nCMD [\"/agentd\"]\n",
        )
        .expect("a Dockerfile that copies the pair agrees");
    }

    /// `FROM` parsing tolerates the decoration a real Dockerfile carries: lowercase,
    /// leading whitespace, a `--platform` flag, and an `AS` alias.
    #[test]
    fn the_from_parser_ignores_decoration_rather_than_validating_syntax() {
        assert_eq!(dockerfile_from_ref("FROM alpine\n"), Some("alpine"));
        assert_eq!(dockerfile_from_ref("  from alpine:3\n"), Some("alpine:3"));
        assert_eq!(
            dockerfile_from_ref("FROM --platform=linux/arm64 alpine AS build\n"),
            Some("alpine")
        );
        assert_eq!(dockerfile_from_ref("RUN echo from nowhere\n"), None);
        assert_eq!(dockerfile_from_ref(""), None);
    }

    /// A `FROM` that disagrees with the base image is refused, and the message names both
    /// refs — the one found and the one expected — because "these must agree" without both
    /// values leaves the caller to guess which to change.
    #[test]
    fn a_dockerfile_from_that_disagrees_with_the_base_is_refused() {
        let base = BaseImage::al2023();
        let error = require_matching_from(&base, "FROM ubuntu:24.04\nCOPY agentd /agentd\n")
            .expect_err("ubuntu is not the managed base");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("ubuntu:24.04"), "{message}");
        assert!(message.contains(&base.docker_ref), "{message}");
        assert!(
            message.contains("none of the measured platform behaviour applies"),
            "{message}"
        );

        require_matching_from(&base, &default_dockerfile(9000, None, &base, None))
            .expect("the derived Dockerfile agrees with its own base");
    }

    /// A digest-pinned `FROM` of the agreeing ref passes: it names one specific manifest of
    /// the base this check already accepts, which is the spelling a supply-chain-conscious
    /// caller uses. The boundary cases stay refused — a digest on a *different* ref is still
    /// a disagreement, and a malformed digest is not a digest.
    #[test]
    fn a_digest_pinned_from_of_the_agreeing_ref_passes() {
        let base = BaseImage::al2023();
        let digest = "c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de";
        let pinned = format!(
            "FROM {}@sha256:{digest}\nCOPY agentd /agentd\n",
            base.docker_ref
        );
        require_matching_from(&base, &pinned).expect("a digest pin of the same ref agrees");

        require_matching_from(&base, &format!("FROM ubuntu:24.04@sha256:{digest}\n"))
            .expect_err("a digest does not launder a different ref");
        require_matching_from(&base, &format!("FROM {}@sha256:abc123\n", base.docker_ref))
            .expect_err("a 6-character digest is not a digest");
        require_matching_from(
            &base,
            &format!("FROM {}@sha256:{}\n", base.docker_ref, "g".repeat(64)),
        )
        .expect_err("64 non-hex characters are not a digest");
    }

    /// A Dockerfile with no `FROM` is not this check's business: the build will say so, and
    /// refusing here would be a Dockerfile validator rather than an agreement check.
    #[test]
    fn a_dockerfile_with_no_from_is_left_to_the_build() {
        require_matching_from(&BaseImage::al2023(), "COPY agentd /agentd\n")
            .expect("no FROM is not a disagreement");
    }

    /// The measured case, reproduced from the Dockerfile that spent it: a hand-written
    /// guest Dockerfile carrying `ENV AGENTD_PORT=8080` — the plausible port, and the wrong
    /// one — against a client sending `hooks.port=9000`. The build succeeds, the daemon logs
    /// `agentd listening`, and the image lands in `CREATE_FAILED` naming no port.
    #[test]
    fn a_dockerfile_port_that_disagrees_with_the_hook_port_is_refused() {
        let error = require_matching_agentd_port(
            crate::control::DEFAULT_AGENT_PORT,
            "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n\
             COPY agentd /agentd\n\
             ENV AGENTD_PORT=8080\n\
             EXPOSE 8080\n\
             CMD [\"/agentd\"]\n",
        )
        .expect_err("8080 is not the port the create call sends");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("8080"), "{message}");
        assert!(message.contains("9000"), "{message}");
        // The remedy is what a reader acts on, and the diagnostic's value is naming the
        // symptom that points away from the cause.
        assert!(message.contains("CREATE_FAILED"), "{message}");
        assert!(message.contains("ready/validate"), "{message}");

        require_matching_agentd_port(
            crate::control::DEFAULT_AGENT_PORT,
            &default_dockerfile(
                crate::control::DEFAULT_AGENT_PORT,
                Some("/work"),
                &BaseImage::al2023(),
                None,
            ),
        )
        .expect("the derived Dockerfile agrees with the port it was derived from");
    }

    /// Both `ENV` spellings set the variable, so a guard reading only `KEY=VALUE` would pass
    /// the legacy form it could not parse. The last assignment wins, as it does at build
    /// time.
    #[test]
    fn the_port_scan_reads_both_env_spellings_and_takes_the_last() {
        assert_eq!(dockerfile_agentd_port("ENV AGENTD_PORT=9000\n"), Some(9000));
        assert_eq!(dockerfile_agentd_port("ENV AGENTD_PORT 8080\n"), Some(8080));
        assert_eq!(dockerfile_agentd_port("env agentd_port=7000\n"), None);
        assert_eq!(
            dockerfile_agentd_port("ENV AGENTD_PORT=8080\nENV AGENTD_PORT=9000\n"),
            Some(9000),
        );
        assert_eq!(dockerfile_agentd_port("ENV AGENTD_LOG=info\n"), None);
        // Neither a missing variable nor an unparseable one is a disagreement: the daemon
        // keeps its own default for both (`agentd/src/config.rs:118`).
        assert_eq!(
            dockerfile_agentd_port("FROM x\nCOPY agentd /agentd\n"),
            None
        );
        assert_eq!(dockerfile_agentd_port("ENV AGENTD_PORT=nine\n"), None);
        require_matching_agentd_port(
            crate::control::DEFAULT_AGENT_PORT,
            "FROM x\nCOPY agentd /agentd\n",
        )
        .expect("no variable agrees with the default, which is what the daemon will use");
    }

    /// The other half of the pair, and the harder one to diagnose: a Dockerfile that names
    /// no port at all against a client that moved off the default. Silence is not neutral —
    /// `Config::from_env` keeps `9000` for an unset variable — so the guest listens on 9000
    /// while the hooks are dialled on the client's port, which is the measured failure with
    /// nothing in the Dockerfile to point at.
    #[test]
    fn a_dockerfile_naming_no_port_is_refused_when_the_client_moved_off_the_default() {
        let error = require_matching_agentd_port(
            8080,
            "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n\
             COPY agentd /agentd\n\
             CMD [\"/agentd\"]\n",
        )
        .expect_err("a silent Dockerfile leaves the daemon on 9000, not on 8080");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("8080"), "{message}");
        assert!(
            message.contains(&crate::control::DEFAULT_AGENT_PORT.to_string()),
            "the daemon's default is the value the reader has to learn: {message}"
        );
        assert!(message.contains("CREATE_FAILED"), "{message}");
        assert!(message.contains("ready/validate"), "{message}");

        // An unparseable value has the same consequence in the guest as no value, so it is
        // refused for the same reason rather than passed as "not this guard's business".
        require_matching_agentd_port(8080, "FROM x\nENV AGENTD_PORT=nine\n")
            .expect_err("a value the daemon cannot parse leaves it on its own default");

        // The default-port client is the common case and stays silent: the Dockerfile that
        // says nothing and the client that changed nothing already agree.
        require_matching_agentd_port(
            crate::control::DEFAULT_AGENT_PORT,
            "FROM x\nCOPY agentd /agentd\n",
        )
        .expect("silence agrees with the default");
    }

    /// The fourth pair of the `FROM`/`WORKDIR`/`AGENTD_PORT` shape, found by sweeping for
    /// the others: a keepalive interval the client's silence tolerance is shorter than. The
    /// resulting error prints the client's own timeout as though it were the keepalive, so
    /// the number a reader would search for never appears.
    #[test]
    fn a_keepalive_at_or_over_the_client_idle_timeout_is_refused() {
        let timeout = crate::session::exec::DEFAULT_STREAM_IDLE_TIMEOUT;
        assert_eq!(timeout.as_secs(), 60, "the guard's arithmetic assumes this");

        let error =
            require_keepalive_under_idle_timeout(timeout, "ENV AGENTD_SSE_KEEPALIVE_SECS=90\n")
                .expect_err("90s of scheduled silence exceeds a 60s tolerance");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("90"), "{message}");
        assert!(message.contains("60"), "{message}");

        // Equality races rather than passing: the timeout fires on the same tick the
        // keepalive is due.
        require_keepalive_under_idle_timeout(timeout, "ENV AGENTD_SSE_KEEPALIVE_SECS=60\n")
            .expect_err("an interval equal to the tolerance is a race, not a margin");

        require_keepalive_under_idle_timeout(timeout, "ENV AGENTD_SSE_KEEPALIVE_SECS 30\n")
            .expect("30s leaves a margin, in the legacy spelling");
        // The daemon's own default is 15s, so silence is safe for this pair.
        require_keepalive_under_idle_timeout(timeout, "FROM x\nCOPY agentd /agentd\n")
            .expect("an unset keepalive leaves the daemon at 15s");
        require_keepalive_under_idle_timeout(
            timeout,
            &default_dockerfile(
                crate::control::DEFAULT_AGENT_PORT,
                Some("/work"),
                &BaseImage::al2023(),
                None,
            ),
        )
        .expect("the derived Dockerfile sets no keepalive");
    }

    /// **Issue #46, both refusable halves.** A Dockerfile with no `CMD` builds an image
    /// that runs the base's default process; a non-empty `ENTRYPOINT` turns the `CMD` into
    /// its arguments. Either way the daemon the artifact carries never starts, and the
    /// failure surfaces as `CREATE_FAILED` or a run-hook timeout naming neither
    /// instruction — so the message must name both the instruction and that symptom.
    #[test]
    fn a_dockerfile_that_never_runs_the_daemon_is_refused_naming_the_symptom() {
        // No CMD at all: the base's default applies, which is not /agentd.
        let error = require_daemon_cmd(
            "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n\
             COPY agentd /agentd\n\
             ENV AGENTD_PORT=9000\n",
        )
        .expect_err("no CMD means the daemon never starts");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("no CMD"), "{message}");
        assert!(message.contains("run-hook timeout"), "{message}");
        assert!(message.contains("CREATE_FAILED"), "{message}");

        // A non-empty ENTRYPOINT swallows the CMD as its arguments.
        let error = require_daemon_cmd(
            "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n\
             COPY agentd /agentd\n\
             ENTRYPOINT [\"/bin/sh\", \"-c\"]\n\
             CMD [\"/agentd\"]\n",
        )
        .expect_err("a non-empty ENTRYPOINT makes CMD its arguments");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("ENTRYPOINT"), "{message}");
        assert!(message.contains("run-hook timeout"), "{message}");

        // An empty CMD is the same absence in different spelling.
        require_daemon_cmd("FROM x\nCMD []\n").expect_err("CMD [] runs nothing");
    }

    /// The shapes that must pass: the deployment invariant itself, in exec and spaced
    /// spellings, and the invariant's `ENTRYPOINT []` with any interior whitespace. The
    /// derived Dockerfile passes its own guard, which is what keeps the default path
    /// unaffected.
    #[test]
    fn the_deployment_invariant_passes_the_daemon_cmd_guard() {
        require_daemon_cmd(&default_dockerfile(
            9000,
            Some("/work"),
            &BaseImage::al2023(),
            None,
        ))
        .expect("the derived Dockerfile is the invariant");
        require_daemon_cmd("FROM x\nENTRYPOINT []\nCMD [\"/agentd\"]\n").expect("the invariant");
        require_daemon_cmd("FROM x\nENTRYPOINT [ ]\nCMD [\"/agentd\"]\n")
            .expect("interior whitespace is still the empty exec form");
        require_daemon_cmd("FROM x\ncmd [\"/agentd\"]\n").expect("case-insensitive, no ENTRYPOINT");
        // Last instruction wins, as at build time: a later empty ENTRYPOINT un-swallows.
        require_daemon_cmd("FROM x\nENTRYPOINT [\"/bin/sh\"]\nENTRYPOINT []\nCMD [\"/agentd\"]\n")
            .expect("the last ENTRYPOINT is the one the build uses");
        // And the reverse ordering is refused for the same reason.
        require_daemon_cmd("FROM x\nENTRYPOINT []\nENTRYPOINT [\"/bin/sh\"]\nCMD [\"/agentd\"]\n")
            .expect_err("the last ENTRYPOINT is non-empty");
    }

    /// Workdir inheritance is refused when neither the base nor the Dockerfile declares
    /// one, and the message says where the symptom would have appeared — in the guest, a
    /// build cycle later.
    #[test]
    fn inheriting_a_workdir_nothing_declares_is_refused() {
        let base = BaseImage::al2023();
        assert!(base.working_dir.is_empty(), "the measured case");

        let error = require_workdir(&base, None).expect_err("nothing declares a workdir");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("no WorkingDir"), "{message}");
        assert!(message.contains("nothing to inherit"), "{message}");
        assert!(message.contains("docs/PLATFORM.md"), "{message}");
    }

    /// It is accepted when *either* side declares one — the Dockerfile's `WORKDIR` or the
    /// base image's own `working_dir`. Both directions, because a check that only read the
    /// Dockerfile would refuse a caller on a purpose-built image that declares one.
    #[test]
    fn either_the_dockerfile_or_the_base_may_supply_the_workdir() {
        let base = BaseImage::al2023();
        require_workdir(&base, Some("FROM x\nWORKDIR /srv\n"))
            .expect("the Dockerfile declares one");

        let purpose_built = BaseImage {
            working_dir: "/app".to_string(),
            ..BaseImage::al2023()
        };
        require_workdir(&purpose_built, None).expect("the base image declares one");
    }

    /// A bare `WORKDIR` with no argument does not count as declaring one. It is the shape a
    /// truncated edit leaves behind, and treating it as a declaration would pass the guard
    /// on a Dockerfile that sets nothing.
    #[test]
    fn a_workdir_with_no_argument_does_not_count_as_a_declaration() {
        let error = require_workdir(&BaseImage::al2023(), Some("FROM x\nWORKDIR\n"))
            .expect_err("WORKDIR with no path declares nothing");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
    }

    /// **IMAGE-1, the acceptance test #220 names.** Wrapping a bare `FROM` produces exactly the
    /// default Dockerfile for that base, so the stanza has one source: the default generator's
    /// output minus its `FROM` line is what a wrap appends. Checked with and without a workdir,
    /// and on a non-default port, because each of those is a branch of the one stanza.
    #[test]
    fn wrapping_a_bare_from_is_the_default_dockerfile_minus_nothing() {
        for (port, workdir) in [
            (DEFAULT_AGENT_PORT_FOR_TESTS, None),
            (DEFAULT_AGENT_PORT_FOR_TESTS, Some("/srv/task")),
            (8080, None),
        ] {
            let base = BaseImage {
                docker_ref: "x".to_string(),
                ..BaseImage::al2023()
            };
            let default = default_dockerfile(port, workdir, &base, None);
            let opts = WrapOptions {
                port,
                workdir: workdir.map(str::to_string),
                ..WrapOptions::default()
            };
            assert_eq!(
                wrap_dockerfile("FROM x\n", &opts).expect("a bare FROM wraps"),
                default,
                "IMAGE-1: port {port} workdir {workdir:?}"
            );
            let (from, stanza) = default.split_once('\n').expect("lines");
            assert_eq!(from, "FROM x");
            assert!(stanza.ends_with("CMD [\"/agentd\"]\n"), "{stanza}");
        }
    }

    /// **IMAGE-2.** Whatever the task set — its own entrypoint, command, port, user — the
    /// wrapped result ends on the bootstrap invariant with the client's port, and a task that
    /// ends on another user has `USER root` restored ahead of the stanza. A task that ends on
    /// root, or never changes user, gets no extra line, which is what keeps IMAGE-1's equality.
    #[test]
    fn a_wrapped_task_ends_on_the_invariant_with_root_restored_when_needed() {
        let task = "FROM python:3.12-slim\nWORKDIR /app\nENV AGENTD_PORT=8080\n\
                    ENTRYPOINT [\"/bin/sh\", \"-c\"]\nCMD [\"python\", \"serve.py\"]\nUSER app\n";
        let wrapped = wrap_dockerfile(task, &WrapOptions::default()).expect("wraps");
        let added = wrapped
            .strip_prefix(task)
            .expect("the task text is kept verbatim");
        assert!(
            added.starts_with("USER root\nCOPY agentd /agentd\n"),
            "{added}"
        );
        assert_eq!(dockerfile_entrypoint(&wrapped), Some("[]"));
        assert_eq!(dockerfile_cmd(&wrapped), Some(r#"["/agentd"]"#));
        assert_eq!(dockerfile_agentd_port(&wrapped), Some(9000));
        require_daemon_cmd(&wrapped).expect("the daemon is the CMD");
        require_matching_agentd_port(9000, &wrapped).expect("the stanza's port wins");

        for (last_user, restored) in [
            ("USER root", false),
            ("USER 0", false),
            ("USER root:root", false),
            ("USER 0:0", false),
            ("user app", true),
            ("USER 1000:1000", true),
            ("USER root:staff", true),
            ("USER ${TASK_USER}", true),
        ] {
            let task = format!("FROM x\n{last_user}\n");
            let added = wrap_dockerfile(&task, &WrapOptions::default())
                .expect("wraps")
                .strip_prefix(task.as_str())
                .expect("verbatim")
                .to_string();
            assert_eq!(
                added.starts_with("USER root\n"),
                restored,
                "IMAGE-2: {last_user:?}: {added}"
            );
        }

        // Only the last USER counts, as at build time.
        let task = "FROM x\nUSER app\nUSER root\n";
        let wrapped = wrap_dockerfile(task, &WrapOptions::default()).expect("wraps");
        let added = wrapped.strip_prefix(task).expect("verbatim");
        assert!(added.starts_with("COPY agentd /agentd\n"), "{added}");
    }

    /// **IMAGE-2, the newline.** A task file without a trailing newline gets one before the
    /// stanza, in either line ending, so the stanza's first line is its own instruction.
    #[test]
    fn a_task_without_a_trailing_newline_is_normalized() {
        let wrapped = wrap_dockerfile("FROM x", &WrapOptions::default()).expect("wraps");
        assert!(
            wrapped.starts_with("FROM x\nCOPY agentd /agentd\n"),
            "{wrapped}"
        );
        let wrapped =
            wrap_dockerfile("FROM x\r\nRUN true", &WrapOptions::default()).expect("wraps");
        assert!(
            wrapped.starts_with("FROM x\r\nRUN true\nCOPY agentd /agentd\n"),
            "{wrapped}"
        );
        let wrapped = wrap_dockerfile("FROM x\r\n", &WrapOptions::default()).expect("wraps");
        assert!(wrapped.starts_with("FROM x\r\nCOPY agentd"), "{wrapped}");
    }

    /// **IMAGE-3.** Each refusal on its own, against an otherwise wrappable task, names its
    /// cause: no `FROM`, a trailing continuation (in the default and a custom escape
    /// character, across blank and comment lines, in either line ending), an open heredoc, a
    /// keepalive the client cannot tolerate, a bad workdir or port, and inheriting a workdir
    /// nothing declares.
    #[test]
    fn a_task_the_stanza_cannot_follow_is_refused_naming_the_cause() {
        let refused = |task: &str, opts: WrapOptions, cause: &str| {
            let error = wrap_dockerfile(task, &opts).expect_err(cause);
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
            let message = error.to_string();
            assert!(message.contains(cause), "IMAGE-3 {cause:?}: {message}");
        };
        let none = WrapOptions::default;
        refused("RUN echo hello\n", none(), "no FROM");
        refused("", none(), "no FROM");
        refused("FROM\n", none(), "no FROM");
        refused(
            "FROM x\nRUN apt-get update && \\\n",
            none(),
            "line continuation",
        );
        refused("FROM x\nRUN a \\   \n", none(), "line continuation");
        refused("FROM x\nRUN a \\\r\n", none(), "line continuation");
        refused(
            "FROM x\nRUN a \\\n\n# comment\n",
            none(),
            "line continuation",
        );
        refused("# escape=`\nFROM x\nRUN a `\n", none(), "line continuation");
        refused("FROM x\nRUN <<EOF\necho never closed\n", none(), "heredoc");
        refused("FROM x\nRUN <<-\"END\"\necho x\n", none(), "heredoc");
        refused("FROM x\nCOPY <<A <<B /dst/\na\nA\nb\n", none(), "heredoc");
        refused(
            "FROM x\nENV AGENTD_SSE_KEEPALIVE_SECS=90\n",
            none(),
            "AGENTD_SSE_KEEPALIVE_SECS=90",
        );
        for workdir in ["relative/dir", "/srv\nRUN evil", "/my dir", "/tab\there"] {
            refused(
                "FROM x\n",
                WrapOptions {
                    workdir: Some(workdir.to_string()),
                    ..none()
                },
                "absolute path",
            );
        }
        refused("FROM x\n", WrapOptions { port: 0, ..none() }, "port");
        refused(
            "FROM x\n",
            WrapOptions {
                inherit_workdir: true,
                ..none()
            },
            "nothing to inherit",
        );
    }

    /// **IMAGE-3, the other side.** What looks like a refusal but is not: a backslash under a
    /// backtick escape, a continuation that was finished, a closed heredoc (including a `<<-`
    /// terminator indented with tabs), a here-string, an escaped keepalive under the timeout,
    /// and inheriting a workdir the task or the options declare.
    #[test]
    fn a_finished_task_is_wrapped_even_when_it_looks_unfinished() {
        let none = WrapOptions::default;
        for task in [
            "# escape=`\nFROM x\nRUN a \\\n",
            "FROM x\nRUN a \\\n  && b\n",
            "FROM x\nRUN <<EOF\necho closed\nEOF\n",
            "FROM x\nRUN <<-EOF\n\techo closed\n\tEOF\n",
            "FROM x\nCOPY <<A <<B /dst/\na\nA\nb\nB\n",
            "FROM x\nRUN cat <<< word\n",
            "FROM x\n# a trailing comment \\\n",
            "FROM x\nENV AGENTD_SSE_KEEPALIVE_SECS=30\n",
        ] {
            wrap_dockerfile(task, &none())
                .unwrap_or_else(|error| panic!("{task:?} was refused: {error}"));
        }
        wrap_dockerfile(
            "FROM x\nWORKDIR /app\n",
            &WrapOptions {
                inherit_workdir: true,
                ..none()
            },
        )
        .expect("the task declares one");
        wrap_dockerfile(
            "FROM x\n",
            &WrapOptions {
                inherit_workdir: true,
                workdir: Some("/srv".to_string()),
                ..none()
            },
        )
        .expect("the options declare one");
        wrap_dockerfile(
            "FROM x\n",
            &WrapOptions {
                workdir: Some(String::new()),
                ..none()
            },
        )
        .expect("an empty workdir is none, as default_dockerfile reads it");
    }

    /// **IMAGE-4.** A derived base takes its ref from the first `FROM`, keeps the managed
    /// base's name (so `baseImageArn` is unchanged) and an empty working dir, and pairs with
    /// the Dockerfile it came from under the `FROM` guard — digest pins included, since the
    /// ref is taken whole. A Dockerfile with no `FROM` has nothing to derive.
    #[test]
    fn a_base_derived_from_a_dockerfile_pairs_with_it_by_construction() {
        let digest = "c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de";
        for (task, docker_ref) in [
            ("FROM python:3.12-slim\n", "python:3.12-slim".to_string()),
            (
                "FROM --platform=linux/arm64 golang:1.23 AS build\nFROM python:3.12-slim\n",
                "golang:1.23".to_string(),
            ),
            (
                &*format!("FROM ubuntu:24.04@sha256:{digest}\n"),
                format!("ubuntu:24.04@sha256:{digest}"),
            ),
        ] {
            let base = BaseImage::from_dockerfile(task).expect("a FROM");
            assert_eq!(base.docker_ref, docker_ref);
            assert_eq!(
                base.name,
                BaseImage::al2023().name,
                "IMAGE-4 keeps the managed base"
            );
            assert_eq!(base.working_dir, "");
            let wrapped = wrap_dockerfile(task, &WrapOptions::default()).expect("wraps");
            require_matching_from(&base, &wrapped).expect("IMAGE-4: the pair agrees");
            assert_eq!(BaseImage::from_dockerfile(&wrapped).expect("a FROM"), base);
        }
        // The inversion #220 removes: under the managed base, a task on another base is
        // refused.
        require_matching_from(
            &BaseImage::al2023(),
            &wrap_dockerfile("FROM python:3.12-slim\n", &WrapOptions::default()).expect("wraps"),
        )
        .expect_err("the managed base does not pair with python:3.12-slim");

        let error = BaseImage::from_dockerfile("RUN echo hello\n").expect_err("no FROM");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        assert!(error.to_string().contains("no FROM"), "{error}");
    }

    /// **The table `model/src/wrap.rs` specifies**, rendered as real Dockerfile text: every
    /// task and option combination the model enumerates, and the refusal (or the `USER root`
    /// verdict) the model's `specified` gives it. The model crate cannot be a dependency here
    /// (a path dependency without a version is a wildcard `deny.toml` refuses), so the table
    /// is restated as `expected` below and the two are kept equal by review.
    #[test]
    fn wrap_agrees_with_the_model_table() {
        #[derive(Debug)]
        enum Expect {
            Refused(&'static str),
            Wrapped { user_root: bool },
        }
        let digest = "c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de";
        let managed = BaseImage::al2023().docker_ref;
        let froms = [
            None,
            Some(managed.clone()),
            Some(format!("{managed}@sha256:{digest}")),
            Some("python:3.12-slim".to_string()),
        ];
        let mut checked = 0;
        for from in &froms {
            for tail in 0..3 {
                for user in 0..3 {
                    for declares_workdir in [false, true] {
                        for keepalive_too_long in [false, true] {
                            for workdir in 0..3 {
                                for inherit_workdir in [false, true] {
                                    let mut text = String::new();
                                    match from {
                                        Some(from) => text.push_str(&format!("FROM {from}\n")),
                                        None => text.push_str("RUN echo no base\n"),
                                    }
                                    if declares_workdir {
                                        text.push_str("WORKDIR /app\n");
                                    }
                                    if keepalive_too_long {
                                        text.push_str("ENV AGENTD_SSE_KEEPALIVE_SECS=60\n");
                                    }
                                    match user {
                                        1 => text.push_str("USER root\n"),
                                        2 => text.push_str("USER app\n"),
                                        _ => {}
                                    }
                                    match tail {
                                        1 => text.push_str("RUN make \\\n"),
                                        2 => text.push_str("RUN <<EOF\necho open\n"),
                                        _ => {}
                                    }
                                    let opts = WrapOptions {
                                        workdir: match workdir {
                                            1 => Some("/srv".to_string()),
                                            2 => Some("srv".to_string()),
                                            _ => None,
                                        },
                                        inherit_workdir,
                                        ..WrapOptions::default()
                                    };
                                    let expected = if from.is_none() {
                                        Expect::Refused("no FROM")
                                    } else if tail != 0 {
                                        Expect::Refused(if tail == 1 {
                                            "line continuation"
                                        } else {
                                            "heredoc"
                                        })
                                    } else if keepalive_too_long {
                                        Expect::Refused("AGENTD_SSE_KEEPALIVE_SECS")
                                    } else if workdir == 2 {
                                        Expect::Refused("absolute path")
                                    } else if inherit_workdir && !declares_workdir && workdir == 0 {
                                        Expect::Refused("nothing to inherit")
                                    } else {
                                        Expect::Wrapped {
                                            user_root: user == 2,
                                        }
                                    };
                                    let got = wrap_dockerfile(&text, &opts);
                                    match (&expected, &got) {
                                        (Expect::Refused(cause), Err(error)) => assert!(
                                            error.to_string().contains(cause),
                                            "{expected:?} for {text:?} {opts:?}: {error}"
                                        ),
                                        (Expect::Wrapped { user_root }, Ok(out)) => assert_eq!(
                                            out.strip_prefix(text.as_str())
                                                .expect("verbatim")
                                                .starts_with("USER root\n"),
                                            *user_root,
                                            "{text:?} {opts:?}"
                                        ),
                                        _ => panic!(
                                            "{expected:?} for {text:?} {opts:?}, got {got:?}"
                                        ),
                                    }
                                    checked += 1;
                                }
                            }
                        }
                    }
                }
            }
        }
        assert_eq!(
            checked,
            4 * 3 * 3 * 2 * 2 * 3 * 2,
            "every combination the model has"
        );
    }

    /// The base image ARN, for the region it is requested in.
    ///
    /// `microvm-image:` with a **colon**, which is the spelling for a managed base *and* for
    /// a customer image alike — measured 2026-08-15 against real ARNs in us-east-1, and the
    /// only form the model's `TaggableResource` pattern admits. The docs above this function
    /// carry the measurement.
    ///
    /// **Falsification** — change the separator to `/` and both assertions go red. That was
    /// the state twelve fakes and the transport encoding test were in, which is why this
    /// test now asserts the separator explicitly rather than only the region.
    #[test]
    fn the_base_image_arn_names_the_request_region_with_a_colon_separator() {
        assert_eq!(
            BaseImage::al2023().arn(&crate::region::Region::UsEast1),
            "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1"
        );
        assert_eq!(
            BaseImage::al2023().arn(&crate::region::Region::ApNortheast1),
            "arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1"
        );
        assert!(
            !BaseImage::al2023()
                .arn(&crate::region::Region::UsEast1)
                .contains("microvm-image/"),
            "the slash form answers AccessDeniedException, not a validation error"
        );
    }
}
