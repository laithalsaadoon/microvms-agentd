// SPDX-License-Identifier: Apache-2.0
//! Managed connector intents and customer-managed VPC egress connector validation.
//!
//! # Why an enum rather than a string
//!
//! Two reasons, and both are measurements.
//!
//! The API takes a **fully-qualified ARN** and rejects the bare name with "Malformed
//! network connector ARN" (measured 2026-08-05). `NetworkConnector` in the service model
//! is just `{type: string, max: 2048, min: 1}` — no pattern, no enum — so a free-form
//! parameter passes every check the model states and fails on the wire, and the value
//! that reads most natural to write (`"ALL_INGRESS"`) is exactly the one that fails.
//! Deriving the ARN from an intent means the caller states what they want and the
//! spelling is not theirs to get wrong.
//!
//! # TRAP-11, revised: the shell connector is a variant, on measured ground
//!
//! `SHELL_INGRESS` **is** a variant here, and it was not always. This module used to
//! omit it on the claim that it gated a console-only debugging flow — "not a
//! programmatic exec path despite the name" — and that omission made requesting it
//! unwriteable. `docs/PLATFORM.md` (measured 2026-08-15) refutes the claim: the shell
//! endpoint is a real PTY over a WebSocket, and it is programmatically drivable. The
//! ground that actually holds is narrower — **one interactive session is not
//! programmatic exec**: no exec ids, no idempotency, no separated stdout/stderr, no exit
//! codes. So the variant exists for callers that want the PTY, and the exec path never
//! requests it; the lifecycle test in [`crate::control::microvm`] asserts a launch
//! carries exactly the connectors its caller asked for.
//!
//! Two measured constraints travel with the variant (both from `docs/PLATFORM.md`):
//! `ALL_INGRESS` cannot combine with any other ingress connector, and the platform says
//! so only at token-mint time — `RunMicrovm` accepts the invalid set, the VM reaches
//! RUNNING, and it bills until something asks for a shell token.
//! [`crate::control::ControlPlane::run_microvm`] refuses the combination locally
//! instead. The pair that works is `[HTTP_INGRESS, SHELL_INGRESS]`.
//!
//! The sibling half of TRAP-11 — the shell-auth operation — is still closed by the
//! absence of a method on [`crate::control::ControlPlane`]; see that module's docs.

use crate::region::Region;

/// Validates a customer-managed connector ARN before launching a billable VM.
pub(super) fn require_egress_connector_arn(arn: &str, region: &Region) -> Result<(), crate::Error> {
    let parts: Vec<&str> = arn.splitn(6, ':').collect();
    let valid = parts.len() == 6
        && (1..=crate::constants::MAX_NETWORK_CONNECTOR_LEN).contains(&arn.len())
        && parts[0] == "arn"
        && parts[1] == "aws"
        && parts[2] == "lambda"
        && parts[3] == region.as_str()
        && parts[4].len() == 12
        && parts[4].bytes().all(|byte| byte.is_ascii_digit())
        && parts[5]
            .strip_prefix("network-connector:")
            .is_some_and(|resource| {
                let mut parts = resource.split(':');
                let name = parts.next().unwrap_or_default();
                let version = parts.next();
                !name.is_empty()
                    && name
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
                    && version.is_none_or(|version| {
                        !version.is_empty()
                            && !version.starts_with('0')
                            && version.bytes().all(|byte| byte.is_ascii_digit())
                    })
                    && parts.next().is_none()
            });
    if !valid {
        return Err(crate::Error::invalid_arg(format!(
            "egressNetworkConnectors requires a customer-managed Lambda network connector ARN \
             in {} (arn:aws:lambda:{}:<12-digit-account>:network-connector:<id>[:<version>]); \
             create the VPC egress connector through lambda-core first",
            region.as_str(),
            region.as_str(),
        )));
    }
    Ok(())
}

/// Compatibility constant: omitting an egress connector does not block internet access.
/// Isolation requires a VPC egress connector and a VPC without an internet gateway
/// or NAT gateway. A launch flag cannot establish the VPC's routing configuration.
pub const PLATFORM_HONOURS_OMITTED_EGRESS: bool = false;

