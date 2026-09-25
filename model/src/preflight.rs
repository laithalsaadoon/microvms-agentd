// SPDX-License-Identifier: Apache-2.0
//! A checked model of `preflight`: three checks, one report, and the calls it may make.
//!
//! It specifies BIND-15 and BIND-16 in `spec/core.symspec.json`, from issue #223. A harness
//! runs a preflight before queueing work, so the report has to be right in both directions:
//! `ok` must not be true while the service is unreachable (the work would fail later, after a
//! build), and a preflight must not itself spend money or change the account.
//!
//! # The world and the procedure
//!
//! The world is chosen once, in the initial state: whether the region resolves (to a supported
//! region, to one the caller opted into with `Region::unlisted`, or not at all), whether the
//! credential chain yields credentials, and how the service answers a listing. The procedure
//! then runs three checks in order and reports:
//!
//! 1. **region**: fatal when it does not resolve, advisory when it is unlisted (the reason is
//!    `doctor`'s: AWS adds regions faster than a constant is re-read, and a caller who wrote
//!    `Region::unlisted` opted in at the call site).
//! 2. **credentials**: the chain resolves credentials. No AWS API call; resolving may reach
//!    an SSO or metadata endpoint, which is free.
//! 3. **service**: one `ListManagedMicrovmImages`, free and read-only, in that region. An
//!    unsupported region answers `AccessDeniedException` with a null message, which is the
//!    check that catches an unlisted region that does not run MicroVMs.
//!
//! A check whose precondition failed is reported as not run, never skipped silently, and not
//! run means no call. [`specified`] is the procedure; the rejected ones are:
//!
//! * [`Behavior::AdvisoryService`], `doctor`'s treatment of its managed-base listing applied
//!   here: a failed listing is advisory, so `ok` is true while the service is unreachable.
//! * [`Behavior::ProbeByLaunch`], reachability tested with a launch, which bills.
//! * [`Behavior::ServiceWithoutCredentials`], the listing sent after credentials failed.
//! * [`Behavior::StopAtFirstFailure`], a report that omits the checks after a failure.

use stateright::{Model, Property};

/// How the region resolved.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum RegionWorld {
    Supported,
    /// `Region::unlisted`: opted into at the call site.
    Unlisted,
    /// An environment variable names a region the client refuses to parse.
    Unresolvable,
}

/// How the service answers `ListManagedMicrovmImages`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ServiceWorld {
    Answers,
    /// `AccessDeniedException`: an IAM denial, or a region that does not run MicroVMs.
    Denied,
    /// No answer at all: DNS, network, or a timeout.
    Unreachable,
}

/// One check's line in the report.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Line {
    Pass,
    /// Failed, and decides `ok`.
    Fatal,
    /// Failed, reported, and does not decide `ok`.
    Advisory,
    /// Not run because an earlier check failed; decides `ok` like a fatal failure.
    NotRun,
}

impl Line {
    fn blocks(self) -> bool {
        matches!(self, Line::Fatal | Line::NotRun)
    }
}

/// An AWS call the procedure made.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Call {
    /// `ListManagedMicrovmImages`: free, read-only.
    FreeRead,
    /// `RunMicrovm`: billable, mutating.
    Billable,
}

/// Which procedure the model runs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    Specified,
    AdvisoryService,
    ProbeByLaunch,
    ServiceWithoutCredentials,
    StopAtFirstFailure,
}

/// Where the procedure is.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Step {
    Region,
    Credentials,
    Service,
    Report,
    Done,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    pub region: RegionWorld,
    pub credentials: bool,
    pub service: ServiceWorld,
    pub step: Step,
    pub lines: [Option<Line>; 3],
    pub free_reads: u8,
    pub billable: u8,
    /// Calls made after the region or the credentials failed to resolve.
    pub calls_after_failure: u8,
    /// `ok` as reported, once reported.
    pub ok: Option<bool>,
}

/// **The specification** (BIND-15): the region line, which decides whether anything else runs.
pub fn region_line(region: RegionWorld) -> Line {
    match region {
        RegionWorld::Supported => Line::Pass,
        RegionWorld::Unlisted => Line::Advisory,
        RegionWorld::Unresolvable => Line::Fatal,
    }
}

