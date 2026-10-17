// SPDX-License-Identifier: Apache-2.0
//! The file adapter preserves durable bytes and keeps ledger and history namespaces apart.

use std::io::ErrorKind;

use microvms_app::run_store::RunStore;
use microvms_edges::run_store::FileRunStore;

#[test]
fn ledger_files_are_discovered_replaced_and_removed_without_touching_history() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path().join("new").join("state");
    let store = FileRunStore::new(&root);
    assert!(store.ledgers().is_empty());

    let original = r#"{"version":1,"microvmId":"vm-a","note":"retained bytes"}"#;
    store.write_ledger("run-a", original).unwrap();
    assert_eq!(
        std::fs::read_to_string(root.join("run-a.json")).unwrap(),
        original
    );
    store.append_history("vm-a", "old history").unwrap();
    std::fs::write(root.join("ignored.txt"), "not a ledger").unwrap();
    std::fs::write(root.join("unreadable.json"), [0xff]).unwrap();

    let mut records = store.ledgers();
    records.sort_by(|a, b| a.key.cmp(&b.key));
    assert_eq!(records.len(), 2);
    assert_eq!(records[0].key, "run-a");
    assert_eq!(records[0].text.as_deref(), Some(original));
    assert_eq!(
        std::path::Path::new(&records[0].location),
        root.join("run-a.json")
    );
    assert_eq!(records[1].key, "unreadable");
    assert_eq!(records[1].text, None);

    store.write_ledger("run-a", "replacement").unwrap();
    assert_eq!(
        std::fs::read_to_string(root.join("run-a.json")).unwrap(),
        "replacement"
    );
    store.remove_ledger("run-a").unwrap();
    assert!(!root.join("run-a.json").exists());
    assert_eq!(store.read_history("vm-a").unwrap(), "old history\n");
    assert_eq!(
        store.remove_ledger("run-a").unwrap_err().kind(),
        ErrorKind::NotFound
    );
}

#[test]
fn history_creates_its_parent_and_appends_without_rewriting_existing_bytes() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path().join("new").join("state");
    let store = FileRunStore::new(&root);
    assert_eq!(
        store.read_history("vm-a").unwrap_err().kind(),
        ErrorKind::NotFound
    );
    store.append_history("vm-a", "first record").unwrap();
    store.append_history("vm-a", "second record: café").unwrap();
    let expected = "first record\nsecond record: café\n";
    assert_eq!(store.read_history("vm-a").unwrap(), expected);
    assert_eq!(
        std::fs::read_to_string(root.join("history/vm-a.jsonl")).unwrap(),
        expected
    );
    assert!(store.ledgers().is_empty());
}

#[test]
fn every_store_operation_refuses_keys_outside_one_file_name_component() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path().join("state");
    let store = FileRunStore::new(&root);
    for key in [
        "",
        ".",
        "..",
        "../escape",
        r"..\escape",
        "C:escape",
        "a/b",
        r"a\b",
    ] {
        assert_eq!(
            store.write_ledger(key, "bad").unwrap_err().kind(),
            ErrorKind::InvalidInput,
            "write {key:?}"
        );
        assert_eq!(
            store.remove_ledger(key).unwrap_err().kind(),
            ErrorKind::InvalidInput,
            "remove {key:?}"
        );
        assert_eq!(
            store.read_history(key).unwrap_err().kind(),
            ErrorKind::InvalidInput,
            "read {key:?}"
        );
        assert_eq!(
            store.append_history(key, "bad").unwrap_err().kind(),
            ErrorKind::InvalidInput,
            "append {key:?}"
        );
    }
    assert!(!root.exists());
    assert!(!directory.path().join("escape.json").exists());
}
