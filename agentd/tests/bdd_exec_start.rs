// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for AGENTD-7 through AGENTD-16, run against the daemon's real
//! router in-process.
//!
//! The scenarios live in `tests/features/exec_start.feature`, tagged with the requirement each
//! one verifies; this file is their step definitions and runner. It is a `harness = false`
//! test, so `cargo test` runs it on every CI system, and it writes a JUnit report when
//! `CUCUMBER_JUNIT` names a file (give that path absolutely: `cargo test` runs this binary from
//! the package directory).
//!
//! # How a scenario reaches a real child
//!
//! Each scenario builds an `AppState` whose passwd and group databases are files in its own
//! temporary directory, bootstraps it through the run-hook route with a token and a launch
//! environment, and posts `/v1/exec/start` through `routes::app` with the bearer token. The
//! answer is read off the response, and the child's output off `GET /v1/exec/{id}`. The guest
//! user "tester" carries this process's own uid and gid, because demotion to your own uid is
//! the one demotion that needs no root, so the scenarios spawn real demoted children on any
//! CI runner.
//!
//! "No child was spawned" is observed two ways: the registry has no entry for the exec id
//! (`GET /v1/exec/{id}` is 404), and the marker file the refused command would have created
//! does not exist after the refusal.

use std::collections::HashMap;
use std::io;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use agentd::{AppState, Config, routes};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use cucumber::gherkin::Step;
use cucumber::{World, WriterExt, cli, given, then, when, writer};
use serde_json::{Value, json};
use tower::ServiceExt;

/// The exec id every scenario starts: one per scenario world, so no two collide.
const EXEC_ID: &str = "bdd-exec";

#[derive(World)]
#[world(init = Self::new)]
struct Daemon {
    /// Holds the passwd, group and marker files for the scenario's life.
    dir: tempfile::TempDir,
    /// What the daemon inherited, before it filtered anything.
    inherited: HashMap<String, String>,
    state: Option<AppState>,
    token: String,
    /// The start body being composed.
    request: Value,
    /// The marker a refused command must not create.
    marker: Option<PathBuf>,
    /// The answer to the start.
    status: Option<StatusCode>,
    answer: Value,
    /// The finished child's poll body.
    outcome: Value,
    /// The last health body.
    health: Value,
    /// The last snapshot a scenario asked for.
    snapshot: HashMap<String, String>,
}

/// By hand, because `AppState` carries the token and has no `Debug` to derive through.
impl std::fmt::Debug for Daemon {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Daemon")
            .field("request", &self.request)
            .field("status", &self.status)
            .field("answer", &self.answer)
            .finish_non_exhaustive()
    }
}

impl Daemon {
    fn new() -> Self {
        Self {
            dir: tempfile::tempdir().expect("a scenario directory"),
            inherited: HashMap::new(),
            state: None,
            token: String::new(),
            request: json!({"exec_id": EXEC_ID}),
            marker: None,
            status: None,
            answer: Value::Null,
            outcome: Value::Null,
            health: Value::Null,
            snapshot: HashMap::new(),
        }
    }

    fn passwd(&self) -> PathBuf {
        self.dir.path().join("passwd")
    }

    fn group(&self) -> PathBuf {
        self.dir.path().join("group")
    }

    fn state(&self) -> &AppState {
        self.state
            .as_ref()
            .expect("the background bootstraps a daemon")
    }

    async fn call(&self, request: Request<Body>) -> (StatusCode, Value) {
        let response = routes::app(self.state().clone())
            .oneshot(request)
            .await
            .expect("the router answers");
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 22)
            .await
            .expect("a body");
        let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, body)
    }

    fn authorized(&self, builder: axum::http::request::Builder) -> axum::http::request::Builder {
        builder.header("authorization", format!("Bearer {}", self.token))
    }
}

/// `id -u` or `id -g` of this test process, as the child would print it.
fn own(flag: &str) -> String {
    let output = Command::new("id").arg(flag).output().expect("id runs");
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

/// A two-column Gherkin table as a map.
fn table(step: &Step) -> HashMap<String, String> {
    step.table
        .as_ref()
        .expect("the step carries a table")
        .rows
        .iter()
        .map(|row| (row[0].clone(), row[1].clone()))
        .collect()
}

#[given(
    expr = "a guest whose passwd lists {string} with this process's uid and gid and home {string}"
)]
async fn passwd_row(world: &mut Daemon, name: String, home: String) {
    let text = format!(
        "root:x:0:0:root:/root:/bin/bash\n# a comment line\n\n{name}:x:{}:{}:Tester:{home}:/bin/sh\n",
        own("-u"),
        own("-g")
    );
    std::fs::write(world.passwd(), text).expect("writes passwd");
}

#[given("the guest's passwd no longer lists this process's uid")]
async fn passwd_without_self(world: &mut Daemon) {
    std::fs::write(world.passwd(), "root:x:0:0:root:/root:/bin/bash\n").expect("writes passwd");
}

