// SPDX-License-Identifier: Apache-2.0
//! Core's answers to the shared case corpus (`verify/parity/cases/`, #272).
//!
//! Here rather than in microvms-domain or microvms-app because this is the one crate whose tests
//! reach every area: the domain can't see the app's `wrap_dockerfile`, `egress_posture_for` or
//! image names, and neither crate has a `tests/` directory. Each handler calls the function the
//! capability table names for core, with nothing between the case and the call but type
//! conversion; the rules for reading and judging a case are in `parity_corpus`.

mod parity_corpus;

use std::sync::Arc;

use futures_util::future::BoxFuture;
use microvms_app::testing::FakeControlPlane;
use microvms_core::agents::{Agent, AgentSpec, AgentVm};
use microvms_core::control::artifact::{
    BaseImage, WrapOptions, default_dockerfile, wrap_dockerfile,
};
use microvms_core::control::ensure::{EnsureImageRequest, prepare};
use microvms_core::control::{ControlPlane, DEFAULT_AGENT_PORT, SystemClock, egress_posture_for};
use microvms_core::cost::{
    CalendarDate, DEFAULT_ESTIMATE_LABEL, PlanUsage, estimate_run, pinned_rates,
};
use microvms_core::names::FileNameStore;
use microvms_core::prelude::*;
use microvms_core::sandbox::{RunRequest, Sandbox};
use microvms_core::session::{HttpBackend, HttpRequest, HttpResponse, OpenStream, Session};
use microvms_core::{Error, Region, SizeClass};
use parity_corpus::{Case, Run};
use serde_json::{Value, json};

const AREAS: [&str; 7] = [
    "image-name",
    "cost",
    "error",
    "egress",
    "names",
    "size-class",
    "wrap-dockerfile",
];

const BUCKET: &str = "parity-cases-bucket";
const BUILD_ROLE: &str = "arn:aws:iam::123456789012:role/build";

#[tokio::test]
async fn core_answers_the_shared_case_corpus() {
    let mut run = Run::plan("core", &AREAS, &[]);
    for case in run.cases() {
        let answer = match case.area.as_str() {
            "image-name" => image_name(&case),
            "cost" => cost(&case),
            "error" => daemon_status(&case).await,
            "egress" => egress(&case).await,
            "names" => names(&case).await,
            "size-class" => size_class(&case),
            "wrap-dockerfile" => wrap(&case),
            other => unreachable!("planned only the owned areas, not {other}"),
        };
        run.judge(&case, answer);
    }
    run.finish();
}

/// The facets every runner reports for a failure.
fn refusal(error: &Error) -> Value {
    json!({"error": {
        "code": error.code(),
        "wire_kind": error.wire_kind().map(|kind| kind.as_str()),
        "retryable": error.retryable(),
    }})
}

/// A control plane that makes no call: the fake has nothing queued, so one would panic
/// naming the operation instead of passing against AWS.
fn offline_plane() -> ControlPlane {
    ControlPlane::with_transport(
        Arc::new(FakeControlPlane::new()),
        Region::UsEast1,
        Arc::new(SystemClock::new()),
    )
}

fn size(case: &Case) -> SizeClass {
    let mib = u32::try_from(case.input_u64("size_mib")).expect("a size in range");
    SizeClass::from_baseline_mib(mib).expect("a documented size class")
}

fn image_name(case: &Case) -> Value {
    match case.capability.as_str() {
        // `ensure_image`'s local half, which names the image before any call.
        "ensure-image" => {
            let dockerfile =
                default_dockerfile(DEFAULT_AGENT_PORT, None, &BaseImage::al2023(), None);
            let mut request = EnsureImageRequest::new(
                case.input_str("name_prefix"),
                case.input_binary(),
                dockerfile,
                BUCKET,
                BUILD_ROLE,
            );
            request.size = size(case);
            match prepare(&offline_plane(), request) {
                Ok(prepared) => json!({"name": prepared.name}),
                Err(error) => refusal(&error),
            }
        }
        "agent-image-name" => {
            let specs = case
                .input_strings("agents")
                .iter()
                .map(|name| {
                    let agent = Agent::parse(name).unwrap_or_else(|| panic!("an agent: {name}"));
                    AgentSpec::new(agent)
                })
                .collect();
            let answer = AgentVm::new(Sandbox::with_control_plane(offline_plane()), specs)
                .and_then(|vm| {
                    let request = vm.image_request(case.input_binary(), BUILD_ROLE, size(case))?;
                    Ok(vm.image_name(&request))
                });
            match answer {
                Ok(name) => json!({"name": name}),
                Err(error) => refusal(&error),
            }
        }
        other => panic!("{}: no core handler for {other:?} in image-name", case.id),
    }
}

