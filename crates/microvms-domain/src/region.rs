// SPDX-License-Identifier: Apache-2.0
//! An AWS region: any well-formed name, plus the regions AWS was measured running MicroVMs
//! in (TRAP-6).
//!
//! # Why any name
//!
//! AWS adds MicroVMs regions faster than a client release can follow: it launched in five
//! regions on 2026-06-22, five more on 2026-08-19 and four more on 2026-10-22. A client that
//! refuses a region AWS has launched in is wrong for every caller there until its next
//! release, so since 1.3.0 [`Region`] parses any well-formed region name, as `aws-types`'
//! `Region` does, and keeps the measured set as data in [`MEASURED_REGIONS`].
//!
//! # What the measured set is for
//!
//! In a region without MicroVMs, the first control-plane call can answer
//! `AccessDeniedException` with the message field **null**. A denied implied IAM action,
//! such as tagging an image on creation, returns the same response (measured 2026-10-17,
//! us-east-1, API 2025-09-09; docs/PLATFORM.md). Nothing before that call can tell the two
//! apart: the service model's `endpointPrefix` is `lambda`, so an endpoint resolves for
//! every region, and a `ListMicrovms` that answers is not evidence either, since regions
//! AWS lists as planned, such as `ca-central-1`, answered it on 2026-10-24. So the client
//! reports instead of refusing. When that response arrives for a region outside
//! [`MEASURED_REGIONS`], the error names the region as a cause to verify alongside the IAM
//! policy (TRAP-6), and `preflight`'s region line reports the region as unmeasured, as an
//! advisory.
//!
//! [`MEASURED_REGIONS`] is the 14 regions the Pricing API priced MicroVMs in on 2026-10-24,
//! which are the 14 AWS's launch posts name. No SDK call states the set:
//! `get_available_endpoints` returns an empty list while `get_available_regions` returns all
//! 34 Lambda regions.
//!
//! # The variants are deprecated
//!
//! Until 1.3.0 this enum was closed over the five regions of the first launch, plus
//! [`Region::Unlisted`] for a name a caller opted into. Its variants still work in 1.x, and a
//! name outside the five still parses to `Unlisted`, but they're deprecated: the constants
//! [`Region::US_EAST_1`] and four more replace the five, and [`Region::as_str`] reads any
//! region. `docs/STABILITY.md` keeps a deprecated item working for at least six calendar
//! months and two minor releases, so 2.0.0 at the earliest makes `Region` a name with no
//! variants (#743).
//!
//! # What parsing checks
//!
//! The shape only. A region name becomes the middle of the endpoint's host name
//! (`lambda.<region>.amazonaws.com`), so [`FromStr`] accepts lowercase ASCII letters, digits
//! and hyphens, starting with a letter and ending with a digit, with at least one hyphen and
//! no two in a row, at most 63 characters. That refuses the typos an environment variable
//! carries, a case change or a trailing space, and anything that would change the host.

use std::fmt;
use std::str::FromStr;

use crate::error::Error;

/// An AWS region, by its name.
///
/// Built by parsing (`"eu-central-1".parse::<Region>()`), from the environment
/// ([`Region::from_env`]), or from a constant ([`Region::US_EAST_1`]). Equality is by name,
/// so a parsed `"us-east-1"` equals the constant. Read a region with [`Region::as_str`]
/// rather than matching on its variants, which are deprecated (see the module docs).
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum Region {
    #[deprecated(
        since = "1.3.0",
        note = "use `Region::US_EAST_1`; `Region`'s variants go no earlier than 2.0.0 (#743)"
    )]
    UsEast1,
    #[deprecated(
        since = "1.3.0",
        note = "use `Region::US_EAST_2`; `Region`'s variants go no earlier than 2.0.0 (#743)"
    )]
    UsEast2,
    #[deprecated(
        since = "1.3.0",
        note = "use `Region::US_WEST_2`; `Region`'s variants go no earlier than 2.0.0 (#743)"
    )]
    UsWest2,
    #[deprecated(
        since = "1.3.0",
        note = "use `Region::EU_WEST_1`; `Region`'s variants go no earlier than 2.0.0 (#743)"
    )]
    EuWest1,
    #[deprecated(
        since = "1.3.0",
        note = "use `Region::AP_NORTHEAST_1`; `Region`'s variants go no earlier than 2.0.0 \
                (#743)"
    )]
    ApNortheast1,
    /// Any region outside the five above, by name.
    ///
    /// Since 1.3.0 every well-formed name outside the five parses to this, whether or not
    /// AWS was measured running MicroVMs there; [`Region::is_measured`] says which.
    #[deprecated(
        since = "1.3.0",
        note = "parse the name with `name.parse::<Region>()` and read it with `as_str`; \
                `Region`'s variants go no earlier than 2.0.0 (#743)"
    )]
    Unlisted(String),
}

