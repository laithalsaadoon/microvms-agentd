// SPDX-License-Identifier: Apache-2.0
//! Additive discovery, separate from the published exhaustive health response.

use schemars::{JsonSchema, Schema, SchemaGenerator};
use serde::{Deserialize, Serialize};

/// `GET /v1/discovery`: authenticated image configuration, not resolved identity.
///
/// A missing route means an older daemon; a present response with `default_user:
/// null` means no image default. Resolution still happens for each exec request.
#[derive(Clone, Debug, Default, Deserialize, Eq, JsonSchema, PartialEq, Serialize)]
#[non_exhaustive]
pub struct Discovery {
    /// The image's `AGENTD_DEFAULT_USER`, or `None` for the daemon/root default.
    // A custom reader makes absence an error while retaining explicit null. The
    // schema function likewise makes the property required while keeping it nullable.
    #[serde(deserialize_with = "Option::<String>::deserialize")]
    #[schemars(schema_with = "optional_user_schema")]
    pub default_user: Option<String>,
}

impl Discovery {
    /// Reports the configured value without resolving or validating a passwd row.
    pub fn new(default_user: Option<String>) -> Self {
        Self { default_user }
    }
}

fn optional_user_schema(generator: &mut SchemaGenerator) -> Schema {
    generator.subschema_for::<Option<String>>()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn discovery_schema_requires_a_nullable_string_default() {
        let schema = serde_json::to_value(schemars::schema_for!(Discovery)).unwrap();
        assert_eq!(schema["required"], json!(["default_user"]));
        assert_eq!(
            schema["properties"]["default_user"]["type"],
            json!(["string", "null"])
        );
        assert_ne!(schema["additionalProperties"], false);

        // Future fields remain readable without weakening this field's value contract.
        let discovery: Discovery = serde_json::from_value(json!({
            "default_user": null,
            "future_field": true
        }))
        .unwrap();
        assert_eq!(discovery.default_user, None);
        for invalid in [json!(true), json!(42), json!([]), json!({})] {
            assert!(
                serde_json::from_value::<Discovery>(json!({ "default_user": invalid })).is_err()
            );
        }
    }
}
