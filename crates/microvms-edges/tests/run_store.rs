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

// The macOS test filesystem rejects invalid UTF-8 names with EILSEQ. Linux permits
// the two byte-distinct filenames this regression needs to exercise lossy aliasing.
#[cfg(target_os = "linux")]
#[test]
fn non_unicode_ledger_names_are_read_only_without_aliasing_unicode_neighbors() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    use std::sync::Arc;

    use microvms_app::ledger::{Ledger, read_all};

    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    let raw_path = root.join(OsString::from_vec(b"bad-\xff.json".to_vec()));
    let unicode_path = root.join("bad-\u{fffd}.json");
    let raw_text = r#"{"runId":"raw","region":"r","microvmId":"vm-raw","leaked":["vm-raw"]}"#;
    let unicode_text =
        r#"{"runId":"unicode","region":"r","microvmId":"vm-unicode","leaked":["vm-unicode"]}"#;
    std::fs::write(&raw_path, raw_text).unwrap();
    let store = Arc::new(FileRunStore::new(root));

    // A raw filename must not create its Unicode replacement when reopened or flushed.
    for mut ledger in Ledger::open_all_for_vm_with_store(store.clone(), "vm-raw") {
        ledger.mark_deleted(&[]);
        assert!(
            !unicode_path.exists(),
            "flush must not create a lossy alias"
        );
        ledger.clear();
    }
    assert_eq!(std::fs::read(&raw_path).unwrap(), raw_text.as_bytes());
    assert!(!unicode_path.exists());

    std::fs::write(&unicode_path, unicode_text).unwrap();
    for mut ledger in Ledger::open_all_for_vm_with_store(store.clone(), "vm-raw") {
        ledger.mark_deleted(&[]);
        assert_eq!(std::fs::read(&raw_path).unwrap(), raw_text.as_bytes());
        assert_eq!(
            std::fs::read(&unicode_path).unwrap(),
            unicode_text.as_bytes()
        );
        ledger.clear();
    }
    assert_eq!(std::fs::read(&raw_path).unwrap(), raw_text.as_bytes());
    assert_eq!(
        std::fs::read(&unicode_path).unwrap(),
        unicode_text.as_bytes()
    );
    let documents = store.ledgers();
    assert_eq!(documents.len(), 2);
    let raw_document = documents.iter().find(|doc| doc.key.is_empty()).unwrap();
    assert_eq!(raw_document.text, None);
    assert_eq!(raw_document.location, format!("{raw_path:?}"));
    assert_ne!(raw_document.location, unicode_path.to_string_lossy());
    assert_eq!(
        store
            .write_ledger(&raw_document.key, "replacement")
            .unwrap_err()
            .kind(),
        ErrorKind::InvalidInput
    );
    assert_eq!(
        store.remove_ledger(&raw_document.key).unwrap_err().kind(),
        ErrorKind::InvalidInput
    );
    let values = read_all(store.as_ref());
    assert!(
        values.iter().any(|value| {
            value["runId"] == "" && value["unreadable"] == format!("{raw_path:?}")
        })
    );
    assert_eq!(
        values
            .iter()
            .find(|value| value["runId"] == "unicode")
            .unwrap(),
        &serde_json::from_str::<serde_json::Value>(unicode_text).unwrap()
    );

    assert_eq!(std::fs::read(&raw_path).unwrap(), raw_text.as_bytes());
    assert_eq!(
        std::fs::read(&unicode_path).unwrap(),
        unicode_text.as_bytes()
    );

    // A literal replacement character is representable and keeps its own exact identity.
    let mut unicode = Ledger::open_all_for_vm_with_store(store, "vm-unicode");
    assert_eq!(unicode.len(), 1);
    unicode[0].mark_deleted(&[]);
    assert!(unicode_path.is_file());
    let updated: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&unicode_path).unwrap()).unwrap();
    assert_eq!(updated["runId"], "unicode");
    assert_eq!(updated["leaked"], serde_json::json!([]));
    assert_eq!(std::fs::read(&raw_path).unwrap(), raw_text.as_bytes());
    unicode[0].clear();
    assert!(!unicode_path.exists());
    assert_eq!(std::fs::read(&raw_path).unwrap(), raw_text.as_bytes());
}
