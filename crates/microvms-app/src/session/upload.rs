// SPDX-License-Identifier: Apache-2.0
//! Ownership handoff for an image's non-root default user. Root writes only a private
//! staging tree; all destination traversal and merging runs with the default user's
//! privileges. In particular, checking a symlink before a root recursive chown would
//! leave a race, even if chown itself did not follow links.

use std::time::Duration;

use crate::error::{Error, ErrorKind};

use super::{HttpRequest, ROOT_UID, Session, files, unauthenticated};

const SCRIPT: &str = include_str!("upload.sh");
const BUDGET: Duration = Duration::from_secs(60);

async fn run(session: &Session, command: Vec<String>, root: bool) -> Result<String, Error> {
    let mut request = protocol::exec::StartRequest::new(session.mint_exec_id(), command)
        .with_cwd(Some("/".into()))
        .with_env(std::collections::HashMap::from([
            ("PATH".into(), "/usr/sbin:/usr/bin:/sbin:/bin".into()),
            ("ENV".into(), String::new()),
            ("BASH_ENV".into(), String::new()),
            ("CDPATH".into(), String::new()),
            // Bash imports these before it reads the script, even as /bin/sh.
            ("SHELLOPTS".into(), String::new()),
            ("BASHOPTS".into(), String::new()),
            ("GLOBIGNORE".into(), String::new()),
        ]))
        .with_timeout_sec(Some(BUDGET.as_secs_f64()));
    if root {
        request = request.with_user(Some(ROOT_UID.into()));
    }
    let result = session
        .run_sync(request, BUDGET + super::DEFAULT_CLIENT_GRACE)
        .await?;
    if !result.succeeded() {
        return Err(Error::new(
            ErrorKind::ExecFailed,
            format!(
                "upload ownership handoff failed: {}",
                result.stderr().trim()
            ),
        ));
    }
    Ok(result.stdout().to_string())
}

async fn script(session: &Session, args: &[&str], root: bool) -> Result<String, Error> {
    let command = ["/bin/sh", "-c", SCRIPT, "microvms-upload"]
        .into_iter()
        .chain(args.iter().copied())
        .map(str::to_string)
        .collect();
    run(session, command, root).await
}

async fn identity(session: &Session) -> Result<(u32, u32), Error> {
    session
        .upload_identity
        .get_or_try_init(|| async {
            let response = session
                .transport
                .send(unauthenticated(HttpRequest::new("GET", "/v1/health")))
                .await?;
            match response
                .headers
                .get(protocol::health::DEFAULT_USER_CONFIGURED_HEADER)
                .map(String::as_str)
            {
                // Published peers predate the image-default feature. They must
                // retain raw uploads even in guests with no shell or id tool.
                None => return Ok((ROOT_UID, ROOT_UID)),
                Some("1") => {}
                Some(_) => {
                    return Err(Error::new(
                        ErrorKind::ExecFailed,
                        "invalid default user capability marker",
                    ));
                }
            }
            let output = run(
                session,
                vec!["/bin/sh".into(), "-c".into(), "id -u; id -g".into()],
                false,
            )
            .await?;
            let ids = output
                .split_whitespace()
                .map(str::parse::<u32>)
                .collect::<Result<Vec<_>, _>>()
                .map_err(|_| Error::new(ErrorKind::ExecFailed, "invalid default user identity"))?;
            match ids.as_slice() {
                [uid, gid] => Ok((*uid, *gid)),
                _ => Err(Error::new(
                    ErrorKind::ExecFailed,
                    "invalid default user identity",
                )),
            }
        })
        .await
        .copied()
}

