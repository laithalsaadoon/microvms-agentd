// SPDX-License-Identifier: Apache-2.0
//! An agent's headless command, run through a real POSIX shell.
//!
//! Here rather than beside `headless_command` in `microvms-app`, whose code can't start a
//! subprocess (ARCH-7): the property is about what `/bin/sh` does with the rendered text, so
//! the test needs a shell.
#![cfg(unix)]

use microvms_core::agents::profile;
use microvms_core::agents::{Agent, AgentPermissionMode, sh_single_quote};

#[test]
fn unrestricted_task_text_is_one_literal_shell_argument() {
    let task = "task ' with $(printf INJECTED) and `printf ALSO` ; $HOME";
    for agent in Agent::ALL {
        let command = profile::headless_command(
            agent,
            &sh_single_quote(task),
            AgentPermissionMode::Unrestricted,
        );
        let script = format!(
            "claude() {{ printf '%s\\n' \"$@\"; }}; codex() {{ printf '%s\\n' \"$@\"; }}; {command}"
        );
        let output = std::process::Command::new("/bin/sh")
            .args(["-c", &script])
            .output()
            .unwrap();
        assert!(output.status.success());
        let stdout = String::from_utf8(output.stdout).unwrap();
        assert_eq!(
            stdout.lines().filter(|line| *line == task).count(),
            1,
            "{stdout}"
        );
    }
}
