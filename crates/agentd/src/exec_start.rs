// SPDX-License-Identifier: Apache-2.0
//! Turning a start request into what gets spawned: the user, the group, the shell, and the
//! environment.
//!
//! Everything here runs **before** anything is spawned, and returns either a [`Plan`] or a
//! [`Rejection`] the handler answers with 400. That order is the property: the predecessor
//! validated its timeout inside the waiter, after the child was running, and a refusal for
//! a running child leaves an orphan nobody can see. The Stateright model in
//! `crates/model/src/exec_start.rs` checks the order and the layering over every request shape
//! (AGENTD-7 through AGENTD-16 in `verify/spec/agentd.symspec.json`); the unit tests below and the
//! bolero harness in `exec_start_fuzz.rs` check this code against the same statements.
//!
//! # Names are resolved here because only the guest can
//!
//! The daemon is PID 1 with the image's `/etc/passwd` and `/etc/group` in front of it; a
//! client outside the VM can only guess. The files are parsed directly rather than through
//! `getpwnam`, because the shipping binary is static musl, where NSS is the same file read
//! with fewer guarantees, and because a direct parse is a pure function the tests and the
//! fuzz harness can drive. Supplementary groups are not set: `Command::groups` is still
//! unstable, and std clears them anyway when a root daemon demotes (`setgroups(0)`).
//!
//! # A request that names no user
//!
//! It runs as the image's default user when the image sets `AGENTD_DEFAULT_USER`, and as the
//! daemon's own user (root) when it doesn't (AGENTD-22). The default is resolved here, per
//! request, to a passwd row: the row its name matches, or the row of the uid its digits
//! spell. Either way the child takes the row's uid, primary gid and identity, so a numeric
//! default doesn't keep the daemon's gid 0. A default with no row, digits included, is
//! refused as `unknown_user` with a detail that names the variable (AGENTD-24). A request's
//! own `user` always wins over the default, `root` and `0` included (AGENTD-23): the default
//! decides what a caller gets by not saying, not what a caller may ask for.
//!
//! # The environment is five layers, lowest first
//!
//! 1. The image environment, only when the request sets `inherit_image_env`: what the
//!    daemon inherited as the container `CMD`, minus every `AGENTD_*` variable, snapshotted
//!    once at startup ([`snapshot_image_env`]).
//! 2. The passwd identity: `HOME`, `USER` and `LOGNAME` from the user's row, when it has
//!    one. Above the image so a user demoted from root does not keep the image's root
//!    `HOME`.
//! 3. The launch environment from the run hook.
//! 4. The request's own `env`.
//!
//! Applied as separate `Command::envs` calls in that order, never as a pre-merged map, for
//! the reason `build_command` has always given: a merge is one `extend` in the wrong
//! direction away from inverting the precedence silently.
//!
//! # The token is in none of them
//!
//! The token arrives in the run-hook payload, after the snapshot is taken, and goes into its
//! own slot (`state.rs`). The daemon cannot write its own environment at all: `set_var` is
//! `unsafe` in edition 2024 and this crate forbids `unsafe`. The `AGENTD_` filter is the
//! second line: even a future daemon that did export something under that prefix would
//! not hand it to a child. The model's `LiveFilteredWithExportedToken` behavior is that
//! scenario.

use std::collections::HashMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use protocol::exec::{
    ERROR_MALFORMED_REQUEST, ERROR_UNKNOWN_GROUP, ERROR_UNKNOWN_SHELL, ERROR_UNKNOWN_USER,
    NameOrId, Shell, StartRequest,
};

use crate::config::DEFAULT_USER_VAR;

/// A child's environment, or one layer of it.
pub type Env = HashMap<String, String>;

/// The prefix of the daemon's own configuration, which never reaches a child.
pub const AGENTD_PREFIX: &str = "AGENTD_";

/// The directories searched for a named shell after the child's and the image's `PATH`.
pub const SHELL_FALLBACK_DIRS: [&str; 2] = ["/bin", "/usr/bin"];

/// The environment a daemon inherited, as the snapshot `inherit_image_env` layers in.
///
/// Drops every `AGENTD_*` variable (the daemon's configuration, AGENTD-12) and every
/// variable whose name or value is not UTF-8, which the wire cannot carry and a child could
/// not have been handed through a JSON launch env either. Called once, at startup, before
/// the daemon serves anything, which is also before any token exists.
pub fn snapshot_image_env(inherited: impl IntoIterator<Item = (OsString, OsString)>) -> Env {
    inherited
        .into_iter()
        .filter_map(|(key, value)| Some((key.into_string().ok()?, value.into_string().ok()?)))
        .filter(|(key, _)| !key.starts_with(AGENTD_PREFIX))
        .collect()
}