pub(super) async fn upload(
    session: &Session,
    path: &str,
    data: &[u8],
    mode: Option<&str>,
    tar: bool,
) -> Result<(), Error> {
    let (uid, gid) = identity(session).await?;
    if uid == ROOT_UID {
        return if tar {
            files::upload_tar(&session.transport, path, data).await
        } else {
            files::upload_file(&session.transport, path, data, mode).await
        };
    }
    // The daemon's upload paths are absolute. Keep shell path splitting away from
    // ambiguous relative destinations, which could vary between the two users.
    if !path.starts_with('/') || path == "/" || path.ends_with('/') {
        return Err(Error::invalid_arg(
            "a non-root upload needs an absolute path without a trailing slash",
        ));
    }
    let uid = uid.to_string();
    let gid = gid.to_string();
    let staging = script(session, &["prepare", path, &uid, &gid], true).await?;
    let staging = staging.trim();
    if !staging.starts_with("/tmp/.microvms-upload-")
        || !staging["/tmp/.microvms-upload-".len()..]
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric())
        || staging == "/tmp/.microvms-upload-"
    {
        return Err(Error::new(
            ErrorKind::ExecFailed,
            "invalid upload staging directory",
        ));
    }
    let payload = format!("{staging}/payload");
    let mut handoff_started = false;
    let result = async {
        if tar {
            files::upload_tar(&session.transport, &payload, data).await?;
        } else {
            files::upload_file(&session.transport, &payload, data, mode).await?;
        }
        // Even a failed response can mean the command ran. Once ownership may
        // have changed, the path can name something a workload substituted.
        handoff_started = true;
        script(session, &["handoff", staging, &uid, &gid], true).await?;
        let operation = if tar { "install-tar" } else { "install-file" };
        script(session, &[operation, staging, path], false).await?;
        Ok(())
    }
    .await;
    if result.is_err() {
        // A handed-off tree is workload-controlled: never recurse through it as
        // root. Before handoff only root can remove the private 0700 stage.
        if script(session, &["cleanup", staging], false).await.is_err() && !handoff_started {
            let _ = script(session, &["cleanup-private", staging], true).await;
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::testing::{Recorder, Reply, session_with};
    use std::sync::Arc;

    fn exec(output: &str, exit: i32) -> [Reply; 3] {
        let finished = || {
            Reply::ok(serde_json::json!({
                "exec_id":"x", "phase":"exited",
                    "exit_code":exit, "signal":null, "timed_out":false,
                    "stdout":output,"stderr":"failure","truncated":false,
                    "writers_may_be_alive":false
            }))
        };
        [
            Reply::ok(serde_json::json!({"exec_id":"x","phase":"running"})),
            finished(),
            finished(),
        ]
    }

    fn configured() -> Reply {
        Reply::Body(200, Vec::new())
    }

    fn configured_recorder(replies: impl IntoIterator<Item = Reply>) -> Arc<Recorder> {
        Recorder::with_headers(
            replies,
            [(
                protocol::health::DEFAULT_USER_CONFIGURED_HEADER.into(),
                "1".into(),
            )],
        )
    }

    fn unprobed(recorder: &Arc<Recorder>) -> Session {
        let (mut session, _, _) = session_with(Arc::clone(recorder));
        session.upload_identity = Arc::new(tokio::sync::OnceCell::new());
        session
    }

    /// A shell-free old daemon retains direct PUTs. Even cloned sessions discover
    /// its missing capability marker only once and never attempt an exec.
    #[tokio::test]
    async fn root_uploads_remain_direct_and_identity_is_probed_once() {
        let recorder = Recorder::with([
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
            Reply::Body(200, Vec::new()),
        ]);
        let session = unprobed(&recorder);
        session
            .upload_file("/tmp/file", b"file", Some("0600"))
            .await
            .unwrap();
        session
            .clone()
            .upload_tar("/workspace", b"tar")
            .await
            .unwrap();
        let requests = recorder.requests();
        assert_eq!(requests.len(), 3);
        assert_eq!(requests[0].method, "GET");
        assert_eq!(requests[0].path, "/v1/health");
        assert_eq!(requests[1].path, "/v1/fs/file?path=%2Ftmp%2Ffile&mode=0600");
        assert_eq!(requests[1].body, b"file");
        assert_eq!(requests[2].path, "/v1/fs/tar?path=%2Fworkspace");
        assert_eq!(requests[2].body, b"tar");
    }

    /// The root handoff sees only a fresh private stage; the final copy always runs
    /// as the image's default user, for both file and tar uploads.
    #[tokio::test]
    async fn nonroot_uploads_stage_then_chown_as_root_then_install_as_default_user() {
        for tar in [false, true] {
            let replies = std::iter::once(configured())
                .chain(exec("1000\n1001\n", 0))
                .chain(exec("/tmp/.microvms-upload-0123456789\n", 0))
                .chain([Reply::Body(200, Vec::new())])
                .chain(exec("", 0))
                .chain(exec("", 0));
            let recorder = configured_recorder(replies);
            let session = unprobed(&recorder);
            upload(&session, "/workspace/project", b"bytes", Some("0600"), tar)
                .await
                .unwrap();
            let requests = recorder.requests();
            let start = |index: usize| -> serde_json::Value {
                serde_json::from_slice(&requests[index].body).unwrap()
            };
            let probe = start(1);
            assert_eq!(
                probe["command"],
                serde_json::json!(["/bin/sh", "-c", "id -u; id -g"])
            );
            assert!(probe["user"].is_null());
            assert_eq!(probe["env"]["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin");
            assert_eq!(probe["env"]["ENV"], "");
            assert_eq!(probe["env"]["BASH_ENV"], "");
            assert_eq!(probe["env"]["CDPATH"], "");
            assert_eq!(probe["env"]["SHELLOPTS"], "");
            assert_eq!(probe["env"]["BASHOPTS"], "");
            assert_eq!(probe["env"]["GLOBIGNORE"], "");
            assert_eq!(start(4)["command"][0], "/bin/sh");
            assert_eq!(start(4)["env"]["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin");
            assert_eq!(start(4)["user"], 0);
            assert_eq!(start(4)["command"][4], "prepare");
            assert_eq!(start(4)["command"][6], "1000");
            assert_eq!(start(4)["command"][7], "1001");
            assert!(
                requests[7]
                    .path
                    .contains("%2Ftmp%2F.microvms-upload-0123456789%2Fpayload")
            );
            assert_eq!(requests[7].body, b"bytes");
            assert_eq!(start(8)["user"], 0);
            assert_eq!(start(8)["command"][4], "handoff");
            assert!(start(11)["user"].is_null());
            assert_eq!(
                start(11)["command"][4],
                if tar { "install-tar" } else { "install-file" }
            );
            assert_eq!(start(11)["command"][6], "/workspace/project");
        }
    }

    /// A slow preparation may finish after its daemon timeout while the result is
    /// still in transit; the client grace must leave time to collect it and install.
    #[tokio::test(start_paused = true)]
    async fn nonroot_uploads_wait_for_completion_during_the_client_grace() {
        let during_grace = BUDGET + crate::session::DEFAULT_CLIENT_GRACE / 2;
        let [started, finished, acked] = exec("/tmp/.microvms-upload-0123456789\n", 0);
        let running = std::iter::repeat_with(|| {
            Reply::ok(serde_json::json!({"exec_id":"x", "phase":"running"}))
        })
        .take(during_grace.as_secs() as usize);
        let recorder = configured_recorder(
            std::iter::once(configured())
                .chain(exec("1000\n1001\n", 0))
                .chain([started])
                .chain(running)
                .chain([finished, acked, Reply::Body(200, Vec::new())])
                .chain(exec("", 0))
                .chain(exec("", 0)),
        );
        let session = unprobed(&recorder);
        let start = tokio::time::Instant::now();
        session
            .upload_file("/workspace/file", b"file", Some("0600"))
            .await
            .expect("the client grace permits collecting preparation and installing the file");
        assert_eq!(start.elapsed(), during_grace);
        let requests = recorder.requests();
        let starts: Vec<serde_json::Value> = requests
            .iter()
            .filter(|request| request.method == "POST" && request.path == "/v1/exec/start")
            .map(|request| serde_json::from_slice(&request.body).unwrap())
            .collect();
        assert_eq!(starts[1]["timeout_sec"], BUDGET.as_secs_f64());
        let install = starts.last().expect("the file was installed");
        assert_eq!(install["command"][4], "install-file");
        assert_eq!(install["command"][6], "/workspace/file");
        assert!(install["user"].is_null());
    }

    /// A failed identity probe or refused destination must not reach either upload
    /// route. A failed upload removes the private stage and preserves its error.
    #[tokio::test]
    async fn refused_uploads_do_not_install_and_failed_writes_clean_the_stage() {
        for status in [401, 503] {
            let recorder = configured_recorder(
                std::iter::once(Reply::Body(status, b"unavailable".to_vec()))
                    .chain(std::iter::once(configured()))
                    .chain(exec("0\n0\n", 0))
                    .chain([Reply::Body(200, Vec::new())]),
            );
            let session = unprobed(&recorder);
            assert!(session.upload_file("/tmp/file", b"x", None).await.is_err());
            session.upload_file("/tmp/file", b"x", None).await.unwrap();
            assert_eq!(
                recorder.requests().len(),
                6,
                "a failed discovery must not cache root"
            );
        }
        for path in ["relative/file", "/", "/workspace/"] {
            let recorder =
                configured_recorder(std::iter::once(configured()).chain(exec("1000\n1000\n", 0)));
            let session = unprobed(&recorder);
            let error = session.upload_file(path, b"file", None).await.unwrap_err();
            assert_eq!(error.kind(), ErrorKind::InvalidArg);
            assert_eq!(
                recorder.requests().len(),
                4,
                "invalid destinations never prepare a stage"
            );
        }
        for staging in [
            "",
            "/root/stage",
            "/tmp/.microvms-upload-",
            "/tmp/.microvms-upload-bad/path",
        ] {
            let recorder = configured_recorder(
                std::iter::once(configured())
                    .chain(exec("1000\n1000\n", 0))
                    .chain(exec(staging, 0)),
            );
            let session = unprobed(&recorder);
            let error = session
                .upload_file("/workspace/file", b"file", None)
                .await
                .unwrap_err();
            assert_eq!(error.kind(), ErrorKind::ExecFailed);
            assert_eq!(
                recorder.requests().len(),
                7,
                "an invalid stage never receives bytes"
            );
        }
        for output in ["", "1000\n", "1000\nno-group\n", "1000\n1000\nextra\n"] {
            let recorder =
                configured_recorder(std::iter::once(configured()).chain(exec(output, 0)));
            let session = unprobed(&recorder);
            assert!(session.upload_tar("/workspace", b"tar").await.is_err());
            assert_eq!(recorder.requests().len(), 4);
        }
        let recorder = configured_recorder(
            std::iter::once(configured())
                .chain(exec("1000\n1000\n", 0))
                .chain(exec("refused symlink", 1)),
        );
        let session = unprobed(&recorder);
        assert!(
            session
                .upload_file("/workspace/file", b"file", None)
                .await
                .is_err()
        );
        assert_eq!(recorder.requests().len(), 7);

        let recorder = configured_recorder(
            std::iter::once(configured())
                .chain(exec("1000\n1000\n", 0))
                .chain(exec("/tmp/.microvms-upload-0123456789\n", 0))
                .chain([Reply::Body(507, b"no space".to_vec())])
                .chain(exec("", 1))
                .chain(exec("", 0)),
        );
        let session = unprobed(&recorder);
        let error = session.upload_tar("/workspace", b"tar").await.unwrap_err();
        assert_eq!(
            error.wire_kind(),
            Some(crate::error::WireKind::InsufficientStorage)
        );
        let cleanup: serde_json::Value =
            serde_json::from_slice(&recorder.requests()[11].body).unwrap();
        assert_eq!(cleanup["command"][4], "cleanup-private");
        assert_eq!(cleanup["user"], 0);

        let recorder = configured_recorder(
            std::iter::once(configured())
                .chain(exec("1000\n1000\n", 0))
                .chain(exec("/tmp/.microvms-upload-0123456789\n", 0))
                .chain([Reply::Body(200, Vec::new())])
                .chain(exec("", 1))
                .chain(exec("", 1)),
        );
        let session = unprobed(&recorder);
        assert!(session.upload_tar("/workspace", b"tar").await.is_err());
        assert_eq!(
            recorder.requests().len(),
            14,
            "root cleanup after an attempted handoff could delete a substituted directory"
        );
        let cleanup: serde_json::Value =
            serde_json::from_slice(&recorder.requests()[11].body).unwrap();
        assert_eq!(cleanup["command"][4], "cleanup");
        assert!(cleanup["user"].is_null());
    }
}
