// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for AGENTD-7 through AGENTD-16: start requests whose `user`, `group` and
//! `shell` are every JSON spelling the union types admit, deserialized and resolved against
//! a fuzzed guest.
//!
//! `bolero::check!` runs this as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test exec_start_fuzz::start_resolution -p agentd -T 120s`
//! (the `exec-start` job in `.github/workflows/fuzz.yml`).
//!
//! # What a case is
//!
//! A start body built as JSON (so the union deserialization is exercised, not bypassed),
//! a passwd file of fuzzed rows, a launch environment, the environment the daemon
//! "inherited" (which may carry `AGENTD_*` keys and a token-shaped value), and a fake
//! filesystem saying which shells exist. Names, keys and values come from small pools so
//! the interesting collisions — a name that matches a row, digits, a key set in two
//! layers — happen on almost every case rather than almost never.
//!
//! # What it checks
//!
//! Each property is the model's (`model/src/exec_start.rs`) stated over bytes instead of
//! symbols: validation refuses exactly the unresolvable names and shells (AGENTD-8,
//! AGENTD-15); a resolved name runs as its row (AGENTD-7) with its identity under the caller's
//! layers (AGENTD-9); the image layer is absent without the flag (AGENTD-10) and lowest with it
//! (AGENTD-11); no `AGENTD_*` image key and no token reaches the plan (AGENTD-12); the snapshot's
//! count is its non-`AGENTD_` keys (AGENTD-13); a named shell runs as the file it resolved to
//! (AGENTD-14); integers and booleans mean what they did (AGENTD-16). A second check feeds raw
//! bytes to the `StartRequest` deserializer, which must refuse or accept but never panic.

use std::path::Path;

use bolero::TypeGenerator;
use protocol::exec::{
    ERROR_MALFORMED_REQUEST, ERROR_UNKNOWN_GROUP, ERROR_UNKNOWN_SHELL, ERROR_UNKNOWN_USER,
    NameOrId, Shell, StartRequest,
};
use serde_json::{Value, json};

use crate::exec_start::{Env, Guest, Program, parse_passwd, plan, snapshot_image_env};

const NAMES: [&str; 8] = [
    "root", "agent", "builder", "ghost", "1000", "4242", "", "12a",
];
const SHELLS: [&str; 8] = [
    "bash",
    "sh",
    "zsh",
    "fish",
    "/usr/bin/bash",
    "/bin/fish",
    "bin/bash",
    "",
];
const KEYS: [&str; 7] = [
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "JAVA_HOME",
    "AGENTD_PORT",
    "AGENTD_TOKEN",
];
/// The ids rows and integer users draw from, so an integer user matches a row on most cases.
const IDS: [u32; 5] = [0, 1000, 1002, 4242, 65534];
const VALUES: [&str; 4] = ["/a", "/b:/usr/local/bin", "/c", "v"];
/// Placed in the inherited environment under `AGENTD_TOKEN` when a case asks, standing in
/// for a daemon that exported its token: the filter must still keep it from every child.
const TOKEN: &str = "tok-fuzz-3c9e7a";
/// The shells the fake filesystem holds.
const PRESENT: [&str; 4] = ["/usr/bin/bash", "/bin/sh", "/usr/local/bin/zsh", "/c/fish"];

fn pick<'a>(pool: &[&'a str], index: u8) -> &'a str {
    pool[usize::from(index) % pool.len()]
}

#[derive(Clone, Copy, Debug, TypeGenerator)]
enum Principal {
    Absent,
    /// An id from [`IDS`].
    Int(u8),
    /// Any id at all.
    Wide(u32),
    Str(u8),
}

#[derive(Clone, Copy, Debug, TypeGenerator)]
enum ShellSpec {
    Flag(bool),
    Named(u8),
}

#[derive(Clone, Copy, Debug, TypeGenerator)]
struct Row {
    name: u8,
    uid: u8,
    gid: u8,
    home: u8,
}

#[derive(Debug, TypeGenerator)]
struct Case {
    user: Principal,
    group: Principal,
    shell: ShellSpec,
    inherit: bool,
    request_env: Vec<(u8, u8)>,
    launch_env: Vec<(u8, u8)>,
    inherited: Vec<(u8, u8)>,
    export_token: bool,
    rows: Vec<Row>,
}

fn env_of(pairs: &[(u8, u8)]) -> Env {
    pairs
        .iter()
        .take(6)
        .map(|(key, value)| {
            (
                pick(&KEYS, *key).to_string(),
                pick(&VALUES, *value).to_string(),
            )
        })
        .collect()
}

