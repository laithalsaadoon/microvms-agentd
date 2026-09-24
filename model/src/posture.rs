// SPDX-License-Identifier: Apache-2.0
//! A checked model of the egress posture a launch reports, and of who may report it.
//!
//! It specifies BIND-11, BIND-12, and BIND-13 in `spec/core.symspec.json`, from issue #227: a
//! Harbor provider advertised `disable_internet` and mapped Harbor's `no-network` to a launch
//! without `--egress`, which the platform does not isolate (`docs/PLATFORM.md`, "A VM launched
//! without the egress connector still has outbound network", measured 2026-09-11 and
//! 2026-09-12). The CLI already reported the posture in its envelope; the bindings did not, so
//! a harness could not read it before or after a launch.
//!
//! # What the evidence is, and who holds it
//!
//! A posture is a claim about a VM's outbound network, and each of the four claims needs
//! different evidence (`docs/NETWORKING.md`):
//!
//! * `open`: the managed `INTERNET_EGRESS` connector is on the request. Claims no isolation.
//! * `unsealed`: isolation is unverified. True of every VM a client can describe, so it is the
//!   weakest true claim and what a process reports when it does not hold the launch options.
//! * `best-effort`: the advisory proxy deny is in the launch environment. A workload can
//!   ignore it, so it claims an advisory deny and nothing more.
//! * `sealed`: internet isolation. It needs a customer-managed VPC egress connector **and**
//!   separately verified routing in that VPC, with no internet gateway or NAT gateway.
//!
//! The second half of `sealed` is a fact about the customer's VPC, and no launch option
//! carries it. The model keeps it as [`State::routing_verified`], set by an operator audit
//! outside the client, so a state with sealed-grade evidence exists and the checker can
//! show that the client never over-claims in it. The specified client derives the posture
//! from the launch options alone ([`specified`]) and therefore never reports `sealed`.
//! [`Behavior::EvidenceAware`] is the hypothetical classifier that also reads the audit, and
//! the tests show it reaches `sealed` exactly under those conditions and nowhere else.
//!
//! # The specification is one pure function
//!
//! [`specified`] is the decision table: a posture, or the refusal a launch would raise. The
//! core function `microvms_core::control::egress_posture_for` mirrors it, and its unit test
//! carries the same table. The rejected behaviors are other functions of the same shape:
//!
//! * [`Behavior::Harvester`] reports a connector-less launch as `sealed` (the #227 defect).
//! * [`Behavior::ConnectorIsSeal`] reports any VPC connector as `sealed` without the audit.
//! * [`Behavior::DenyIsSeal`] reports the advisory deny as `sealed`.
//! * [`Behavior::BindingRederives`] has the binding derive the session posture itself, from
//!   `egress` alone, so it disagrees with the CLI envelope on a denied launch.
//! * [`Behavior::PredictSkipsRefusal`] answers a request the launch would refuse.
//! * [`Behavior::AdoptAssumesOpen`] has an adopting process report `open` for a VM whose
//!   options it never saw.

use stateright::{Model, Property};

/// The four postures, in the wire spelling's order.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Posture {
    Open,
    Unsealed,
    BestEffort,
    Sealed,
}

/// The isolation a posture claims. `open` and `unsealed` claim none.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Claim {
    None,
    /// An advisory in-guest deny that a workload can bypass.
    Advisory,
    /// Internet isolation.
    Isolated,
}

impl Posture {
    pub fn claim(self) -> Claim {
        match self {
            Posture::Open | Posture::Unsealed => Claim::None,
            Posture::BestEffort => Claim::Advisory,
            Posture::Sealed => Claim::Isolated,
        }
    }
}

/// Why a launch with these options is refused before any AWS call.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Refusal {
    /// Managed egress and the advisory deny ask for opposite things (core `Sandbox::run`).
    EgressWithDeny,
    /// Managed egress bypasses a VPC connector (core `ControlPlane::run_microvm`).
    EgressWithConnectors,
    /// A connector is not a customer-managed connector ARN in the launch region.
    MalformedConnector,
    /// More connectors than `NetworkConnectorList` allows.
    TooManyConnectors,
}

