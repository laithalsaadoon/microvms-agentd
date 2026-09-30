// SPDX-License-Identifier: Apache-2.0
//! Lossless conversion from JavaScript numbers to Rust integer inputs.
//!
//! N-API's integer conversion truncates fractions and wraps at 32 bits. Extract
//! numbers as `f64` first so invalid input cannot change a timeout, UID, or port.
//! Service-specific limits remain in the core.

use microvms_core::Error;

fn integer(value: f64, maximum: f64, field: &str) -> Result<f64, Error> {
    if !value.is_finite() || value.fract() != 0.0 || value < 0.0 || value > maximum {
        return Err(Error::invalid_arg(format!(
            "{field} must be an integer between 0 and {maximum}, got {value}"
        )));
    }
    Ok(value)
}

pub(crate) fn u32_number(value: f64, field: &str) -> Result<u32, Error> {
    Ok(integer(value, f64::from(u32::MAX), field)? as u32)
}

pub(crate) fn u16_number(value: f64, field: &str) -> Result<u16, Error> {
    Ok(integer(value, f64::from(u16::MAX), field)? as u16)
}

/// A whole number up to `Number.MAX_SAFE_INTEGER`, past which a JS number can't represent
/// every integer exactly.
fn safe_integer(value: f64, field: &str) -> Result<u64, Error> {
    Ok(integer(value, 9_007_199_254_740_991.0, field)? as u64)
}

pub(crate) fn offset_number(value: f64) -> Result<u64, Error> {
    // Larger JS numbers cannot represent every byte offset exactly.
    safe_integer(value, "offset")
}

/// An optional line number. Zero converts: whether a line exists is core's to say.
pub(crate) fn optional_line(value: Option<f64>, field: &str) -> Result<Option<u64>, Error> {
    value.map(|value| safe_integer(value, field)).transpose()
}

pub(crate) fn optional_u32(value: Option<f64>, field: &str) -> Result<Option<u32>, Error> {
    value.map(|value| u32_number(value, field)).transpose()
}