fn principal_json(principal: Principal) -> Option<Value> {
    match principal {
        Principal::Absent => None,
        Principal::Int(index) => Some(json!(IDS[usize::from(index) % IDS.len()])),
        Principal::Wide(id) => Some(json!(id)),
        Principal::Str(index) => Some(json!(pick(&NAMES, index))),
    }
}

/// The start body as a client would send it.
fn body(case: &Case) -> Value {
    let mut body = json!({
        "exec_id": "fuzz",
        "command": ["echo hi"],
        "inherit_image_env": case.inherit,
        "env": env_of(&case.request_env),
        "shell": match case.shell {
            ShellSpec::Flag(flag) => json!(flag),
            ShellSpec::Named(index) => json!(pick(&SHELLS, index)),
        },
    });
    if let Some(user) = principal_json(case.user) {
        body["user"] = user;
    }
    if let Some(group) = principal_json(case.group) {
        body["group"] = group;
    }
    body
}

fn passwd_text(rows: &[Row]) -> String {
    rows.iter()
        .take(5)
        .map(|row| {
            format!(
                "{}:x:{}:{}::{}:/bin/sh\n",
                pick(&NAMES, row.name),
                IDS[usize::from(row.uid) % IDS.len()],
                IDS[usize::from(row.gid) % IDS.len()] + 1,
                pick(&VALUES, row.home)
            )
        })
        .collect()
}

fn digits(raw: &str) -> Option<u32> {
    (!raw.is_empty() && raw.bytes().all(|b| b.is_ascii_digit()))
        .then(|| raw.parse().ok())
        .flatten()
}