/// Conservative network posture inferred from launch options.
///
/// Customer-managed connector ARNs do not prove internet isolation: their VPC routing
/// must be checked separately. No combination of flags is automatically `Sealed`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum EgressPosture {
    /// `INTERNET_EGRESS` is on the request: the VM has outbound network by design.
    Open,
    /// Internet isolation has not been verified. This is the default launch posture.
    Unsealed,
    /// Advisory in-guest proxy denial ([`crate::sandbox::RunRequest::deny_egress`]).
    /// A workload can bypass it by ignoring its environment.
    BestEffort,
    /// Internet isolation verified separately through VPC routing configuration.
    /// Retained for stored labels; never inferred by [`EgressPosture::for_launch`].
    Sealed,
}

impl EgressPosture {
    /// The posture of a launch that requested `egress` and asked for `deny_egress`.
    ///
    /// These flags cannot verify VPC routing, so this never returns `Sealed`.
    pub fn for_launch(egress: bool, deny_egress: bool) -> Self {
        match (egress, deny_egress) {
            (true, _) => EgressPosture::Open,
            (false, true) => EgressPosture::BestEffort,
            (false, false) => EgressPosture::Unsealed,
        }
    }

    /// The wire spelling, which is what an envelope and a `--json` consumer branch on.
    ///
    /// Four values and no `Option`: a consumer never has to guard against a missing
    /// posture, and none of the four is the empty string a `bool` degrades into.
    pub fn as_str(self) -> &'static str {
        match self {
            EgressPosture::Open => "open",
            EgressPosture::Unsealed => "unsealed",
            EgressPosture::BestEffort => "best-effort",
            EgressPosture::Sealed => "sealed",
        }
    }

    /// Whether outbound traffic is refused by something other than the workload's goodwill.
    ///
    /// Only [`EgressPosture::Sealed`] answers `true`. [`EgressPosture::BestEffort`] does
    /// not, and that is the whole reason this predicate exists rather than a
    /// `posture != Open` test at each call site.
    pub fn is_sealed(self) -> bool {
        matches!(self, EgressPosture::Sealed)
    }

    /// The posture a stored label names, or `None` for a string this version does not know.
    ///
    /// The inverse of [`EgressPosture::as_str`], and it exists for one caller: the CLI's
    /// name registry persists the label of the launch a name was registered for, and
    /// `agent-up`'s refresh path reads it back in a later process rather than assuming the
    /// posture of the launch it did not perform. `None` rather than a default, so the caller
    /// decides what an unreadable record means instead of inheriting a claim.
    pub fn from_label(label: &str) -> Option<Self> {
        [
            EgressPosture::Open,
            EgressPosture::Unsealed,
            EgressPosture::BestEffort,
            EgressPosture::Sealed,
        ]
        .into_iter()
        .find(|posture| posture.as_str() == label)
    }

    /// One sentence a human can act on, printed beside the label.
    ///
    /// Each names the mechanism rather than a verdict, because "no egress" read as a
    /// verdict is the defect.
    pub fn describe(self) -> &'static str {
        match self {
            EgressPosture::Open => {
                "the INTERNET_EGRESS connector is on the request; the VM reaches the \
                 internet by design"
            }
            EgressPosture::Unsealed => {
                "internet isolation is unverified. Use a VPC egress connector with a VPC \
                 without an internet gateway or NAT gateway; connector omission does not \
                 block internet access. Not a seal"
            }
            EgressPosture::BestEffort => {
                "advisory proxy-deny environment variables affect clients that honor them; \
                 workloads can bypass them. Not a seal"
            }
            EgressPosture::Sealed => {
                "internet isolation requires separately verified VPC routing without an \
                 internet gateway or NAT gateway; launch flags alone do not establish it"
            }
        }
    }
}

/// Defaults to the posture of a launch with no network flags.
impl Default for EgressPosture {
    fn default() -> Self {
        EgressPosture::for_launch(false, false)
    }
}

