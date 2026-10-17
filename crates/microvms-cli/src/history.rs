// SPDX-License-Identifier: Apache-2.0
//! The shared client's durable VM history, composed with the local file store.
pub use microvms_core::history::*;
#[cfg(test)]
use std::path::PathBuf;

#[cfg(test)]
mod tests {
    use super::*;

    /// A temporary directory that cleans itself up. To be replaced by the `tempfile`
    /// dev-dependency in the dependency sweep, with the crate's other copies.
    struct TempDir(PathBuf, #[allow(dead_code)] tempfile::TempDir);

    impl TempDir {
        fn new(label: &str) -> Self {
            let dir = tempfile::Builder::new()
                .prefix(&format!("microvm-cli-history-{label}-"))
                .tempdir()
                .expect("a temp dir");
            Self(dir.path().to_path_buf(), dir)
        }
    }

    fn launched() -> Event {
        Event::Launched {
            image_identifier: "arn:image".to_string(),
            endpoint: "https://mvm-1.example".to_string(),
            region: "us-east-1".to_string(),
        }
    }

    /// An append round-trips through the real file into what a later invocation reads.
    ///
    /// Through the filesystem rather than a mock, for the ledger's reason: the whole
    /// property is that the bytes outlive the process, and an in-memory round trip would
    /// assert nothing about it.
    #[test]
    fn an_appended_event_round_trips_through_the_real_file() {
        let dir = TempDir::new("roundtrip");
        for_vm(&dir.0, "mvm-1").append(launched());

        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(read.len(), 1, "{read:?}");
        assert_eq!(read[0]["event"], "launched");
        assert_eq!(read[0]["imageIdentifier"], "arn:image");
        assert_eq!(read[0]["endpoint"], "https://mvm-1.example");
        assert_eq!(read[0]["region"], "us-east-1");
        assert_eq!(read[0]["seq"], 0);
        assert!(read[0]["at"].as_u64().is_some_and(|at| at > 0), "{read:?}");
    }

    /// `seq` is monotonic across appends, including appends from two `History` values.
    ///
    /// Two values rather than one reused, because that is the real shape: `run` writes
    /// `launched` and a later `microvm terminate` — a different process entirely — writes
    /// `terminated`. A counter held in memory would restart at zero there; counting the
    /// file is what makes the sequence one sequence.
    #[test]
    fn seq_is_monotonic_across_two_appends_from_two_writers() {
        let dir = TempDir::new("seq");
        for_vm(&dir.0, "mvm-1").append(launched());
        for_vm(&dir.0, "mvm-1").append(Event::Suspended);

        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(read.len(), 2);
        assert_eq!(read[0]["seq"], 0);
        assert_eq!(read[1]["seq"], 1);
        assert_eq!(read[1]["event"], "suspended");
    }

    /// The record survives a terminate: the terminated event appends, and the file stays.
    ///
    /// **Falsification** — add a ledger-style `clear()` after the `Terminated` append (the
    /// tidy-up this module's docs refuse) and both assertions go red: the whole value of
    /// history is being readable after the VM is gone. Broken exactly so on 2026-08-25,
    /// failed as stated, restored.
    #[test]
    fn a_terminated_event_appends_and_the_file_survives_it() {
        let dir = TempDir::new("survives-terminate");
        let history = for_vm(&dir.0, "mvm-1");
        history.append(launched());
        history.append(Event::Terminated {
            terminate_accepted: true,
            undeleted: vec!["arn:image".to_string()],
        });

        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(read.len(), 2, "the record must outlive the VM: {read:?}");
        assert_eq!(read[1]["event"], "terminated");
        assert_eq!(read[1]["terminateAccepted"], true);
        assert_eq!(read[1]["undeleted"], serde_json::json!(["arn:image"]));
        assert!(
            dir.0.join("history").join("mvm-1.jsonl").exists(),
            "nothing ever deletes a history file"
        );
    }

    /// An unwritable directory does not raise.
    ///
    /// The teardown path calls this, and a history write that raised would replace the
    /// caller's real failure with a filesystem one.
    ///
    /// The unwritable root is a regular *file*, not `/proc/...`: a path component that is a
    /// file cannot become a directory on any platform, whereas `/proc/definitely-not-writable`
    /// resolves to a perfectly creatable `<drive>:\proc\...` on Windows — measured as exactly
    /// this test writing there and failing on the windows-latest CI tier.
    #[test]
    fn an_unwritable_state_directory_is_survivable() {
        let dir = TempDir::new("unwritable");
        let root = dir.0.join("a-file-not-a-directory");
        std::fs::write(&root, "occupied").expect("writes");
        // The append really is attempted against the unwritable root — rather than a
        // history that quietly writes nowhere and would pass for the wrong reason.
        for_vm(&root, "mvm-1").append(launched());
        assert!(
            read_events(&root, "mvm-1").is_empty(),
            "nothing was written, and nothing raised"
        );
        assert!(root.is_file(), "the root is still the file it was");
    }

    /// A truncated last line is reported as unreadable rather than skipped.
    ///
    /// It is what a process killed mid-append leaves, and that process is the one whose
    /// history most needs reading — so silence is the worst available answer. The intact
    /// lines before it still parse, because one bad line must not cost the record.
    #[test]
    fn a_truncated_last_line_is_reported_rather_than_skipped() {
        let dir = TempDir::new("truncated");
        for_vm(&dir.0, "mvm-1").append(launched());
        {
            use std::io::Write as _;
            let mut file = std::fs::OpenOptions::new()
                .append(true)
                .open(dir.0.join("history").join("mvm-1.jsonl"))
                .expect("opens");
            write!(file, "{{\"seq\":1,\"at\":175").expect("writes the torn tail");
        }

        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(
            read.len(),
            2,
            "the torn line is a record, not a skip: {read:?}"
        );
        assert_eq!(read[0]["event"], "launched");
        assert!(read[1]["unreadable"].is_string(), "{read:?}");
        assert_eq!(read[1]["line"], 2);
    }

    /// The wire shape is camelCase, key for key.
    ///
    /// Pinned the way the ledger pins its own: the file is read by whichever `microvm` is
    /// on a caller's PATH, and a record only one build can read fails exactly when someone
    /// reaches for it.
    #[test]
    fn every_event_serializes_with_camel_case_keys() {
        let dir = TempDir::new("camelcase");
        let history = for_vm(&dir.0, "mvm-1");
        history.append(Event::ImageBuilt {
            image_identifier: "arn:image".to_string(),
            image_name: "img".to_string(),
        });
        history.append(launched());
        history.append(Event::Exec {
            exec_id: "x-1".to_string(),
            exit_code: Some(0),
            truncated: false,
            writers_may_be_alive: Some(false),
        });
        history.append(Event::HookObserved {
            hook: "validate".to_string(),
            fired_at: 1_756_500_000,
        });
        history.append(Event::Terminated {
            terminate_accepted: false,
            undeleted: vec!["mvm-1".to_string()],
        });

        let read = read_events(&dir.0, "mvm-1");
        let keys_of = |value: &serde_json::Value| -> Vec<String> {
            let mut keys: Vec<String> = value
                .as_object()
                .expect("an object")
                .keys()
                .cloned()
                .collect();
            keys.sort();
            keys
        };
        assert_eq!(
            keys_of(&read[0]),
            ["at", "event", "imageIdentifier", "imageName", "seq"]
        );
        assert_eq!(
            keys_of(&read[1]),
            [
                "at",
                "endpoint",
                "event",
                "imageIdentifier",
                "region",
                "seq"
            ]
        );
        assert_eq!(
            keys_of(&read[2]),
            [
                "at",
                "event",
                "execId",
                "exitCode",
                "seq",
                "truncated",
                "writersMayBeAlive"
            ]
        );
        assert_eq!(
            keys_of(&read[3]),
            ["at", "event", "firedAt", "hook", "seq"],
            "the hook line's keys are camelCase like every other event's"
        );
        assert_eq!(read[3]["event"], "hookObserved");
        assert_eq!(read[3]["hook"], "validate");
        assert_eq!(read[3]["firedAt"], 1_756_500_000_u64);
        assert_eq!(
            keys_of(&read[4]),
            ["at", "event", "seq", "terminateAccepted", "undeleted"]
        );

        // And the enum round-trips, so the reader above is the writer's own shape.
        let line = serde_json::to_string(&Record {
            seq: 9,
            at: 1,
            event: Event::Suspended,
        })
        .expect("serializes");
        let back: Record = serde_json::from_str(&line).expect("round trips");
        assert_eq!(back.event, Event::Suspended);
        assert_eq!(back.seq, 9);
    }

    /// The hook variant round-trips through the file, and its wire keys are the
    /// camelCase spellings the renderer and the dedup both read.
    #[test]
    fn a_hook_observation_round_trips_through_the_real_file() {
        let dir = TempDir::new("hook-roundtrip");
        for_vm(&dir.0, "mvm-1").append(Event::HookObserved {
            hook: "run".to_string(),
            fired_at: 1_756_500_100,
        });

        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(read.len(), 1, "{read:?}");
        assert_eq!(read[0]["event"], "hookObserved");
        assert_eq!(read[0]["hook"], "run");
        assert_eq!(read[0]["firedAt"], 1_756_500_100_u64);

        // And through the enum, so the reader above is the writer's own shape.
        let line = serde_json::to_string(&Record {
            seq: 0,
            at: 1,
            event: Event::HookObserved {
                hook: "run".to_string(),
                fired_at: 1_756_500_100,
            },
        })
        .expect("serializes");
        let back: Record = serde_json::from_str(&line).expect("round trips");
        assert_eq!(
            back.event,
            Event::HookObserved {
                hook: "run".to_string(),
                fired_at: 1_756_500_100,
            }
        );
    }

    /// `append_unseen_hooks` appends only the pairs the file has not seen: a repeat
    /// poll appends nothing, a poll with one new firing appends exactly it, and `seq`
    /// stays one monotonic sequence through it all.
    ///
    /// **Falsification** — drop the `seen.insert(..)` check (append unconditionally)
    /// and the second read below counts four rather than two. Broken exactly so on
    /// 2026-08-30, failed as stated, restored.
    #[test]
    fn unseen_hook_observations_append_once_and_a_repeat_poll_appends_nothing() {
        use microvms_core::protocol::health::HookObservation;

        let dir = TempDir::new("hook-dedup");
        let first_poll = vec![
            HookObservation {
                hook: "validate".to_string(),
                fired_at: 100,
                handler: None,
            },
            HookObservation {
                hook: "run".to_string(),
                fired_at: 200,
                handler: None,
            },
        ];
        append_unseen_hooks(&dir.0, "mvm-1", &first_poll);
        assert_eq!(read_events(&dir.0, "mvm-1").len(), 2);

        // The same body again: the daemon's log is append-only, so a re-poll repeats
        // every pair, and none of them may re-append.
        append_unseen_hooks(&dir.0, "mvm-1", &first_poll);
        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(read.len(), 2, "a repeat poll appends nothing: {read:?}");

        // One new firing beside the old two appends exactly it. A second `run` at a
        // different firedAt is a different fact — a platform retry — and is kept.
        let second_poll = vec![
            HookObservation {
                hook: "validate".to_string(),
                fired_at: 100,
                handler: None,
            },
            HookObservation {
                hook: "run".to_string(),
                fired_at: 200,
                handler: None,
            },
            HookObservation {
                hook: "run".to_string(),
                fired_at: 250,
                handler: None,
            },
        ];
        append_unseen_hooks(&dir.0, "mvm-1", &second_poll);
        let read = read_events(&dir.0, "mvm-1");
        assert_eq!(read.len(), 3, "{read:?}");
        assert_eq!(read[2]["hook"], "run");
        assert_eq!(read[2]["firedAt"], 250);
        assert_eq!(read[2]["seq"], 2, "seq is one sequence across the polls");
    }

    /// A VM this state dir never saw reads back empty, not as an error.
    ///
    /// Asking is a question, not a mistake: the id may be real and the record may live in
    /// another machine's state directory.
    #[test]
    fn an_unknown_vm_reads_back_as_a_clean_empty_record() {
        let dir = TempDir::new("unknown");
        assert!(read_events(&dir.0, "mvm-never-seen").is_empty());
    }
}
