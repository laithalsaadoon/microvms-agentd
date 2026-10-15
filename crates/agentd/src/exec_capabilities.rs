// SPDX-License-Identifier: Apache-2.0
//! AGENTD-26: image-owned, default-off restriction on capabilities inherited across exec.

use std::ffi::OsString;
use std::io;

pub const SETTING: &str = "AGENTD_DROP_EXEC_CAPABILITIES";

// Preserve NET_ADMIN, CHOWN, SETUID and SETGID: routing and ordinary file/user
// operations remain available. This is not an allow-list for daemon privileges.
const DROP_BITS: [u32; 7] = [16, 17, 19, 21, 22, 38, 39];
const DROP_MASK: u64 = 0x00c0_006b_0000;

/// Apply before tracing, the runtime, or any other thread is created. Capability
/// sets belong to a thread; later threads inherit this verified reduction.
pub fn apply_from_env() -> io::Result<bool> {
    apply(enabled(std::env::var_os(SETTING))?, &mut Host)
        .map_err(|error| io::Error::new(error.kind(), format!("{SETTING}: {error}")))
}

fn enabled(value: Option<OsString>) -> io::Result<bool> {
    match value.as_deref().map(std::ffi::OsStr::to_str) {
        None | Some(Some("false" | "0")) => Ok(false),
        Some(Some("true" | "1")) => Ok(true),
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{SETTING} must be true, false, 1, or 0"),
        )),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Snapshot {
    effective: u64,
    permitted: u64,
    inheritable: u64,
    bounding: u64,
    ambient: u64,
    supported: u64,
}

trait Kernel {
    fn snapshot(&mut self) -> io::Result<Snapshot>;
    fn clear_ambient(&mut self) -> io::Result<()>;
    fn clear_inheritable(&mut self, before: Snapshot) -> io::Result<()>;
    fn drop_bounding(&mut self, bit: u32) -> io::Result<()>;
}

fn apply(enabled: bool, kernel: &mut impl Kernel) -> io::Result<bool> {
    if !enabled {
        return Ok(false);
    }
    let before = kernel.snapshot()?;
    if before.supported & DROP_MASK != DROP_MASK {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "kernel lacks a required capability bit",
        ));
    }
    // Empty I/A prevent a later exec from restoring a capability excluded from
    // B. Preserve the daemon's P/E exactly so identity repair still works.
    kernel.clear_ambient()?;
    kernel.clear_inheritable(before)?;
    for bit in DROP_BITS {
        if before.bounding & (1 << bit) != 0 {
            kernel.drop_bounding(bit)?;
        }
    }
    let after = kernel.snapshot()?;
    if after.effective != before.effective
        || after.permitted != before.permitted
        || after.inheritable != 0
        || after.ambient != 0
        || after.bounding != before.bounding & !DROP_MASK
        || after.supported != before.supported
    {
        return Err(io::Error::other(
            "exec capability policy postconditions failed",
        ));
    }
    Ok(true)
}

#[cfg(target_os = "linux")]
struct Host;

#[cfg(not(target_os = "linux"))]
use unsupported::Host;

#[cfg(target_os = "linux")]
impl Kernel for Host {
    fn snapshot(&mut self) -> io::Result<Snapshot> {
        use rustix::thread::{self, CapabilitySet};
        let sets = thread::capabilities(None)?;
        let mut snapshot = Snapshot {
            effective: sets.effective.bits(),
            permitted: sets.permitted.bits(),
            inheritable: sets.inheritable.bits(),
            bounding: 0,
            ambient: 0,
            supported: 0,
        };
        for bit in 0..64 {
            let cap = CapabilitySet::from_bits_retain(1 << bit);
            match thread::capability_is_in_bounding_set(cap) {
                Ok(present) => {
                    snapshot.supported |= 1 << bit;
                    if present {
                        snapshot.bounding |= 1 << bit;
                    }
                }
                // Linux capability numbers are contiguous. EINVAL marks the
                // end of the kernel's supported set; required bits are checked
                // before any mutation. Other errors never mean "already absent".
                Err(rustix::io::Errno::INVAL) => break,
                Err(error) => return Err(error.into()),
            }
            if thread::capability_is_in_ambient_set(cap)? {
                snapshot.ambient |= 1 << bit;
            }
        }
        Ok(snapshot)
    }