#[given(expr = "a guest whose group file lists {string} with this process's gid")]
async fn group_row(world: &mut Daemon, name: String) {
    std::fs::write(
        world.group(),
        format!("root:x:0:\n{name}:x:{}:tester\n", own("-g")),
    )
    .expect("writes group");
}

#[given("a daemon that inherited the environment:")]
async fn inherited(world: &mut Daemon, step: &Step) {
    world.inherited = table(step);
}

#[given(expr = "the run hook installed the token {string} with the launch environment:")]
async fn run_hook(world: &mut Daemon, token: String, step: &Step) {
    let config = Config {
        passwd_path: world.passwd(),
        group_path: world.group(),
        ..Config::default()
    };
    world.state = Some(AppState::with_image_env(config, world.inherited.clone()));
    world.token = token.clone();
    let body = json!({
        "runHookPayload": serde_json::to_string(&json!({
            "agent_token": token,
            "env": table(step),
        }))
        .expect("serializes"),
    });
    let request = Request::post(format!("{}/run", routes::HOOK_PREFIX))
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .expect("a request");
    let (status, _) = world.call(request).await;
    assert_eq!(status, StatusCode::OK, "the run hook bootstraps the daemon");
}

#[given(expr = "a start of {string} under shell {string}")]
async fn start_script(world: &mut Daemon, script: String, shell: String) {
    world.request["command"] = json!([script]);
    world.request["shell"] = match shell.as_str() {
        "true" => json!(true),
        "false" => json!(false),
        name => json!(name),
    };
}

#[given(expr = "a start that would leave a marker file, under shell {string}")]
async fn start_marker(world: &mut Daemon, shell: String) {
    let marker = world.dir.path().join("spawned");
    start_script(world, format!("touch {}", marker.display()), shell).await;
    world.marker = Some(marker);
}

#[given(expr = "a start of the argv {string}")]
async fn start_argv(world: &mut Daemon, argv: String) {
    world.request["command"] = json!(argv.split_whitespace().collect::<Vec<_>>());
    world.request["shell"] = json!(false);
}

#[given(expr = "the user named {string}")]
async fn user_named(world: &mut Daemon, name: String) {
    world.request["user"] = json!(name);
}

#[given("the user numbered with this process's uid")]
async fn user_numbered(world: &mut Daemon) {
    let uid: u32 = own("-u").parse().expect("a numeric uid");
    world.request["user"] = json!(uid);
}

#[given(expr = "the group named {string}")]
async fn group_named(world: &mut Daemon, name: String) {
    world.request["group"] = json!(name);
}

#[given(expr = "the request sets {string} to {string}")]
async fn request_env(world: &mut Daemon, key: String, value: String) {
    if !world.request["env"].is_object() {
        world.request["env"] = json!({});
    }
    world.request["env"][key] = json!(value);
}

#[given("the request inherits the image environment")]
async fn inherits(world: &mut Daemon) {
    world.request["inherit_image_env"] = json!(true);
}