/// One row of `/etc/passwd`: the fields resolution reads.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PasswdRow {
    pub name: String,
    pub uid: u32,
    pub gid: u32,
    pub home: String,
}

/// One row of `/etc/group`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GroupRow {
    pub name: String,
    pub gid: u32,
}

/// The rows of a passwd file. Blank lines, comments, NIS `+`/`-` entries and lines that do
/// not have seven fields with numeric ids are skipped rather than failing the whole file: a
/// guest with one odd line still resolves every good one.
pub fn parse_passwd(text: &str) -> Vec<PasswdRow> {
    text.lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split(':').collect();
            let [name, _, uid, gid, _, home, _] = fields.as_slice() else {
                return None;
            };
            if name.is_empty() || name.starts_with(['#', '+', '-']) {
                return None;
            }
            Some(PasswdRow {
                name: (*name).to_string(),
                uid: uid.parse().ok()?,
                gid: gid.parse().ok()?,
                home: (*home).to_string(),
            })
        })
        .collect()
}

/// The rows of a group file, skipped by [`parse_passwd`]'s rules with four fields.
pub fn parse_group(text: &str) -> Vec<GroupRow> {
    text.lines()
        .filter_map(|line| {
            let fields: Vec<&str> = line.split(':').collect();
            let [name, _, gid, _] = fields.as_slice() else {
                return None;
            };
            if name.is_empty() || name.starts_with(['#', '+', '-']) {
                return None;
            }
            Some(GroupRow {
                name: (*name).to_string(),
                gid: gid.parse().ok()?,
            })
        })
        .collect()
}

/// A start request the daemon refuses before spawning: the 400's slug and detail.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Rejection {
    pub error: &'static str,
    pub detail: String,
}

impl Rejection {
    fn new(error: &'static str, detail: impl Into<String>) -> Self {
        Self {
            error,
            detail: detail.into(),
        }
    }
}

/// What the guest looks like to one start request.
pub struct Guest<'a> {
    /// The launch environment from the run hook.
    pub launch_env: &'a Env,
    /// The startup snapshot, if the daemon holds one.
    pub image_env: Option<&'a Env>,
    /// The passwd file's text. Empty when the file is absent.
    pub passwd: &'a str,
    /// The group file's text. Empty when the file is absent.
    pub group: &'a str,
    /// The image's default user (`Config::default_user`), for a request that names none.
    pub default_user: Option<&'a str>,
    /// Whether a path is an executable regular file. The real check is
    /// [`is_executable_file`]; tests and the fuzz harness hand in a fake filesystem.
    pub is_executable: &'a dyn Fn(&Path) -> bool,
}

/// How the command is run.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Program {
    /// `command[0]` with `command[1..]`.
    Argv { program: String, args: Vec<String> },
    /// `<shell> -c <script>`: `/bin/sh` for `shell: true`, the resolved path for a name.
    Script { shell: PathBuf, script: String },
}

/// Everything the spawn needs, decided before it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Plan {
    pub program: Program,
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    /// The environment layers, lowest first. Empty layers are kept so the order is fixed.
    pub layers: [Env; 4],
}

impl Plan {
    /// The child's environment as the layers compose it: each key from its highest layer.
    ///
    /// For the shell search and for tests. The spawn applies the layers one `envs` call at
    /// a time instead, which composes identically.
    pub fn environment(&self) -> Env {
        let mut env = Env::new();
        for layer in &self.layers {
            env.extend(layer.iter().map(|(k, v)| (k.clone(), v.clone())));
        }
        env
    }
}

/// A user as resolution found it.
struct User<'a> {
    uid: u32,
    row: Option<&'a PasswdRow>,
    /// Takes the row's primary gid when the request names no group: a name the request
    /// gave that matched a row, or the image's default user. A uid the request gave, as an
    /// integer or as digits, keeps the daemon's group (AGENTD-16).
    primary_gid: bool,
}

/// All ASCII digits that fit a `u32`: a string read as an id when it names no row.
fn digits(raw: &str) -> Option<u32> {
    if raw.is_empty() || !raw.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    raw.parse().ok()
}

