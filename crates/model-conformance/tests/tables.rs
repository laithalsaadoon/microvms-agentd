// SPDX-License-Identifier: Apache-2.0
//! The app's pure policies, driven over the rows `agentd-model` exposes for them.
//!
//! Each model states a policy as a function of a small input space and checks it with
//! stateright; the app implements the same policy on real inputs. The app's own unit tests
//! restate each model's table by hand, which catches a change to the app, but not a change to
//! the model that the copy never followed. Here the rows come from the model itself: every
//! input it enumerates, with the answer its specification gives. Each test renders a row as the
//! app's real input, calls the app's function, and compares.
//!
//! Every test starts at a floor: a model that returns no rows would pass every comparison
//! below, so it fails instead. The floor also pins how many rows the model returns, so a row
//! function that stops ranging over part of its input space (one value of a flag, say) fails
//! too, and a change that means to grow or shrink a model's rows edits the count here. A
//! variant added to one of the model's enums doesn't move a count by itself, since the image,
//! run and provision row functions list the variants they range over. Every such enum is
//! matched exhaustively in this file (`app_found`, `exec_result`, `lookups`,
//! `Played::checksums`), so the change that adds one stops this file compiling until it's
//! mapped here, which is where the row function's list and the count are updated beside it.
//!
//! `trace:check` doesn't credit the keys these tests name yet: tools/check-trace.py's
//! `RUST_TEST_DIRS` doesn't list this crate's tests. Adding the directory is a check-trace.py
//! change, left for after #297a's edits to that file land.
//!
//! Not here, and why: the CLI's output table (`crates/model/src/output.rs`), because the CLI has no
//! library target to call; the two-caller race in `crates/model/src/image.rs` and the platform side
//! of `crates/model/src/client.rs`, which model the service rather than our code; and the provision
//! model's serving policy, which lives in `microvms-edges` and is tied by
//! `crates/microvms-core/tests/features/provision.feature`.

use std::cell::RefCell;
use std::sync::Arc;
use std::time::Duration;

use agentd_model::{image, posture, preflight, provision, run, wrap};
use microvms_app::control::artifact::{
    BaseImage, WrapOptions, default_dockerfile, wrap_dockerfile,
};
use microvms_app::control::connector::{EgressPosture, egress_posture_for};
use microvms_app::control::ensure;
use microvms_app::control::transport::Transport;
use microvms_app::preflight::{Check, preflight_with};
use microvms_app::provision::{
    ASSET, AttestationVerifier, Bundles, ReleaseSource, Signer, Verification, fetch_release,
    sha256_hex,
};
use microvms_app::session::complete::{ClientDeadline, KillAnswer};
use microvms_app::session::exec::ExecResult;
use microvms_app::testing::{Answer, FakeControlPlane, TestClock, control_plane};
use microvms_app::{Error, ErrorKind, Region};

/// The rows a model returned, refusing none at all (a comparison over no rows passes whatever
/// the app does) and holding their number to `count`, the size of the input space the row
/// function ranges over.
fn floor<T>(model: &str, count: usize, rows: Vec<T>) -> Vec<T> {
    assert!(
        !rows.is_empty(),
        "the {model} model returned no rows, so nothing was checked"
    );
    assert_eq!(
        rows.len(),
        count,
        "the {model} model returned {} rows, not the {count} its row function ranges over",
        rows.len()
    );
    rows
}

// ── wrap: `crates/model/src/wrap.rs` and `wrap_dockerfile` ─────────────────────────

/// A task Dockerfile with exactly the features `task` names, as real text.
fn dockerfile(task: wrap::Task) -> String {
    let managed = BaseImage::al2023().docker_ref;
    let digest = "c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de";
    let mut text = match task.from {
        wrap::From::Absent => "RUN echo no base\n".to_string(),
        wrap::From::Managed => format!("FROM {managed}\n"),
        wrap::From::ManagedPinned => format!("FROM {managed}@sha256:{digest}\n"),
        wrap::From::Other => "FROM python:3.12-slim\n".to_string(),
    };
    if task.declares_workdir {
        text.push_str("WORKDIR /app\n");
    }
    if task.keepalive_too_long {
        text.push_str("ENV AGENTD_SSE_KEEPALIVE_SECS=60\n");
    }
    match task.user {
        wrap::User::Unset => {}
        wrap::User::Root => text.push_str("USER root\n"),
        wrap::User::Other => text.push_str("USER app\n"),
    }
    match task.tail {
        wrap::Tail::Complete => {}
        wrap::Tail::Continuation => text.push_str("RUN make \\\n"),
        wrap::Tail::OpenHeredoc => text.push_str("RUN <<EOF\necho open\n"),
    }
    text
}