/// **The specification** (BIND-15): `ok` is true exactly when no line blocks.
pub fn specified(lines: [Line; 3]) -> bool {
    !lines.iter().any(|line| line.blocks())
}

#[derive(Clone, Debug)]
pub struct PreflightModel {
    pub behavior: Behavior,
}

impl Model for PreflightModel {
    type State = State;
    type Action = Step;

    fn init_states(&self) -> Vec<State> {
        let mut states = Vec::new();
        for region in [
            RegionWorld::Supported,
            RegionWorld::Unlisted,
            RegionWorld::Unresolvable,
        ] {
            for credentials in [true, false] {
                for service in [
                    ServiceWorld::Answers,
                    ServiceWorld::Denied,
                    ServiceWorld::Unreachable,
                ] {
                    states.push(State {
                        region,
                        credentials,
                        service,
                        step: Step::Region,
                        lines: [None; 3],
                        free_reads: 0,
                        billable: 0,
                        calls_after_failure: 0,
                        ok: None,
                    });
                }
            }
        }
        states
    }

    fn actions(&self, state: &State, actions: &mut Vec<Step>) {
        if state.step != Step::Done {
            actions.push(state.step);
        }
    }

    fn next_state(&self, last: &State, step: Step) -> Option<State> {
        let mut next = *last;
        let region_failed = last.region == RegionWorld::Unresolvable;
        match step {
            Step::Region => {
                next.lines[0] = Some(region_line(last.region));
                next.step = Step::Credentials;
            }
            Step::Credentials => {
                next.lines[1] = Some(if region_failed {
                    Line::NotRun
                } else if last.credentials {
                    Line::Pass
                } else {
                    Line::Fatal
                });
                next.step = Step::Service;
            }
            Step::Service => {
                let blocked = region_failed
                    || (!last.credentials && self.behavior != Behavior::ServiceWithoutCredentials);
                next.lines[2] = Some(if blocked {
                    Line::NotRun
                } else {
                    let call = if self.behavior == Behavior::ProbeByLaunch {
                        Call::Billable
                    } else {
                        Call::FreeRead
                    };
                    match call {
                        Call::FreeRead => next.free_reads += 1,
                        Call::Billable => next.billable += 1,
                    }
                    if region_failed || !last.credentials {
                        next.calls_after_failure += 1;
                    }
                    let answered = last.credentials && last.service == ServiceWorld::Answers;
                    if answered {
                        Line::Pass
                    } else if self.behavior == Behavior::AdvisoryService {
                        Line::Advisory
                    } else {
                        Line::Fatal
                    }
                });
                next.step = Step::Report;
            }
            Step::Report => {
                let mut lines = next.lines.map(|line| line.unwrap_or(Line::NotRun));
                if self.behavior == Behavior::StopAtFirstFailure {
                    let first = lines.iter().position(|line| line.blocks());
                    if let Some(first) = first {
                        for index in first + 1..3 {
                            next.lines[index] = None;
                        }
                        lines = next.lines.map(|line| line.unwrap_or(Line::Pass));
                    }
                }
                next.ok = Some(specified(lines));
                next.step = Step::Done;
            }
            Step::Done => return None,
        }
        (next != *last).then_some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            // ── BIND-15 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-15 ok only when the region resolves, credentials resolve, and the service answers",
                |_, s| {
                    s.ok != Some(true)
                        || (s.region != RegionWorld::Unresolvable
                            && s.credentials
                            && s.service == ServiceWorld::Answers)
                },
            ),
            Property::<Self>::always(
                "BIND-15 a preflight that could launch reports ok",
                |_, s| {
                    let launchable = s.region != RegionWorld::Unresolvable
                        && s.credentials
                        && s.service == ServiceWorld::Answers;
                    !launchable || s.ok != Some(false)
                },
            ),
            Property::<Self>::always("BIND-15 the report names every check", |_, s| {
                s.step != Step::Done || s.lines.iter().all(Option::is_some)
            }),
            Property::<Self>::sometimes("BIND-15 witness: ok in a supported region", |_, s| {
                s.ok == Some(true) && s.region == RegionWorld::Supported
            }),
            Property::<Self>::sometimes(
                "BIND-15 witness: ok in an unlisted region, advisory line and all",
                |_, s| s.ok == Some(true) && s.region == RegionWorld::Unlisted,
            ),
            Property::<Self>::sometimes("BIND-15 witness: no region to check", |_, s| {
                s.ok == Some(false) && s.region == RegionWorld::Unresolvable
            }),
            Property::<Self>::sometimes("BIND-15 witness: no credentials", |_, s| {
                s.ok == Some(false) && !s.credentials && s.region == RegionWorld::Supported
            }),
            Property::<Self>::sometimes(
                "BIND-15 witness: the service denies the listing",
                |_, s| s.ok == Some(false) && s.credentials && s.service == ServiceWorld::Denied,
            ),
            Property::<Self>::sometimes("BIND-15 witness: the service does not answer", |_, s| {
                s.ok == Some(false) && s.credentials && s.service == ServiceWorld::Unreachable
            }),
            // ── BIND-16 ───────────────────────────────────────────────────────
            Property::<Self>::always("BIND-16 no billable or mutating call", |_, s| {
                s.billable == 0
            }),
            Property::<Self>::always(
                "BIND-16 no AWS call after the region or the credentials failed",
                |_, s| s.calls_after_failure == 0,
            ),
            Property::<Self>::always("BIND-16 at most one AWS operation", |_, s| {
                s.free_reads + s.billable <= 1
            }),
            Property::<Self>::sometimes("BIND-16 witness: the one free read is made", |_, s| {
                s.free_reads == 1 && s.step == Step::Done
            }),
            Property::<Self>::sometimes(
                "BIND-16 witness: a failed credential chain makes no call",
                |_, s| !s.credentials && s.step == Step::Done && s.free_reads == 0,
            ),
            // ── liveness ──────────────────────────────────────────────────────
            // Sound because the model is acyclic: every step moves forward.
            Property::<Self>::eventually("every preflight reports", |_, s| s.step == Step::Done),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<PreflightModel> {
        PreflightModel { behavior }.checker().spawn_bfs().join()
    }

