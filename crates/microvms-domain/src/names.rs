// SPDX-License-Identifier: Apache-2.0
//! Named MicroVMs: the record that lets a later process find a VM by name and adopt it.
//!
//! The platform offers no lookup of its own. `RunMicrovm` takes no tags and tagging a
//! running MicroVM fails (docs/PLATFORM.md, "Tagging works on images and not on
//! MicroVMs"), so a name has to live beside the caller. A [`NameRecord`] carries
//! everything an adopt needs (id, endpoint, agent token, region), so a name replaces the
//! whole triple rather than just the id.
//!
//! This is the half with no I/O: the name rule, the record and its JSON form, and
//! [`resolve_record`] over a store's answer. The stores themselves (`NameStore` and the
//! CLI's `FileNameStore`) are in `microvms_core::names`.
//!
//! **A record holds a secret.** The agent token is a bearer credential for the VM, and
//! the optional identity seed can impersonate the launching host to it. Keep records in
//! private storage; neither value appears in `Debug` output or error messages.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::error::{Error, ErrorKind};
use crate::region::Region;

/// A VM name's shape: ASCII letters, digits, `-`, `_`, at most 128 bytes, and never a
/// MicroVM id prefix.
///
/// The charset is the image-name pattern (`[a-zA-Z0-9-_]+`). It makes the CLI's
/// identifier resolution total — an identifier starting with `microvm-` can only be an id,
/// because a legal name is refused that prefix, and an ARN cannot match because `:` is
/// outside the set — and it makes every name a safe file name.
pub fn validate_name(name: &str) -> Result<(), String> {
    if name.is_empty() {
        return Err("a VM name cannot be empty".to_string());
    }
    if name.len() > 128 {
        return Err(format!(
            "a VM name is at most 128 bytes; this one is {}",
            name.len()
        ));
    }
    // Both spellings: `microvm-` is the id prefix the real service answers (measured
    // 2026-08-28, first live run of names — the fakes' `mvm-` fixture shape let a
    // passthrough keyed on `mvm-` alone pass every scripted test and fail against AWS),
    // and `mvm-` stays refused because it is the fixture shape every scripted body uses.
    if name.starts_with("microvm-") || name.starts_with("mvm-") {
        return Err(format!(
            "{name:?} starts with a MicroVM id prefix — a name shaped like an id would make \
             `microvm suspend <identifier>` ambiguous about which VM it addresses"
        ));
    }
    if let Some(bad) = name
        .chars()
        .find(|c| !c.is_ascii_alphanumeric() && *c != '-' && *c != '_')
    {
        return Err(format!(
            "{bad:?} is not a legal VM-name character: names take ASCII letters, digits, `-` \
             and `_`, the image-name pattern"
        ));
    }
    Ok(())
}

/// One kept VM's name, and everything an adopt needs to address it.
///
/// `camelCase` on the wire: this is the CLI registry's on-disk format, read by later
/// versions, so fields are only ever added, and optional ones default when absent.
#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NameRecord {
    pub name: String,
    pub microvm_id: String,
    pub endpoint: String,
    /// The launch's agent token: a bearer credential for the VM.
    pub agent_token: String,
    pub region: String,
    /// Seconds since the epoch when the name was registered.
    pub at: u64,
    /// The launching host's identity secret, base64, when `run --identity` generated one.
    ///
    /// Persisted for the reason the agent token is: a later `tunnel --verify-identity`
    /// needs it. It raises what a stolen record can do from "call the VM" to "call the VM
    /// and impersonate the launching host to it", which is the same trust domain.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identity_host_seed: Option<String>,
    /// The VM's public key, base64 — the pin `--verify-identity` checks the far end against.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub identity_vm_public_key: Option<String>,
    /// The egress posture label of the launch this name was registered for, when the
    /// registering command launched the VM. `None` means unknown, never "open".
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub egress_posture: Option<String>,
}

impl NameRecord {
    /// A record for a VM this process can already address, registered `at` seconds after the
    /// Unix epoch.
    ///
    /// Refuses an illegal name or an empty id, endpoint, or token: a record missing any of
    /// them resolves to a VM nobody can adopt. The time is the caller's, since this crate
    /// doesn't read the clock (ARCH-6); `microvms_core::prelude::NameRecordExt::new` stamps
    /// the current time.
    pub fn new_at(
        name: impl Into<String>,
        microvm_id: impl Into<String>,
        endpoint: impl Into<String>,
        agent_token: impl Into<String>,
        region: impl Into<String>,
        at: u64,
    ) -> Result<Self, Error> {
        let record = Self {
            name: name.into(),
            microvm_id: microvm_id.into(),
            endpoint: endpoint.into(),
            agent_token: agent_token.into(),
            region: region.into(),
            at,
            identity_host_seed: None,
            identity_vm_public_key: None,
            egress_posture: None,
        };
        record.check()?;
        Ok(record)
    }

    /// The record as JSON, secrets included — the form to store privately.
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::to_value(self).unwrap_or(serde_json::Value::Null)
    }

    /// A record from its JSON form, checked the way [`NameRecord::new_at`] checks.
    pub fn from_json(value: serde_json::Value) -> Result<Self, Error> {
        // The serde message can quote the offending value, which may be the token itself.
        let record: Self = serde_json::from_value(value).map_err(|_| {
            Error::invalid_arg(
                "not a name record: it needs string name, microvmId, endpoint, agentToken, \
                 and region, and a numeric at",
            )
        })?;
        record.check()?;
        Ok(record)
    }

    /// The region the VM runs in. A record's region is one this client wrote, so a name it
    /// no longer lists is taken as written rather than refused.
    pub fn region(&self) -> Region {
        Region::unlisted(self.region.as_str())
    }

    fn check(&self) -> Result<(), Error> {
        validate_name(&self.name).map_err(Error::invalid_arg)?;
        for (field, value) in [
            ("microvmId", &self.microvm_id),
            ("endpoint", &self.endpoint),
            ("agentToken", &self.agent_token),
            ("region", &self.region),
        ] {
            if value.is_empty() {
                return Err(Error::invalid_arg(format!(
                    "name record {:?} has an empty {field}; a record must carry everything an \
                     adopt needs",
                    self.name
                )));
            }
        }
        Ok(())
    }
}

