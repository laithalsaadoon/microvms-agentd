// SPDX-License-Identifier: Apache-2.0
//! Listener defaults share one owner while retaining the existing serving import.

use microvms_core::{defaults, session};

#[test]
fn listener_defaults_and_compatibility_paths_follow_the_shared_owner() {
    let expected = microvms_app::session::DEFAULT_BIND;
    assert!(expected.ip().is_loopback());
    assert_eq!(expected.port(), 0);
    assert_eq!(session::DEFAULT_BIND, expected);
    assert_eq!(session::serve::DEFAULT_BIND, expected);
    assert_eq!(session::serve::bind_address(None).unwrap(), expected);

    let manifest = defaults::client_defaults();
    assert_eq!(manifest["listener"]["bindAddress"], expected.to_string());
    assert_eq!(manifest["listener"]["bindIp"], expected.ip().to_string());

    let explicit = "192.0.2.1:12345";
    assert_eq!(
        session::serve::bind_address(Some(explicit)).unwrap(),
        explicit.parse::<std::net::SocketAddr>().unwrap()
    );
}