/// The launch options that decide the posture.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Options {
    pub egress: bool,
    /// Connector ARNs on the request, well-formed or not.
    pub connectors: u8,
    /// One of them is not a connector ARN in the launch region.
    pub malformed: bool,
    pub deny: bool,
}

/// **The specification** (BIND-11, BIND-13): the posture a launch with `options` reports, or
/// the refusal it raises. `max` is the connector ceiling (10 on the wire; smaller here).
///
/// The refusals are checked in the order core checks them. The posture reads only the
/// options, so it is never `sealed`: no option carries the VPC routing audit.
pub fn specified(options: Options, max: u8) -> Result<Posture, Refusal> {
    if options.egress && options.deny {
        return Err(Refusal::EgressWithDeny);
    }
    if options.egress && options.connectors > 0 {
        return Err(Refusal::EgressWithConnectors);
    }
    if options.malformed {
        return Err(Refusal::MalformedConnector);
    }
    if options.connectors > max {
        return Err(Refusal::TooManyConnectors);
    }
    Ok(if options.egress {
        Posture::Open
    } else if options.deny {
        Posture::BestEffort
    } else {
        Posture::Unsealed
    })
}

/// Whether the evidence about a VM supports the isolation `claim`.
///
/// `routing_verified` is the operator's audit of the VPC; see the module docs.
pub fn supports(options: Options, routing_verified: bool, claim: Claim) -> bool {
    match claim {
        Claim::None => true,
        Claim::Advisory => options.deny,
        Claim::Isolated => !options.egress && options.connectors > 0 && routing_verified,
    }
}

/// Which behavior the model runs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// [`specified`] everywhere.
    Specified,
    /// A hypothetical classifier that also reads the routing audit.
    EvidenceAware,
    /// "No `--egress` means no network": the provider in issue #227.
    Harvester,
    /// A VPC connector read as a seal, with no audit.
    ConnectorIsSeal,
    /// The advisory deny read as a seal.
    DenyIsSeal,
    /// The binding derives the session posture from `egress` alone.
    BindingRederives,
    /// The request-side answer ignores the connector refusal.
    PredictSkipsRefusal,
    /// An adopting process assumes the VM is `open`.
    AdoptAssumesOpen,
}

impl Behavior {
    /// The request-side answer (`egress_posture_for`).
    fn predict(
        self,
        options: Options,
        routing_verified: bool,
        max: u8,
    ) -> Result<Posture, Refusal> {
        let base = specified(options, max);
        match self {
            Behavior::Specified | Behavior::BindingRederives | Behavior::AdoptAssumesOpen => base,
            Behavior::EvidenceAware => base.map(|posture| {
                if supports(options, routing_verified, Claim::Isolated) {
                    Posture::Sealed
                } else {
                    posture
                }
            }),
            Behavior::Harvester => base.map(|posture| {
                if options.egress {
                    posture
                } else {
                    Posture::Sealed
                }
            }),
            Behavior::ConnectorIsSeal => base.map(|posture| {
                if options.connectors > 0 {
                    Posture::Sealed
                } else {
                    posture
                }
            }),
            Behavior::DenyIsSeal => base.map(|posture| {
                if options.deny {
                    Posture::Sealed
                } else {
                    posture
                }
            }),
            Behavior::PredictSkipsRefusal => match base {
                Err(Refusal::EgressWithConnectors) => Ok(Posture::Open),
                other => other,
            },
        }
    }

    /// The posture the launched session reports, given the launch accepted `options`.
    fn session(self, options: Options, routing_verified: bool, max: u8) -> Posture {
        match self {
            Behavior::BindingRederives => {
                if options.egress {
                    Posture::Open
                } else {
                    Posture::Unsealed
                }
            }
            // The launch refuses exactly what `specified` refuses, so a skipped refusal in the
            // request-side answer does not change what an accepted launch reports.
            Behavior::PredictSkipsRefusal => specified(options, max).unwrap_or(Posture::Unsealed),
            other => other
                .predict(options, routing_verified, max)
                .unwrap_or(Posture::Unsealed),
        }
    }