/// A user by uid or name (AGENTD-7); a name that matches no row and is not all digits is
/// `unknown_user` (AGENTD-8).
fn resolve_user<'a>(spec: &NameOrId, rows: &'a [PasswdRow]) -> Result<User<'a>, Rejection> {
    let by_uid = |uid: u32| rows.iter().find(|row| row.uid == uid);
    match spec {
        NameOrId::Id(uid) => Ok(User {
            uid: *uid,
            row: by_uid(*uid),
            primary_gid: false,
        }),
        NameOrId::Name(name) => {
            if let Some(row) = rows.iter().find(|row| row.name == *name) {
                return Ok(User {
                    uid: row.uid,
                    row: Some(row),
                    primary_gid: true,
                });
            }
            match digits(name) {
                Some(uid) => Ok(User {
                    uid,
                    row: by_uid(uid),
                    primary_gid: false,
                }),
                None => Err(Rejection::new(
                    ERROR_UNKNOWN_USER,
                    format!("user {name:?} is not in the guest's /etc/passwd"),
                )),
            }
        }
    }
}

/// The image's default user as its passwd row (AGENTD-22): the row its name matches, or the
/// row of the uid its digits spell.
///
/// Stricter than a request's `user`, where digits with no row run as a bare uid. The default
/// runs every start that names no user, and a uid with no row has no primary gid to give it,
/// so the child would keep the daemon's gid 0. A default with no row is refused instead
/// (AGENTD-24). The error is the clause a refusal's detail ends with, naming the value and
/// the variable that set it, because the caller sent no name to look for and the fix is in
/// the image.
fn default_user_row<'a>(name: &str, rows: &'a [PasswdRow]) -> Result<&'a PasswdRow, String> {
    match resolve_user(&NameOrId::Name(name.to_string()), rows) {
        Ok(User { row: Some(row), .. }) => Ok(row),
        Ok(User { row: None, .. }) => Err(format!(
            "the default user {name:?} that {DEFAULT_USER_VAR} sets is a uid with no row in \
             the guest's /etc/passwd (the image needs a passwd row for it)"
        )),
        Err(_) => Err(format!(
            "the default user {name:?} that {DEFAULT_USER_VAR} sets is not in the guest's \
             /etc/passwd"
        )),
    }
}

/// The user a request resolves to: its own `user` when it names one (AGENTD-23), else the
/// image's default user's row (AGENTD-22), else none, which is the daemon's own user.
///
/// A default with no row is `unknown_user` like a request's unknown name (AGENTD-24), with
/// a detail that says the request named no user and which variable set the one that failed.
fn resolve_request_user<'a>(
    request: &StartRequest,
    default_user: Option<&str>,
    rows: &'a [PasswdRow],
) -> Result<Option<User<'a>>, Rejection> {
    match (&request.user, default_user) {
        (Some(spec), _) => resolve_user(spec, rows).map(Some),
        (None, Some(name)) => match default_user_row(name, rows) {
            Ok(row) => Ok(Some(User {
                uid: row.uid,
                row: Some(row),
                primary_gid: true,
            })),
            Err(why) => Err(Rejection::new(
                ERROR_UNKNOWN_USER,
                format!("the request names no user, and {why}"),
            )),
        },
        (None, None) => Ok(None),
    }
}

/// A group by gid or name (AGENTD-7); an unmatched name is `unknown_group` (AGENTD-8).
fn resolve_group(spec: &NameOrId, rows: &[GroupRow]) -> Result<u32, Rejection> {
    match spec {
        NameOrId::Id(gid) => Ok(*gid),
        NameOrId::Name(name) => rows
            .iter()
            .find(|row| row.name == *name)
            .map(|row| row.gid)
            .or_else(|| digits(name))
            .ok_or_else(|| {
                Rejection::new(
                    ERROR_UNKNOWN_GROUP,
                    format!("group {name:?} is not in the guest's /etc/group"),
                )
            }),
    }
}