// The constants are the variants' replacements, so they are the one place that names them.
#[allow(deprecated)]
impl Region {
    pub const US_EAST_1: Region = Region::UsEast1;
    pub const US_EAST_2: Region = Region::UsEast2;
    pub const US_WEST_2: Region = Region::UsWest2;
    pub const EU_WEST_1: Region = Region::EuWest1;
    pub const AP_NORTHEAST_1: Region = Region::ApNortheast1;
}

/// The regions AWS was measured running MicroVMs in: the 14 the Pricing API priced on
/// 2026-10-24, in the order AWS launched them (2026-06-22, 2026-08-19, 2026-10-22).
///
/// Names rather than [`Region`] values, because a region outside the five constants holds
/// its name in a `String`, which a constant can't build; parse a name for its `Region`.
///
/// Not model-backed and it cannot be: see the module docs. `constants::as_json` publishes
/// this set so `tools/check-model-drift.py` can compare it with the list
/// `tools/check-live-rates.py` keeps, which is the only check available for a value no
/// service model states.
pub const MEASURED_REGIONS: [&str; 14] = [
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-west-1",
    "ap-northeast-1",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "eu-central-1",
    "eu-north-1",
    "ap-east-1",
    "ap-southeast-5",
    "ap-southeast-7",
    "eu-south-2",
];

/// The five regions of AWS's first MicroVMs launch (2026-06-22), the ones with constants.
#[deprecated(
    since = "1.3.0",
    note = "AWS runs MicroVMs in more regions than these five: use `MEASURED_REGIONS` and \
            `Region::is_measured` (#743)"
)]
pub const MICROVM_REGIONS: [Region; 5] = [
    Region::US_EAST_1,
    Region::US_EAST_2,
    Region::US_WEST_2,
    Region::EU_WEST_1,
    Region::AP_NORTHEAST_1,
];

/// The five named regions, which the parse and the deprecated escape hatch both read.
const NAMED: [Region; 5] = [
    Region::US_EAST_1,
    Region::US_EAST_2,
    Region::US_WEST_2,
    Region::EU_WEST_1,
    Region::AP_NORTHEAST_1,
];

impl Region {
    /// The wire spelling, which is also the endpoint's middle segment.
    #[allow(deprecated)]
    pub fn as_str(&self) -> &str {
        match self {
            Region::UsEast1 => "us-east-1",
            Region::UsEast2 => "us-east-2",
            Region::UsWest2 => "us-west-2",
            Region::EuWest1 => "eu-west-1",
            Region::ApNortheast1 => "ap-northeast-1",
            Region::Unlisted(name) => name,
        }
    }

    /// Whether AWS was measured running MicroVMs here: membership in [`MEASURED_REGIONS`].
    ///
    /// False means unmeasured, not refused. The region still works when AWS runs MicroVMs
    /// there; what it changes is what a null-message `AccessDeniedException` there most
    /// likely means.
    pub fn is_measured(&self) -> bool {
        MEASURED_REGIONS.contains(&self.as_str())
    }