#[test]
fn start_resolution() {
    bolero::check!().with_type::<Case>().for_each(|case| {
        let body = body(case);
        let request: StartRequest =
            serde_json::from_value(body.clone()).expect("every generated body is well-formed");

        // The unions write back as they came: an integer stays an integer and a boolean a
        // boolean, which is what keeps an old daemon reading a new client (AGENTD-16).
        let written = serde_json::to_value(&request).expect("serializes");
        for field in ["user", "group", "shell"] {
            if let Some(sent) = body.get(field) {
                assert_eq!(&written[field], sent, "{field} changed shape on the way back");
            }
        }

        let mut inherited = env_of(&case.inherited);
        if case.export_token {
            inherited.insert("AGENTD_TOKEN".into(), TOKEN.into());
        }
        let snapshot = snapshot_image_env(
            inherited
                .iter()
                .map(|(key, value)| (key.into(), value.into())),
        );
        // AGENTD-12, AGENTD-13: the snapshot is the inherited map minus AGENTD_*, and that
        // count is what health reports.
        assert!(snapshot.keys().all(|key| !key.starts_with("AGENTD_")));
        assert_eq!(
            snapshot.len(),
            inherited.keys().filter(|key| !key.starts_with("AGENTD_")).count()
        );

        let passwd = passwd_text(&case.rows);
        let rows = parse_passwd(&passwd);
        let group = "staff:x:50:\nagent:x:1001:\n";
        let launch = env_of(&case.launch_env);
        let exists = |path: &Path| PRESENT.iter().any(|present| Path::new(present) == path);
        let result = plan(
            &request,
            &Guest {
                launch_env: &launch,
                image_env: Some(&snapshot),
                passwd: &passwd,
                group,
                is_executable: &exists,
            },
        );

        // What the resolution must decide, derived independently of it.
        let user_row = |name: &str| rows.iter().find(|row| row.name == name);
        let user_ok = match &request.user {
            Some(NameOrId::Name(name)) => user_row(name).is_some() || digits(name).is_some(),
            _ => true,
        };
        let group_ok = match &request.group {
            Some(NameOrId::Name(name)) => {
                ["staff", "agent"].contains(&name.as_str()) || digits(name).is_some()
            }
            _ => true,
        };

        match result {
            Err(rejection) => {
                // AGENTD-8, AGENTD-15: a refusal is always for the first unresolvable field.
                let expected = if !user_ok {
                    ERROR_UNKNOWN_USER
                } else if !group_ok {
                    ERROR_UNKNOWN_GROUP
                } else if matches!(&request.shell, Shell::Named(name) if name.is_empty()) {
                    ERROR_MALFORMED_REQUEST
                } else {
                    ERROR_UNKNOWN_SHELL
                };
                assert_eq!(rejection.error, expected, "{rejection:?} for {body}");
                if let Shell::Named(name) = &request.shell
                    && expected == ERROR_UNKNOWN_SHELL
                {
                    assert!(rejection.detail.contains(name.as_str()));
                }
            }
            Ok(plan) => {
                assert!(user_ok && group_ok, "an unresolvable name was planned: {body}");
                let env = plan.environment();

                // AGENTD-7 and AGENTD-16: the uid is the row's for a matching name, the number
                // otherwise; the gid is the group's, a named user's primary, or unchanged.
                let row = match &request.user {
                    Some(NameOrId::Name(name)) => user_row(name).or_else(|| {
                        digits(name).and_then(|uid| rows.iter().find(|row| row.uid == uid))
                    }),
                    Some(NameOrId::Id(uid)) => rows.iter().find(|row| row.uid == *uid),
                    None => None,
                };
                match &request.user {
                    Some(NameOrId::Name(name)) => match user_row(name) {
                        Some(named) => assert_eq!(plan.uid, Some(named.uid)),
                        None => assert_eq!(plan.uid, digits(name)),
                    },
                    Some(NameOrId::Id(uid)) => assert_eq!(plan.uid, Some(*uid)),
                    None => assert_eq!(plan.uid, None),
                }
                if request.group.is_none() {
                    let named = matches!(&request.user, Some(NameOrId::Name(name)) if user_row(name).is_some());
                    let expected = if named { row.map(|row| row.gid) } else { None };
                    assert_eq!(plan.gid, expected, "{body}");
                }

                // The expected environment, layer by layer, lowest first.
                let mut expected = Env::new();
                if request.inherit_image_env {
                    expected.extend(snapshot.clone());
                }
                if let Some(row) = row {
                    expected.insert("HOME".into(), row.home.clone());
                    expected.insert("USER".into(), row.name.clone());
                    expected.insert("LOGNAME".into(), row.name.clone());
                }
                expected.extend(launch.clone());
                expected.extend(request.env.clone());
                // AGENTD-9, AGENTD-10, AGENTD-11 at once: exactly the layered map.
                assert_eq!(env, expected, "{body}");
                // AGENTD-12: the token and the image's AGENTD_ keys never reach a child.
                assert!(env.values().all(|value| value != TOKEN));
                for key in env.keys().filter(|key| key.starts_with("AGENTD_")) {
                    assert!(
                        launch.contains_key(key) || request.env.contains_key(key),
                        "{key} came from the image"
                    );
                }

                match (&request.shell, &plan.program) {
                    (Shell::Flag(false), Program::Argv { program, .. }) => {
                        assert_eq!(program, "echo hi")
                    }
                    (Shell::Flag(true), Program::Script { shell, .. }) => {
                        assert_eq!(shell, Path::new("/bin/sh"))
                    }
                    // AGENTD-14: the resolved file exists and carries the requested name.
                    (Shell::Named(name), Program::Script { shell, .. }) => {
                        assert!(exists(shell), "{shell:?}");
                        let wanted = Path::new(name).file_name().expect("a file name");
                        assert_eq!(shell.file_name(), Some(wanted));
                    }
                    (shell, program) => panic!("{shell:?} planned as {program:?}"),
                }
            }
        }
    });
}

/// Arbitrary bytes into the `StartRequest` deserializer: refused or accepted, never a panic.
/// An accepted body's union fields are one of the two shapes each admits.
#[test]
fn start_request_deserialization() {
    bolero::check!().for_each(|bytes: &[u8]| {
        if let Ok(request) = serde_json::from_slice::<StartRequest>(bytes) {
            let written = serde_json::to_value(&request).expect("serializes");
            for field in ["user", "group"] {
                let value = &written[field];
                assert!(
                    value.is_null() || value.is_u64() || value.is_string(),
                    "{value}"
                );
            }
            assert!(written["shell"].is_boolean() || written["shell"].is_string());
        }
    });
    // A handful the byte fuzzer takes a while to find.
    for body in [
        r#"{"exec_id":"e","command":[],"user":"root","shell":"bash"}"#,
        r#"{"exec_id":"e","command":[],"user":4294967295,"group":"0"}"#,
    ] {
        serde_json::from_str::<StartRequest>(body).expect("accepted");
    }
    for body in [
        r#"{"exec_id":"e","command":[],"user":4294967296}"#,
        r#"{"exec_id":"e","command":[],"shell":{"name":"bash"}}"#,
    ] {
        assert!(
            serde_json::from_str::<StartRequest>(body).is_err(),
            "{body}"
        );
    }
}
