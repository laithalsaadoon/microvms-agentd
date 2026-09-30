// SPDX-License-Identifier: Apache-2.0
//! A module that marks itself test-only.
#![cfg(test)]

fn inner_attribute() {
    std::process::Command::new("testing");
}

impl kernel::Fetch for Scripted {}
