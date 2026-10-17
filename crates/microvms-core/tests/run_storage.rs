// SPDX-License-Identifier: Apache-2.0
//! Frozen 1.1.1 readers/writers prove the storage move needs no data migration.

use std::sync::Arc;

use microvms_core::prelude::LedgerExt as _;
use microvms_core::{history, ledger};
use serde::{Deserialize, Serialize};

// These structs deliberately do not reuse the new records. They are the 1.1.1
// storage contracts, so changing today's serializer cannot silently change both sides.
#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
struct LegacyRun {
    run_id: String,
    region: String,
    #[serde(default)]
    image_identifier: Option<String>,
    #[serde(default)]
    image_name: Option<String>,
    #[serde(default)]
    microvm_id: Option<String>,
    #[serde(default)]
    leaked: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    vm_log_group: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(
    tag = "event",
    rename_all = "camelCase",
    rename_all_fields = "camelCase"
)]
enum LegacyEvent {
    ImageBuilt {
        image_identifier: String,
        image_name: String,
    },
    Exec {
        exec_id: String,
        exit_code: Option<i32>,
        truncated: bool,
        writers_may_be_alive: Option<bool>,
    },
    Suspended,
    Resumed,
    Launched {
        image_identifier: String,
        endpoint: String,
        region: String,
    },
    HookObserved {
        hook: String,
        fired_at: u64,
    },
    Terminated {
        terminate_accepted: bool,
        undeleted: Vec<String>,
    },
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
struct LegacyHistory {
    seq: u64,
    at: u64,
    #[serde(flatten)]
    event: LegacyEvent,
}

#[test]
fn legacy_ledger_is_reopened_updated_and_read_by_the_legacy_reader() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("1700000000-1.json");
    // Earlier records did not contain vmLogGroup; no rewrite or backfill is needed.
    let old = r#"{"runId":"1700000000-1","region":"us-east-1","imageIdentifier":"arn:image","imageName":"image","microvmId":"vm-1","leaked":["vm-1","arn:image"]}"#;
    std::fs::write(&path, old).unwrap();
    let all = ledger::Ledger::open_all_for_vm(root.path(), "vm-1");
    assert_eq!(all.len(), 1);
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        old,
        "opening never rewrites old data"
    );
    let mut run = all.into_iter().next().unwrap();
    assert_eq!(run.record.vm_log_group, None);
    run.mark_deleted(&["arn:image".to_string()]);
    run.record_vm_log_group("/example/run");
    let legacy: LegacyRun = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert_eq!(legacy.run_id, "1700000000-1");
    assert_eq!(legacy.microvm_id.as_deref(), Some("vm-1"));
    assert_eq!(legacy.image_identifier.as_deref(), Some("arn:image"));
    assert_eq!(legacy.leaked, ["arn:image"]);
    assert_eq!(legacy.vm_log_group.as_deref(), Some("/example/run"));
    run.clear();
    assert!(path.exists(), "an unresolved resource keeps its record");
    run.mark_deleted(&[]);
    run.clear();
    assert!(!path.exists());
}

#[test]
fn new_ledger_is_read_and_rewritten_by_the_legacy_writer() {
    let root = tempfile::tempdir().unwrap();
    let mut run = ledger::new("us-west-2", root.path());
    run.record_image("arn:new", "new");
    run.record_microvm("vm-2");
    run.mark_outstanding();
    let path = root.path().join(format!("{}.json", run.record.run_id));
    let mut legacy: LegacyRun =
        serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert_eq!(legacy.region, "us-west-2");
    assert_eq!(legacy.leaked, ["vm-2", "arn:new"]);
    legacy.leaked = vec!["arn:new".into()];
    std::fs::write(&path, serde_json::to_string_pretty(&legacy).unwrap()).unwrap();
    let reopened = ledger::open_all_for_vm(root.path(), "vm-2");
    assert_eq!(reopened[0].record.leaked, ["arn:new"]);
    assert_eq!(reopened[0].record.image_name.as_deref(), Some("new"));
}

