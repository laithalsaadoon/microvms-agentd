// SPDX-License-Identifier: Apache-2.0
//! The sentinel's kernel: its public traits are the ports, and its own code is read for port
//! impls the way the app's is.

pub trait Fetch {}

pub trait TokenMinter {}

// Private, so not a port.
trait Private {}

#[cfg(test)]
pub trait OnlyInTests {}

pub fn run(argv: &[String]) {
    let _ = std::process::Command::new(&argv[0]);
}

// A port implemented in the kernel itself, which is reported: a use case implements a port only
// where a decision says why.
pub struct Local;

impl Fetch for Local {}

// Behind the shared test doubles' feature, in each form that counts as test code.
#[cfg(feature = "test-support")]
impl Fetch for FeatureOnly {}

#[cfg(any(test, feature = "test-support"))]
pub mod testing {
    impl super::TokenMinter for super::Local {}
    fn doubles() {
        std::process::Command::new("test-support");
    }
}

#[cfg(any(test, feature = "test-support"))]
mod fakes;