    /// What a process that adopts the VM, without its launch options, reports.
    fn adopted(self) -> Posture {
        match self {
            Behavior::AdoptAssumesOpen => Posture::Open,
            _ => Posture::Unsealed,
        }
    }
}

/// The model's knobs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Config {
    pub behavior: Behavior,
    /// The connector ceiling. One is the least that makes "over the ceiling" reachable.
    pub max_connectors: u8,
}

impl Config {
    pub fn with(behavior: Behavior) -> Self {
        Self {
            behavior,
            max_connectors: 1,
        }
    }
}

/// Where the launch is.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Phase {
    /// The harness is still choosing options.
    Composing,
    /// The launch was refused locally.
    Refused(Refusal),
    /// The launch was accepted and a session exists.
    Launched,
}

/// One harness composing a launch, maybe asking for its posture first, then launching, and
/// maybe handing the VM to a second process.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    pub options: Options,
    /// The operator's audit of the connector's VPC: no internet gateway, no NAT gateway.
    pub routing_verified: bool,
    /// The request-side answer, when the harness asked before launching, with the options
    /// and audit it answered for: the harness may change its options after asking.
    pub predicted: Option<Result<Posture, Refusal>>,
    pub predicted_for: Option<(Options, bool)>,
    pub phase: Phase,
    /// What the CLI envelope reports for the same options (the reference, BIND-12).
    pub envelope: Option<Posture>,
    /// What the launched session reports.
    pub session: Option<Posture>,
    /// What a second process that adopted the VM reports.
    pub adopted: Option<Posture>,
    /// Control-plane calls made.
    pub aws_calls: u8,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    RequestEgress,
    AddConnector,
    /// A connector string that is not a connector ARN in the launch region.
    AddMalformedConnector,
    RequestDeny,
    /// The operator audits the connector's VPC routing. Outside the client.
    AuditRouting,
    /// `egress_posture_for` on the current options.
    Predict,
    Launch,
    /// A second process adopts the launched VM.
    Adopt,
}

/// The model.
#[derive(Clone, Debug)]
pub struct PostureModel {
    pub cfg: Config,
}

impl PostureModel {
    pub fn new(cfg: Config) -> Self {
        Self { cfg }
    }
}

/// No launch options: what a process that adopted a VM holds.
pub const NO_OPTIONS: Options = Options {
    egress: false,
    connectors: 0,
    malformed: false,
    deny: false,
};

/// Every reported posture in `state`, with the evidence its reporter holds: the options and
/// audit a prediction answered for, the launch's own for the session, none for an adopter.
fn reports(state: &State) -> impl Iterator<Item = (Posture, Options, bool)> {
    let predicted = match (state.predicted, state.predicted_for) {
        (Some(Ok(posture)), Some((options, audited))) => Some((posture, options, audited)),
        _ => None,
    };
    [
        predicted,
        state
            .session
            .map(|posture| (posture, state.options, state.routing_verified)),
        state
            .adopted
            .map(|posture| (posture, NO_OPTIONS, state.routing_verified)),
    ]
    .into_iter()
    .flatten()
}