fn wrap_options(opts: wrap::Opts) -> WrapOptions {
    WrapOptions {
        workdir: match opts.workdir {
            wrap::Workdir::Unset => None,
            wrap::Workdir::Absolute => Some("/srv".to_string()),
            wrap::Workdir::Invalid => Some("srv".to_string()),
        },
        inherit_workdir: opts.inherit_workdir,
        ..WrapOptions::default()
    }
}

/// The stanza `default_dockerfile` writes for these options: the one source IMAGE-1 names.
fn generator_stanza(options: &WrapOptions) -> String {
    let base = BaseImage::al2023();
    let default = default_dockerfile(options.port, options.workdir.as_deref(), &base, None);
    default
        .strip_prefix(&format!("FROM {}\n", base.docker_ref))
        .expect("the default Dockerfile starts with its FROM")
        .to_string()
}

/// Words the app's refusal carries for the model's reason. The model has one reason for an
/// unfinished tail, and the app words the two tails apart.
fn wrap_refusal_words(refusal: wrap::Refusal, task: wrap::Task) -> &'static str {
    match refusal {
        wrap::Refusal::NoFrom => "no FROM",
        wrap::Refusal::Unfinished if task.tail == wrap::Tail::OpenHeredoc => "heredoc",
        wrap::Refusal::Unfinished => "line continuation",
        wrap::Refusal::Keepalive => "AGENTD_SSE_KEEPALIVE_SECS",
        wrap::Refusal::BadWorkdir => "absolute path",
        wrap::Refusal::NothingToInherit => "nothing to inherit",
    }
}

/// **IMAGE-1, IMAGE-2 and IMAGE-3, the model's every row.** `wrap_dockerfile` refuses what
/// the model refuses, for its reason, and wraps the rest with the task text verbatim, `USER
/// root` exactly where the model writes it, and then the stanza `default_dockerfile` writes
/// for the same options, unchanged (the model's `source: Generator` and `stanza_intact`).
///
/// **Falsification** (2026-09-29). Drop `workdir.is_none() &&` from `wrap_dockerfile`'s
/// inherit check and this fails on the first row that passes an absolute workdir with
/// `inherit_workdir`; `cargo test -p agentd-model` stays green. Restored.
#[test]
fn wrap_dockerfile_answers_every_row_of_the_wrap_model() {
    for (task, opts, expected) in floor("wrap", 864, wrap::rows()) {
        let text = dockerfile(task);
        let options = wrap_options(opts);
        let got = wrap_dockerfile(&text, &options);
        let row = format!("{task:?} {opts:?}");
        match (expected, got) {
            (wrap::Wrap::Refused(refusal), Err(error)) => {
                let words = wrap_refusal_words(refusal, task);
                assert!(
                    error.to_string().contains(words),
                    "the wrap model's row {row} refuses for {refusal:?}, and wrap_dockerfile \
                     refused with: {error}"
                );
            }
            (wrap::Wrap::Wrapped(wrapped), Ok(out)) => {
                let appended = out.strip_prefix(text.as_str()).unwrap_or_else(|| {
                    panic!("the wrap model's row {row}: the task text isn't kept verbatim")
                });
                assert_eq!(
                    appended.starts_with("USER root\n"),
                    wrapped.user_root,
                    "the wrap model's row {row} writes USER root: {}; wrap_dockerfile appended \
                     {appended:?}",
                    wrapped.user_root
                );
                assert!(
                    wrapped.source == wrap::Source::Generator && wrapped.stanza_intact,
                    "the wrap model's row {row} answers {wrapped:?}, which no generator \
                     stanza stands for"
                );
                let stanza = generator_stanza(&options);
                let tail = appended.strip_prefix("USER root\n").unwrap_or(appended);
                assert_eq!(
                    tail, stanza,
                    "the wrap model's row {row} ends on the generator's stanza, intact; \
                     wrap_dockerfile appended {appended:?}"
                );
            }
            (expected, got) => panic!(
                "the wrap model's row {row} answers {expected:?}, and wrap_dockerfile answered \
                 {got:?}"
            ),
        }
    }
}

// ── posture: `crates/model/src/posture.rs` and `egress_posture_for` ────────────────

fn app_posture(posture: posture::Posture) -> EgressPosture {
    match posture {
        posture::Posture::Open => EgressPosture::Open,
        posture::Posture::Unsealed => EgressPosture::Unsealed,
        posture::Posture::BestEffort => EgressPosture::BestEffort,
        posture::Posture::Sealed => EgressPosture::Sealed,
    }
}

