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

/// In-memory run documents for tests of the ledger and history policies.
#[derive(Debug, Default)]
pub struct MemoryRunStore {
    pub documents: std::sync::Mutex<Vec<crate::run_store::StoredLedger>>,
    pub histories: std::sync::Mutex<std::collections::HashMap<String, String>>,
    pub removed: std::sync::Mutex<Vec<String>>,
}

impl crate::run_store::RunStore for MemoryRunStore {
    fn ledgers(&self) -> Vec<crate::run_store::StoredLedger> {
        self.documents.lock().unwrap().clone()
    }

    fn write_ledger(&self, key: &str, text: &str) -> std::io::Result<()> {
        let mut documents = self.documents.lock().unwrap();
        documents.retain(|document| document.key != key);
        documents.push(crate::run_store::StoredLedger {
            key: key.into(),
            location: format!("memory/{key}"),
            text: Some(text.into()),
        });
        Ok(())
    }

    fn remove_ledger(&self, key: &str) -> std::io::Result<()> {
        self.documents
            .lock()
            .unwrap()
            .retain(|document| document.key != key);
        self.removed.lock().unwrap().push(key.into());
        Ok(())
    }

    fn read_history(&self, vm_id: &str) -> std::io::Result<String> {
        self.histories
            .lock()
            .unwrap()
            .get(vm_id)
            .cloned()
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "no history"))
    }

    fn append_history(&self, vm_id: &str, line: &str) -> std::io::Result<()> {
        let mut histories = self.histories.lock().unwrap();
        let history = histories.entry(vm_id.into()).or_default();
        history.push_str(line);
        history.push('\n');
        Ok(())
    }
}

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

/// A strict, ordered transport for parity fixtures. Responses stay literal service JSON;
/// callers cannot silently consume a response for a different operation or repeat the last.
#[derive(Debug)]
pub struct ParityControlPlane {
    exchanges: std::sync::Mutex<std::collections::VecDeque<serde_json::Value>>,
    calls: std::sync::Mutex<Vec<String>>,
}

impl ParityControlPlane {
    pub fn from_json(script: &str) -> Result<Arc<Self>, crate::Error> {
        let exchanges: Vec<serde_json::Value> = serde_json::from_str(script).map_err(|error| {
            crate::Error::new(
                crate::ErrorKind::InvalidArg,
                format!("invalid parity script: {error}"),
            )
        })?;
        if exchanges.is_empty()
            || exchanges.iter().any(|row| {
                row["operation"].as_str().is_none()
                    || row["status"].as_u64().is_none()
                    || row["body"].as_str().is_none()
            })
        {
            return Err(crate::Error::new(
                crate::ErrorKind::InvalidArg,
                "parity script needs exchanges with operation, status and literal body",
            ));
        }
        Ok(Arc::new(Self {
            exchanges: std::sync::Mutex::new(exchanges.into()),
            calls: std::sync::Mutex::new(Vec::new()),
        }))
    }

    pub fn plane(self: &Arc<Self>) -> ControlPlane {
        control_plane(
            self.clone(),
            Region::UsEast1,
            Arc::new(YieldingClock::new()),
        )
    }

    pub fn calls(&self) -> Vec<String> {
        self.calls.lock().expect("parity calls lock").clone()
    }
}

impl Transport for ParityControlPlane {
    fn send(
        &self,
        call: crate::control::transport::Call,
    ) -> std::pin::Pin<
        Box<
            dyn std::future::Future<Output = Result<crate::control::transport::Reply, crate::Error>>
                + Send
                + '_,
        >,
    > {
        let answer = self
            .exchanges
            .lock()
            .expect("parity exchanges lock")
            .pop_front();
        self.calls
            .lock()
            .expect("parity calls lock")
            .push(call.operation.to_string());
        Box::pin(async move {
            let answer = answer.ok_or_else(|| {
                crate::Error::new(crate::ErrorKind::Precondition, "parity script exhausted")
            })?;
            if answer["operation"].as_str() != Some(call.operation) {
                return Err(crate::Error::new(
                    crate::ErrorKind::Precondition,
                    format!(
                        "parity script expected {}, received {}",
                        answer["operation"], call.operation
                    ),
                ));
            }
            Ok(crate::control::transport::Reply {
                status: u16::try_from(answer["status"].as_u64().unwrap()).map_err(|_| {
                    crate::Error::new(crate::ErrorKind::InvalidArg, "invalid parity status")
                })?,
                body: answer["body"].as_str().unwrap().as_bytes().to_vec(),
                error_type: answer["error_type"].as_str().map(str::to_string),
            })
        })
    }
}
