// SPDX-License-Identifier: Apache-2.0
//! `ls --remote` (#159): the one local command with a remote half.

#![cfg(test)]

use std::sync::Arc;

use microvms_core::testing::YieldingClock;

use super::support::{
    RefusingSeam, SENTINEL, ScriptedSeam, ScriptedTransport, TempDir, dispatch_with, full_infra,
    list_images_body, region_flags,
};
use crate::cli::{Command, LsArgs};
use crate::seam::Door;

/// `ListMicrovmsResponse`, in the model's own spelling: `(microvmId, state)` pairs.
///
/// A literal, like every body in this file: a response produced by the same serializer the
/// client deserializes with cannot catch a misspelled member.
fn list_microvms_body(vms: &[(&str, &str)]) -> String {
    let items: Vec<String> = vms
        .iter()
        .map(|(id, state)| {
            format!(
                r#"{{"microvmId": "{id}", "state": "{state}",
                     "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:img",
                     "imageVersion": "1"}}"#
            )
        })
        .collect();
    format!(r#"{{"items": [{}]}}"#, items.join(", "))
}

/// The image ARN `list_images_body` spells for `name`.
fn image_arn(name: &str) -> String {
    format!("arn:aws:lambda:us-east-1:123456789012:microvm-image:{name}")
}

/// `ls` over `dir`, with or without the remote half.
fn ls_args(dir: &std::path::Path, remote: bool, prune: bool) -> LsArgs {
    LsArgs {
        state_dir: Some(dir.to_path_buf()),
        watch: false,
        interval_sec: 2.0,
        max_refreshes: None,
        remote,
        prune,
        region: region_flags(),
    }
}

/// One ledger record under `dir`, in the shape `Ledger::flush` writes.
fn write_ledger(
    dir: &std::path::Path,
    run_id: &str,
    region: &str,
    microvm_id: Option<&str>,
    image: Option<&str>,
) {
    let mut leaked: Vec<&str> = Vec::new();
    leaked.extend(microvm_id);
    leaked.extend(image);
    write_ledger_leaked(dir, run_id, region, microvm_id, image, &leaked);
}

/// A ledger record whose `leaked` list is spelled by the test rather than derived from the
/// named fields — the shape `tear_down` leaves when a delete reported some identifiers gone
/// and others not (a service-created log group, most often).
fn write_ledger_leaked(
    dir: &std::path::Path,
    run_id: &str,
    region: &str,
    microvm_id: Option<&str>,
    image: Option<&str>,
    leaked: &[&str],
) {
    let record = serde_json::json!({
        "runId": run_id,
        "region": region,
        "imageIdentifier": image,
        "imageName": image.map(|_| "img"),
        "microvmId": microvm_id,
        "leaked": leaked,
        // A key no version of this CLI wrote, standing in for a sibling change's additions:
        // the reconciliation reads what it names and carries the rest through opaque.
        "someFutureKey": {"nested": true},
    });
    std::fs::write(
        dir.join(format!("{run_id}.json")),
        serde_json::to_string_pretty(&record).expect("serializes"),
    )
    .expect("writes");
}

/// **A plain `ls` enters no door; `ls --remote` enters the control plane and nothing else.**
///
/// The topology half of #159. `ls` stays in `LOCAL_ONLY` because its default is a file read,
/// and this is the assertion that the flag is the only thing that changes that — through the
/// seam's control-plane door (the single AWS service this CLI talks to), never a sandbox or a
/// session.
#[tokio::test]
async fn a_plain_ls_enters_no_door_and_ls_remote_enters_only_the_control_plane() {
    let dir = TempDir::new("ls-doors");
    write_ledger(&dir.0, "1-1", "us-east-1", Some("mvm-1"), None);

    let seam = RefusingSeam::new();
    let (result, _) = dispatch_with(
        &seam,
        &Command::Ls(ls_args(&dir.0, false, false)),
        full_infra(),
    )
    .await;
    let rendered =
        result.expect("a plain ls never touches AWS, so the refusing seam cannot fail it");
    assert!(
        seam.doors().is_empty(),
        "plain ls entered {:?}",
        seam.doors()
    );
    assert_eq!(rendered.data["source"], "local-ledger");
    assert_eq!(rendered.data["remote"], serde_json::Value::Null);
    assert_eq!(rendered.data["pruned"], serde_json::json!([]));

    let seam = RefusingSeam::new();
    let (result, _) = dispatch_with(
        &seam,
        &Command::Ls(ls_args(&dir.0, true, false)),
        full_infra(),
    )
    .await;
    let failure = result.expect_err("the refusing seam fails the remote read");
    assert!(failure.message.contains(SENTINEL), "{}", failure.message);
    assert_eq!(seam.doors(), vec![Door::ControlPlane]);
    // A refused listing removed nothing: the ledger is untouched by a read that did not happen.
    assert!(dir.0.join("1-1.json").exists());
}

