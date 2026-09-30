// SPDX-License-Identifier: Apache-2.0
//! The test doubles the crate's own tests use, for any crate that drives it in a test.
//!
//! Behind the `test-support` feature, which a dependent turns on in its `[dev-dependencies]`
//! only. The CLI's guards and core's integration tests used to keep near-copies of these (a
//! yielding clock, a scripted control plane), and each copy had to be changed by hand whenever
//! the port it implemented changed. One set, used by every suite, changes once.
//!
//! Each double follows the rule [`FakeControlPlane`] states: it answers in the platform's own
//! spelling and records what was emitted, and it never asserts on a value the client computed.

use std::sync::Arc;

pub use crate::adapters::testing::TestAdapters;
pub use crate::clock::testing::{TestClock, YieldingClock};
pub use crate::control::fake::*;
pub use crate::entropy::testing::SequenceEntropy;
pub use crate::session::proxy::testing::CountingMinter;
pub use crate::session::testing::{HealthyDaemon, Recorder, Reply, health_body, session_with};

use crate::clock::Clock;
use crate::control::ControlPlane;
use crate::control::transport::Transport;
use crate::region::Region;

/// Daemon bytes every check accepts: a little-endian ELF header naming aarch64
/// (`EM_AARCH64`), then `payload`, so two tests that need different artifact hashes pass
/// different payloads.
pub fn aarch64_daemon(payload: &[u8]) -> Vec<u8> {
    elf_daemon(microvms_domain::provision::REQUIRED_ELF_MACHINE, payload)
}

/// A little-endian ELF header naming `machine`, then `payload`: `0x3E` (x86_64) is the
/// wrong-architecture daemon a build must refuse (#257).
pub fn elf_daemon(machine: u16, payload: &[u8]) -> Vec<u8> {
    let mut bytes = vec![0_u8; 20];
    bytes[..4].copy_from_slice(b"\x7fELF");
    bytes[5] = 1;
    bytes[18..20].copy_from_slice(&machine.to_le_bytes());
    bytes.extend_from_slice(payload);
    bytes
}

/// A control plane over `transport` whose other ports are test doubles: `clock`, a
/// [`SequenceEntropy`], and [`TestAdapters`].
///
/// The entropy is deterministic on purpose, so a test can say which draw became which token.
/// Two planes built here draw the same sequence, which is why a test of cross-attempt
/// distinctness (TRAP-1) uses one plane, or the OS pool.
pub fn control_plane(
    transport: Arc<dyn Transport>,
    region: Region,
    clock: Arc<dyn Clock>,
) -> ControlPlane {
    ControlPlane::from_ports(
        transport,
        region,
        clock,
        Arc::new(SequenceEntropy::new()),
        Arc::new(TestAdapters::new()),
    )
}