/// Resolves a named shell (AGENTD-14): an absolute path is checked as given; a bare name is
/// searched on the child's `PATH`, then the image's `PATH`, then `/bin` and `/usr/bin`. A
/// miss is `unknown_shell` (AGENTD-15).
///
/// The child's `PATH` first because it is where the shell would look for everything else;
/// the image's next because a daemon started as the container `CMD` knows it even when the
/// child does not inherit it, and a lookup reveals nothing to the child. Relative `PATH`
/// entries are skipped: they would resolve against the daemon's directory, not the child's.
pub fn resolve_shell(
    name: &str,
    child_path: Option<&str>,
    image_path: Option<&str>,
    is_executable: &dyn Fn(&Path) -> bool,
) -> Result<PathBuf, Rejection> {
    if name.is_empty() {
        return Err(Rejection::new(
            ERROR_MALFORMED_REQUEST,
            "shell must be true, false, or a shell name, not an empty string",
        ));
    }
    if name.contains('/') {
        let path = Path::new(name);
        if path.is_absolute() && is_executable(path) {
            return Ok(path.to_path_buf());
        }
        return Err(Rejection::new(
            ERROR_UNKNOWN_SHELL,
            format!("shell {name:?} is not an executable file in the guest"),
        ));
    }
    let searched = child_path
        .into_iter()
        .chain(image_path)
        .flat_map(|path| path.split(':'))
        .chain(SHELL_FALLBACK_DIRS)
        .filter(|dir| dir.starts_with('/'));
    for dir in searched {
        let candidate = Path::new(dir).join(name);
        if is_executable(&candidate) {
            return Ok(candidate);
        }
    }
    Err(Rejection::new(
        ERROR_UNKNOWN_SHELL,
        format!(
            "shell {name:?} is not an executable file on the child's PATH, the image's PATH, \
             /bin or /usr/bin"
        ),
    ))
}

/// **The resolution.** A start request against a guest, to a plan or a 400.
///
/// User, then group, then the environment, then the shell (whose search reads the
/// environment's `PATH`), so the first field that cannot be resolved names the error. The
/// empty-argv check comes last, as it did in `build_command` before this module existed.
pub fn plan(request: &StartRequest, guest: &Guest<'_>) -> Result<Plan, Rejection> {
    let passwd = if request.user.is_some() || guest.default_user.is_some() {
        parse_passwd(guest.passwd)
    } else {
        Vec::new()
    };
    let user = resolve_request_user(request, guest.default_user, &passwd)?;

    let groups = match &request.group {
        Some(NameOrId::Name(_)) => parse_group(guest.group),
        _ => Vec::new(),
    };
    let gid = match &request.group {
        Some(spec) => Some(resolve_group(spec, &groups)?),
        // A named user takes its row's primary group, the way `su` and `docker -u name` do,
        // and so does the default user, however the image spelled it (AGENTD-22). A uid the
        // request gives keeps the daemon's group, which is what protocol 1 did (AGENTD-16).
        None => user
            .as_ref()
            .filter(|user| user.primary_gid)
            .and_then(|user| user.row)
            .map(|row| row.gid),
    };

    // The image layer: empty unless the request asks (AGENTD-10), lowest when it does
    // (AGENTD-11).
    let image = match (request.inherit_image_env, guest.image_env) {
        (true, Some(image)) => image.clone(),
        _ => Env::new(),
    };
    // The passwd identity, above the image and beneath the launch and the request (AGENTD-9).
    let mut identity = Env::new();
    if let Some(row) = user.as_ref().and_then(|user| user.row) {
        identity.insert("HOME".into(), row.home.clone());
        identity.insert("USER".into(), row.name.clone());
        identity.insert("LOGNAME".into(), row.name.clone());
    }
    let layers = [
        image,
        identity,
        guest.launch_env.clone(),
        request.env.clone(),
    ];

    let program = match &request.shell {
        Shell::Flag(false) => {
            let Some((program, args)) = request.command.split_first() else {
                return Err(Rejection::new(
                    ERROR_MALFORMED_REQUEST,
                    "command must not be empty when shell is false",
                ));
            };
            Program::Argv {
                program: program.clone(),
                args: args.to_vec(),
            }
        }
        // A single argument to `sh -c`, not a constructed wrapper: see `build_command`.
        Shell::Flag(true) => Program::Script {
            shell: PathBuf::from("/bin/sh"),
            script: request.command.join("\n"),
        },
        Shell::Named(name) => {
            let child_path = layers.iter().rev().find_map(|layer| layer.get("PATH"));
            let image_path = guest.image_env.and_then(|image| image.get("PATH"));
            Program::Script {
                shell: resolve_shell(
                    name,
                    child_path.map(String::as_str),
                    image_path.map(String::as_str),
                    guest.is_executable,
                )?,
                script: request.command.join("\n"),
            }
        }
    };

    Ok(Plan {
        program,
        uid: user.map(|user| user.uid),
        gid,
        layers,
    })
}

