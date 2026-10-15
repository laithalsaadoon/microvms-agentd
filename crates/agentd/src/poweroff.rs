// SPDX-License-Identifier: Apache-2.0
//! Optional guest shutdown, performed by the daemon after observed group completion.
//!
//! Tests inject both privilege checks and the action. Constructing ordinary AppState
//! never installs a host shutdown action. Linux capabilities stay as configured: this
//! code neither raises a capability nor starts a privileged helper process.

use std::collections::BTreeMap;
use std::io;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

type Action = Arc<dyn Fn() -> io::Result<()> + Send + Sync>;
type GroupProbe = Arc<dyn Fn(u32) -> io::Result<bool> + Send + Sync>;

#[derive(Clone)]
pub struct Poweroff(Arc<Inner>);

struct Inner {
    check: Action,
    action: Action,
    group_empty: GroupProbe,
    requested: AtomicBool,
}

impl Poweroff {
    pub fn unavailable() -> Self {
        Self::new(
            Arc::new(|| {
                Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    "poweroff_on_exit is unavailable",
                ))
            }),
            Arc::new(|| {
                Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    "poweroff_on_exit is unavailable",
                ))
            }),
            Arc::new(|pgid| group_empty(Path::new("/proc"), pgid)),
        )
    }

    /// Install only in the daemon executable, never as a test-state default.
    pub fn host() -> Self {
        Self::new(
            Arc::new(|| {
                let caps = rustix::thread::capabilities(None)?;
                let required = rustix::thread::CapabilitySet::SYS_BOOT;
                if caps.effective.contains(required) && caps.permitted.contains(required) {
                    Ok(())
                } else {
                    Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "poweroff_on_exit requires daemon CAP_SYS_BOOT",
                    ))
                }
            }),
            Arc::new(|| {
                match nix::sys::reboot::reboot(nix::sys::reboot::RebootMode::RB_POWER_OFF) {
                    Ok(never) => match never {},
                    Err(error) => Err(error.into()),
                }
            }),
            Arc::new(|pgid| group_empty(Path::new("/proc"), pgid)),
        )
    }

    fn new(check: Action, action: Action, group_empty: GroupProbe) -> Self {
        Self(Arc::new(Inner {
            check,
            action,
            group_empty,
            requested: AtomicBool::new(false),
        }))
    }

    pub fn check(&self) -> io::Result<()> {
        (self.0.check)()
    }

    /// AGENTD-25: observe completion before requesting shutdown, without signaling.
    pub async fn after_group_exit(&self, pgid: u32) -> io::Result<()> {
        loop {
            let probe = self.0.group_empty.clone();
            let empty = tokio::task::spawn_blocking(move || probe(pgid))
                .await
                .map_err(io::Error::other)??;
            if empty {
                // Recheck at the action boundary: accepting an exec does not freeze
                // the daemon's credentials. No capability is restored here.
                self.check()?;
                if !self.0.requested.swap(true, Ordering::AcqRel)
                    && let Err(error) = (self.0.action)()
                {
                    self.0.requested.store(false, Ordering::Release);
                    return Err(error);
                }
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    #[cfg(test)]
    pub(crate) fn testing(check: Action, action: Action, group_empty: GroupProbe) -> Self {
        Self::new(check, action, group_empty)
    }
}

#[derive(Debug, Eq, PartialEq)]
struct Process {
    start_time: u64,
    live_group: Option<u32>,
}

fn process(stat: &str) -> io::Result<Process> {
    let malformed = || io::Error::new(io::ErrorKind::InvalidData, "incomplete process status");
    let tail = stat.rsplit_once(')').ok_or_else(malformed)?.1;
    let fields: Vec<_> = tail.split_whitespace().collect();
    let state = *fields.first().ok_or_else(malformed)?;
    // live_pgrp also returns None for malformed records. Validate its inputs
    // with the same ranges so an invalid record cannot count as an exited member.
    fields
        .get(2)
        .ok_or_else(malformed)?
        .parse::<u32>()
        .map_err(|_| malformed())?;
    if !matches!(
        state,
        "R" | "S" | "D" | "Z" | "T" | "t" | "X" | "x" | "K" | "W" | "P" | "I"
    ) {
        return Err(malformed());
    }
    let threads: u32 = fields
        .get(17)
        .ok_or_else(malformed)?
        .parse()
        .map_err(|_| malformed())?;
    if threads == 0 {
        return Err(malformed());
    }
    let start_time = fields
        .get(19)
        .ok_or_else(malformed)?
        .parse()
        .map_err(|_| malformed())?;
    Ok(Process {
        start_time,
        live_group: crate::exec::live_pgrp(stat),
    })
}

fn snapshot(root: &Path) -> io::Result<Option<BTreeMap<u32, Process>>> {
    let mut processes = BTreeMap::new();
    for entry in std::fs::read_dir(root)? {
        let entry = entry?;
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        let stat = match std::fs::read_to_string(entry.path().join("stat")) {
            Ok(stat) => stat,
            // A process can fork a successor after enumeration and disappear
            // before this read. Absence is inconclusive, never proof of exit.
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        processes.insert(pid, process(&stat)?);
    }
    Ok(Some(processes))
}

fn snapshots_empty(
    first: Option<BTreeMap<u32, Process>>,
    second: Option<BTreeMap<u32, Process>>,
    pgid: u32,
) -> bool {
    match (first, second) {
        (Some(first), Some(second)) => {
            first == second && first.values().all(|p| p.live_group != Some(pgid))
        }
        _ => false,
    }
}

pub(crate) fn group_empty(root: &Path, pgid: u32) -> io::Result<bool> {
    // Start times prevent PID-name reuse from disguising churn. A numeric PGID
    // reused after the original group ends can only postpone this observation;
    // this monitor never signals it. Future joins cannot be prohibited by /proc.
    Ok(snapshots_empty(snapshot(root)?, snapshot(root)?, pgid))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    fn stat(state: &str, group: u32, start: u64) -> String {
        stat_threads(state, group, start, 1)
    }

    fn stat_threads(state: &str, group: u32, start: u64, threads: u64) -> String {
        let mut fields = vec!["0".to_string(); 20];
        fields[0] = state.into();
        fields[1] = "1".into();
        fields[2] = group.to_string();
        fields[17] = threads.to_string();
        fields[19] = start.to_string();
        format!("7 (odd ) name) {}", fields.join(" "))
    }

    /// AGENTD-25: a permitted capability alone does not authorize shutdown.
    #[cfg(target_os = "linux")]
    #[test]
    fn host_requires_sys_boot_to_be_effective_as_well_as_permitted() {
        const CHILD: &str = "AGENTD_POWEROFF_CAPABILITY_TEST_CHILD";
        const TEST: &str =
            "poweroff::tests::host_requires_sys_boot_to_be_effective_as_well_as_permitted";
        if std::env::var_os(CHILD).is_none() {
            // capset changes only this isolated child, never the test runner.
            let output = std::process::Command::new("unshare")
                .args(["--user", "--map-root-user"])
                .arg(std::env::current_exe().unwrap())
                .args(["--exact", TEST, "--nocapture"])
                .env(CHILD, "1")
                .output()
                .expect("unshare is required for the isolated capability test");
            assert!(
                output.status.success(),
                "isolated capability test: {}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }

        use rustix::thread::{self, CapabilitySet};
        let required = CapabilitySet::SYS_BOOT;
        let mut caps = thread::capabilities(None).unwrap();
        assert!(caps.effective.contains(required));
        assert!(caps.permitted.contains(required));
        assert!(Poweroff::host().check().is_ok());
        caps.effective.remove(required);
        thread::set_capabilities(None, caps).unwrap();
        let after = thread::capabilities(None).unwrap();
        assert!(!after.effective.contains(required));
        assert!(after.permitted.contains(required));
        // Read the host's real check, but never invoke its shutdown action.
        assert_eq!(
            Poweroff::host().check().unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn a_dead_leader_with_other_threads_does_not_prove_group_completion() {
        for state in ["Z", "X"] {
            assert_eq!(
                process(&stat_threads(state, 7, 4, 2)).unwrap().live_group,
                Some(7)
            );
            assert_eq!(
                process(&stat_threads(state, 7, 4, 1)).unwrap().live_group,
                None
            );
        }
        assert!(process(&stat_threads("Z", 7, 4, 0)).is_err());
        assert!(process(&stat_threads("?", 7, 4, 1)).is_err());
        assert!(process(&stat_threads("Z", 7, 4, 1).replacen(" Z 1 7 ", " Z 1 bad ", 1)).is_err());
    }

    /// AGENTD-25: an out-of-range thread count cannot establish group completion.
    #[test]
    fn oversized_thread_counts_cannot_prove_group_completion() {
        for state in ["S", "Z", "X"] {
            assert!(process(&stat_threads(state, 7, 4, u64::from(u32::MAX) + 1)).is_err());
            assert_eq!(
                process(&stat_threads(state, 7, 4, u64::from(u32::MAX)))
                    .unwrap()
                    .live_group,
                Some(7)
            );
        }
    }

    #[test]
    fn group_observation_excludes_zombies_but_refuses_missing_and_malformed_records() {
        assert_eq!(process(&stat("Z", 7, 4)).unwrap().live_group, None);
        assert_eq!(process(&stat("S", 7, 4)).unwrap().live_group, Some(7));
        assert!(process("7 (broken) S 1").is_err());
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("7")).unwrap();
        assert!(
            !group_empty(dir.path(), 7).unwrap(),
            "vanished stat is inconclusive"
        );
        std::fs::write(dir.path().join("7/stat"), stat("Z", 7, 4)).unwrap();
        assert!(group_empty(dir.path(), 7).unwrap());
        std::fs::write(dir.path().join("7/stat"), "malformed").unwrap();
        assert!(group_empty(dir.path(), 7).is_err());
        // A directory produces a read error even when the tests run as root.
        std::fs::remove_file(dir.path().join("7/stat")).unwrap();
        std::fs::create_dir(dir.path().join("7/stat")).unwrap();
        assert_ne!(
            group_empty(dir.path(), 7).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn fork_exit_churn_and_pid_reuse_cannot_prove_group_completion() {
        let before = BTreeMap::from([(7, process(&stat("Z", 7, 4)).unwrap())]);
        let forked = BTreeMap::from([(8, process(&stat("S", 7, 5)).unwrap())]);
        assert!(!snapshots_empty(Some(before), Some(forked), 7));
        let before = BTreeMap::from([(7, process(&stat("Z", 7, 4)).unwrap())]);
        let reused = BTreeMap::from([(7, process(&stat("Z", 9, 6)).unwrap())]);
        assert!(!snapshots_empty(Some(before), Some(reused), 7));
        assert!(!snapshots_empty(None, Some(BTreeMap::new()), 7));
    }

    #[tokio::test]
    async fn poweroff_waits_for_group_completion_and_runs_once() {
        let probes = Arc::new(AtomicUsize::new(0));
        let count = Arc::new(AtomicUsize::new(0));
        let seen = count.clone();
        let checked = probes.clone();
        let poweroff = Poweroff::testing(
            Arc::new(|| Ok(())),
            Arc::new(move || {
                seen.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }),
            Arc::new(move |_| Ok(checked.fetch_add(1, Ordering::SeqCst) > 0)),
        );
        poweroff.after_group_exit(7).await.unwrap();
        poweroff.after_group_exit(7).await.unwrap();
        assert!(probes.load(Ordering::SeqCst) >= 2);
        assert_eq!(count.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn observation_and_privilege_failures_never_request_poweroff() {
        for privilege in [true, false] {
            let count = Arc::new(AtomicUsize::new(0));
            let seen = count.clone();
            let poweroff = Poweroff::testing(
                Arc::new(move || {
                    if privilege {
                        Ok(())
                    } else {
                        Err(io::Error::other("denied"))
                    }
                }),
                Arc::new(move || {
                    seen.fetch_add(1, Ordering::SeqCst);
                    Ok(())
                }),
                Arc::new(move |_| {
                    if privilege {
                        Err(io::Error::other("unreadable"))
                    } else {
                        Ok(true)
                    }
                }),
            );
            assert!(poweroff.after_group_exit(7).await.is_err());
            assert_eq!(count.load(Ordering::SeqCst), 0);
        }
    }

    #[tokio::test]
    async fn a_failed_shutdown_is_reported_as_failure() {
        let poweroff = Poweroff::testing(
            Arc::new(|| Ok(())),
            Arc::new(|| Err(io::Error::other("reboot refused"))),
            Arc::new(|_| Ok(true)),
        );
        assert!(
            poweroff
                .after_group_exit(7)
                .await
                .unwrap_err()
                .to_string()
                .contains("reboot refused")
        );
    }
}
