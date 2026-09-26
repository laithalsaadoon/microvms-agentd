// SPDX-License-Identifier: Apache-2.0
//! Test-only because `lib.rs` declares it under `#[cfg(any(test, feature = "test-support"))]`.

impl crate::Fetch for Scripted {}