    /// Whether this is one of the five named regions.
    #[deprecated(
        since = "1.3.0",
        note = "AWS runs MicroVMs in more regions than the five named ones: use \
                `is_measured` (#743)"
    )]
    pub fn is_supported(&self) -> bool {
        NAMED.contains(self)
    }

    /// A region by name, without the shape check.
    ///
    /// Every well-formed name parses since 1.3.0, so this is [`str::parse`] without its
    /// refusal of a malformed name. A named region's name comes back as its constant, so
    /// `unlisted("us-east-1") == Region::US_EAST_1`.
    #[deprecated(
        since = "1.3.0",
        note = "every well-formed region name parses: use `name.parse::<Region>()`"
    )]
    pub fn unlisted(name: impl Into<String>) -> Region {
        Region::named(name.into())
    }

    /// A region name this client wrote into a record, taken as written.
    ///
    /// A name record's region came from a [`Region`], so it needs no second check, and a
    /// record a 1.2 client wrote through `unlisted` still reads back.
    pub(crate) fn as_written(name: &str) -> Region {
        Region::named(name.to_string())
    }

    /// The constant for a named region, or `Unlisted` for any other name. The single place
    /// a name becomes a value, so a parsed region and a constant are one spelling.
    #[allow(deprecated)]
    fn named(name: String) -> Region {
        NAMED
            .into_iter()
            .find(|region| region.as_str() == name)
            .unwrap_or(Region::Unlisted(name))
    }

    /// The region the environment names: `$AWS_REGION`, then `$AWS_DEFAULT_REGION`, then
    /// `us-east-1`.
    ///
    /// Parsed rather than accepted, because an environment variable is where a stray space
    /// or a case change arrives. `env` is a lookup rather than `std::env::var` so a caller
    /// (and a test) decides where the variables come from. The CLI's region resolution and
    /// `microvms_core::preflight::preflight` both read it here.
    pub fn from_env(env: &dyn Fn(&str) -> Option<String>) -> Result<Region, Error> {
        match env("AWS_REGION").or_else(|| env("AWS_DEFAULT_REGION")) {
            Some(name) => name.parse::<Region>().map_err(|error| {
                Error::invalid_arg(format!(
                    "{error} It arrived from $AWS_REGION or $AWS_DEFAULT_REGION."
                ))
            }),
            None => Ok(Region::US_EAST_1),
        }
    }
}