    /// The headline: over all eighteen worlds the specified procedure reports ok exactly when
    /// a launch could proceed, names every check, and makes one free read at most.
    #[test]
    fn the_specified_preflight_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(checker.unique_state_count() > 50);
    }

    /// `doctor`'s advisory listing, reused as the service check, reports ok while the service
    /// is unreachable.
    #[test]
    fn an_advisory_service_check_reports_ok_for_a_dead_service() {
        let checker = checked(Behavior::AdvisoryService);
        let last = *checker
            .assert_any_discovery(
                "BIND-15 ok only when the region resolves, credentials resolve, and the service answers",
            )
            .last_state();
        assert_ne!(last.service, ServiceWorld::Answers, "{last:?}");
    }

    /// Reachability tested with a launch bills.
    #[test]
    fn probing_by_launch_is_a_billable_call() {
        let checker = checked(Behavior::ProbeByLaunch);
        checker.assert_any_discovery("BIND-16 no billable or mutating call");
    }

    /// A listing sent after the credential chain failed.
    #[test]
    fn a_listing_after_failed_credentials_is_caught() {
        let checker = checked(Behavior::ServiceWithoutCredentials);
        let last = *checker
            .assert_any_discovery("BIND-16 no AWS call after the region or the credentials failed")
            .last_state();
        assert!(!last.credentials, "{last:?}");
    }

    /// A report that stops at the first failure hides the rest.
    #[test]
    fn a_report_that_stops_at_the_first_failure_is_caught() {
        let checker = checked(Behavior::StopAtFirstFailure);
        checker.assert_any_discovery("BIND-15 the report names every check");
    }

    /// The table `microvms_core::preflight`'s aggregation test mirrors.
    #[test]
    fn the_specification_table() {
        use Line::*;
        assert!(specified([Pass, Pass, Pass]));
        assert!(specified([Advisory, Pass, Pass]));
        assert!(!specified([Fatal, NotRun, NotRun]));
        assert!(!specified([Pass, Fatal, NotRun]));
        assert!(!specified([Advisory, Fatal, NotRun]));
        assert!(!specified([Pass, Pass, Fatal]));
        assert!(
            !specified([Pass, Pass, NotRun]),
            "a check that never ran is not a pass, whatever came before it"
        );
        assert_eq!(region_line(RegionWorld::Unlisted), Advisory);
    }
}