#[test]
fn history_appends_across_old_and_new_writers_without_replacing_old_lines() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("history/vm-1.jsonl");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    let legacy = LegacyHistory {
        seq: 0,
        at: 1700000000,
        event: LegacyEvent::Launched {
            image_identifier: "arn:old".into(),
            endpoint: "https://vm.example".into(),
            region: "us-east-1".into(),
        },
    };
    let old_line = format!("{}\n", serde_json::to_string(&legacy).unwrap());
    std::fs::write(&path, &old_line).unwrap();
    let clock = Arc::new(microvms_app::testing::TestClock::new());
    clock.set_unix_now(std::time::Duration::from_secs(1700000123));
    let history = history::History::from_store(
        "vm-1",
        Arc::new(ledger::FileRunStore::new(root.path())),
        clock,
    );
    history.append(history::Event::Terminated {
        terminate_accepted: true,
        undeleted: vec!["arn:old".into()],
    });
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(text.starts_with(&old_line));
    let mut old_read: Vec<LegacyHistory> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(old_read.remove(0), legacy);
    assert_eq!(
        old_read[0],
        LegacyHistory {
            seq: 1,
            at: 1700000123,
            event: LegacyEvent::Terminated {
                terminate_accepted: true,
                undeleted: vec!["arn:old".into()]
            }
        }
    );
    // A 1.1.1 writer may subsequently append a hook; the new reader and dedup must see it.
    use std::io::Write as _;
    let hook = LegacyHistory {
        seq: 2,
        at: 1700000124,
        event: LegacyEvent::HookObserved {
            hook: "run".into(),
            fired_at: 1700000001,
        },
    };
    writeln!(
        std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap(),
        "{}",
        serde_json::to_string(&hook).unwrap()
    )
    .unwrap();
    let observed = [
        microvms_core::protocol::health::HookObservation {
            hook: "run".into(),
            fired_at: 1700000001,
            handler: None,
        },
        microvms_core::protocol::health::HookObservation {
            hook: "suspend".into(),
            fired_at: 1700000002,
            handler: None,
        },
    ];
    history::append_unseen_hooks(root.path(), "vm-1", &observed);
    history::append_unseen_hooks(root.path(), "vm-1", &observed);
    let read = history::read_events(root.path(), "vm-1");
    assert_eq!(read.len(), 4);
    assert_eq!(read[2]["firedAt"], 1700000001);
    assert_eq!(read[3]["seq"], 3);
    assert_eq!(read[3]["event"], "hookObserved");
    assert_eq!(read[3]["hook"], "suspend");
    assert_eq!(read[3]["firedAt"], 1700000002);
    let mut run = ledger::new("us-east-1", root.path());
    run.flush();
    run.mark_deleted(&[]);
    run.clear();
    assert_eq!(
        history::read_events(root.path(), "vm-1"),
        read,
        "clearing a run leaves history intact"
    );
}

#[test]
fn every_history_event_retains_the_legacy_wire_shape() {
    let root = tempfile::tempdir().unwrap();
    let events = vec![
        LegacyEvent::ImageBuilt {
            image_identifier: "arn:image".into(),
            image_name: "image".into(),
        },
        LegacyEvent::Launched {
            image_identifier: "arn:image".into(),
            endpoint: "https://vm.example".into(),
            region: "us-east-1".into(),
        },
        LegacyEvent::Exec {
            exec_id: "x-1".into(),
            exit_code: None,
            truncated: false,
            writers_may_be_alive: None,
        },
        LegacyEvent::Exec {
            exec_id: "x-2".into(),
            exit_code: Some(7),
            truncated: true,
            writers_may_be_alive: Some(true),
        },
        LegacyEvent::HookObserved {
            hook: "run".into(),
            fired_at: 42,
        },
        LegacyEvent::Suspended,
        LegacyEvent::Resumed,
        LegacyEvent::Terminated {
            terminate_accepted: false,
            undeleted: vec!["vm-1".into()],
        },
    ];
    let writer = history::for_vm(root.path(), "vm-1");
    for event in &events {
        let event: history::Event =
            serde_json::from_value(serde_json::to_value(event).unwrap()).unwrap();
        writer.append(event);
    }
    let path = root.path().join("history/vm-1.jsonl");
    let text = std::fs::read_to_string(&path).unwrap();
    let rows: Vec<LegacyHistory> = text
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), events.len());
    for (index, (row, expected)) in rows.iter().zip(events).enumerate() {
        assert_eq!(row.seq, index as u64);
        assert_eq!(row.event, expected);
    }
    let old_written: String = rows
        .iter()
        .map(|row| format!("{}\n", serde_json::to_string(row).unwrap()))
        .collect();
    std::fs::write(path, old_written).unwrap();
    let read = history::read_events(root.path(), "vm-1");
    assert_eq!(
        read,
        rows.iter()
            .map(|row| serde_json::to_value(row).unwrap())
            .collect::<Vec<_>>()
    );
}

