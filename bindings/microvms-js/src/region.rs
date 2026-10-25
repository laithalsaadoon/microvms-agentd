// SPDX-License-Identifier: Apache-2.0
//! An AWS region by name, and the regions AWS was measured running MicroVMs in (TRAP-6).
//!
//! # Why a class rather than a string parameter
//!
//! The sandbox takes a `Region` object, so a region name is parsed once, at [`Region::parse`]
//! or a named factory, and every call after it carries a checked value. There is no
//! `#[napi(constructor)]`, so `new Region("eu-central-1")` throws rather than producing an
//! unchecked region. `parse` takes any well-formed region name since 1.3.0 (#743): AWS adds
//! MicroVMs regions faster than a release can follow, so the client reports a region instead
//! of refusing it. [`Region::measured`] lists the 14 regions AWS was measured running
//! MicroVMs in, and `isMeasured` says whether a region is one of them. `unlisted`,
//! `supported` and `isSupported` are deprecated since 1.3.0 and keep their 1.2 behavior until
//! 2.0.0 at the earliest.
//!
//! # What the measured list is for
//!
//! Measured 2026-08-07 and again 2026-10-24 (in `us-west-1`): a region that does not run
//! MicroVMs can answer `AccessDeniedException` with the message field **null**, which an IAM
//! denial of an implied action also produces. Nothing before that call objects, because the
//! service model's `endpointPrefix` is `lambda` and an endpoint resolves for any region. When
//! that response arrives in a region outside the list, the error names the region's service
//! availability as a cause to verify alongside the IAM policy.

use std::str::FromStr;

use microvms_core::Region as CoreRegion;
use napi_derive::napi;

use crate::errors::js;

/// An AWS region, by its name.
///
/// `Region.parse` takes any well-formed region name; the five named factories cover the
/// regions of AWS's first MicroVMs launch.
#[napi]
#[derive(Clone)]
pub struct Region {
    pub(crate) inner: CoreRegion,
}

#[napi]
impl Region {
    #[napi(factory)]
    pub fn us_east_1() -> Region {
        Region {
            inner: CoreRegion::US_EAST_1,
        }
    }

    #[napi(factory)]
    pub fn us_east_2() -> Region {
        Region {
            inner: CoreRegion::US_EAST_2,
        }
    }

    #[napi(factory)]
    pub fn us_west_2() -> Region {
        Region {
            inner: CoreRegion::US_WEST_2,
        }
    }

    #[napi(factory)]
    pub fn eu_west_1() -> Region {
        Region {
            inner: CoreRegion::EU_WEST_1,
        }
    }

    #[napi(factory)]
    pub fn ap_northeast_1() -> Region {
        Region {
            inner: CoreRegion::AP_NORTHEAST_1,
        }
    }

    /// Any well-formed region name, or an `ERR_INVALID_ARG` error for a malformed one.
    ///
    /// The boundary a region name arrives at from an environment variable or a config file,
    /// where it is still a string. The check is the core's `FromStr`, message and all —
    /// this is a call, not a check written here.
    #[napi(factory)]
    pub fn parse(name: String) -> napi::Result<Region, String> {
        Ok(Region {
            inner: CoreRegion::from_str(&name).map_err(js)?,
        })
    }

    /// Builds the region without the shape check, as 1.2 did.
    /// `Region.unlisted("us-east-1")` equals `Region.usEast1()`.
    ///
    /// @deprecated Since 1.3.0: `Region.parse` takes any well-formed region name.
    #[napi(factory)]
    pub fn unlisted(name: String) -> Region {
        #[allow(deprecated)]
        let inner = CoreRegion::unlisted(name);
        Region { inner }
    }

    /// The regions AWS was measured running MicroVMs in: the 14 the Pricing API priced on
    /// 2026-10-24.
    #[napi]
    pub fn measured() -> Vec<Region> {
        microvms_core::region::MEASURED_REGIONS
            .into_iter()
            .map(|name| Region {
                inner: CoreRegion::from_str(name).expect("a measured name is well formed"),
            })
            .collect()
    }

    /// The five named regions, as 1.2 returned.
    ///
    /// @deprecated Since 1.3.0: AWS runs MicroVMs in more regions than these five; use
    /// `Region.measured()`.
    #[napi]
    pub fn supported() -> Vec<Region> {
        #[allow(deprecated)]
        let named = microvms_core::region::MICROVM_REGIONS;
        named.into_iter().map(|inner| Region { inner }).collect()
    }

    /// The wire spelling, which is also the endpoint's middle segment.
    #[napi(getter)]
    pub fn name(&self) -> String {
        self.inner.as_str().to_string()
    }

    /// Whether AWS was measured running MicroVMs here: membership in `Region.measured()`.
    #[napi(getter)]
    pub fn is_measured(&self) -> bool {
        self.inner.is_measured()
    }

    /// Whether this is one of the five named regions, as in 1.2.
    ///
    /// @deprecated Since 1.3.0: use `isMeasured`.
    #[napi(getter)]
    #[allow(deprecated)]
    pub fn is_supported(&self) -> bool {
        self.inner.is_supported()
    }

    #[napi(js_name = "toString")]
    pub fn display_string(&self) -> String {
        self.inner.as_str().to_string()
    }

    /// Whether two regions are the same one.
    ///
    /// A method and not `===`: JS compares class instances by reference, so two
    /// `Region.usEast1()` calls are different objects. Named `equals` because that is what a
    /// JS reader expects to reach for.
    #[napi]
    pub fn equals(&self, other: &Region) -> bool {
        self.inner == other.inner
    }
}