    fn clear_ambient(&mut self) -> io::Result<()> {
        Ok(rustix::thread::clear_ambient_capability_set()?)
    }

    fn clear_inheritable(&mut self, before: Snapshot) -> io::Result<()> {
        use rustix::thread::{self, CapabilitySet, CapabilitySets};
        Ok(thread::set_capabilities(
            None,
            CapabilitySets {
                effective: CapabilitySet::from_bits_retain(before.effective),
                permitted: CapabilitySet::from_bits_retain(before.permitted),
                inheritable: CapabilitySet::empty(),
            },
        )?)
    }

    fn drop_bounding(&mut self, bit: u32) -> io::Result<()> {
        Ok(rustix::thread::remove_capability_from_bounding_set(
            rustix::thread::CapabilitySet::from_bits_retain(1 << bit),
        )?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Call {
        Read,
        Ambient,
        Inheritable,
        Drop(u32),
    }

    struct Fake {
        value: Snapshot,
        calls: Vec<Call>,
        fail_at: Option<usize>,
        fault: Option<&'static str>,
    }

    impl Fake {
        fn new() -> Self {
            Self {
                value: Snapshot {
                    // Preserve unknown future bits as well as today's daemon rights.
                    effective: u64::MAX,
                    permitted: u64::MAX,
                    inheritable: (1 << 21) | 1,
                    bounding: u64::MAX,
                    ambient: 1,
                    supported: u64::MAX,
                },
                calls: Vec::new(),
                fail_at: None,
                fault: None,
            }
        }
        fn call(&mut self, call: Call) -> io::Result<()> {
            self.calls.push(call);
            if self.fail_at == Some(self.calls.len()) {
                Err(io::Error::from(io::ErrorKind::PermissionDenied))
            } else {
                Ok(())
            }
        }
    }

    impl Kernel for Fake {
        fn snapshot(&mut self) -> io::Result<Snapshot> {
            self.call(Call::Read)?;
            Ok(self.value)
        }
        fn clear_ambient(&mut self) -> io::Result<()> {
            self.call(Call::Ambient)?;
            if self.fault != Some("ambient") {
                self.value.ambient = 0;
            }
            Ok(())
        }
        fn clear_inheritable(&mut self, before: Snapshot) -> io::Result<()> {
            self.call(Call::Inheritable)?;
            assert_eq!(before.effective, self.value.effective);
            assert_eq!(before.permitted, self.value.permitted);
            if self.fault != Some("inheritable") {
                self.value.inheritable = 0;
            }
            if self.fault == Some("effective") {
                self.value.effective &= !1;
            }
            if self.fault == Some("permitted") {
                self.value.permitted &= !1;
            }
            if self.fault == Some("retained") {
                self.value.bounding &= !1;
            }
            if self.fault == Some("supported") {
                self.value.supported &= !1;
            }
            Ok(())
        }
        fn drop_bounding(&mut self, bit: u32) -> io::Result<()> {
            self.call(Call::Drop(bit))?;
            if self.fault != Some("bounding") {
                self.value.bounding &= !(1 << bit);
            }
            Ok(())
        }
    }

    #[test]
    fn the_setting_is_strict_and_default_off() {
        assert!(!enabled(None).unwrap());
        for value in ["0", "false"] {
            assert!(!enabled(Some(value.into())).unwrap());
        }
        for value in ["1", "true"] {
            assert!(enabled(Some(value.into())).unwrap());
        }
        for value in ["", "TRUE", "yes", " true", "false ", "2"] {
            assert_eq!(
                enabled(Some(value.into())).unwrap_err().kind(),
                io::ErrorKind::InvalidInput
            );
        }
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStringExt;
            assert!(enabled(Some(OsString::from_vec(vec![0xff]))).is_err());
        }
        let mut fake = Fake::new();
        assert!(!apply(false, &mut fake).unwrap());
        assert!(
            fake.calls.is_empty(),
            "default off makes no capability syscall"
        );
    }

    /// AGENTD-26: preserve daemon rights while limiting later execs.
    #[test]
    fn reduction_clears_i_a_and_only_the_seven_bounding_bits() {
        let mut fake = Fake::new();
        let before = fake.value;
        assert!(apply(true, &mut fake).unwrap());
        assert_eq!(DROP_BITS, [16, 17, 19, 21, 22, 38, 39]);
        assert_eq!(
            DROP_MASK,
            DROP_BITS.map(|bit| 1_u64 << bit).iter().sum::<u64>()
        );
        assert_eq!(fake.value.effective, before.effective);
        assert_eq!(fake.value.permitted, before.permitted);
        assert_eq!(fake.value.inheritable, 0);
        assert_eq!(fake.value.ambient, 0);
        assert_eq!(fake.value.bounding, before.bounding & !DROP_MASK);
        assert_eq!(
            fake.calls,
            [
                Call::Read,
                Call::Ambient,
                Call::Inheritable,
                Call::Drop(16),
                Call::Drop(17),
                Call::Drop(19),
                Call::Drop(21),
                Call::Drop(22),
                Call::Drop(38),
                Call::Drop(39),
                Call::Read
            ]
        );
        // Already-absent bits need no privilege to drop, but are still verified.
        fake.calls.clear();
        assert!(apply(true, &mut fake).unwrap());
        assert_eq!(
            fake.calls,
            [Call::Read, Call::Ambient, Call::Inheritable, Call::Read]
        );
    }

    #[test]
    fn every_syscall_failure_aborts_and_every_postcondition_is_checked() {
        for index in 1..=11 {
            let mut fake = Fake::new();
            fake.fail_at = Some(index);
            assert!(
                apply(true, &mut fake).is_err(),
                "failure {index} must stop startup"
            );
            assert_eq!(fake.calls.len(), index, "no work after failure {index}");
        }
        for fault in [
            "ambient",
            "inheritable",
            "effective",
            "permitted",
            "retained",
            "supported",
            "bounding",
        ] {
            let mut fake = Fake::new();
            fake.fault = Some(fault);
            assert!(
                apply(true, &mut fake).is_err(),
                "undetected {fault} postcondition"
            );
        }
        let mut fake = Fake::new();
        fake.value.supported &= !(1 << 39);
        assert_eq!(
            apply(true, &mut fake).unwrap_err().kind(),
            io::ErrorKind::Unsupported
        );
        assert_eq!(
            fake.calls,
            [Call::Read],
            "unsupported kernel is refused before mutation"
        );
    }

    #[test]
    fn unsupported_platform_is_disabled_or_refuses_every_kernel_operation() {
        let mut host = unsupported::Host;
        assert!(!apply(false, &mut host).unwrap());
        for result in [
            host.snapshot().map(|_| ()),
            host.clear_ambient(),
            host.clear_inheritable(Fake::new().value),
            host.drop_bounding(21),
            apply(true, &mut host).map(|_| ()),
        ] {
            assert_eq!(result.unwrap_err().kind(), io::ErrorKind::Unsupported);
        }
    }

    /// AGENTD-26: irreversible changes belong only to an isolated subprocess.
    #[cfg(target_os = "linux")]
    #[test]
    fn real_reduction_clears_nonempty_i_a_and_reaches_future_threads_and_execs() {
        const CHILD: &str = "AGENTD_CAPABILITY_TEST_CHILD";
        const TEST: &str = "exec_capabilities::tests::real_reduction_clears_nonempty_i_a_and_reaches_future_threads_and_execs";
        match std::env::var(CHILD).as_deref() {
            Ok("probe") => {
                let value = Host.snapshot().unwrap();
                assert_eq!(value.bounding & DROP_MASK, 0);
                assert_eq!(value.permitted & DROP_MASK, 0);
                assert_eq!(value.effective & DROP_MASK, 0);
                assert_eq!(value.inheritable, 0);
                assert_eq!(value.ambient, 0);
                // An exec that already lost a bit cannot put it back into I/A.
                let mut forged = rustix::thread::capabilities(None).unwrap();
                forged.inheritable |= rustix::thread::CapabilitySet::SYS_ADMIN;
                assert!(rustix::thread::set_capabilities(None, forged).is_err());
                assert!(
                    rustix::thread::configure_capability_in_ambient_set(
                        rustix::thread::CapabilitySet::SYS_ADMIN,
                        true
                    )
                    .is_err()
                );
            }
            Ok("apply") => {
                use rustix::thread::{self, CapabilitySet};
                // Include a nonzero bit so a broken ambient-bit shift cannot
                // masquerade as a correct snapshot of bit zero alone.
                let inherited = CapabilitySet::CHOWN | CapabilitySet::SETUID;
                let mut seeded = thread::capabilities(None).unwrap();
                seeded.inheritable = inherited;
                thread::set_capabilities(None, seeded).expect("seed inheritable capabilities");
                for cap in [CapabilitySet::CHOWN, CapabilitySet::SETUID] {
                    thread::configure_capability_in_ambient_set(cap, true)
                        .expect("seed ambient capability");
                }
                let before = Host.snapshot().unwrap();
                assert_eq!(before.inheritable, inherited.bits());
                assert_eq!(before.ambient, inherited.bits());
                // Exercise this primitive before I is cleared, because Linux
                // also clears corresponding ambient bits when clearing I.
                Host.clear_ambient().unwrap();
                assert_eq!(
                    Host.snapshot().unwrap(),
                    Snapshot {
                        ambient: 0,
                        ..before
                    }
                );
                for cap in [CapabilitySet::CHOWN, CapabilitySet::SETUID] {
                    thread::configure_capability_in_ambient_set(cap, true)
                        .expect("restore ambient capability");
                }
                assert_eq!(Host.snapshot().unwrap(), before);
                assert!(!apply(false, &mut Host).unwrap());
                assert_eq!(Host.snapshot().unwrap(), before);
                assert!(apply(true, &mut Host).unwrap());
                let after = Host.snapshot().unwrap();
                assert_eq!(after.effective, before.effective);
                assert_eq!(after.permitted, before.permitted);
                assert_eq!(after.bounding, before.bounding & !DROP_MASK);
                assert_eq!(after.inheritable, 0);
                assert_eq!(after.ambient, 0);
                assert_eq!(
                    std::thread::spawn(|| Host.snapshot().unwrap())
                        .join()
                        .unwrap(),
                    after
                );
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .build()
                    .unwrap();
                assert_eq!(
                    runtime.block_on(async {
                        tokio::task::spawn_blocking(|| Host.snapshot().unwrap())
                            .await
                            .unwrap()
                    }),
                    after
                );
                let output = std::process::Command::new(std::env::current_exe().unwrap())
                    .args(["--exact", TEST, "--nocapture"])
                    .env(CHILD, "probe")
                    .output()
                    .unwrap();
                assert!(
                    output.status.success(),
                    "exec probe: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
            }
            _ => {
                // unshare executes the test binary only after creating a private
                // user namespace. No capability mutation reaches the runner.
                let output = std::process::Command::new("unshare")
                    .args(["--user", "--map-root-user"])
                    .arg(std::env::current_exe().unwrap())
                    .args(["--exact", TEST, "--nocapture"])
                    .env(CHILD, "apply")
                    .output()
                    .expect("unshare is required for the isolated capability test");
                assert!(
                    output.status.success(),
                    "isolated capability test: {}\n{}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                );
            }
        }
    }
}

// Compile the same refusal implementation into tests on Linux, where the
// daemon's test and mutation suites run, as well as on unsupported targets.
#[cfg(any(not(target_os = "linux"), test))]
mod unsupported {
    use super::{Kernel, Snapshot, io};

    pub(super) struct Host;

    impl Kernel for Host {
        fn snapshot(&mut self) -> io::Result<Snapshot> {
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "exec capability policy requires Linux",
            ))
        }
        fn clear_ambient(&mut self) -> io::Result<()> {
            self.snapshot().map(|_| ())
        }
        fn clear_inheritable(&mut self, _: Snapshot) -> io::Result<()> {
            self.snapshot().map(|_| ())
        }
        fn drop_bounding(&mut self, _: u32) -> io::Result<()> {
            self.snapshot().map(|_| ())
        }
    }
}