impl Model for PostureModel {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        vec![State {
            options: NO_OPTIONS,
            routing_verified: false,
            predicted: None,
            predicted_for: None,
            phase: Phase::Composing,
            envelope: None,
            session: None,
            adopted: None,
            aws_calls: 0,
        }]
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        match state.phase {
            Phase::Composing => {
                if !state.options.egress {
                    actions.push(Action::RequestEgress);
                }
                if state.options.connectors <= self.cfg.max_connectors {
                    actions.push(Action::AddConnector);
                    if !state.options.malformed {
                        actions.push(Action::AddMalformedConnector);
                    }
                }
                if !state.options.deny {
                    actions.push(Action::RequestDeny);
                }
                if state.options.connectors > 0 && !state.routing_verified {
                    actions.push(Action::AuditRouting);
                }
                if state.predicted.is_none() {
                    actions.push(Action::Predict);
                }
                actions.push(Action::Launch);
            }
            Phase::Launched if state.adopted.is_none() => actions.push(Action::Adopt),
            Phase::Launched | Phase::Refused(_) => {}
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = *last;
        let behavior = self.cfg.behavior;
        let max = self.cfg.max_connectors;
        match action {
            Action::RequestEgress => next.options.egress = true,
            Action::AddConnector => next.options.connectors += 1,
            Action::AddMalformedConnector => {
                next.options.connectors += 1;
                next.options.malformed = true;
            }
            Action::RequestDeny => next.options.deny = true,
            Action::AuditRouting => next.routing_verified = true,
            Action::Predict => {
                next.predicted = Some(behavior.predict(last.options, last.routing_verified, max));
                next.predicted_for = Some((last.options, last.routing_verified));
            }
            Action::Launch => match specified(last.options, max) {
                Err(refusal) => next.phase = Phase::Refused(refusal),
                Ok(posture) => {
                    next.phase = Phase::Launched;
                    next.aws_calls += 1;
                    next.envelope = Some(posture);
                    next.session = Some(behavior.session(last.options, last.routing_verified, max));
                }
            },
            Action::Adopt => next.adopted = Some(behavior.adopted()),
        }
        (next != *last).then_some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        let mut properties = vec![
            // ── BIND-11 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-11 a reported posture never claims more isolation than its evidence supports",
                |_, s| {
                    reports(s).all(|(posture, options, audited)| {
                        supports(options, audited, posture.claim())
                    })
                },
            ),
            Property::<Self>::always(
                "BIND-11 sealed needs a VPC connector and verified routing",
                |_, s| {
                    reports(s).all(|(posture, options, audited)| {
                        posture != Posture::Sealed
                            || (!options.egress && options.connectors > 0 && audited)
                    })
                },
            ),
            Property::<Self>::sometimes("BIND-11 witness: a session reports open", |_, s| {
                s.session == Some(Posture::Open)
            }),
            Property::<Self>::sometimes("BIND-11 witness: a session reports unsealed", |_, s| {
                s.session == Some(Posture::Unsealed) && s.options.connectors > 0
            }),
            Property::<Self>::sometimes(
                "BIND-11 witness: a session reports best-effort",
                |_, s| s.session == Some(Posture::BestEffort),
            ),
            Property::<Self>::sometimes(
                "BIND-11 witness: a launch carries sealed-grade evidence",
                |_, s| {
                    s.phase == Phase::Launched
                        && supports(s.options, s.routing_verified, Claim::Isolated)
                },
            ),
            // ── BIND-12 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-12 a session reports what the CLI envelope reports for its options",
                |m, s| m.cfg.behavior == Behavior::EvidenceAware || s.session == s.envelope,
            ),
            Property::<Self>::always(
                "BIND-12 a session without its launch options reports unsealed",
                |_, s| s.adopted.is_none_or(|posture| posture == Posture::Unsealed),
            ),
            Property::<Self>::sometimes(
                "BIND-12 witness: an open VM is adopted by a second process",
                |_, s| s.session == Some(Posture::Open) && s.adopted.is_some(),
            ),
            // ── BIND-13 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-13 the request-side answer is what the launch does",
                |_, s| {
                    if s.predicted_for != Some((s.options, s.routing_verified)) {
                        return true;
                    }
                    match (s.predicted, s.phase) {
                        (Some(Err(refusal)), Phase::Refused(got)) => refusal == got,
                        (Some(Ok(posture)), Phase::Launched) => s.session == Some(posture),
                        (_, Phase::Composing) | (None, _) => true,
                        (Some(Err(_)), Phase::Launched) | (Some(Ok(_)), Phase::Refused(_)) => false,
                    }
                },
            ),
            Property::<Self>::always("BIND-13 a refused launch makes no AWS call", |_, s| {
                !matches!(s.phase, Phase::Refused(_)) || s.aws_calls == 0
            }),
            Property::<Self>::always(
                "BIND-13 asking for the posture makes no AWS call",
                |_, s| s.phase != Phase::Composing || s.aws_calls == 0,
            ),
            Property::<Self>::sometimes(
                "BIND-13 witness: egress with the deny is refused",
                |_, s| s.phase == Phase::Refused(Refusal::EgressWithDeny),
            ),
            Property::<Self>::sometimes(
                "BIND-13 witness: egress with a VPC connector is refused",
                |_, s| s.phase == Phase::Refused(Refusal::EgressWithConnectors),
            ),
            Property::<Self>::sometimes(
                "BIND-13 witness: a malformed connector is refused",
                |_, s| s.phase == Phase::Refused(Refusal::MalformedConnector),
            ),
            Property::<Self>::sometimes(
                "BIND-13 witness: too many connectors are refused",
                |_, s| s.phase == Phase::Refused(Refusal::TooManyConnectors),
            ),
            Property::<Self>::sometimes(
                "BIND-13 witness: a harness asks, then launches",
                |_, s| matches!(s.predicted, Some(Ok(_))) && s.phase == Phase::Launched,
            ),
        ];
        if self.cfg.behavior == Behavior::EvidenceAware {
            properties.push(Property::<Self>::sometimes(
                "BIND-11 witness: a session reports sealed",
                |_, s| s.session == Some(Posture::Sealed),
            ));
        }
        properties
    }
}

