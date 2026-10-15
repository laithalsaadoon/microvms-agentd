// SPDX-License-Identifier: Apache-2.0
//! The executable installs the host power-off check before serving health.

/// AGENTD-25: the executable serves real health with host power-off support.
#[cfg(target_os = "linux")]
#[test]
fn daemon_startup_serves_health_with_host_poweroff_support() {
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::process::{Child, Command, Stdio};
    use std::time::{Duration, Instant};

    const CHILD: &str = "AGENTD_POWEROFF_STARTUP_TEST_CHILD";
    const TEST: &str = "daemon_startup_serves_health_with_host_poweroff_support";
    if std::env::var_os(CHILD).is_none() {
        // The real executable binds all interfaces. Keep that listener inside
        // a private network namespace, with capabilities private to this user namespace.
        let output = Command::new("unshare")
            .args(["--user", "--map-root-user", "--net"])
            .arg(std::env::current_exe().unwrap())
            .args(["--exact", TEST, "--nocapture"])
            .env(CHILD, "1")
            .output()
            .expect("unshare is required for the isolated startup test");
        assert!(
            output.status.success(),
            "isolated startup test: {}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }

    let loopback = Command::new("ip")
        .args(["link", "set", "lo", "up"])
        .output()
        .expect("iproute2 is required to bring up the private loopback interface");
    assert!(
        loopback.status.success(),
        "private loopback: {}",
        String::from_utf8_lossy(&loopback.stderr)
    );

    struct Stop(Child);
    impl Drop for Stop {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let mut daemon = Stop(
        Command::new(env!("CARGO_BIN_EXE_agentd"))
            .env_clear()
            .env("AGENTD_PORT", "9000")
            .env("AGENTD_REPAIR_IDENTITY", "false")
            .env("AGENTD_DEFAULT_USER", "worker")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut stream = loop {
        assert!(
            daemon.0.try_wait().unwrap().is_none(),
            "daemon exited before health"
        );
        if let Ok(stream) = TcpStream::connect_timeout(
            &"127.0.0.1:9000".parse().unwrap(),
            Duration::from_millis(100),
        ) {
            break stream;
        }
        assert!(Instant::now() < deadline, "daemon never served health");
        std::thread::sleep(Duration::from_millis(10));
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    stream
        .write_all(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    let (headers, body) = response.split_once("\r\n\r\n").unwrap();
    assert!(headers.starts_with("HTTP/1.1 200 "), "{headers}");
    for name in [
        protocol::health::POWEROFF_ON_EXIT_HEADER,
        protocol::health::DEFAULT_USER_CONFIGURED_HEADER,
    ] {
        assert!(
            headers.lines().any(|line| line == format!("{name}: 1")),
            "{headers}"
        );
    }
    let health: protocol::health::Health = serde_json::from_str(body).unwrap();
    assert_eq!(health.version, env!("CARGO_PKG_VERSION"));
    assert!(!health.bootstrapped);
    assert!(!health.busy);
    assert_eq!(health.execs, 0);
    assert!(daemon.0.try_wait().unwrap().is_none());
}
