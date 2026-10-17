// SPDX-License-Identifier: Apache-2.0
//! The shared script refuses missing or misordered input before a parity verdict.

use microvms_app::testing::ParityControlPlane;
use microvms_core::control::transport::{Call, Transport};

#[test]
fn the_parity_script_refuses_empty_input() {
    assert!(ParityControlPlane::from_json("[]").is_err());
}

#[tokio::test]
async fn the_parity_script_preserves_literal_responses_and_order() {
    let script = r#"[{"operation":"ResumeMicrovm","status":400,"body":"{\"message\":null}","error_type":"ValidationException"}]"#;
    let fixture = ParityControlPlane::from_json(script).unwrap();
    let reply = fixture
        .send(Call::post_empty("ResumeMicrovm", "/resume"))
        .await
        .unwrap();
    assert_eq!(reply.status, 400);
    assert_eq!(reply.body, br#"{"message":null}"#);
    assert_eq!(reply.error_type.as_deref(), Some("ValidationException"));
    assert_eq!(fixture.calls(), ["ResumeMicrovm"]);
    assert!(
        fixture
            .send(Call::post_empty("ResumeMicrovm", "/resume"))
            .await
            .is_err()
    );
    let fixture = ParityControlPlane::from_json(script).unwrap();
    assert!(
        fixture
            .send(Call::post_empty("SuspendMicrovm", "/suspend"))
            .await
            .is_err()
    );
}