fn posture_refusal_words(refusal: posture::Refusal) -> &'static str {
    match refusal {
        posture::Refusal::EgressWithDeny => "opposite things",
        posture::Refusal::EgressWithConnectors => "INTERNET_EGRESS cannot be combined",
        posture::Refusal::MalformedConnector => "network connector ARN",
        posture::Refusal::TooManyConnectors => "NetworkConnectorList ceiling",
    }
}

/// **BIND-11 and BIND-13, the model's every row.** The posture a launch reports, or the
/// refusal it raises, for every launch the model describes at the wire ceiling and past it.
///
/// **Falsification** (2026-09-29). Make `egress_posture_for` answer
/// `for_launch(egress, false)` and this fails on the first denied launch, which the model
/// reports `BestEffort`; `cargo test -p agentd-model` stays green. Restored.
#[test]
fn egress_posture_for_answers_every_row_of_the_posture_model() {
    let region = Region::UsEast1;
    let rows = floor("posture", 92, posture::rows());
    for row in posture::TABLE {
        assert!(
            rows.contains(&row),
            "the posture model's TABLE row {row:?} isn't among the rows it exposes"
        );
    }
    for (egress, count, malformed, deny, expected) in rows {
        let mut connectors: Vec<String> = (0..count)
            .map(|index| {
                format!("arn:aws:lambda:us-east-1:123456789012:network-connector:vpc-{index}")
            })
            .collect();
        if malformed {
            connectors[0] = "network-connector:vpc-0".to_string();
        }
        let row = format!("egress={egress} connectors={count} malformed={malformed} deny={deny}");
        match (
            expected,
            egress_posture_for(egress, &connectors, deny, Some(&region)),
        ) {
            (Ok(want), Ok(got)) => assert_eq!(
                got,
                app_posture(want),
                "the posture model's row {row} reports {want:?}"
            ),
            (Err(refusal), Err(error)) => {
                assert_eq!(error.kind(), ErrorKind::InvalidArg, "{row}: {error}");
                assert!(
                    error.to_string().contains(posture_refusal_words(refusal)),
                    "the posture model's row {row} refuses for {refusal:?}, and \
                     egress_posture_for refused with: {error}"
                );
            }
            (expected, got) => panic!(
                "the posture model's row {row} answers {expected:?}, and egress_posture_for \
                 answered {got:?}"
            ),
        }
    }
}

// ── preflight: `crates/model/src/preflight.rs` and `preflight_with` ────────────────

/// The model's line for one of the app's checks.
fn line_of(check: &Check) -> preflight::Line {
    match (check.ran, check.ok, check.fatal) {
        (false, _, _) => preflight::Line::NotRun,
        (true, true, _) => preflight::Line::Pass,
        (true, false, true) => preflight::Line::Fatal,
        (true, false, false) => preflight::Line::Advisory,
    }
}

