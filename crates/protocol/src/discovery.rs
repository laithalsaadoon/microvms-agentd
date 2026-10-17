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
