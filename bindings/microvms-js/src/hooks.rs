// SPDX-License-Identifier: Apache-2.0
//! The two hook-timeout families, kept as two classes (BIND-2).
//!
//! # Why two classes and not two numbers
//!
//! The ceilings are 60x apart: the `run`/`resume`/`suspend`/`terminate` family caps at 60
//! seconds and the `ready`/`validate` image family at 3600. In Rust the core makes that S1 —
//! two types with no conversion in either direction, so a 3600-second build timeout cannot
//! reach a field that caps at 60.
//!
//! Two `number` fields would give that away, and worse than in Python: they can be
//! *transposed*, which is the specific mistake the two types exist to prevent and the one a
//! numeric parameter cannot see. So both are `#[napi]` classes. napi v3 generates real
//! TypeScript classes, and at runtime napi's argument conversion rejects a non-instance before
//! any Rust runs, the structurally identical `{ seconds: 30 }` included, which is what a
//! `#[napi(object)]` would have accepted.
//!
//! `tsc` compares classes by structure, not by name, and the two classes have the same
//! members. So each declares its ceiling as a literal type, `maxSecs: 60` and `maxSecs: 3600`,
//! the one member whose type differs, and that's what makes `tsc` reject one where the other is
//! wanted. With `number` there it accepted either for both (#337). `__test__/types/hooks.ts`
//! holds that claim, and the assertion below holds each literal to core's constant.
//!
//! The range check inside each constructor is the core's `try_new`, message and all: each
//! refusal names **both** ceilings, because the caller who hits it is nearly always someone
//! who picked a number from the other family.

use microvms_core::{BuildHookTimeout as CoreBuild, RunHookTimeout as CoreRun};
use napi_derive::napi;

use crate::errors::js;

// The `maxSecs` literal types below are core's two ceilings, written out because an attribute
// can't name a constant. A ceiling core moves fails this build rather than leaving the
// declarations promising the old one.
const _: () = assert!(
    CoreRun::MAX_SECS == 60 && CoreBuild::MAX_SECS == 3600,
    "a hook ceiling moved: update the `maxSecs` ts_return_type literals in hooks.rs"
);

/// A timeout for the `run`, `resume`, `suspend`, or `terminate` hook: 1..=60 seconds.
///
/// A distinct class from [`BuildHookTimeout`] and deliberately not interchangeable with it.
#[napi]
#[derive(Clone, Copy)]
pub struct RunHookTimeout {
    pub(crate) inner: CoreRun,
}

#[napi]
impl RunHookTimeout {
    /// A run-family timeout, or a refusal naming **both** ceilings.
    #[napi(constructor)]
    pub fn new(seconds: f64) -> napi::Result<RunHookTimeout, String> {
        Ok(RunHookTimeout {
            inner: CoreRun::try_new(crate::numbers::u32_number(seconds, "seconds").map_err(js)?)
                .map_err(js)?,
        })
    }

    /// The service ceiling for this family: 60.
    #[napi(getter, ts_return_type = "60")]
    pub fn max_secs(&self) -> u32 {
        CoreRun::MAX_SECS
    }

    #[napi(getter)]
    pub fn seconds(&self) -> u32 {
        self.inner.as_secs()
    }

    #[napi(js_name = "toString")]
    pub fn display_string(&self) -> String {
        self.inner.to_string()
    }
}

/// A timeout for the `ready` or `validate` image-build hook: 1..=3600 seconds.
#[napi]
#[derive(Clone, Copy)]
pub struct BuildHookTimeout {
    pub(crate) inner: CoreBuild,
}

#[napi]
impl BuildHookTimeout {
    /// A build-family timeout, or a refusal naming both ceilings.
    #[napi(constructor)]
    pub fn new(seconds: f64) -> napi::Result<BuildHookTimeout, String> {
        Ok(BuildHookTimeout {
            inner: CoreBuild::try_new(crate::numbers::u32_number(seconds, "seconds").map_err(js)?)
                .map_err(js)?,
        })
    }

    /// The service ceiling for this family: 3600.
    #[napi(getter, ts_return_type = "3600")]
    pub fn max_secs(&self) -> u32 {
        CoreBuild::MAX_SECS
    }

    #[napi(getter)]
    pub fn seconds(&self) -> u32 {
        self.inner.as_secs()
    }

    #[napi(js_name = "toString")]
    pub fn display_string(&self) -> String {
        self.inner.to_string()
    }
}