/// **BIND-15 and BIND-16, the model's every world.** Each world is built from the app's fakes:
/// the region as `preflight` would have resolved it, a credential chain that resolves or
/// doesn't, and a listing that answers, is denied, or never completes. The report's three lines
/// and its `ok` are the model's, and the listing is sent exactly when the model makes its one
/// free read.
///
/// The clock is paused because an unreachable service is retried with backoff, and a paused
/// clock skips the waits.
///
/// **Falsification** (2026-09-29). Run `service_check` in `preflight_with` whatever the
/// credentials line says and this fails on the first world whose credentials don't resolve;
/// `cargo test -p agentd-model` stays green. Restored.
#[tokio::test(start_paused = true)]
async fn preflight_reports_every_row_of_the_preflight_model() {
    for row in floor("preflight", 18, preflight::rows()) {
        let resolved = match row.region {
            preflight::RegionWorld::Supported => Ok(Region::UsEast1),
            preflight::RegionWorld::Unlisted => Ok(Region::unlisted("ap-south-1")),
            preflight::RegionWorld::Unresolvable => Err(Error::invalid_arg(
                "AWS_REGION names a region this client can't parse",
            )),
        };
        let recorder = Arc::new(FakeControlPlane::new());
        if !row.credentials {
            recorder.fail_credentials("nothing in the chain");
        }
        match row.service {
            preflight::ServiceWorld::Answers => {
                recorder.answer("ListManagedMicrovmImages", Answer::ok(r#"{"items": []}"#));
            }
            preflight::ServiceWorld::Denied => {
                recorder.answer(
                    "ListManagedMicrovmImages",
                    Answer::failure(403, "AccessDeniedException"),
                );
            }
            preflight::ServiceWorld::Unreachable => {
                recorder.fail_transport("ListManagedMicrovmImages", usize::MAX);
            }
        }
        let transport = Arc::clone(&recorder) as Arc<dyn Transport>;
        let report = preflight_with(resolved, |region| async move {
            Ok(control_plane(transport, region, Arc::new(TestClock::new())))
        })
        .await;
        let lines: Vec<preflight::Line> = report.checks.iter().map(line_of).collect();
        assert_eq!(
            lines, row.lines,
            "the preflight model's row {row:?}: the report's lines, from {:#?}",
            report.checks
        );
        assert_eq!(report.ok(), row.ok, "the preflight model's row {row:?}");
        let mut operations = recorder.operations();
        operations.dedup();
        let reads = if row.free_reads == 0 {
            Vec::new()
        } else {
            vec!["ListManagedMicrovmImages"]
        };
        assert_eq!(
            operations, reads,
            "the preflight model's row {row:?} makes {} free read(s)",
            row.free_reads
        );
    }
}

// ── image: `crates/model/src/image.rs` and `ensure::plan` ──────────────────────────

fn app_found(found: image::Found) -> ensure::Found {
    match found {
        image::Platform::Absent => ensure::Found::Absent,
        image::Platform::Building => ensure::Found::Building,
        image::Platform::Ready => ensure::Found::Ready,
        image::Platform::Failed => ensure::Found::Failed,
        image::Platform::Deleting => ensure::Found::Deleting,
    }
}

fn app_plan(plan: image::Plan) -> ensure::Plan {
    match plan {
        image::Plan::Reuse => ensure::Plan::Reuse,
        image::Plan::Wait => ensure::Plan::Wait,
        image::Plan::Delete => ensure::Plan::Delete,
        image::Plan::AwaitAbsent => ensure::Plan::AwaitAbsent,
        image::Plan::Build => ensure::Plan::Build,
    }
}

/// **IMAGE-9 and IMAGE-10, the model's every row.** What `ensure_image` does after a describe.
///
/// **Falsification** (2026-09-29). Plan `Build` for a `Deleting` image in `ensure::plan` and
/// this fails on that row; `cargo test -p agentd-model` stays green. Restored.
#[test]
fn the_ensure_plan_is_every_row_of_the_image_model() {
    for (found, force, expected) in floor("image", 10, image::plan_rows()) {
        assert_eq!(
            ensure::plan(app_found(found), force),
            app_plan(expected),
            "the image model's row {found:?} force={force} plans {expected:?}"
        );
    }
}

// ── run: `crates/model/src/run.rs` and `ExecResult::posix_exit_code` ───────────────

/// The result `run_to_completion` would return for the row: acked with the daemon's outcome
/// when there is one, still running when there isn't, and the client's deadline beside it.
fn exec_result(status: Option<run::Status>, client: Option<run::ClientDeadline>) -> ExecResult {
    ExecResult {
        exec_id: "e1".into(),
        phase: if status.is_some() {
            microvms_app::protocol::exec::Phase::Acked
        } else {
            microvms_app::protocol::exec::Phase::Running
        },
        outcome: status.map(|status| microvms_app::protocol::exec::Outcome {
            exit_code: status.exit_code,
            signal: status.signal,
            timed_out: status.timed_out,
            truncated: status.truncated,
            ..microvms_app::protocol::exec::Outcome::default()
        }),
        client_deadline: client.map(|client| ClientDeadline {
            after: Duration::from_secs(90),
            kill: match client.kill {
                run::KillAnswer::Signalled => KillAnswer::Signalled,
                run::KillAnswer::AlreadyGone => KillAnswer::AlreadyGone,
                run::KillAnswer::Failed => KillAnswer::Failed("connection reset".into()),
            },
            ack_error: client.synthesized.then(|| "poll reset".to_string()),
        }),
    }
}

/// **BIND-6, the model's every row.** 124 when a deadline ended the command, `128 + signal`
/// for any other signal death, the exit code otherwise, and nothing for a running exec with no
/// deadline.
///
/// **Falsification** (2026-09-29). Let an exit code win over `timed_out` in
/// `posix_exit_code` and this fails on the child that exits 0 after the daemon's deadline;
/// `cargo test -p agentd-model` stays green. Restored.
#[test]
fn posix_exit_code_is_every_row_of_the_run_model() {
    for (status, client, expected) in floor("run", 175, run::posix_exit_code_rows()) {
        let result = exec_result(status, client);
        assert_eq!(
            result.posix_exit_code(),
            expected,
            "the run model's row {status:?} {client:?} has exit code {expected:?}"
        );
    }
}

// ── provision: `crates/model/src/provision.rs` and `fetch_release` ─────────────────

/// A release played from one of the model's rows, recording which of its files were read.
struct Played {
    bundles: Bundles,
    curl: provision::Curl,
    asset: Vec<u8>,
    ran: RefCell<Vec<&'static str>>,
}

impl ReleaseSource for Played {
    fn asset(&self, _: &str, name: &str) -> Result<Vec<u8>, String> {
        self.ran.borrow_mut().push("asset");
        assert_eq!(name, ASSET);
        Ok(self.asset.clone())
    }

    fn checksums(&self, _: &str) -> Result<String, String> {
        self.ran.borrow_mut().push("checksums");
        match self.curl {
            provision::Curl::Fails => Err("connection reset".into()),
            provision::Curl::NoSums => Err("HTTP 404".into()),
            provision::Curl::SumsMatch => Ok(format!("{}  {ASSET}\n", sha256_hex(&self.asset))),
            provision::Curl::SumsMismatch => {
                Ok(format!("{}  {ASSET}\n", sha256_hex(b"other bytes")))
            }
        }
    }

    fn attestations(&self, _: &str, _: &str) -> Bundles {
        self.ran.borrow_mut().push("attestations");
        self.bundles.clone()
    }
}

/// Accepts the bundle spelled `good`, and nothing else.
struct Verifier;

impl AttestationVerifier for Verifier {
    fn verify(&self, bundle: &str, _: &[u8], _: &Signer) -> Result<(), String> {
        match bundle {
            "good" => Ok(()),
            _ => Err("identity mismatch".into()),
        }
    }
}

/// The attestation lookups that read as the model's `gh`. The module docs of
/// `crates/model/src/provision.rs` give the reading: a bundle that arrives and verifies is `Attested`;
/// one that arrives and doesn't, or a release that answers it has none, is `Unattested`; no
/// answer at all is `Unavailable`.
fn lookups(gh: provision::Gh) -> Vec<Bundles> {
    match gh {
        provision::Gh::Attested => vec![Bundles::Published(vec!["good".into()])],
        provision::Gh::Unattested => vec![
            Bundles::Published(vec!["bad".into()]),
            Bundles::Absent("HTTP 404".into()),
            Bundles::Published(Vec::new()),
        ],
        provision::Gh::Unavailable => vec![Bundles::Unreachable("HTTP 403: rate limited".into())],
    }
}

/// **BIND-18, the model's every row.** Attestation when a bundle verifies; refusal without the
/// checksum when one arrives and doesn't, or the release says there is none; and otherwise
/// only a matching `SHA256SUMS` passes. A served fetch hands back the asset's own bytes.
///
/// **Falsification** (2026-09-29). Let `fetch_release` fall through to `SHA256SUMS` when the
/// release answers it has no bundle, and this fails on the first `Unattested` row read as that
/// answer; `cargo test -p agentd-model` stays green. Restored.
#[test]
fn fetch_release_verifies_every_row_of_the_provision_model() {
    for (release, expected) in floor("provision", 12, provision::verification_rows()) {
        for bundles in lookups(release.gh) {
            let source = Played {
                bundles: bundles.clone(),
                curl: release.curl,
                asset: b"\x7fELF release bytes".to_vec(),
                ran: RefCell::new(Vec::new()),
            };
            let fetched = fetch_release(&source, &Verifier, "v9.9.9", &mut |_| {});
            let row = format!("{:?} {:?} as {bundles:?}", release.gh, release.curl);
            let want = match expected {
                provision::Verdict::Attestation => Some(Verification::Attestation),
                provision::Verdict::Checksum => Some(Verification::Checksum),
                provision::Verdict::Refused => None,
                provision::Verdict::Unverified => {
                    panic!("the specified policy never serves unverified bytes: {row}")
                }
            };
            assert_eq!(
                fetched.as_ref().ok().map(|fetched| fetched.verification),
                want,
                "the provision model's row {row} is {expected:?}; fetch_release answered \
                 {fetched:?}"
            );
            if let Ok(fetched) = &fetched {
                assert_eq!(fetched.bytes, source.asset, "{row}");
            }
            if release.gh != provision::Gh::Unavailable {
                assert!(
                    !source.ran.borrow().contains(&"checksums"),
                    "the provision model's row {row} never consults SHA256SUMS, and \
                     fetch_release did"
                );
            }
        }
    }
}
