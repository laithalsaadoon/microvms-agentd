// SPDX-License-Identifier: Apache-2.0
//! The sentinel's adapter: one shipped case of each finding, then every test-only form the
//! collectors must skip. `ratchet/fixtures/sentinel/expected.json` lists what must be reported.

pub fn upload() {
    std::process::Command::new("aws");
}

// A macro's arguments are raw tokens to tree-sitter, so this one takes its own rule.
pub async fn raced() {
    tokio::select! {
        _ = tokio::process::Command::new("gh").output() => {}
    }
}

pub struct Minter;

impl kernel::TokenMinter for Minter {}

impl std::fmt::Display for Minter {}

impl Minter {}

#[cfg(test)]
fn item_level() {
    std::process::Command::new("item");
}

/// A doc comment between the attribute and the item.
#[cfg(test)]
/// And one after it.
#[allow(dead_code)]
fn documented() {
    std::process::Command::new("documented");
}

#[cfg(test)]
mod tests {
    fn inner() {
        std::process::Command::new("tests");
        let _ = vec![std::process::Command::new("in-a-macro")];
    }
}

#[cfg(test)]
mod fake {
    impl kernel::Fetch for Fake {}
    fn inner() {
        tokio::process::Command::new("fake");
    }
}

#[test]
fn a_test() {
    std::process::Command::new("test-fn");
}

#[tokio::test(flavor = "multi_thread")]
async fn an_async_test() {
    std::process::Command::new("tokio-test");
}

#[cfg(test)]
impl Fetch for OnlyInTests {}

#[cfg(test)]
mod helpers;
mod session;
mod testing;
