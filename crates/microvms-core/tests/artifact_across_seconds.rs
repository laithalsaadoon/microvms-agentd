// SPDX-License-Identifier: Apache-2.0
//! IMAGE-7 across the wall clock: an artifact built again a second later is the same bytes.
//!
//! Here rather than beside the zip builder in `microvms-app`, whose code can't block on
//! `std::thread::sleep` (ARCH-7): the property is that nothing reads the real clock, so the
//! test has to let the real clock move. A zip entry's DOS time has two-second resolution, so
//! the gap covers at least one tick of it.

use microvms_core::control::artifact::build_artifact_with_context;
use microvms_core::control::context::{BuildContext, ContextEntry};

#[test]
fn an_artifact_built_a_second_later_is_the_same_bytes() {
    let context = BuildContext::from_entries(vec![
        ContextEntry {
            name: "app/run.sh".to_string(),
            mode: 0o755,
            bytes: b"#!/bin/sh\n".to_vec(),
        },
        ContextEntry {
            name: "data".to_string(),
            mode: 0o644,
            bytes: b"d".to_vec(),
        },
    ])
    .expect("valid entries");
    let build =
        || build_artifact_with_context(b"daemon", "FROM x\n", None, Some(&context)).expect("zips");
    let first = build();
    std::thread::sleep(std::time::Duration::from_millis(2100));
    assert_eq!(first, build(), "IMAGE-7: equal inputs, identical bytes");
}