/// `NetworkConnectorList`'s ceiling on the wire (`MAX_NETWORK_CONNECTORS` in core).
pub const WIRE_CEILING: u8 = 10;

/// One row of [`TABLE`]: `(egress, connectors, malformed, deny, answer)`.
pub type Row = (bool, u8, bool, bool, Result<Posture, Refusal>);

/// The decision table at [`WIRE_CEILING`].
///
/// `microvms_core::control::egress_posture_for`'s unit test carries the same rows, so the
/// model and the implementation are checked against one statement.
pub const TABLE: [Row; 14] = [
    (false, 0, false, false, Ok(Posture::Unsealed)),
    (false, 0, false, true, Ok(Posture::BestEffort)),
    (false, 1, false, false, Ok(Posture::Unsealed)),
    (false, 1, false, true, Ok(Posture::BestEffort)),
    (false, 10, false, false, Ok(Posture::Unsealed)),
    (false, 1, true, false, Err(Refusal::MalformedConnector)),
    (false, 11, false, false, Err(Refusal::TooManyConnectors)),
    (false, 11, false, true, Err(Refusal::TooManyConnectors)),
    (false, 11, true, false, Err(Refusal::MalformedConnector)),
    (true, 0, false, false, Ok(Posture::Open)),
    (true, 0, false, true, Err(Refusal::EgressWithDeny)),
    (true, 1, false, false, Err(Refusal::EgressWithConnectors)),
    (true, 1, false, true, Err(Refusal::EgressWithDeny)),
    (true, 1, true, false, Err(Refusal::EgressWithConnectors)),
];

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<PostureModel> {
        PostureModel::new(Config::with(behavior))
            .checker()
            .spawn_bfs()
            .join()
    }

    /// The headline: the specified client satisfies BIND-11, BIND-12, and BIND-13 over every
    /// order of composing, asking, launching, and adopting, and witnesses every case.
    #[test]
    fn the_specified_client_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 50,
            "a space this small could not reach the orderings: {}",
            checker.unique_state_count()
        );
    }

    /// **Sealed is reachable exactly under the repo's conditions.** A classifier that also
    /// reads the VPC routing audit reports `sealed`, and never over-claims doing so. It is not
    /// what the client does: no launch option carries the audit.
    #[test]
    fn sealed_needs_the_audit_a_launch_option_cannot_carry() {
        let checker = checked(Behavior::EvidenceAware);
        checker.assert_properties();
        let last = *checker
            .assert_any_discovery("BIND-11 witness: a session reports sealed")
            .last_state();
        assert!(
            last.options.connectors > 0 && last.routing_verified && !last.options.egress,
            "sealed needs a connector and the audit, got {last:?}"
        );
    }

    /// Applies `actions` from the initial state.
    fn run(behavior: Behavior, actions: &[Action]) -> State {
        let model = PostureModel::new(Config::with(behavior));
        actions
            .iter()
            .fold(model.init_states()[0], |state, action| {
                model
                    .next_state(&state, *action)
                    .expect("each step changes the state")
            })
    }

    /// **#227 in the model.** "No `--egress`" reported as a seal over-claims on the default
    /// launch, with no connector and no audit.
    #[test]
    fn the_harvester_reading_claims_isolation_on_a_default_launch() {
        let checker = checked(Behavior::Harvester);
        checker.assert_any_discovery(
            "BIND-11 a reported posture never claims more isolation than its evidence supports",
        );
        let launched = run(Behavior::Harvester, &[Action::Launch]);
        assert_eq!(launched.session, Some(Posture::Sealed));
        assert!(!supports(launched.options, false, Claim::Isolated));
    }

    /// A VPC connector without the routing audit is not a seal.
    #[test]
    fn a_connector_alone_is_not_a_seal() {
        let checker = checked(Behavior::ConnectorIsSeal);
        checker.assert_any_discovery("BIND-11 sealed needs a VPC connector and verified routing");
        let launched = run(
            Behavior::ConnectorIsSeal,
            &[Action::AddConnector, Action::Launch],
        );
        assert_eq!(launched.session, Some(Posture::Sealed));
        assert!(!launched.routing_verified);
    }

    /// The advisory deny is not a seal.
    #[test]
    fn the_advisory_deny_is_not_a_seal() {
        let checker = checked(Behavior::DenyIsSeal);
        let steps = checker
            .assert_any_discovery(
                "BIND-11 a reported posture never claims more isolation than its evidence supports",
            )
            .into_actions();
        assert!(steps.contains(&Action::RequestDeny), "{steps:?}");
    }

    /// A binding that derives the posture itself under-claims, which BIND-11 allows, and
    /// disagrees with the CLI envelope, which BIND-12 does not.
    #[test]
    fn a_binding_that_rederives_the_posture_disagrees_with_the_envelope() {
        let checker = checked(Behavior::BindingRederives);
        checker.assert_no_discovery(
            "BIND-11 a reported posture never claims more isolation than its evidence supports",
        );
        let steps = checker
            .assert_any_discovery(
                "BIND-12 a session reports what the CLI envelope reports for its options",
            )
            .into_actions();
        assert!(steps.contains(&Action::RequestDeny), "{steps:?}");
    }

    /// An adopter that assumes `open` claims a connector it never saw on a request.
    #[test]
    fn an_adopter_reports_unsealed_not_what_it_assumes() {
        let checker = checked(Behavior::AdoptAssumesOpen);
        let steps = checker
            .assert_any_discovery("BIND-12 a session without its launch options reports unsealed")
            .into_actions();
        assert_eq!(steps.last(), Some(&Action::Adopt), "{steps:?}");
    }

    /// A request-side answer that skips a refusal promises a launch that never happens.
    #[test]
    fn a_prediction_that_skips_a_refusal_is_caught() {
        let checker = checked(Behavior::PredictSkipsRefusal);
        let steps = checker
            .assert_any_discovery("BIND-13 the request-side answer is what the launch does")
            .into_actions();
        assert!(
            steps.contains(&Action::RequestEgress) && steps.contains(&Action::AddConnector),
            "{steps:?}"
        );
    }

    /// [`TABLE`] is what [`specified`] answers at the wire ceiling, and nothing in the whole
    /// option space at that ceiling is `sealed`.
    #[test]
    fn the_specification_table() {
        for (egress, connectors, malformed, deny, expected) in TABLE {
            let options = Options {
                egress,
                connectors,
                malformed,
                deny,
            };
            assert_eq!(specified(options, WIRE_CEILING), expected, "{options:?}");
        }
        for egress in [false, true] {
            for connectors in 0..=WIRE_CEILING + 1 {
                for malformed in [false, true] {
                    for deny in [false, true] {
                        let options = Options {
                            egress,
                            connectors,
                            malformed: malformed && connectors > 0,
                            deny,
                        };
                        assert_ne!(
                            specified(options, WIRE_CEILING),
                            Ok(Posture::Sealed),
                            "{options:?}"
                        );
                    }
                }
            }
        }
    }
}