/// **`ls --remote` marks each ledger entry against BOTH listings, names what the ledger does
/// not, and `--prune` removes exactly the gone entries.**
///
/// Six records, one per way an entry can stand against the account: `live` names a VM the
/// listing still carries RUNNING; `gone` names a VM and an image neither listing carries;
/// `dead` names a VM the listing carries TERMINATED, which is not alive (the same predicate
/// `tools/verify-clean.py` uses, so the two tools cannot disagree about a leak); `other`
/// was recorded in a region this invocation did not ask, so no listing can judge it;
/// `loggroup` names a TERMINATED VM and a `/aws/lambda-microvms/...` log group in `leaked`,
/// which neither listing can see, so it is `unjudged` and its file — the only pointer to that
/// group — survives a prune (the shape 3 of 72 real records had on 2026-09-12); `odd` leaks
/// an identifier in a spelling neither listing speaks for, and is left alone for the same
/// reason. One stranger of each kind in the account stands for the sibling-client case the
/// issue measured (two `microvm-cli-*` images from another pid).
///
/// The VM ids are spelled `microvm-…` because that is how the service spells them and how
/// the reconciliation tells an absent VM id from an identifier it cannot judge; a fixture
/// spelled `mvm-…` would read `unjudged` here, which is the id-prefix lesson of 2026-08-28
/// pointing the other way.
///
/// **Guard proof.** Invert the status (`live` for an absent VM, `gone` for a listed one)
/// and the `entries` assertion reads the inverted table; the prune half then removes the
/// live record. Run 2026-09-12 against the inverted predicate; failed at the first
/// `entries` assertion. Judge the record by its named fields instead of its `leaked` list
/// (the code before the #164 review) and `5-loggroup` reads `gone`; run 2026-09-12 against
/// that code, failed at the same assertion.
#[tokio::test]
async fn ls_remote_reconciles_against_both_listings_and_prunes_only_gone_entries() {
    let dir = TempDir::new("ls-remote");
    let live_arn = image_arn("kept");
    let gone_arn = image_arn("deleted-long-ago");
    write_ledger(
        &dir.0,
        "1-live",
        "us-east-1",
        Some("microvm-live"),
        Some(&live_arn),
    );
    write_ledger(
        &dir.0,
        "2-gone",
        "us-east-1",
        Some("microvm-gone"),
        Some(&gone_arn),
    );
    write_ledger(&dir.0, "3-dead", "us-east-1", Some("microvm-dead"), None);
    write_ledger(&dir.0, "4-other", "eu-west-1", Some("microvm-other"), None);
    write_ledger_leaked(
        &dir.0,
        "5-loggroup",
        "us-east-1",
        Some("microvm-dead"),
        None,
        &["/aws/lambda-microvms/x"],
    );
    write_ledger_leaked(
        &dir.0,
        "6-odd",
        "us-east-1",
        Some("microvm-dead"),
        None,
        &["mvm-odd"],
    );

    let transport = Arc::new(ScriptedTransport::new());
    transport
        .answer(
            "ListMicrovms",
            200,
            &list_microvms_body(&[
                ("microvm-live", "RUNNING"),
                ("microvm-dead", "TERMINATED"),
                ("microvm-stranger", "RUNNING"),
            ]),
        )
        .answer(
            "ListMicrovmImages",
            200,
            &list_images_body(&["kept", "stranger-image"], None),
        );
    let seam = ScriptedSeam {
        transport: Arc::clone(&transport),
        clock: Arc::new(YieldingClock::default()),
    };

    let (result, _) = dispatch_with(
        &seam,
        &Command::Ls(ls_args(&dir.0, true, false)),
        full_infra(),
    )
    .await;
    let rendered = result.expect("a scripted listing succeeds");
    assert_eq!(transport.called("ListMicrovms"), 1);
    assert_eq!(transport.called("ListMicrovmImages"), 1);
    assert_eq!(
        transport.calls().len(),
        2,
        "two listings and nothing else: {:?}",
        transport.calls()
    );

    let remote = &rendered.data["remote"];
    assert_eq!(remote["region"], "us-east-1");
    assert_eq!(remote["microvms"].as_array().expect("an array").len(), 3);
    assert_eq!(remote["images"].as_array().expect("an array").len(), 2);
    let statuses: Vec<(String, String)> = remote["entries"]
        .as_array()
        .expect("entries")
        .iter()
        .map(|entry| {
            (
                entry["runId"].as_str().unwrap_or_default().to_string(),
                entry["status"].as_str().unwrap_or_default().to_string(),
            )
        })
        .collect();
    assert_eq!(
        statuses,
        [
            ("1-live".to_string(), "live".to_string()),
            ("2-gone".to_string(), "gone".to_string()),
            ("3-dead".to_string(), "gone".to_string()),
            ("4-other".to_string(), "unjudged".to_string()),
            ("5-loggroup".to_string(), "unjudged".to_string()),
            ("6-odd".to_string(), "unjudged".to_string()),
        ],
        "{remote}"
    );
    // The per-resource states say WHY: a live entry names a RUNNING VM; the dead one names a
    // TERMINATED VM (listed, not alive); the gone one names nothing the account has. The
    // log-group record's VM is TERMINATED too — the state is reported, and the verdict still
    // is not `gone`, because the state of the VM says nothing about the group.
    let entries = remote["entries"].as_array().expect("entries");
    assert_eq!(entries[0]["microvmState"], "RUNNING");
    assert_eq!(entries[0]["imageState"], "ACTIVE");
    assert_eq!(entries[1]["microvmState"], serde_json::Value::Null);
    assert_eq!(entries[1]["imageState"], serde_json::Value::Null);
    assert_eq!(entries[2]["microvmState"], "TERMINATED");
    assert_eq!(entries[4]["microvmState"], "TERMINATED");
    assert_eq!(
        remote["unknownToLedger"]["microvms"],
        serde_json::json!(["microvm-stranger"]),
        "{remote}"
    );
    assert_eq!(
        remote["unknownToLedger"]["images"],
        serde_json::json!([image_arn("stranger-image")]),
        "{remote}"
    );
    // Without --prune nothing is removed, and the envelope says so with an empty array.
    assert_eq!(rendered.data["pruned"], serde_json::json!([]));
    for name in [
        "1-live",
        "2-gone",
        "3-dead",
        "4-other",
        "5-loggroup",
        "6-odd",
    ] {
        assert!(
            dir.0.join(format!("{name}.json")).exists(),
            "{name} removed without --prune"
        );
    }
    // The opaque key survived into the runs list untouched.
    assert_eq!(rendered.data["runs"][0]["someFutureKey"]["nested"], true);

    // --prune: exactly the gone entries go, in ledger order; live and unjudged stay.
    let (result, _) = dispatch_with(
        &seam,
        &Command::Ls(ls_args(&dir.0, true, true)),
        full_infra(),
    )
    .await;
    let rendered = result.expect("a scripted listing succeeds");
    assert_eq!(
        rendered.data["pruned"],
        serde_json::json!(["2-gone", "3-dead"]),
        "{}",
        rendered.data["pruned"]
    );
    assert!(
        !dir.0.join("2-gone.json").exists(),
        "the gone record must be removed"
    );
    assert!(
        !dir.0.join("3-dead.json").exists(),
        "a TERMINATED VM is not alive; its record goes"
    );
    assert!(
        dir.0.join("1-live.json").exists(),
        "a live record is never pruned"
    );
    assert!(
        dir.0.join("4-other.json").exists(),
        "an unjudged record is never pruned"
    );
    assert!(
        dir.0.join("5-loggroup.json").exists(),
        "a record whose leaked list names a log group is the only pointer to that group; \
         no listing can judge it, so a prune must leave it"
    );
    assert!(
        dir.0.join("6-odd.json").exists(),
        "an identifier in a spelling neither listing speaks for is never pruned on a guess"
    );
    // And the runs list is the post-prune ledger, so the envelope agrees with the disk.
    assert_eq!(rendered.data["runs"].as_array().expect("runs").len(), 4);
    // The human text carries the ledger's own definition and the reconciliation verdicts.
    assert!(
        rendered.text.starts_with("local ledger of "),
        "{}",
        rendered.text
    );
    assert!(
        rendered.text.contains("pruned 2 gone record(s)"),
        "{}",
        rendered.text
    );
}
