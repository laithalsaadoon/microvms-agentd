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
    if !is_connector_arn_in(arn, region.as_str()) {
        return Err(connector_arn_error(region.as_str()));
    }
    Ok(())
}

/// The refusal for a string that is not a customer-managed connector ARN in `region`.
fn connector_arn_error(region: &str) -> crate::Error {
    crate::Error::invalid_arg(format!(
        "egressNetworkConnectors requires a customer-managed Lambda network connector ARN \
         in {region} (arn:aws:lambda:{region}:<12-digit-account>:network-connector:<id>[:<version>]); \
         create the VPC egress connector through lambda-core first",
    ))
}

/// Whether `arn` is a customer-managed connector ARN in the region spelled `region`.
fn is_connector_arn_in(arn: &str, region: &str) -> bool {
    let parts: Vec<&str> = arn.splitn(6, ':').collect();
    parts.len() == 6
        && (1..=crate::constants::MAX_NETWORK_CONNECTOR_LEN).contains(&arn.len())
        && parts[0] == "arn"
        && parts[1] == "aws"
        && parts[2] == "lambda"
        && parts[3] == region
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
            })
}

/// The refusal for managed internet egress beside customer-managed connectors.
pub(crate) fn egress_with_connectors() -> crate::Error {
    crate::Error::invalid_arg(
        "INTERNET_EGRESS cannot be combined with customer-managed egress connectors: \
         choose managed internet egress or VPC routing",
    )
}

/// The refusal for managed internet egress beside the advisory deny.
pub(crate) fn egress_with_deny() -> crate::Error {
    crate::Error::invalid_arg(
        "egress and deny_egress ask for opposite things: egress puts the \
         INTERNET_EGRESS connector on the launch, and deny_egress sets the guest's \
         proxy variables to a black hole so a well-behaved client refuses to leave \
         the VM. Pick one. Neither seals the VM — omitting the connector is measured \
         not to (docs/PLATFORM.md), and the deny is advisory.",
    )
}

/// The refusal for a `NetworkConnectorList` member over its ceiling, or `None` within it.
pub(crate) fn over_connector_ceiling(member: &str, count: usize) -> Option<crate::Error> {
    (count > crate::constants::MAX_NETWORK_CONNECTORS).then(|| {
        crate::Error::invalid_arg(format!(
            "{member} has {count} network connectors, over the NetworkConnectorList \
             ceiling of {} (service model {}).",
            crate::constants::MAX_NETWORK_CONNECTORS,
            crate::constants::MODEL_API_VERSION,
        ))
    })
}