#[when("the daemon answers the start")]
async fn answer(world: &mut Daemon) {
    let request = world
        .authorized(Request::post("/v1/exec/start"))
        .header("content-type", "application/json")
        .body(Body::from(world.request.to_string()))
        .expect("a request");
    let (status, body) = world.call(request).await;
    world.status = Some(status);
    world.answer = body;
    if status != StatusCode::OK {
        return;
    }
    for _ in 0..1_000 {
        let poll = world
            .authorized(Request::get(format!("/v1/exec/{EXEC_ID}")))
            .body(Body::empty())
            .expect("a request");
        let (_, body) = world.call(poll).await;
        if body["phase"] != "running" {
            world.outcome = body;
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("the exec never finished");
}

#[when("health is read")]
async fn health(world: &mut Daemon) {
    let request = Request::get("/v1/health")
        .body(Body::empty())
        .expect("a request");
    let (status, body) = world.call(request).await;
    assert_eq!(status, StatusCode::OK);
    world.health = body;
}

#[when("the daemon snapshots a startup environment of:")]
async fn snapshot(world: &mut Daemon, step: &Step) {
    let state = AppState::with_image_env(Config::default(), table(step));
    world.snapshot = state.image_env().cloned().unwrap_or_default();
}

#[then(expr = "the start was accepted and the child exited {int}")]
async fn accepted(world: &mut Daemon, code: i32) {
    assert_eq!(
        world.status,
        Some(StatusCode::OK),
        "the start was refused: {}",
        world.answer
    );
    assert_eq!(
        world.outcome["exit_code"],
        json!(code),
        "stdout {:?}, stderr {:?}",
        world.outcome["stdout"],
        world.outcome["stderr"]
    );
}

fn line(world: &Daemon, number: usize) -> String {
    world.outcome["stdout"]
        .as_str()
        .unwrap_or_default()
        .lines()
        .nth(number - 1)
        .unwrap_or_default()
        .to_string()
}

#[then(expr = "output line {int} is this process's uid")]
async fn line_uid(world: &mut Daemon, number: usize) {
    assert_eq!(line(world, number), own("-u"));
}

#[then(expr = "output line {int} is this process's gid")]
async fn line_gid(world: &mut Daemon, number: usize) {
    assert_eq!(line(world, number), own("-g"));
}

#[then(expr = "output line {int} is {string}")]
async fn line_is(world: &mut Daemon, number: usize, expected: String) {
    assert_eq!(
        line(world, number),
        expected,
        "stdout: {}",
        world.outcome["stdout"]
    );
}

#[then(expr = "output line {int} ends with {string}")]
async fn line_ends(world: &mut Daemon, number: usize, suffix: String) {
    let text = line(world, number);
    assert!(text.ends_with(&suffix), "line {number} is {text:?}");
}

#[then("the child's environment is exactly:")]
async fn environment(world: &mut Daemon, step: &Step) {
    let printed: HashMap<String, String> = world.outcome["stdout"]
        .as_str()
        .unwrap_or_default()
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
    assert_eq!(printed, table(step));
}

#[then("the child's output does not contain the token")]
async fn no_token(world: &mut Daemon) {
    let stdout = world.outcome["stdout"].as_str().unwrap_or_default();
    assert!(!stdout.contains(&world.token), "the token reached a child");
}

#[then(expr = "the child's output does not contain {string}")]
async fn output_lacks(world: &mut Daemon, text: String) {
    let stdout = world.outcome["stdout"].as_str().unwrap_or_default();
    assert!(!stdout.contains(&text), "{stdout}");
}

#[then(expr = "the answer is {int} with error {string} naming {string}")]
async fn refused(world: &mut Daemon, code: u16, error: String, named: String) {
    assert_eq!(world.status.map(|status| status.as_u16()), Some(code));
    assert_eq!(world.answer["error"], json!(error), "{}", world.answer);
    let detail = world.answer["detail"].as_str().unwrap_or_default();
    assert!(
        detail.contains(&named),
        "the detail names {named:?}: {detail}"
    );
}

#[then("no child was spawned")]
async fn nothing_spawned(world: &mut Daemon) {
    let poll = world
        .authorized(Request::get(format!("/v1/exec/{EXEC_ID}")))
        .body(Body::empty())
        .expect("a request");
    let (status, _) = world.call(poll).await;
    assert_eq!(
        status,
        StatusCode::NOT_FOUND,
        "a refused start registered an exec"
    );
    // Long enough for a spawned `touch` to have run.
    tokio::time::sleep(Duration::from_millis(300)).await;
    let marker = world.marker.as_ref().expect("the scenario named a marker");
    assert!(
        !marker.exists(),
        "the refused command ran: {}",
        marker.display()
    );
}

#[then(expr = "health reports {int} image environment keys")]
async fn health_count(world: &mut Daemon, keys: usize) {
    assert_eq!(
        world.health["image_env_keys"],
        json!(keys),
        "{}",
        world.health
    );
}

#[then(expr = "the health body does not contain {string}")]
async fn health_lacks(world: &mut Daemon, text: String) {
    assert!(
        !world.health.to_string().contains(&text),
        "{}",
        world.health
    );
}

#[then("the snapshot is exactly:")]
async fn snapshot_is(world: &mut Daemon, step: &Step) {
    assert_eq!(world.snapshot, table(step));
}

/// Flags `cargo test` forwards to every test binary, which cucumber's own parser refuses.
const LIBTEST_FLAGS: [&str; 6] = [
    "--exact",
    "--nocapture",
    "--quiet",
    "--test-threads",
    "--color",
    "--ignored",
];

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let features = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/features/exec_start.feature"
    );
    // A libtest filter naming something else selects nothing here, as libtest would.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_exec_start exec_start".contains(filter))
    {
        return;
    }
    let libtest = !filters.is_empty()
        || args
            .iter()
            .any(|arg| LIBTEST_FLAGS.iter().any(|flag| arg.starts_with(flag)));
    macro_rules! run {
        ($cucumber:expr) => {
            if libtest {
                $cucumber
                    .with_cli(cli::Opts::<_, _, _, cli::Empty>::default())
                    .run_and_exit(features)
                    .await
            } else {
                $cucumber.run_and_exit(features).await
            }
        };
    }
    // An undefined step is a failure, not a skip: a scenario that silently stops at a step
    // nobody wrote verifies nothing.
    let cucumber = Daemon::cucumber()
        .max_concurrent_scenarios(4)
        .fail_on_skipped();
    match std::env::var_os("CUCUMBER_JUNIT") {
        Some(path) => {
            let report = std::fs::File::create(&path).expect("the JUnit report file");
            run!(
                cucumber.with_writer(
                    writer::Basic::raw(io::stdout(), writer::Coloring::Never, 0)
                        .summarized()
                        .tee::<Daemon, _>(writer::JUnit::for_tee(report, 0))
                        .normalized(),
                )
            )
        }
        None => run!(cucumber),
    }
}
