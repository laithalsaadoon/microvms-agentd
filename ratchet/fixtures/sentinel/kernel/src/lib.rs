// SPDX-License-Identifier: Apache-2.0
//! The sentinel's kernel: its public traits are the ports.

pub trait Fetch {}

pub trait TokenMinter {}

// Private, so not a port.
trait Private {}

#[cfg(test)]
pub trait OnlyInTests {}

pub fn run(argv: &[String]) {
    let _ = std::process::Command::new(&argv[0]);
}