impl std::fmt::Display for EgressPosture {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Lambda-managed connector intents. Customer-managed VPC connectors use explicit ARNs.
///
/// Named for the *intent* rather than for the wire value, because the intent is what a
/// caller has: "let the proxy reach the VM" and "let the VM reach the internet". The
/// wire spellings ([`ConnectorIntent::wire_name`]) are an implementation detail of the
/// ARN.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ConnectorIntent {
    /// Lets the endpoint proxy reach the VM. Required for any session to work.
    ///
    /// The union that cannot be intersected: the platform refuses to combine it with
    /// any other ingress connector, and only at token-mint time — see the module docs.
    /// A VM that needs a shell requests [`ConnectorIntent::HttpIngress`] plus
    /// [`ConnectorIntent::ShellIngress`] instead of this.
    AllIngress,
    /// Lets the endpoint proxy reach the VM's HTTP surface, without the shell.
    ///
    /// The finer-grained sibling of [`ConnectorIntent::AllIngress`], and the half of the
    /// measured pair `[HTTP_INGRESS, SHELL_INGRESS]` that keeps the daemon reachable
    /// (`docs/PLATFORM.md`, measured 2026-08-15).
    HttpIngress,
    /// Lets the VM mint shell tokens and serve its PTY WebSocket.
    ///
    /// One interactive session, not programmatic exec — the module docs carry the
    /// revision of TRAP-11 that admitted this variant. Never combined with
    /// [`ConnectorIntent::AllIngress`]; the pair that launches and mints is with
    /// [`ConnectorIntent::HttpIngress`].
    ShellIngress,
    /// Lets the VM reach the internet.
    ///
    /// Omitted by default — the right default for a daemon that needs none. Omitting it
    /// omits the connector from the request; measured 2026-09-11 and 2026-09-12 the
    /// platform still gave such a VM outbound network (`docs/PLATFORM.md`, "A VM launched
    /// without the egress connector still has outbound network").
    Egress,
}

impl ConnectorIntent {
    /// Every intent, so a test can enumerate the complete set.
    ///
    /// Maintained by hand; the tests below assert its length, so an edit here is a
    /// deliberate one. The set grew from two to four when TRAP-11 was revised — see the
    /// module docs.
    pub const ALL: [ConnectorIntent; 4] = [
        ConnectorIntent::AllIngress,
        ConnectorIntent::HttpIngress,
        ConnectorIntent::ShellIngress,
        ConnectorIntent::Egress,
    ];

