// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for IMAGE-1 through IMAGE-4: [`wrap_dockerfile`] and
//! [`BaseImage::from_dockerfile`] over arbitrary task Dockerfile text.
//!
//! `bolero::check!` runs this as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test wrap_case -p microvms-core --test wrap_fuzz -T 60s`.
//!
//! # What a case is
//!
//! A task Dockerfile assembled from pieces a real one carries — `FROM` in its decorated
//! spellings, `USER`, `WORKDIR`, `ENV`, `ENTRYPOINT`, `CMD`, comments, blank lines, line
//! continuations, heredocs, an escape directive — plus raw bytes, with or without a trailing
//! newline and in either line ending, and the wrap options.
//!
//! # What it checks, against oracles the function does not share
//!
//! * Nothing panics, and every refusal is an invalid-argument error (IMAGE-3).
//! * A wrapped result is the task text, then a newline when the task lacked one, then at most
//!   `USER root`, then exactly the default generator's output minus its `FROM` line for the
//!   same port and workdir (IMAGE-1). `USER root` appears only when the task has a `USER`
//!   line (IMAGE-2).
//! * The result's last `ENTRYPOINT` is `[]`, its last `CMD` is `["/agentd"]`, its
//!   `AGENTD_PORT` is the client's, its first `FROM` is the task's, and the create call's
//!   daemon, port, and `FROM` guards all accept it under the derived base (IMAGE-2, IMAGE-4).
//! * A task the generator knows to have no `FROM`, or to end inside a continuation or an open
//!   heredoc, is refused (IMAGE-3).

use microvms_core::ErrorKind;
use microvms_core::control::artifact::{
    BaseImage, WrapOptions, default_dockerfile, dockerfile_agentd_port, dockerfile_cmd,
    dockerfile_entrypoint, dockerfile_from_ref, require_daemon_cmd, require_matching_agentd_port,
    require_matching_from, wrap_dockerfile,
};

/// One piece of a task Dockerfile.
#[derive(Debug, bolero::TypeGenerator)]
enum Piece {
    From(u8),
    Run,
    User(u8),
    Workdir(bool),
    Env(u8, u16),
    Entrypoint(bool),
    Cmd(bool),
    Comment,
    Blank,
    /// A `RUN` whose line ends in the escape character.
    Continued,
    /// `RUN <<EOF`, opening a heredoc.
    HeredocOpen,
    /// The `EOF` that closes one.
    HeredocClose,
    /// Arbitrary bytes, read lossily as UTF-8.
    Raw(Vec<u8>),
}

#[derive(Debug, bolero::TypeGenerator)]
struct Case {
    /// Whether the text starts with `# escape=` + backtick.
    backtick_escape: bool,
    pieces: Vec<Piece>,
    trailing_newline: bool,
    crlf: bool,
    /// 0 none, 1 absolute, 2 relative, 3 with a line break, 4 empty.
    workdir: u8,
    inherit_workdir: bool,
    port: u16,
}

/// Pieces per case, so a run stays short.
const MAX_PIECES: usize = 16;

const FROMS: [&str; 6] = [
    "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal",
    "FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de",
    "FROM python:3.12-slim",
    "from --platform=linux/arm64 golang:1.23 AS build",
    "  FROM ${BASE}",
    "FROM",
];
const USERS: [&str; 6] = ["root", "0", "app", "root:root", "1000:1000", "${USER}"];

/// What the generator knows about the text it produced, independent of the function.
struct Rendered {
    text: String,
    /// The first `FROM` line names an image, when no raw bytes could have hidden or added
    /// one.
    known_from: Option<bool>,
    /// The text ends inside a continuation or an open heredoc, when the structured pieces
    /// alone determine that.
    known_unfinished: Option<bool>,
    has_user_line: bool,
}

fn render(case: &Case) -> Rendered {
    let escape = if case.backtick_escape { '`' } else { '\\' };
    let newline = if case.crlf { "\r\n" } else { "\n" };
    let mut lines: Vec<String> = Vec::new();
    if case.backtick_escape {
        lines.push("# escape=`".to_string());
    }
    let mut raw = false;
    let mut first_from: Option<bool> = None;
    let mut ambiguous = false;
    let mut continued = false;
    let mut heredoc_open = false;
    let mut has_user_line = false;
    for piece in case.pieces.iter().take(MAX_PIECES) {
        let line = match piece {
            Piece::From(pick) => {
                let from = FROMS[usize::from(*pick) % FROMS.len()];
                // The first line whose first word is FROM is the one `dockerfile_from_ref`
                // reads, wherever it sits; a bare `FROM` names nothing.
                first_from.get_or_insert(from != "FROM");
                from.to_string()
            }
            Piece::Run => "RUN echo hello".to_string(),
            Piece::User(pick) => {
                has_user_line = true;
                format!("USER {}", USERS[usize::from(*pick) % USERS.len()])
            }
            Piece::Workdir(with) => {
                if *with {
                    "WORKDIR /app".to_string()
                } else {
                    "WORKDIR".to_string()
                }
            }
            Piece::Env(which, value) => match which % 3 {
                0 => format!("ENV AGENTD_PORT={value}"),
                1 => format!("ENV AGENTD_SSE_KEEPALIVE_SECS={}", value % 120),
                _ => "ENV TASK=1".to_string(),
            },
            Piece::Entrypoint(empty) => {
                if *empty {
                    "ENTRYPOINT []".to_string()
                } else {
                    r#"ENTRYPOINT ["/bin/sh", "-c"]"#.to_string()
                }
            }
            Piece::Cmd(daemon) => {
                if *daemon {
                    r#"CMD ["/agentd"]"#.to_string()
                } else {
                    r#"CMD ["python", "serve.py"]"#.to_string()
                }
            }
            Piece::Comment => "# a comment".to_string(),
            Piece::Blank => String::new(),
            Piece::Continued => format!("RUN echo one {escape}"),
            Piece::HeredocOpen => "RUN <<EOF".to_string(),
            Piece::HeredocClose => "EOF".to_string(),
            Piece::Raw(bytes) => {
                raw = true;
                String::from_utf8_lossy(bytes).into_owned()
            }
        };
        // Track how the structured pieces leave the text: a continuation carries over blank
        // and comment lines, a heredoc runs until its terminator.
        match piece {
            Piece::Continued if !heredoc_open => continued = true,
            // A heredoc marker joined onto a continued line opens a heredoc of the joined
            // instruction; the generator does not follow that far.
            Piece::HeredocOpen if continued => ambiguous = true,
            Piece::HeredocOpen if !heredoc_open => heredoc_open = true,
            Piece::HeredocClose if heredoc_open => heredoc_open = false,
            Piece::Blank | Piece::Comment => {}
            _ if heredoc_open => {}
            _ => continued = false,
        }
        lines.push(line);
    }
    let mut text = lines.join(newline);
    if case.trailing_newline && !text.is_empty() {
        text.push_str(newline);
    }
    Rendered {
        text,
        known_from: (!raw).then_some(first_from.unwrap_or(false)),
        known_unfinished: (!raw && !ambiguous).then_some(continued || heredoc_open),
        has_user_line: has_user_line || raw,
    }
}

