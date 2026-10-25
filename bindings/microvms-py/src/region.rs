// SPDX-License-Identifier: Apache-2.0
//! An AWS region by name, and the regions AWS was measured running MicroVMs in (TRAP-6).
//!
//! # Why a class rather than a string parameter
//!
//! [`crate::sandbox::PySandbox::new`] takes a `Region` object, so a region name is parsed once,
//! at [`PyRegion::parse`] or a named constructor, and every call after it carries a checked
//! value. `parse` takes any well-formed region name since 1.3.0 (#743): AWS adds MicroVMs
//! regions faster than a release can follow, so the client reports a region instead of
//! refusing it. [`PyRegion::measured`] lists the 14 regions AWS was measured running MicroVMs
//! in, and `is_measured` says whether a region is one of them. `unlisted`, `supported` and
//! `is_supported` are deprecated since 1.3.0 and raise a `DeprecationWarning`; they keep
//! their 1.2 behavior until 2.0.0 at the earliest.
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

use microvms_core::Region;
use pyo3::exceptions::PyDeprecationWarning;
use pyo3::prelude::*;

use crate::errors::PyCoreResult;

/// An AWS region, by its name.
///
/// `Region.parse` takes any well-formed region name; the five named constructors cover the
/// regions of AWS's first MicroVMs launch.
#[pyclass(frozen, from_py_object, name = "Region", module = "microvms")]
#[derive(Clone)]
pub struct PyRegion {
    pub(crate) inner: Region,
}

#[pymethods]
impl PyRegion {
    #[staticmethod]
    fn us_east_1() -> PyRegion {
        PyRegion {
            inner: Region::US_EAST_1,
        }
    }

    #[staticmethod]
    fn us_east_2() -> PyRegion {
        PyRegion {
            inner: Region::US_EAST_2,
        }
    }

    #[staticmethod]
    fn us_west_2() -> PyRegion {
        PyRegion {
            inner: Region::US_WEST_2,
        }
    }

    #[staticmethod]
    fn eu_west_1() -> PyRegion {
        PyRegion {
            inner: Region::EU_WEST_1,
        }
    }

    #[staticmethod]
    fn ap_northeast_1() -> PyRegion {
        PyRegion {
            inner: Region::AP_NORTHEAST_1,
        }
    }

    /// Any well-formed region name, or `InvalidArgError` for a malformed one.
    ///
    /// The boundary a region name arrives at from an environment variable or a config
    /// file, where it is still a string. The check is the core's `FromStr`, message and
    /// all — this is a call, not a check written here.
    #[staticmethod]
    fn parse(name: &str) -> PyCoreResult<PyRegion> {
        Ok(PyRegion {
            inner: Region::from_str(name)?,
        })
    }

    /// Deprecated since 1.3.0: `Region.parse` takes any well-formed region name.
    ///
    /// Builds the region without the shape check, as 1.2 did, and raises a
    /// `DeprecationWarning`. `Region.unlisted("us-east-1") == Region.us_east_1()`.
    #[staticmethod]
    fn unlisted(py: Python<'_>, name: &str) -> PyResult<PyRegion> {
        PyErr::warn(
            py,
            &py.get_type::<PyDeprecationWarning>(),
            c"Region.unlisted is deprecated since 1.3.0: Region.parse takes any well-formed region name",
            1,
        )?;
        #[allow(deprecated)]
        let inner = Region::unlisted(name);
        Ok(PyRegion { inner })
    }

    /// The regions AWS was measured running MicroVMs in: the 14 the Pricing API priced on
    /// 2026-10-24.
    #[staticmethod]
    fn measured() -> Vec<PyRegion> {
        microvms_core::region::MEASURED_REGIONS
            .into_iter()
            .map(|name| PyRegion {
                inner: Region::from_str(name).expect("a measured name is well formed"),
            })
            .collect()
    }

    /// Deprecated since 1.3.0: AWS runs MicroVMs in more regions than these five; use
    /// `Region.measured()`.
    ///
    /// The five named regions, as 1.2 returned, and a `DeprecationWarning`.
    #[staticmethod]
    fn supported(py: Python<'_>) -> PyResult<Vec<PyRegion>> {
        PyErr::warn(
            py,
            &py.get_type::<PyDeprecationWarning>(),
            c"Region.supported is deprecated since 1.3.0: Region.measured lists the regions AWS runs MicroVMs in",
            1,
        )?;
        #[allow(deprecated)]
        let named = microvms_core::region::MICROVM_REGIONS;
        Ok(named.into_iter().map(|inner| PyRegion { inner }).collect())
    }

    /// The wire spelling, which is also the endpoint's middle segment.
    #[getter]
    fn name(&self) -> String {
        self.inner.as_str().to_string()
    }

    /// Whether AWS was measured running MicroVMs here: membership in `Region.measured()`.
    #[getter]
    fn is_measured(&self) -> bool {
        self.inner.is_measured()
    }

    /// Deprecated since 1.3.0: use `is_measured`.
    ///
    /// Whether this is one of the five named regions, as in 1.2, and a `DeprecationWarning`.
    #[getter]
    fn is_supported(&self, py: Python<'_>) -> PyResult<bool> {
        PyErr::warn(
            py,
            &py.get_type::<PyDeprecationWarning>(),
            c"Region.is_supported is deprecated since 1.3.0: is_measured covers the regions AWS runs MicroVMs in",
            1,
        )?;
        #[allow(deprecated)]
        let named = self.inner.is_supported();
        Ok(named)
    }

    fn __str__(&self) -> String {
        self.inner.as_str().to_string()
    }

    fn __repr__(&self) -> String {
        format!(
            "Region({:?}, is_measured={})",
            self.inner.as_str(),
            self.inner.is_measured()
        )
    }

    fn __eq__(&self, other: &PyRegion) -> bool {
        self.inner == other.inner
    }

    fn __hash__(&self) -> u64 {
        use std::hash::{DefaultHasher, Hash as _, Hasher as _};
        let mut hasher = DefaultHasher::new();
        self.inner.hash(&mut hasher);
        hasher.finish()
    }
}
