// SPDX-License-Identifier: Apache-2.0
//! Seconds that arrive as a float, as a [`Duration`].
//!
//! A wait, a window, an interval or a token's lifetime reaches every surface as a number of
//! seconds: a CLI flag, a keyword argument, a JSON field. [`of_secs_f64`] is the conversion all
//! of them call, so a figure that can't be a duration is refused before any work starts, with
//! one message everywhere (#268). A figure bound for a cost report goes through
//! [`crate::cost::duration_of_secs_f64`] instead, whose refusal also says why a negative span
//! matters there; a wait has no report to put a credit on, so its refusal doesn't claim one
//! (#338).

use std::time::Duration;

use crate::error::Error;

/// Seconds from a float, refused when the float can't be a duration.
///
/// `Duration::try_from_secs_f64` is what refuses a negative, non-finite or too-large figure,
/// which is why the adapters' `clippy.toml` bans the panicking conversions.
pub fn of_secs_f64(seconds: f64) -> Result<Duration, Error> {
    of_secs_f64_because(seconds, "")
}

/// [`of_secs_f64`] with `why` appended to the refusal, for a figure that has its own reason to
/// be a duration.
pub(crate) fn of_secs_f64_because(seconds: f64, why: &str) -> Result<Duration, Error> {
    // `Debug`, not `Display`: `Display` spells `1e300` as a 1 and 300 zeros.
    Duration::try_from_secs_f64(seconds).map_err(|source| {
        Error::invalid_arg(format!(
            "{seconds:?} seconds is not a duration: it must be finite, non-negative and below \
             2^64{why}"
        ))
        .with_source(source)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorKind;

    /// The refusal names the figure as typed and the whole range it missed, `1e300` included.
    ///
    /// **Falsification**: `verify/guards/faults/seconds-flags.toml` entry
    /// `domain-duration-refusal-as-typed` prints the figure with `Display`, and the `1e300` row
    /// reads as 301 digits.
    #[test]
    fn a_refused_figure_is_named_as_typed_with_the_range_it_missed() {
        for (seconds, shown) in [(1e300, "1e300"), (f64::INFINITY, "inf"), (-5.0, "-5.0")] {
            let message = of_secs_f64(seconds)
                .expect_err("not a duration")
                .to_string();
            assert!(
                message.starts_with(&format!("{shown} seconds is not a duration")),
                "{message}"
            );
            assert!(message.contains("below 2^64"), "{message}");
        }
        assert_eq!(of_secs_f64(1.25).ok(), Some(Duration::from_millis(1250)));
    }

    /// A wait's refusal says what a duration must be and nothing about a report: a caller who
    /// passed `timeout=-1` has no report a credit could land on (#338). The cost figure's
    /// refusal keeps that reason, since there it's why the span is refused.
    ///
    /// **Falsification**: `verify/guards/faults/seconds-flags.toml` entry
    /// `domain-wait-refusal-names-a-credit` gives `of_secs_f64` the cost refusal's reason, and
    /// this goes red on the `-1.0` row.
    #[test]
    fn a_wait_refusal_names_no_report_and_a_cost_refusal_still_does() {
        for seconds in [-1.0, f64::NAN, f64::INFINITY] {
            let wait = of_secs_f64(seconds).expect_err("not a wait");
            assert_eq!(wait.kind(), ErrorKind::InvalidArg);
            assert!(!wait.to_string().contains("report"), "{wait}");
            let cost = crate::cost::duration_of_secs_f64(seconds).expect_err("not a span");
            assert!(cost.to_string().contains("credit on the report"), "{cost}");
            assert!(cost.to_string().starts_with(&wait.to_string()), "{cost}");
        }
    }
}