/// Whether `name` has the shape of an AWS region name; see the module docs.
fn is_region_shaped(name: &str) -> bool {
    let bytes = name.as_bytes();
    let (Some(first), Some(last)) = (bytes.first(), bytes.last()) else {
        return false;
    };
    bytes.len() <= 63
        && first.is_ascii_lowercase()
        && last.is_ascii_digit()
        && bytes.contains(&b'-')
        && !name.contains("--")
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

impl fmt::Display for Region {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Region {
    type Err = Error;

    /// Parses any well-formed region name, and refuses a malformed one.
    ///
    /// A named region's name comes back equal to its constant.
    fn from_str(name: &str) -> Result<Region, Error> {
        if is_region_shaped(name) {
            return Ok(Region::named(name.to_string()));
        }
        Err(Error::invalid_arg(format!(
            "{name:?} is not a region name. A region name is lowercase letters, digits and \
             hyphens, starts with a letter and ends with a digit, such as us-east-1."
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorKind;

    /// The 14 spellings, written out. A generated list would agree with a typo, and a typo
    /// here marks a region measured that never was.
    #[test]
    fn the_measured_regions_are_the_fourteen_aws_prices() {
        assert_eq!(
            MEASURED_REGIONS,
            [
                "us-east-1",
                "us-east-2",
                "us-west-2",
                "eu-west-1",
                "ap-northeast-1",
                "ap-south-1",
                "ap-southeast-1",
                "ap-southeast-2",
                "eu-central-1",
                "eu-north-1",
                "ap-east-1",
                "ap-southeast-5",
                "ap-southeast-7",
                "eu-south-2",
            ]
        );
    }

    /// Every measured name parses back to a region that names it, and the five named ones
    /// parse to their constants, so a parsed region and a constant are one spelling, not two.
    #[test]
    fn each_measured_region_round_trips_through_its_wire_name() {
        for name in MEASURED_REGIONS {
            let parsed: Region = name.parse().expect("a measured name parses");
            assert_eq!(parsed.as_str(), name);
            assert!(parsed.is_measured());
        }
        for region in NAMED {
            assert_eq!(region.as_str().parse::<Region>().unwrap(), region);
        }
    }

    /// A region AWS has not launched MicroVMs in still parses, and says it is unmeasured.
    /// `us-west-1` answered `ListMicrovms` with the null-message denial on 2026-10-24.
    #[test]
    fn an_unmeasured_region_parses_and_reports_it() {
        for name in ["us-west-1", "ca-central-1", "us-gov-west-1", "us-east-3"] {
            let region: Region = name.parse().expect("a well-formed name parses");
            assert_eq!(region.as_str(), name);
            assert!(!region.is_measured(), "{name}");
        }
    }

    /// The shapes a typo or an injected host takes are refused before they reach a URL: a
    /// case change, a stray space, the empty string, a name with no hyphen or no trailing
    /// digit, a doubled hyphen, and characters that would change the host.
    #[test]
    fn malformed_names_are_refused_naming_the_shape() {
        for name in [
            "US-EAST-1",
            "us-east-1 ",
            " us-east-1",
            "",
            "useast1",
            "us-east",
            "1us-east-1",
            "us--east-1",
            "-us-east-1",
            "us-east-1.example.com",
            "evil.com/us-east-1",
            "us_east_1",
        ] {
            let error = name
                .parse::<Region>()
                .expect_err("a malformed name is refused");
            assert_eq!(error.kind(), ErrorKind::InvalidArg, "{name:?}");
            assert!(
                error.to_string().contains("not a region name"),
                "{name:?}: {error}"
            );
        }
        let too_long = format!("{}-1", "a".repeat(62));
        assert!(too_long.parse::<Region>().is_err());
    }

    /// The environment's region is parsed with the same check, and the error says where the
    /// name came from.
    #[test]
    fn the_environment_region_is_parsed_and_defaults_to_us_east_1() {
        let none = |_: &str| None;
        assert_eq!(Region::from_env(&none).unwrap(), Region::US_EAST_1);

        let default_only =
            |key: &str| (key == "AWS_DEFAULT_REGION").then(|| "eu-central-1".to_string());
        assert_eq!(
            Region::from_env(&default_only).unwrap().as_str(),
            "eu-central-1"
        );

        let both = |key: &str| match key {
            "AWS_REGION" => Some("us-west-1".to_string()),
            _ => Some("eu-central-1".to_string()),
        };
        assert_eq!(Region::from_env(&both).unwrap().as_str(), "us-west-1");

        let typo = |key: &str| (key == "AWS_REGION").then(|| "us-east-1 ".to_string());
        let error = Region::from_env(&typo).expect_err("a stray space is refused");
        assert!(error.to_string().contains("$AWS_REGION"), "{error}");
    }

    /// The deprecated 1.x surface keeps its 1.2 behavior: the escape hatch normalizes a named
    /// region to its constant, a name beyond the five is `Unlisted`, the old list is the five,
    /// and `is_supported` is true for the five alone.
    #[test]
    #[allow(deprecated)]
    fn the_deprecated_surface_keeps_its_one_two_behavior() {
        assert_eq!(Region::unlisted("us-east-1"), Region::US_EAST_1);
        assert_eq!(Region::US_EAST_1, Region::UsEast1);
        let parsed: Region = "eu-central-1".parse().unwrap();
        assert_eq!(parsed, Region::Unlisted("eu-central-1".to_string()));
        assert_eq!(Region::unlisted("eu-central-1"), parsed);
        assert_eq!(MICROVM_REGIONS, NAMED);
        assert!(Region::AP_NORTHEAST_1.is_supported());
        assert!(!parsed.is_supported());
        assert!(parsed.is_measured());
    }
}
