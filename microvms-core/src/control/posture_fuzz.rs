// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for BIND-11, BIND-12, and BIND-13: the request-side answer against the
//! launch, over arbitrary launch options.
//!
//! `bolero::check!` runs this as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test control::posture_fuzz::posture_matches_the_launch -p microvms-core -T 120s`
//! (the `egress-posture` job in `.github/workflows/fuzz.yml`).
//!
//! # What an input is
//!
//! `egress`, `deny_egress`, a region, and up to 16 connectors, each a well-formed connector
//! ARN in the launch region, one in another region, or arbitrary bytes. The mix reaches every
//! refusal in [`super::egress_posture_for`] and the ceiling on both sides.
//!
//! # What it checks
//!
//! * BIND-11: the answer is never `sealed`, and it is the decision table's row
//!   ([`expected`], the rules of `TABLE` in `model/src/posture.rs`).
//! * BIND-13: a refused answer is the refusal [`crate::sandbox::Sandbox::run`] raises for the
//!   same options, message for message, with zero control-plane calls; an accepted answer is
//!   a launch that proceeds.
//! * BIND-12: the launched session reports the answered posture.

use std::sync::Arc;

use crate::constants::MAX_NETWORK_CONNECTORS;
use crate::control::fake::{self, Answer, FakeControlPlane, TestClock};
use crate::control::{ControlPlane, EgressPosture, egress_posture_for};
use crate::error::ErrorKind;
use crate::region::Region;
use crate::sandbox::{RunRequest, Sandbox};

/// One connector string, chosen by `kind`.
fn connector(kind: u8, index: u8, region: &Region, junk: &[u8]) -> String {
    match kind % 4 {
        // Well-formed in the launch region, twice as likely as each of the others so a long
        // list of good ones reaches the ceiling.
        0 | 1 => format!(
            "arn:aws:lambda:{}:123456789012:network-connector:vpc-{index}",
            region.as_str()
        ),
        2 => format!("arn:aws:lambda:eu-west-1:123456789012:network-connector:vpc-{index}"),
        _ => String::from_utf8_lossy(junk).into_owned(),
    }
}

/// Which refusal the decision table names for these options, or the posture it answers.
fn expected(
    egress: bool,
    connectors: &[String],
    deny: bool,
    region: &Region,
) -> Result<EgressPosture, &'static str> {
    let well_formed =
        |arn: &String| super::connector::require_egress_connector_arn(arn, region).is_ok();
    if egress && deny {
        return Err("opposite things");
    }
    if egress && !connectors.is_empty() {
        return Err("INTERNET_EGRESS cannot be combined");
    }
    if !connectors.iter().all(well_formed) {
        return Err("customer-managed Lambda network connector ARN");
    }
    if connectors.len() > MAX_NETWORK_CONNECTORS {
        return Err("NetworkConnectorList ceiling");
    }
    Ok(if egress {
        EgressPosture::Open
    } else if deny {
        EgressPosture::BestEffort
    } else {
        EgressPosture::Unsealed
    })
}

fn launch(
    runtime: &tokio::runtime::Runtime,
    request: RunRequest,
) -> (Result<EgressPosture, crate::Error>, usize) {
    let recorder = Arc::new(FakeControlPlane::new());
    recorder
        .answer(
            "RunMicrovm",
            Answer::ok(fake::microvm_response("PENDING", None)),
        )
        .answer(
            "GetMicrovm",
            Answer::ok(fake::microvm_response("RUNNING", None)),
        )
        .answer(
            "CreateMicrovmAuthToken",
            Answer::ok(fake::auth_token_response("proxy-token")),
        );
    let plane = ControlPlane::with_transport(
        Arc::clone(&recorder) as Arc<dyn crate::control::transport::Transport>,
        Region::UsEast1,
        Arc::new(TestClock::new()) as Arc<dyn crate::control::Clock>,
    );
    let mut sandbox = Sandbox::with_control_plane(plane);
    let posture = runtime.block_on(async {
        let posture = sandbox
            .run(request)
            .await
            .map(|session| session.egress_posture());
        // Quiet the drop warning; the fake answers nothing for terminate, which is fine.
        let _ = sandbox.detach();
        posture
    });
    (posture, recorder.calls().len())
}

#[test]
fn posture_matches_the_launch() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("a runtime");
    bolero::check!()
        .with_type::<(bool, bool, Vec<(u8, u8)>, Vec<u8>)>()
        .for_each(|(egress, deny, kinds, junk)| {
            let region = Region::UsEast1;
            let connectors: Vec<String> = kinds
                .iter()
                .take(16)
                .map(|(kind, index)| connector(*kind, *index, &region, junk))
                .collect();

            let answer = egress_posture_for(*egress, &connectors, *deny, Some(&region));
            let rule = expected(*egress, &connectors, *deny, &region);
            match (&answer, &rule) {
                (Ok(posture), Ok(row)) => {
                    assert_eq!(posture, row, "BIND-11: the decision table's row");
                    assert!(!posture.is_sealed(), "BIND-11: never sealed from options");
                }
                (Err(error), Err(needle)) => {
                    assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
                    assert!(error.to_string().contains(needle), "{needle}: {error}");
                }
                _ => panic!("BIND-11: {answer:?} is not the table's {rule:?}"),
            }

            let mut request = RunRequest::new().with_image("arn:image");
            request.egress = *egress;
            request.egress_network_connectors = connectors.clone();
            request.deny_egress = *deny;
            let (launched, calls) = launch(&runtime, request);
            match (answer, launched) {
                (Ok(answered), Ok(session)) => {
                    assert_eq!(session, answered, "BIND-12: the session's posture");
                }
                (Err(answered), Err(refused)) => {
                    assert_eq!(
                        answered.to_string(),
                        refused.to_string(),
                        "BIND-13: the same refusal"
                    );
                    assert_eq!(calls, 0, "BIND-13: a refused launch costs nothing");
                }
                (answered, launched) => {
                    panic!("BIND-13: answered {answered:?} but the launch gave {launched:?}")
                }
            }
        });
}