    /// The connector's name as the ARN spells it.
    ///
    /// `INTERNET_EGRESS` rather than `EGRESS`: the wire name is not the intent's name,
    /// which is the second reason this is a table rather than a `Display` derive.
    pub fn wire_name(self) -> &'static str {
        match self {
            ConnectorIntent::AllIngress => "ALL_INGRESS",
            ConnectorIntent::HttpIngress => "HTTP_INGRESS",
            ConnectorIntent::ShellIngress => "SHELL_INGRESS",
            ConnectorIntent::Egress => "INTERNET_EGRESS",
        }
    }

    /// The fully-qualified ARN for this connector in `region`.
    ///
    /// One interpolation for both directions. The Python client's earlier shape derived
    /// the egress ARN by string-replacing `ALL_INGRESS` inside the ingress one, which
    /// produced a valid ARN only as long as the two names never became substrings of
    /// each other (`sandbox.py:391-398`).
    ///
    /// The doubled `aws-network-connector` segment is not a typo: the resource type and
    /// the resource name are both present, and the format is copied from
    /// `sandbox.py:398` rather than reconstructed from the ARN grammar.
    pub fn arn(self, region: &Region) -> String {
        format!(
            "arn:aws:lambda:{}:aws:network-connector:aws-network-connector:{}",
            region.as_str(),
            self.wire_name()
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn customer_connector_arn_length_uses_the_run_microvm_limit() {
        let prefix = "arn:aws:lambda:us-east-1:123456789012:network-connector:";
        let maximum = crate::constants::MAX_NETWORK_CONNECTOR_LEN;
        let arn = format!("{prefix}{}", "x".repeat(maximum - prefix.len()));
        require_egress_connector_arn(&arn, &Region::UsEast1).expect("at the request limit");
        require_egress_connector_arn(&format!("{arn}x"), &Region::UsEast1)
            .expect_err("one over the request limit");
    }

    /// An omitted connector cannot establish internet isolation.
    #[test]
    fn a_launch_with_no_connector_is_unsealed_and_not_sealed() {
        let posture = EgressPosture::for_launch(false, false);
        assert_eq!(posture, EgressPosture::Unsealed);
        assert_eq!(posture.as_str(), "unsealed");
        assert!(
            !posture.is_sealed(),
            "omitting the connector does not seal the VM; it is measured not to"
        );
    }

    /// The other three postures, and the two rules that order them: `--egress` is `open`
    /// whatever else was asked for, and proxy denial is only advisory.
    #[test]
    fn the_posture_table_is_the_whole_domain() {
        assert_eq!(EgressPosture::for_launch(true, false), EgressPosture::Open);
        assert_eq!(
            EgressPosture::for_launch(true, true),
            EgressPosture::Open,
            "asking for egress is asking for egress; the advisory deny cannot dress it as \
             anything narrower"
        );
        assert_eq!(
            EgressPosture::for_launch(false, true),
            EgressPosture::BestEffort
        );
        assert_eq!(EgressPosture::BestEffort.as_str(), "best-effort");
        assert!(
            !EgressPosture::BestEffort.is_sealed(),
            "best effort is not a seal, and this predicate is the only place that decides it"
        );
        assert!(EgressPosture::Sealed.is_sealed());
    }

    /// Every posture renders a distinct non-empty label and a sentence that says what the
    /// mechanism is, because a consumer branches on the label and a human reads the line.
    #[test]
    fn every_posture_has_a_distinct_label_and_a_description() {
        let postures = [
            EgressPosture::Open,
            EgressPosture::Unsealed,
            EgressPosture::BestEffort,
            EgressPosture::Sealed,
        ];
        let labels: Vec<&str> = postures.iter().map(|posture| posture.as_str()).collect();
        for (i, a) in labels.iter().enumerate() {
            assert!(!a.is_empty());
            for b in &labels[i + 1..] {
                assert_ne!(a, b);
            }
        }
        for posture in postures {
            assert!(posture.describe().len() > 40, "{posture}");
        }
        // The two postures that are not seals must say so in words, not only in the
        // predicate: the human line is what a reviewer reads.
        for posture in [EgressPosture::Unsealed, EgressPosture::BestEffort] {
            assert!(
                posture.describe().contains("Not a seal"),
                "{posture} must state that it is not a seal: {}",
                posture.describe()
            );
        }
    }

    /// Defaults and explicitly empty launch options report the same posture.
    #[test]
    fn the_default_posture_is_the_derived_one() {
        assert_eq!(
            EgressPosture::default(),
            EgressPosture::for_launch(false, false),
            "a defaulted posture must be the posture of a launch that asks for nothing, \
             derived from the same launch options"
        );
    }

    /// Every label round-trips, and an unknown one is `None` rather than a guess.
    ///
    /// The registry stores the label, so a record written by a later version — or corrupted
    /// — must not be read as a posture this version would then report as measured.
    #[test]
    fn every_label_round_trips_and_an_unknown_one_is_none() {
        for posture in [
            EgressPosture::Open,
            EgressPosture::Unsealed,
            EgressPosture::BestEffort,
            EgressPosture::Sealed,
        ] {
            assert_eq!(EgressPosture::from_label(posture.as_str()), Some(posture));
        }
        assert_eq!(EgressPosture::from_label("sealed-ish"), None);
        assert_eq!(EgressPosture::from_label(""), None);
        assert_eq!(
            EgressPosture::from_label("Open"),
            None,
            "the labels are the wire spelling, and case is part of it"
        );
    }

    /// The exact ARN, as a literal, for the region the measurements were taken in. The
    /// format came from a measurement rather than from the ARN grammar, so a
    /// reconstruction of it in the test would agree with a reconstruction in the code.
    #[test]
    fn the_ingress_arn_is_the_measured_literal() {
        assert_eq!(
            ConnectorIntent::AllIngress.arn(&Region::UsEast1),
            "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"
        );
    }

    /// The egress ARN, likewise — and note the wire name is `INTERNET_EGRESS` while the
    /// variant is `Egress`, which is the thing a `Display` derive would have got wrong.
    #[test]
    fn the_egress_arn_uses_the_internet_egress_wire_name() {
        assert_eq!(
            ConnectorIntent::Egress.arn(&Region::UsEast1),
            "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
        );
    }

    /// The region is interpolated rather than fixed, for every region including the
    /// escape hatch. TRAP-4 is "for the request region", and a hardcoded `us-east-1`
    /// would pass both tests above.
    #[test]
    fn the_arn_carries_the_request_region_for_every_region() {
        for region in crate::region::MICROVM_REGIONS {
            let arn = ConnectorIntent::AllIngress.arn(&region);
            assert!(
                arn.contains(&format!(":lambda:{}:aws:", region.as_str())),
                "{arn}"
            );
        }
        let unlisted = Region::unlisted("me-south-1");
        assert!(
            ConnectorIntent::Egress
                .arn(&unlisted)
                .contains(":lambda:me-south-1:aws:"),
            "the escape hatch still derives an ARN for its own region"
        );
    }

    /// TRAP-11, rewritten on the ground that holds. This test used to assert no intent
    /// names `SHELL_INGRESS`, standing on the claim that the shell was a console-only
    /// debugging path — a claim `docs/PLATFORM.md` (measured 2026-08-15) refutes: the
    /// shell endpoint is a real PTY and programmatically drivable. What holds instead is
    /// that **one interactive session is not programmatic exec**, so the variant exists,
    /// exactly one intent renders it, and the check on the rendered ARNs stays — a
    /// variant named something else must not render `SHELL_INGRESS` either.
    ///
    /// The other half of the revised guard — a launch carries exactly the connectors its
    /// caller asked for — lives with the lifecycle test in `microvm.rs`.
    ///
    /// **Falsification** — add a second variant whose wire name contains `SHELL`, or
    /// rename [`ConnectorIntent::ShellIngress`]'s wire name, and this fails.
    #[test]
    fn shell_ingress_is_one_deliberate_intent() {
        assert_eq!(
            ConnectorIntent::ALL.len(),
            4,
            "four intents, and shell is deliberately one of them"
        );
        let shells: Vec<ConnectorIntent> = ConnectorIntent::ALL
            .into_iter()
            .filter(|intent| intent.wire_name().contains("SHELL"))
            .collect();
        assert_eq!(
            shells,
            vec![ConnectorIntent::ShellIngress],
            "exactly one intent names the shell, and it is the one that says so"
        );
        assert_eq!(
            ConnectorIntent::ShellIngress.arn(&Region::UsEast1),
            "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:SHELL_INGRESS"
        );
    }

    /// `HTTP_INGRESS`, the measured literal — the finer-grained ingress that pairs with
    /// the shell connector, since `ALL_INGRESS` cannot combine with either
    /// (`docs/PLATFORM.md`, measured 2026-08-15).
    #[test]
    fn the_http_ingress_arn_is_the_measured_literal() {
        assert_eq!(
            ConnectorIntent::HttpIngress.arn(&Region::UsEast1),
            "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:HTTP_INGRESS"
        );
    }

    /// No two intents render the same ARN. The string-replace shape this replaced
    /// could produce two identical ARNs if the names ever became substrings of one
    /// another, and that is the failure this asserts against — now across the whole
    /// set rather than the original pair.
    #[test]
    fn no_two_intents_render_the_same_arn() {
        let arns: Vec<String> = ConnectorIntent::ALL
            .iter()
            .map(|intent| intent.arn(&Region::EuWest1))
            .collect();
        for (i, a) in arns.iter().enumerate() {
            for b in &arns[i + 1..] {
                assert_ne!(a, b);
            }
        }
        assert!(!arns[0].contains("INTERNET"), "{}", arns[0]);
    }

    /// Every derived ARN clears the model's `NetworkConnector` bounds (min 1, max 2048),
    /// including for the longest region name. The model states no pattern, so length is
    /// the only constraint there is to check — and it is checked here rather than
    /// trusted because a 2048-character ARN would be rejected on the wire with the same
    /// "malformed" message a bare name gets.
    #[test]
    fn every_derived_arn_fits_the_models_connector_bounds() {
        for region in crate::region::MICROVM_REGIONS {
            for intent in ConnectorIntent::ALL {
                let arn = intent.arn(&region);
                assert!((1..=2048).contains(&arn.len()), "{arn}");
                assert!(arn.starts_with("arn:aws:lambda:"), "{arn}");
            }
        }
    }
}