/// The egress posture a launch with these options reports, or the refusal it raises
/// (BIND-11, BIND-13). Pure: no AWS call, no credentials.
///
/// A harness decides with this, before it pays for a build, whether a task that must not
/// reach the network can run: only [`EgressPosture::Sealed`] is internet isolation, and this
/// never answers it, because no launch option carries the evidence `sealed` needs (a VPC
/// egress connector **and** separately verified VPC routing without an internet gateway or
/// NAT gateway, `docs/NETWORKING.md`). [`crate::sandbox::Sandbox::run`] refuses and derives
/// through this same function, and the session it builds carries the answer
/// ([`crate::session::Session::egress_posture`]), which is also what the CLI envelope's
/// `egressPosture` reports.
///
/// The refusals, in the order a launch raises them: `egress` with `deny_egress`, `egress`
/// with any connector, a connector that is not a customer-managed connector ARN in `region`,
/// and more connectors than `NetworkConnectorList` allows. Without a `region`, each ARN is
/// checked against the region it names; a launch also requires that region to be its own.
pub fn egress_posture_for(
    egress: bool,
    egress_network_connectors: &[String],
    deny_egress: bool,
    region: Option<&Region>,
) -> Result<EgressPosture, crate::Error> {
    if egress && deny_egress {
        return Err(egress_with_deny());
    }
    if egress && !egress_network_connectors.is_empty() {
        return Err(egress_with_connectors());
    }
    for arn in egress_network_connectors {
        match region {
            Some(region) => require_egress_connector_arn(arn, region)?,
            None => {
                // The region the ARN names, when it names a plausible one.
                let named = arn.split(':').nth(3).unwrap_or_default();
                let plausible = !named.is_empty()
                    && named.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'
                    });
                if !(plausible && is_connector_arn_in(arn, named)) {
                    return Err(connector_arn_error(if plausible {
                        named
                    } else {
                        "<region>"
                    }));
                }
            }
        }
    }
    if let Some(refusal) =
        over_connector_ceiling("egressNetworkConnectors", egress_network_connectors.len())
    {
        return Err(refusal);
    }
    Ok(EgressPosture::for_launch(egress, deny_egress))
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

    /// A connector ARN in `region`.
    fn vpc(region: &str, index: usize) -> String {
        format!("arn:aws:lambda:{region}:123456789012:network-connector:vpc-{index}")
    }

    /// **BIND-11 and BIND-13: the decision table.** The rows of `TABLE` in
    /// `model/src/posture.rs`, at the wire ceiling of 10 connectors, each with the refusal's
    /// words. A refusal is the launch's own message (the bdd scenarios and the fuzz harness
    /// compare them against `Sandbox::run`).
    ///
    /// **Falsification** — 2026-09-24. Make `egress_posture_for` answer `Sealed` for a
    /// connector-bearing request and the `(false, 1, ..)` rows go red; drop the ARN check and
    /// the malformed rows answer `unsealed`. Both run and restored.
    #[test]
    fn the_posture_of_launch_options_is_the_models_table() {
        use EgressPosture::{BestEffort, Open, Unsealed};
        /// `(egress, connectors, malformed, deny, answer or the refusal's words)`.
        type Row = (bool, usize, bool, bool, Result<EgressPosture, &'static str>);
        let region = Region::UsEast1;
        let malformed = "network-connector:vpc-0".to_string();
        let table: [Row; 14] = [
            (false, 0, false, false, Ok(Unsealed)),
            (false, 0, false, true, Ok(BestEffort)),
            (false, 1, false, false, Ok(Unsealed)),
            (false, 1, false, true, Ok(BestEffort)),
            (false, 10, false, false, Ok(Unsealed)),
            (false, 1, true, false, Err("network connector ARN")),
            (false, 11, false, false, Err("NetworkConnectorList ceiling")),
            (false, 11, false, true, Err("NetworkConnectorList ceiling")),
            (false, 11, true, false, Err("network connector ARN")),
            (true, 0, false, false, Ok(Open)),
            (true, 0, false, true, Err("opposite things")),
            (
                true,
                1,
                false,
                false,
                Err("INTERNET_EGRESS cannot be combined"),
            ),
            (true, 1, false, true, Err("opposite things")),
            (
                true,
                1,
                true,
                false,
                Err("INTERNET_EGRESS cannot be combined"),
            ),
        ];
        for (egress, count, bad, deny, expected) in table {
            let mut connectors: Vec<String> = (0..count).map(|i| vpc("us-east-1", i)).collect();
            if bad {
                connectors[0] = malformed.clone();
            }
            let answer = egress_posture_for(egress, &connectors, deny, Some(&region));
            let row = format!("egress={egress} connectors={count} malformed={bad} deny={deny}");
            match (answer, expected) {
                (Ok(posture), Ok(want)) => assert_eq!(posture, want, "{row}"),
                (Err(error), Err(needle)) => {
                    assert_eq!(error.kind(), crate::ErrorKind::InvalidArg, "{row}: {error}");
                    assert!(error.to_string().contains(needle), "{row}: {error}");
                }
                (answer, expected) => panic!("{row}: {answer:?}, expected {expected:?}"),
            }
        }
    }

    /// **BIND-11: no launch option is a seal.** The whole option space at and past the ceiling.
    #[test]
    fn no_launch_options_answer_sealed() {
        for egress in [false, true] {
            for deny in [false, true] {
                for count in 0..=crate::constants::MAX_NETWORK_CONNECTORS + 1 {
                    let connectors: Vec<String> = (0..count).map(|i| vpc("us-east-1", i)).collect();
                    if let Ok(posture) =
                        egress_posture_for(egress, &connectors, deny, Some(&Region::UsEast1))
                    {
                        assert!(!posture.is_sealed(), "{egress} {count} {deny}");
                    }
                }
            }
        }
    }

    /// **BIND-13: without a region, each ARN is checked against the region it names**; with
    /// one, an ARN in another region is the launch's own refusal.
    #[test]
    fn a_connector_is_checked_against_the_launch_region_when_there_is_one() {
        let elsewhere = [vpc("eu-west-1", 0)];
        assert_eq!(
            egress_posture_for(false, &elsewhere, false, None).expect("a well-formed ARN"),
            EgressPosture::Unsealed
        );
        let error = egress_posture_for(false, &elsewhere, false, Some(&Region::UsEast1))
            .expect_err("an ARN from another region cannot attach to a us-east-1 launch");
        assert!(error.to_string().contains("in us-east-1"), "{error}");
        let error = egress_posture_for(false, &["not-an-arn".into()], false, None)
            .expect_err("a string that is no ARN names no region either");
        assert_eq!(error.kind(), crate::ErrorKind::InvalidArg);
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
