// SPDX-License-Identifier: Apache-2.0
//! The image-owned security setting is validated before startup side effects.

#[cfg(target_os = "linux")]
#[test]
fn malformed_policy_refuses_startup_before_tracing_or_listening() {
    use std::ffi::OsString;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    let mut values = vec![OsString::from(""), "yes".into(), "TRUE".into()];
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        values.push(OsString::from_vec(vec![0xff]));
    }
    for value in values {
        // Keep even a seeded startup bypass from exposing the daemon's listener.
        // The user namespace grants the child its private network namespace only.
        let mut child = Command::new("unshare")
            .args(["--user", "--map-root-user", "--net"])
            .arg(env!("CARGO_BIN_EXE_agentd"))
            .env(agentd::exec_capabilities::SETTING, value)
            // Config logs this if reached; a malformed security setting must
            // fail even before tracing and config initialization.
            .env("AGENTD_DEFAULT_USER", "startup-order-probe")
            .env("AGENTD_PORT", "0")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut timed_out = false;
        while child.try_wait().unwrap().is_none() {
            if Instant::now() >= deadline {
                timed_out = true;
                child.kill().unwrap();
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let output = child.wait_with_output().unwrap();
        assert!(!timed_out, "malformed setting must refuse startup");
        assert!(!output.status.success());
        assert!(output.stdout.is_empty(), "startup reached tracing/config");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(agentd::exec_capabilities::SETTING)
        );
    }
}
