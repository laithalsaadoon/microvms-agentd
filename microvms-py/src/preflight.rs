// SPDX-License-Identifier: Apache-2.0
//! `microvms.preflight`: core's preflight, as Python sees it (BIND-15, BIND-16).
//!
//! A pass-through: the checks, their order, their words, and the one free call are core's
//! (`microvms_core::preflight`), which the CLI's `doctor` shares.

use microvms_core::preflight::{Check, PreflightReport};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::region::PyRegion;
use crate::runtime;

/// One line of a preflight report.
#[pyclass(frozen, name = "PreflightCheck", module = "microvms")]
pub struct PyPreflightCheck {
    inner: Check,
}

#[pymethods]
impl PyPreflightCheck {
    /// `"region"`, `"credentials"`, or `"service"`.
    #[getter]
    fn name(&self) -> &'static str {
        self.inner.name
    }

    #[getter]
    fn ok(&self) -> bool {
        self.inner.ok
    }

    /// Whether a failure decides the report's `ok`. An unlisted region's line is advisory.
    #[getter]
    fn fatal(&self) -> bool {
        self.inner.fatal
    }

    /// False when an earlier check's failure kept this one from running (and calling AWS).
    #[getter]
    fn ran(&self) -> bool {
        self.inner.ran
    }

    #[getter]
    fn detail(&self) -> &str {
        &self.inner.detail
    }

    /// What to do about a failure; empty on a pass.
    #[getter]
    fn remedy(&self) -> &str {
        &self.inner.remedy
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        check_dict(py, &self.inner)
    }

    fn __repr__(&self) -> String {
        format!(
            "PreflightCheck(name={:?}, ok={}, fatal={}, ran={})",
            self.inner.name,
            py_bool(self.inner.ok),
            py_bool(self.inner.fatal),
            py_bool(self.inner.ran),
        )
    }
}

/// What `preflight` found: three checks and whether a launch could proceed.
#[pyclass(frozen, name = "PreflightReport", module = "microvms")]
pub struct PyPreflightReport {
    inner: PreflightReport,
}

#[pymethods]
impl PyPreflightReport {
    /// True exactly when no fatal check failed or was skipped.
    #[getter]
    fn ok(&self) -> bool {
        self.inner.ok()
    }

    /// The region checked, or `None` when none resolved.
    #[getter]
    fn region(&self) -> Option<String> {
        self.inner
            .region
            .as_ref()
            .map(|region| region.as_str().to_string())
    }

    /// `region`, `credentials`, and `service`, in that order.
    #[getter]
    fn checks(&self) -> Vec<PyPreflightCheck> {
        self.inner
            .checks
            .iter()
            .cloned()
            .map(|inner| PyPreflightCheck { inner })
            .collect()
    }

    /// The check named `name`, or `None`.
    fn check(&self, name: &str) -> Option<PyPreflightCheck> {
        self.inner
            .check(name)
            .cloned()
            .map(|inner| PyPreflightCheck { inner })
    }

    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item("ok", self.inner.ok())?;
        dict.set_item("region", self.region())?;
        let checks = self
            .inner
            .checks
            .iter()
            .map(|check| check_dict(py, check))
            .collect::<PyResult<Vec<_>>>()?;
        dict.set_item("checks", checks)?;
        Ok(dict)
    }

    fn __repr__(&self) -> String {
        format!(
            "PreflightReport(ok={}, region={:?}, checks={:?})",
            py_bool(self.inner.ok()),
            self.region(),
            self.inner
                .checks
                .iter()
                .map(|check| check.name)
                .collect::<Vec<_>>(),
        )
    }
}

fn py_bool(value: bool) -> &'static str {
    if value { "True" } else { "False" }
}

fn check_dict<'py>(py: Python<'py>, check: &Check) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("name", check.name)?;
    dict.set_item("ok", check.ok)?;
    dict.set_item("fatal", check.fatal)?;
    dict.set_item("ran", check.ran)?;
    dict.set_item("detail", &check.detail)?;
    dict.set_item("remedy", &check.remedy)?;
    Ok(dict)
}

/// Whether a harness can launch in `region` (default: `$AWS_REGION`, `$AWS_DEFAULT_REGION`,
/// then us-east-1), checked before it queues work.
///
/// Three checks: the region resolves (advisory when `Region.unlisted`), the credential chain
/// resolves credentials (no AWS call), and one `ListManagedMicrovmImages` page answers in that
/// region, the only AWS operation, free and read-only. A check after a failure is reported
/// with `ran=False` and makes no call. Nothing billable, nothing mutating. It does not check
/// roles, the artifact bucket, quotas, or VPC connectors, and there is no boto3 service-model
/// check: this client speaks the API version it was built against, and the listing is the
/// evidence the endpoint accepts it. Never raises; read `report.ok`.
#[pyfunction]
#[pyo3(signature = (region=None))]
pub(crate) fn preflight(py: Python<'_>, region: Option<PyRegion>) -> PyPreflightReport {
    let region = region.map(|region| region.inner);
    PyPreflightReport {
        inner: runtime::block_on(py, microvms_core::preflight::preflight(region)),
    }
}
