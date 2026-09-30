// SPDX-License-Identifier: Apache-2.0
//! Test-only because `lib.rs` declares it under `#[cfg(test)]`, and so is everything below it.

mod nested;

fn declared_test_only() {
    std::process::Command::new("helpers");
}
