// SPDX-License-Identifier: Apache-2.0
//! The published port imports and image defaults agree with the daemon contract.

use microvms_core::{control, defaults, protocol, session};

#[test]
fn default_daemon_port_keeps_its_value_and_compatibility_paths() {
    let expected = protocol::DEFAULT_AGENT_PORT;
    assert_eq!(expected, 9000, "the published default is stable");
    assert_eq!(control::DEFAULT_AGENT_PORT, expected);
    assert_eq!(session::DEFAULT_AGENT_PORT, expected);
    assert_eq!(session::proxy::DEFAULT_AGENT_PORT, expected);
    assert_eq!(control::WrapOptions::default().port, expected);
    assert_eq!(defaults::client_defaults()["agentPort"], expected);
}