fn cost(case: &Case) -> Value {
    assert_eq!(case.capability, "estimate", "{}: an estimate case", case.id);
    let mut plan = PlanUsage {
        running_seconds: case.input_f64("running_seconds"),
        suspended_seconds: case.input_f64("suspended_seconds"),
        suspend_resume_cycles: u32::try_from(case.input_u64("suspend_resume_cycles"))
            .expect("a cycle count in range"),
        ..PlanUsage::default()
    };
    // Core has no optional arguments, so its runner applies the defaults every surface's caller
    // gets by leaving `launched` and the label out: core's inference and core's label.
    plan.launched = plan.infer_launched();
    match estimate_run(
        size(case),
        &plan,
        &pinned_rates(),
        CalendarDate::today_utc(),
        DEFAULT_ESTIMATE_LABEL,
    ) {
        Ok(report) => report.to_json(),
        Err(error) => refusal(&error),
    }
}

/// A daemon that answers every request with the case's status and body.
struct ScriptedStatus {
    status: u16,
    body: Vec<u8>,
}

impl HttpBackend for ScriptedStatus {
    fn send(&self, _request: HttpRequest) -> BoxFuture<'_, Result<HttpResponse, Error>> {
        let response = HttpResponse {
            status: self.status,
            headers: std::collections::HashMap::new(),
            body: self.body.clone(),
        };
        Box::pin(async move { Ok(response) })
    }

    fn open_stream(
        &self,
        request: HttpRequest,
        _idle_timeout: std::time::Duration,
    ) -> BoxFuture<'_, Result<OpenStream, Error>> {
        panic!(
            "no error case streams, but {} {} did",
            request.method, request.path
        )
    }
}

async fn daemon_status(case: &Case) -> Value {
    let backend = Arc::new(ScriptedStatus {
        status: u16::try_from(case.input_u64("status")).expect("a status"),
        body: case.input_str("body").as_bytes().to_vec(),
    });
    let session = Session::builder("http://127.0.0.1:9", "agent-token")
        .with_backend(backend)
        .build()
        .expect("the session builds");
    let result = match case.capability.as_str() {
        "health" => session.health().await.map(|_| ()),
        "upload-file" => {
            session
                .upload_file(case.input_str("path"), b"parity", None)
                .await
        }
        other => panic!("{}: no core handler for {other:?} in error", case.id),
    };
    match result {
        Ok(()) => json!({"ok": true}),
        Err(error) => refusal(&error),
    }
}

async fn egress(case: &Case) -> Value {
    let egress = case.input_bool("egress");
    let connectors = case.input_strings("egress_network_connectors");
    let deny_egress = case.input_bool("deny_egress");
    match case.capability.as_str() {
        "launch" => {
            let mut request = RunRequest::new()
                .with_image("arn:aws:lambda:us-east-1:123456789012:microvm-image:img");
            request.egress = egress;
            request.egress_network_connectors = connectors;
            request.deny_egress = deny_egress;
            let mut sandbox = Sandbox::with_control_plane(offline_plane());
            match sandbox.run(request).await {
                Ok(_) => json!({"ok": true}),
                Err(error) => refusal(&error),
            }
        }
        "egress-posture-for" => match egress_posture_for(egress, &connectors, deny_egress, None) {
            Ok(posture) => json!({"posture": posture.as_str()}),
            Err(error) => refusal(&error),
        },
        other => panic!("{}: no core handler for {other:?} in egress", case.id),
    }
}

/// The case's record written where the CLI's registry keeps it, then adopted by name.
///
/// `Sandbox::from_name` is `names::resolve`, a plane for the record's region, then
/// `adopt_record`. The plane is the one step swapped, for the offline one, so a regression that
/// lets the record through fails on the fake's panic rather than resolving credentials and
/// calling AWS from the test host. The Python and TypeScript runners call `from_name` itself,
/// behind a proxy nothing listens on.
async fn names(case: &Case) -> Value {
    assert_eq!(
        case.capability, "from-name",
        "{}: a from-name case",
        case.id
    );
    let state = tempfile::tempdir().expect("a state directory");
    let store = FileNameStore::in_state_root(state.path());
    let name = case.input_str("name");
    std::fs::create_dir_all(store.dir()).expect("the names directory");
    std::fs::write(store.path_of(name), case.input_str("record_text")).expect("writes the record");
    let region: Region = case.input_str("region").parse().expect("a region");
    let adopted = match microvms_core::names::resolve(&store, name, Some(&region)) {
        Ok(record) => Sandbox::adopt_record(offline_plane(), record).await,
        Err(error) => Err(error),
    };
    match adopted {
        Ok(_) => json!({"ok": true}),
        Err(error) => {
            let mut answer = refusal(&error);
            answer["message_mentions"] = case.message_mentions(&error.to_string());
            answer
        }
    }
}

fn size_class(case: &Case) -> Value {
    let cpus = case.input("cpus").as_f64();
    let memory = case
        .input("memory_mib")
        .as_u64()
        .map(|mib| u32::try_from(mib).expect("a memory figure in range"));
    match SizeClass::from_request(cpus, memory) {
        Ok(class) => json!({"baseline_mib": class.baseline_mib()}),
        Err(error) => refusal(&error),
    }
}

fn wrap(case: &Case) -> Value {
    match wrap_dockerfile(case.input_str("task"), &WrapOptions::default()) {
        Ok(text) => json!({"dockerfile": text}),
        Err(error) => refusal(&error),
    }
}
