// SPDX-License-Identifier: Apache-2.0
//! Network connectors: a closed intent enum, and the ARN each one derives (TRAP-4).
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

/// Whether the platform is measured to honour an **omitted** egress connector.
///
/// `false`, and that is a measurement rather than a guess: on 2026-09-11 (microvm 0.5.0,
/// issue #154), 2026-09-12 (0.7.0) and 2026-09-13, us-east-1, API version `2025-09-09`, a
/// VM launched with no `egressNetworkConnectors` member reached `example.com`,
/// `github.com`, `pypi.org` and `extensions.duckdb.org` exactly as a `--egress` VM did
/// (`docs/PLATFORM.md`, "A VM launched without the egress connector still has outbound
/// network").
///
/// This constant exists so that "no egress" has **one** spelling in the client. Every
/// posture label, every envelope key and every human line derives from
/// [`EgressPosture::for_launch`], so the day the platform starts honouring the omission
/// the repair is this one `bool` — not a search for the places that claimed a seal. The
/// live suite pins the measurement against this constant
/// (`conformance/run_rs.py`, `drive_platform_posture`), so a platform that starts sealing
/// goes red here rather than silently making the label pessimistic.
pub const PLATFORM_HONOURS_OMITTED_EGRESS: bool = false;

/// What a launch's outbound network actually is, as opposed to what was requested.
///
/// # Why a request flag is not an answer
///
/// `egress: false` says what the client asked for. It does not say what the VM got, and
/// for three measurement dates running those are different things: the omission is the
/// strongest request `RunMicrovm` accepts — the API's whole outbound surface is
/// `egressNetworkConnectors`, a list of connector ARNs, with no deny-all, VPC-only or
/// policy member to set (service model `2025-09-09`) — and the platform grants outbound
/// network anyway. A caller reading `egress: false` as "sealed" is the mistake this type
/// exists to make unwriteable: an external review reached `extensions.duckdb.org` from a
/// VM launched without `--egress` and installed a 242 MB DuckDB extension, which was
/// documented platform behaviour and read as a client defect, because the envelope said
/// `egress: false` and nothing said what that means.
///
/// So the client reports the posture, always, and names the weakest of the two claims it
/// can support.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum EgressPosture {
    /// `INTERNET_EGRESS` is on the request: the VM has outbound network by design.
    Open,
    /// No connector requested, and the platform does not honour the omission. The VM
    /// reaches the internet. **This is the posture of a default launch today.**
    Unsealed,
    /// No connector requested, plus the advisory in-guest deny
    /// ([`crate::sandbox::RunRequest::deny_egress`]). A well-behaved client refuses to
    /// leave the VM; a workload that ignores its environment does not.
    BestEffort,
    /// No connector requested, and the platform honours the omission: no outbound path.
    ///
    /// Unreachable while [`PLATFORM_HONOURS_OMITTED_EGRESS`] is `false`, and reachable by
    /// flipping that one constant when a re-measurement earns it.
    Sealed,
}

impl EgressPosture {
    /// The posture of a launch that requested `egress` and asked for `deny_egress`.
    ///
    /// A platform seal outranks the advisory deny, because it is the stronger claim of the
    /// two and the advisory one is then redundant.
    pub fn for_launch(egress: bool, deny_egress: bool) -> Self {
        match (egress, PLATFORM_HONOURS_OMITTED_EGRESS, deny_egress) {
            (true, _, _) => EgressPosture::Open,
            (false, true, _) => EgressPosture::Sealed,
            (false, false, true) => EgressPosture::BestEffort,
            (false, false, false) => EgressPosture::Unsealed,
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
                "no egress connector was requested, and the platform does not honour the \
                 omission: the VM still reaches the internet (measured 2026-09-11, \
                 2026-09-12 and 2026-09-13, us-east-1; docs/PLATFORM.md). Not a seal — \
                 size the execution role accordingly (docs/TRUST.md)"
            }
            EgressPosture::BestEffort => {
                "no egress connector, plus the advisory proxy deny in the launch \
                 environment: a well-behaved client (curl, uv, pip, npm) refuses to leave \
                 the VM, and a workload that ignores its environment reaches the internet \
                 anyway. Not a seal"
            }
            EgressPosture::Sealed => {
                "no egress connector, and the platform honours the omission: the VM has no \
                 outbound path"
            }
        }
    }
}

/// The posture of a launch that asks for nothing, which is the honest default in both
/// directions: `Sealed` would be the defect this type exists to prevent, and `Open` would
/// overclaim what a connector-less launch was given.
///
/// **Derived through [`EgressPosture::for_launch`] rather than named.** A `#[default]`
/// attribute on `Unsealed` was the first spelling and it was wrong: it hardcodes the answer
/// past [`PLATFORM_HONOURS_OMITTED_EGRESS`], so the day that constant flips, a default
/// posture would keep saying `unsealed` while every derived one said `sealed` — the
/// one-constant repair this type is built around, broken by its own `Default`.
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

/// The Lambda-managed connectors this client will name, and no others.
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

    /// **The posture of a default launch is `unsealed`, never `sealed`.**
    ///
    /// The label a caller reads for `egress: false` is the whole finding: an omitted
    /// connector is the strongest request the API accepts and the platform grants outbound
    /// network anyway, so the client must not spell that state as a seal.
    ///
    /// **Falsification** — flip [`PLATFORM_HONOURS_OMITTED_EGRESS`] to `true` and this
    /// test fails on the first assertion (it reads `sealed`), which is exactly the
    /// re-measurement gate the constant is for. Done on 2026-09-13, seen red, restored.
    #[test]
    fn a_launch_with_no_connector_is_unsealed_and_not_sealed() {
        let posture = EgressPosture::for_launch(false, false);
        assert_eq!(posture, EgressPosture::Unsealed);
        assert_eq!(posture.as_str(), "unsealed");
        assert!(
            !posture.is_sealed(),
            "omitting the connector does not seal the VM; it is measured not to"
        );
        // The constant itself is not asserted here: clippy refuses an assertion on a
        // constant, and the three assertions above already fail when it flips, which is the
        // behaviour that matters rather than the value.
    }

    /// The other three postures, and the two rules that order them: `--egress` is `open`
    /// whatever else was asked for, and a platform seal outranks the advisory deny.
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

    /// **`Default` derives through `for_launch`, so one constant still decides everything.**
    ///
    /// The first spelling of this was `#[default] Unsealed`, which hardcodes the answer past
    /// [`PLATFORM_HONOURS_OMITTED_EGRESS`] — flip the constant and a defaulted posture keeps
    /// saying `unsealed` while every derived one says `sealed`. Caught in review of the
    /// change that introduced it.
    ///
    /// **Falsification** — 2026-09-13. Restore `#[derive(Default)]` with `#[default]` on
    /// `Unsealed`, flip the constant to `true`, and this test fails while the label tests
    /// still pass. With the manual impl, the same flip keeps them equal.
    #[test]
    fn the_default_posture_is_the_derived_one() {
        assert_eq!(
            EgressPosture::default(),
            EgressPosture::for_launch(false, false),
            "a defaulted posture must be the posture of a launch that asks for nothing, \
             derived from the same constant"
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