#[test]
fn file_store_refuses_keys_that_leave_their_namespace() {
    use ledger::RunStore as _;
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("runs");
    std::fs::create_dir_all(&root).unwrap();
    let outside = dir.path().join("outside.json");
    std::fs::write(&outside, "unchanged").unwrap();
    let store = ledger::FileRunStore::new(&root);
    let absolute = dir.path().join("outside").to_string_lossy().into_owned();
    for key in [
        "",
        ".",
        "..",
        "../outside",
        "../names/x",
        "nested/key",
        "..\\outside",
        "C:outside",
        absolute.as_str(),
    ] {
        assert_eq!(
            store.write_ledger(key, "changed").unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput,
            "write: {key}"
        );
        assert_eq!(
            store.remove_ledger(key).unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput,
            "remove: {key}"
        );
        assert_eq!(
            store.read_history(key).unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput,
            "read: {key}"
        );
        assert_eq!(
            store.append_history(key, "changed").unwrap_err().kind(),
            std::io::ErrorKind::InvalidInput,
            "append: {key}"
        );
    }
    assert_eq!(std::fs::read_to_string(outside).unwrap(), "unchanged");
    assert_eq!(std::fs::read_dir(&root).unwrap().count(), 0);
}

#[test]
fn legacy_file_names_remain_readable_and_reopened_writes_keep_the_original_key() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legacy.run with spaces.json");
    let original =
        r#"{"runId":"different-id","region":"us-east-1","microvmId":"vm-1","leaked":["vm-1"]}"#;
    std::fs::write(&path, original).unwrap();
    assert_eq!(ledger::read_all(dir.path())[0]["runId"], "different-id");
    let mut runs = ledger::open_all_for_vm(dir.path(), "vm-1");
    assert_eq!(runs.len(), 1);
    runs[0].mark_deleted(&[]);
    assert!(
        path.exists(),
        "reopening keeps the stored key, not the runId field"
    );
    assert!(!dir.path().join("different-id.json").exists());
    runs[0].clear();
    assert!(!path.exists());
}

#[test]
fn removing_a_run_deletes_only_its_stored_key_and_reports_a_missing_record() {
    let root = tempfile::tempdir().unwrap();
    let key = "1700000000-1";
    let removed = root.path().join(format!("{key}.json"));
    let neighbor = root.path().join("keep.json");
    std::fs::write(&removed, "removed record").unwrap();
    std::fs::write(&neighbor, "retained record").unwrap();
    history::for_vm(root.path(), "vm-1").append(history::Event::Suspended);
    let events = history::read_events(root.path(), "vm-1");

    ledger::remove(root.path(), key).unwrap();
    assert!(
        !removed.exists(),
        "a successful remove deletes the stored file"
    );
    assert_eq!(
        std::fs::read_to_string(&neighbor).unwrap(),
        "retained record"
    );
    assert_eq!(history::read_events(root.path(), "vm-1"), events);
    assert_eq!(
        ledger::remove(root.path(), key).unwrap_err().kind(),
        std::io::ErrorKind::NotFound
    );
    assert_eq!(
        ledger::remove(root.path(), "../keep").unwrap_err().kind(),
        std::io::ErrorKind::InvalidInput
    );
    assert_eq!(
        std::fs::read_to_string(&neighbor).unwrap(),
        "retained record"
    );
}