fn options(case: &Case) -> WrapOptions {
    WrapOptions {
        // Port 0 is not a port a control plane can hold; the wrap takes the plane's.
        port: case.port.max(1),
        workdir: match case.workdir % 5 {
            0 => None,
            1 => Some("/srv/task".to_string()),
            2 => Some("relative/dir".to_string()),
            3 => Some("/srv\nRUN evil".to_string()),
            _ => Some(String::new()),
        },
        inherit_workdir: case.inherit_workdir,
    }
}

/// The default generator's output for this port and workdir, minus its `FROM` line.
fn default_stanza(port: u16, workdir: Option<&str>) -> String {
    let full = default_dockerfile(port, workdir, &BaseImage::al2023(), None);
    let (_from, rest) = full.split_once('\n').expect("the default has lines");
    rest.to_string()
}

fn check(case: &Case) {
    let rendered = render(case);
    let task = rendered.text.as_str();
    let opts = options(case);
    let result = wrap_dockerfile(task, &opts);

    let out = match result {
        Err(error) => {
            assert_eq!(
                error.kind(),
                ErrorKind::InvalidArg,
                "IMAGE-3: a refusal is an invalid argument: {error}"
            );
            return;
        }
        Ok(out) => out,
    };
    assert_ne!(
        rendered.known_from,
        Some(false),
        "IMAGE-3: a task with no FROM was wrapped:\n{task}"
    );
    assert_ne!(
        rendered.known_unfinished,
        Some(true),
        "IMAGE-3: a task ending inside an unfinished instruction was wrapped:\n{task}"
    );

    // IMAGE-1: the task text, the newline it lacked, at most USER root, the default stanza.
    let added = out
        .strip_prefix(task)
        .unwrap_or_else(|| panic!("IMAGE-1: the result starts with the task text:\n{out}"));
    let added = if task.is_empty() || task.ends_with('\n') {
        added
    } else {
        added
            .strip_prefix('\n')
            .unwrap_or_else(|| panic!("IMAGE-2: the missing newline was supplied:\n{out}"))
    };
    let workdir = opts.workdir.as_deref().filter(|dir| !dir.is_empty());
    let stanza = default_stanza(opts.port, workdir);
    let restored = match added.strip_prefix("USER root\n") {
        Some(rest) => {
            assert_eq!(rest, stanza, "IMAGE-1: the stanza is the default's:\n{out}");
            true
        }
        None => {
            assert_eq!(
                added, stanza,
                "IMAGE-1: the stanza is the default's:\n{out}"
            );
            false
        }
    };
    assert!(
        !restored || rendered.has_user_line,
        "IMAGE-2: USER root was added to a task that sets no user:\n{task}"
    );

    // IMAGE-2: the bootstrap invariant and the client's port, read back by the scanners.
    assert_eq!(dockerfile_entrypoint(&out), Some("[]"), "{out}");
    assert_eq!(dockerfile_cmd(&out), Some(r#"["/agentd"]"#), "{out}");
    assert_eq!(dockerfile_agentd_port(&out), Some(opts.port), "{out}");
    require_daemon_cmd(&out).expect("IMAGE-2: the daemon is the CMD");
    require_matching_agentd_port(opts.port, &out).expect("IMAGE-2: the port agrees");
    assert_eq!(
        dockerfile_from_ref(&out),
        dockerfile_from_ref(task),
        "IMAGE-2: wrapping keeps the first FROM"
    );

    // IMAGE-4: the derived base pairs with the wrapped Dockerfile by construction.
    let base = BaseImage::from_dockerfile(&out).expect("IMAGE-4: a wrapped Dockerfile has a FROM");
    assert_eq!(base.name, BaseImage::al2023().name);
    assert_eq!(Some(base.docker_ref.as_str()), dockerfile_from_ref(&out));
    require_matching_from(&base, &out).expect("IMAGE-4: the FROM guard accepts the pair");
}

#[test]
fn wrap_case() {
    bolero::check!().with_type::<Case>().for_each(check);
}