/// Redacts the agent token and the identity seed: this type reaches logs through errors
/// and test output, and both values are credentials.
impl std::fmt::Debug for NameRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NameRecord")
            .field("name", &self.name)
            .field("microvm_id", &self.microvm_id)
            .field("endpoint", &self.endpoint)
            .field("agent_token", &"<redacted>")
            .field("region", &self.region)
            .field("at", &self.at)
            .field(
                "identity_host_seed",
                &self.identity_host_seed.as_ref().map(|_| "<redacted>"),
            )
            .field("identity_vm_public_key", &self.identity_vm_public_key)
            .field("egress_posture", &self.egress_posture)
            .finish()
    }
}

/// The record for `name` out of what a store answered, refused when it is missing or
/// registered in another region.
///
/// `found` is the store's answer for `name`, and `store` describes the store for the error
/// message. The region check is the guard: an id from one region addresses nothing in another,
/// and adopting it would fail at the first control-plane call with a not-found that says
/// nothing about the name.
pub fn resolve_record(
    name: &str,
    found: Option<NameRecord>,
    store: &str,
    expected_region: Option<&Region>,
) -> Result<NameRecord, Error> {
    let record = found.ok_or_else(|| {
        Error::new(
            ErrorKind::Precondition,
            format!("no VM is named {name:?} in {store}; register one first, or adopt by id"),
        )
    })?;
    if let Some(expected) = expected_region
        && expected.as_str() != record.region
    {
        return Err(Error::invalid_arg(format!(
            "{name:?} was registered in {}, not {}",
            record.region,
            expected.as_str()
        )));
    }
    Ok(record)
}

/// The state directory the CLI uses: `$MICROVM_STATE_DIR`, else `~/.microvm/runs`.
pub fn default_state_root(env: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(dir) = env("MICROVM_STATE_DIR") {
        return PathBuf::from(dir);
    }
    let home = env("HOME").unwrap_or_else(|| ".".to_string());
    PathBuf::from(home).join(".microvm").join("runs")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(name: &str, id: &str) -> NameRecord {
        NameRecord::new_at(
            name,
            id,
            "https://vm.example",
            "secret-token",
            "us-east-1",
            1,
        )
        .expect("ok")
    }

    #[test]
    fn neither_debug_nor_a_rejection_prints_a_secret() {
        let mut canaried = record("ci", "microvm-a");
        canaried.agent_token = "tok-CANARY".into();
        canaried.identity_host_seed = Some("seed-CANARY".into());
        let debug = format!("{canaried:?}");
        assert!(!debug.contains("CANARY"), "{debug}");
        assert!(debug.contains("<redacted>"), "{debug}");
        let error = NameRecord::from_json(serde_json::json!({
            "name": "ci", "microvmId": "microvm-a", "endpoint": "e",
            "agentToken": "tok-CANARY", "region": "us-east-1", "at": "not-a-number"
        }))
        .expect_err("at must be numeric");
        assert!(!error.to_string().contains("CANARY"), "{error}");
    }

    #[test]
    fn records_refuse_illegal_names_and_missing_fields() {
        let new =
            |name: &str, token: &str| NameRecord::new_at(name, "id", "e", token, "us-east-1", 1);
        assert!(new("microvm-x", "t").is_err());
        assert!(new("has space", "t").is_err());
        let error = new("ci", "").expect_err("no token");
        assert!(error.to_string().contains("agentToken"), "{error}");
        let round = NameRecord::from_json(record("ci", "microvm-a").to_json()).expect("ok");
        assert_eq!(round, record("ci", "microvm-a"));
    }

    #[test]
    fn a_record_carries_the_time_it_was_given() {
        let record =
            NameRecord::new_at("ci", "id", "e", "t", "us-east-1", 1_789_000_000).expect("ok");
        assert_eq!(record.at, 1_789_000_000);
    }

    #[test]
    fn resolving_refuses_a_missing_name_and_a_foreign_region() {
        let missing = resolve_record("ci", None, "/state/names", None).expect_err("no such name");
        assert_eq!(missing.kind(), ErrorKind::Precondition);
        assert!(missing.to_string().contains("/state/names"), "{missing}");
        let found = || Some(record("ci", "microvm-a"));
        assert_eq!(
            resolve_record("ci", found(), "s", Some(&Region::UsEast1))
                .expect("same region")
                .microvm_id,
            "microvm-a"
        );
        let foreign =
            resolve_record("ci", found(), "s", Some(&Region::UsWest2)).expect_err("other region");
        assert_eq!(foreign.kind(), ErrorKind::InvalidArg);
        assert!(!foreign.to_string().contains("secret-token"), "{foreign}");
    }

    #[test]
    fn the_state_root_is_the_variable_or_the_home_default() {
        let pinned = |key: &str| (key == "MICROVM_STATE_DIR").then(|| "/state".to_string());
        assert_eq!(default_state_root(&pinned), PathBuf::from("/state"));
        let home = |key: &str| (key == "HOME").then(|| "/home/u".to_string());
        assert_eq!(
            default_state_root(&home),
            PathBuf::from("/home/u/.microvm/runs")
        );
    }
}