/// Whether `path` is a regular file with any execute bit set.
///
/// Any bit rather than the demoted user's: the daemon runs as root, and a shell only its
/// owner can execute is one the spawn will report as a 500 naming the errno, which is
/// honest; refusing it here would need the target user's credentials this check does not
/// have.
pub fn is_executable_file(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

/// A database file's text, or empty when it cannot be read. An absent `/etc/group` is a
/// guest where every group name is unknown, which the resolution then says by name.
pub fn read_database(path: &Path) -> String {
    match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) => {
            if err.kind() != std::io::ErrorKind::NotFound {
                tracing::warn!(path = %path.display(), %err, "cannot read a guest database");
            }
            String::new()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PASSWD: &str = "root:x:0:0:root:/root:/bin/bash\n\
        # a comment\n\
        \n\
        +nisuser::0:0:::\n\
        short:x:1\n\
        badid:x:abc:1:::\n\
        agent:x:1000:1001:Agent:/home/agent:/bin/bash\n\
        builder:x:1002:1002::/home/builder:/bin/sh\n";
    const GROUP: &str = "root:x:0:\nstaff:x:50:agent,builder\nagent:x:1001:\n";

    fn request(user: Option<NameOrId>, group: Option<NameOrId>, shell: Shell) -> StartRequest {
        StartRequest::new("e1", vec!["true".into()])
            .with_shell(shell)
            .with_user(user)
            .with_group(group)
    }

    fn env(pairs: &[(&str, &str)]) -> Env {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    /// A filesystem where exactly these paths are executable files.
    fn only(paths: &'static [&'static str]) -> impl Fn(&Path) -> bool {
        move |path: &Path| paths.iter().any(|p| Path::new(p) == path)
    }

    fn resolve(
        request: &StartRequest,
        launch: &Env,
        image: Option<&Env>,
    ) -> Result<Plan, Rejection> {
        resolve_in(request, launch, image, None)
    }

    /// [`resolve`] against a guest whose image sets `default_user`, with no launch or image
    /// environment.
    fn resolve_with_default(
        request: &StartRequest,
        default_user: Option<&str>,
    ) -> Result<Plan, Rejection> {
        resolve_in(request, &Env::new(), None, default_user)
    }

    fn resolve_in(
        request: &StartRequest,
        launch: &Env,
        image: Option<&Env>,
        default_user: Option<&str>,
    ) -> Result<Plan, Rejection> {
        let exec = only(&["/usr/bin/bash", "/bin/sh", "/image/bin/zsh"]);
        plan(
            request,
            &Guest {
                launch_env: launch,
                image_env: image,
                passwd: PASSWD,
                group: GROUP,
                default_user,
                is_executable: &exec,
            },
        )
    }

    /// The parser keeps every well-formed row and skips the rest without failing the file.
    #[test]
    fn the_passwd_parser_skips_what_it_cannot_read() {
        let rows = parse_passwd(PASSWD);
        assert_eq!(
            rows.iter().map(|row| row.name.as_str()).collect::<Vec<_>>(),
            ["root", "agent", "builder"]
        );
        assert_eq!(
            parse_group(GROUP)[1],
            GroupRow {
                name: "staff".into(),
                gid: 50
            }
        );
    }

    /// **AGENTD-7, AGENTD-9.** A name resolves to its row's uid, its primary gid, and its
    /// identity variables.
    #[test]
    fn a_name_resolves_to_its_row() {
        let plan = resolve(
            &request(Some("agent".into()), None, false.into()),
            &Env::new(),
            None,
        )
        .expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1000), Some(1001)));
        assert_eq!(
            plan.environment(),
            env(&[
                ("HOME", "/home/agent"),
                ("USER", "agent"),
                ("LOGNAME", "agent")
            ])
        );
    }

    /// **AGENTD-7.** A group name resolves against `/etc/group` and overrides the primary.
    #[test]
    fn a_group_name_resolves_and_overrides_the_primary_group() {
        let plan = resolve(
            &request(Some("agent".into()), Some("staff".into()), false.into()),
            &Env::new(),
            None,
        )
        .expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1000), Some(50)));
    }

    /// **AGENTD-8.** Unknown names are refused with their own slugs, naming the value.
    #[test]
    fn unknown_names_are_refused_by_name() {
        let refused = resolve(
            &request(Some("ghost".into()), None, false.into()),
            &Env::new(),
            None,
        )
        .expect_err("unknown user");
        assert_eq!(refused.error, ERROR_UNKNOWN_USER);
        assert!(refused.detail.contains("\"ghost\""));
        let refused = resolve(
            &request(None, Some("ghosts".into()), false.into()),
            &Env::new(),
            None,
        )
        .expect_err("unknown group");
        assert_eq!(refused.error, ERROR_UNKNOWN_GROUP);
        assert!(refused.detail.contains("\"ghosts\""));
    }

    /// Digits that name no row are an id, the way `docker exec -u 1000` reads them; digits
    /// that do name a row resolve as that name.
    #[test]
    fn digits_that_name_no_row_are_an_id() {
        let plan = resolve(
            &request(Some("4242".into()), Some("77".into()), false.into()),
            &Env::new(),
            None,
        )
        .expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(4242), Some(77)));
        assert!(plan.environment().is_empty(), "no row, no identity");
        // "1000" names no row by *name*, so it is uid 1000, whose row supplies the identity
        // but not the group: a number keeps the daemon's group.
        let plan = resolve(
            &request(Some("1000".into()), None, false.into()),
            &Env::new(),
            None,
        )
        .expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1000), None));
        assert_eq!(plan.environment()["HOME"], "/home/agent");
    }

    /// **AGENTD-16, AGENTD-9.** An integer uid keeps protocol 1's meaning: that uid, no gid
    /// change; its row, if any, supplies the identity; with no row, nothing.
    #[test]
    fn an_integer_uid_keeps_the_daemons_group() {
        let plan = resolve(
            &request(Some(1000.into()), None, true.into()),
            &Env::new(),
            None,
        )
        .expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1000), None));
        assert_eq!(
            plan.program,
            Program::Script {
                shell: "/bin/sh".into(),
                script: "true".into()
            }
        );
        assert_eq!(plan.environment()["USER"], "agent");
        let plan = resolve(
            &request(Some(4242.into()), Some(7.into()), false.into()),
            &Env::new(),
            None,
        )
        .expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(4242), Some(7)));
        assert!(plan.environment().is_empty());
    }

    /// **AGENTD-9, AGENTD-10, AGENTD-11.** image < passwd < launch < request, and without the
    /// flag the image layer is empty even when a snapshot exists.
    #[test]
    fn the_layers_stack_in_order() {
        let image = env(&[
            ("HOME", "/root"),
            ("PATH", "/image/bin"),
            ("ONLY_IMAGE", "i"),
        ]);
        let launch = env(&[("PATH", "/launch/bin"), ("LOGNAME", "launch")]);
        let mut req = request(Some("agent".into()), None, false.into());
        req.env = env(&[("USER", "request")]);

        let plan = resolve(&req, &launch, Some(&image)).expect("resolves");
        assert!(plan.layers[0].is_empty(), "the flag is unset");
        assert_eq!(
            plan.environment(),
            env(&[
                ("HOME", "/home/agent"),
                ("USER", "request"),
                ("LOGNAME", "launch"),
                ("PATH", "/launch/bin"),
            ])
        );

        req.inherit_image_env = true;
        let plan = resolve(&req, &launch, Some(&image)).expect("resolves");
        assert_eq!(
            plan.environment(),
            env(&[
                ("HOME", "/home/agent"),
                ("USER", "request"),
                ("LOGNAME", "launch"),
                ("PATH", "/launch/bin"),
                ("ONLY_IMAGE", "i"),
            ])
        );
    }

    /// **AGENTD-14, AGENTD-15.** A name is searched on the child's PATH, the image's PATH,
    /// then /bin and /usr/bin; an absolute path is checked as given; a miss is refused.
    #[test]
    fn a_named_shell_is_searched_in_order_and_a_miss_refused() {
        let image = env(&[("PATH", "relative:/image/bin")]);
        let found = |name: &str, req_path: Option<&str>| {
            let mut req = request(None, None, name.into());
            if let Some(path) = req_path {
                req.env = env(&[("PATH", path)]);
            }
            resolve(&req, &Env::new(), Some(&image)).map(|plan| plan.program)
        };
        let script = |shell: &str| {
            Ok(Program::Script {
                shell: shell.into(),
                script: "true".into(),
            })
        };
        assert_eq!(found("bash", None), script("/usr/bin/bash"));
        assert_eq!(
            found("zsh", None),
            script("/image/bin/zsh"),
            "the image PATH is searched"
        );
        assert_eq!(found("sh", Some("/usr/bin")), script("/bin/sh"));
        assert_eq!(found("/usr/bin/bash", None), script("/usr/bin/bash"));
        for missing in ["fish", "/bin/fish", "bin/bash", "../bash"] {
            let refused = found(missing, None).expect_err(missing);
            assert_eq!(refused.error, ERROR_UNKNOWN_SHELL, "{missing}");
            assert!(refused.detail.contains(missing));
        }
        assert_eq!(
            found("", None).expect_err("empty").error,
            ERROR_MALFORMED_REQUEST
        );
    }

    /// **AGENTD-8, AGENTD-15.** Resolution order names the first failure: user, then group,
    /// then shell; the empty-argv refusal is unchanged.
    #[test]
    fn the_first_unresolvable_field_names_the_error() {
        let all_bad = request(Some("ghost".into()), Some("ghosts".into()), "fish".into());
        assert_eq!(
            resolve(&all_bad, &Env::new(), None).unwrap_err().error,
            ERROR_UNKNOWN_USER
        );
        let group_bad = request(None, Some("ghosts".into()), "fish".into());
        assert_eq!(
            resolve(&group_bad, &Env::new(), None).unwrap_err().error,
            ERROR_UNKNOWN_GROUP
        );
        let mut empty = request(None, None, false.into());
        empty.command.clear();
        assert_eq!(
            resolve(&empty, &Env::new(), None).unwrap_err().error,
            ERROR_MALFORMED_REQUEST
        );
    }

    /// **AGENTD-22.** A request that names no user resolves the image's default to a passwd
    /// row: a name to its row, digits to the row of that uid, each with the row's uid,
    /// primary gid and identity, and a named group still overrides the primary. With no
    /// default it runs as the daemon, as it always did.
    #[test]
    fn a_request_that_names_no_user_resolves_the_default_as_its_user() {
        let nameless = request(None, None, false.into());

        let plan = resolve_with_default(&nameless, None).expect("resolves");
        assert_eq!(
            (plan.uid, plan.gid),
            (None, None),
            "no default, the daemon's"
        );
        assert!(plan.environment().is_empty(), "no default, no identity");

        let plan = resolve_with_default(&nameless, Some("agent")).expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1000), Some(1001)));
        assert_eq!(
            plan.environment(),
            env(&[
                ("HOME", "/home/agent"),
                ("USER", "agent"),
                ("LOGNAME", "agent")
            ])
        );
        assert_eq!(
            plan,
            resolve_with_default(&request(Some("agent".into()), None, false.into()), None)
                .expect("resolves"),
            "the default resolves exactly as the same name in the request"
        );

        // Digits are the row of that uid, with its primary gid: a request's "1000" keeps the
        // daemon's group, and a default's doesn't, or the child would keep gid 0.
        let plan = resolve_with_default(&nameless, Some("1000")).expect("resolves");
        assert_eq!(
            plan,
            resolve_with_default(&nameless, Some("agent")).expect("resolves"),
            "a uid the default spells resolves as its row's name"
        );
        let plan = resolve_with_default(&nameless, Some("0")).expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(0), Some(0)));
        assert_eq!(plan.environment()["HOME"], "/root");

        let grouped = request(None, Some("staff".into()), false.into());
        let plan = resolve_with_default(&grouped, Some("agent")).expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1000), Some(50)));
        let plan = resolve_with_default(&grouped, Some("1002")).expect("resolves");
        assert_eq!((plan.uid, plan.gid), (Some(1002), Some(50)));
    }

    /// **AGENTD-23.** A user the request names wins over the default, `root` and `0`
    /// included, and an unknown one is refused for its own name rather than replaced by the
    /// default; a default that names nobody doesn't stop a request that names someone.
    #[test]
    fn a_user_the_request_names_wins_over_the_default() {
        let root = resolve_with_default(
            &request(Some("root".into()), None, false.into()),
            Some("agent"),
        )
        .expect("resolves");
        assert_eq!((root.uid, root.gid), (Some(0), Some(0)));
        assert_eq!(root.environment()["HOME"], "/root");

        let zero =
            resolve_with_default(&request(Some(0.into()), None, false.into()), Some("agent"))
                .expect("resolves");
        assert_eq!(
            (zero.uid, zero.gid),
            (Some(0), None),
            "an integer keeps the group"
        );

        let builder = resolve_with_default(
            &request(Some("builder".into()), None, false.into()),
            Some("ghost"),
        )
        .expect("an unknown default is never resolved for a request that names a user");
        assert_eq!((builder.uid, builder.gid), (Some(1002), Some(1002)));

        let refused = resolve_with_default(
            &request(Some("ghost".into()), None, false.into()),
            Some("agent"),
        )
        .expect_err("an unknown user the request names");
        assert_eq!(refused.error, ERROR_UNKNOWN_USER);
        assert!(refused.detail.contains("\"ghost\""), "{}", refused.detail);
        assert!(
            !refused.detail.contains(DEFAULT_USER_VAR),
            "the caller named this user, so the detail is about it: {}",
            refused.detail
        );
    }

    /// **AGENTD-24.** A default with no passwd row, empty and all-digit included, refuses a
    /// request that names no user as `unknown_user`, with a detail naming the value and the
    /// variable, and it is resolved first, ahead of the group and the shell. Digits with no
    /// row say the image needs one, since a request's same digits would run.
    #[test]
    fn a_default_that_names_nobody_is_refused_naming_the_variable() {
        for default in ["ghost", "", "12a", "4242"] {
            let refused = resolve_with_default(&request(None, None, false.into()), Some(default))
                .expect_err(default);
            assert_eq!(refused.error, ERROR_UNKNOWN_USER, "{default:?}");
            assert!(
                refused.detail.contains(&format!("{default:?}"))
                    && refused.detail.contains(DEFAULT_USER_VAR),
                "{}",
                refused.detail
            );
            assert_eq!(
                refused.detail.contains("needs a passwd row"),
                default == "4242",
                "{}",
                refused.detail
            );
        }
        assert_eq!(
            resolve_with_default(
                &request(Some("4242".into()), None, false.into()),
                Some("4242")
            )
            .expect("a request's own digits run with no row")
            .uid,
            Some(4242)
        );
        let all_bad = request(None, Some("ghosts".into()), "fish".into());
        assert_eq!(
            resolve_with_default(&all_bad, Some("ghost"))
                .unwrap_err()
                .error,
            ERROR_UNKNOWN_USER
        );
    }

    /// **AGENTD-22, AGENTD-24.** The default's row is the one a name matches before the one a
    /// uid matches, so a user named with digits is that user, and a miss is the clause a
    /// refusal ends with.
    #[test]
    fn the_default_user_row_prefers_a_name_to_a_uid() {
        let rows =
            parse_passwd("1000:x:1500:1500::/home/digits:/bin/sh\nagent:x:1000:1001::/a:/bin/sh\n");
        assert_eq!(default_user_row("1000", &rows).map(|row| row.uid), Ok(1500));
        assert_eq!(default_user_row("1500", &rows).map(|row| row.gid), Ok(1500));
        assert_eq!(
            default_user_row("agent", &rows).map(|row| row.gid),
            Ok(1001)
        );
        let missing = default_user_row("77", &rows).expect_err("no row");
        assert!(missing.starts_with("the default user \"77\""), "{missing}");
    }

    /// **AGENTD-12.** The snapshot drops every `AGENTD_` variable and anything not UTF-8.
    #[test]
    fn the_snapshot_drops_agentd_configuration_and_non_utf8() {
        use std::os::unix::ffi::OsStringExt;
        let snapshot = snapshot_image_env([
            ("PATH".into(), "/usr/bin".into()),
            ("AGENTD_PORT".into(), "9000".into()),
            ("AGENTD_TOKEN".into(), "would-be-secret".into()),
            (OsString::from_vec(vec![0xff]), "x".into()),
            ("BAD_VALUE".into(), OsString::from_vec(vec![0xfe])),
        ]);
        assert_eq!(snapshot, env(&[("PATH", "/usr/bin")]));
    }

    /// **AGENTD-12.** A snapshot of this very process, taken after a bootstrap installed a
    /// token, does not contain the token: bootstrap never writes the process environment,
    /// and cannot, because `set_var` needs `unsafe` and this crate forbids it.
    #[test]
    fn a_snapshot_after_bootstrap_does_not_hold_the_token() {
        let state = crate::AppState::new(crate::Config::default());
        let token = "tok-snapshot-probe-5d1e";
        state.bootstrap(token.as_bytes(), Env::new());
        let snapshot = snapshot_image_env(std::env::vars_os());
        assert!(snapshot.values().all(|value| !value.contains(token)));
        assert!(snapshot.keys().all(|key| !key.starts_with(AGENTD_PREFIX)));
    }
}
