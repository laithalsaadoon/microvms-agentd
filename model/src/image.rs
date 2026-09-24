// SPDX-License-Identifier: Apache-2.0
//! A checked model of `Sandbox::ensure_image`: two concurrent callers building or reusing one
//! content-addressed image name, against a platform that moves the image through its states.
//!
//! It specifies IMAGE-9 through IMAGE-11 in `spec/core.symspec.json`, which issue #221 asked
//! for. A Harbor provider runs many trials of one task at once, every trial derives the same
//! image name from the same inputs, and each one has to come back with a usable image: the
//! one already built, the one a sibling is building, or the one it builds itself.
//!
//! # What the model holds
//!
//! The platform keeps one image under the name, in one of five states: absent, building,
//! ready, failed, or deleting. A build in progress settles to ready or failed at a moment the
//! checker chooses, and a deletion finishes at a moment the checker chooses, so every
//! interleaving of the two callers' describes, deletes, creates, and waits against those
//! transitions is explored. A create against a name that exists is refused, which is what the
//! service does and what makes the race a race.
//!
//! Each caller runs [`plan`], the decision table `microvms-core` implements as
//! `control::ensure::plan`: what to do with what a describe found, and whether the caller
//! asked to force a rebuild. The Rust tests mirror the table (`the_plan_table`).
//!
//! # The specification and the policies it rejects
//!
//! * [`Behavior::FailOnRefusedCreate`] reports the losing create as the caller's failure: a
//!   sibling that simply started a moment earlier fails the trial (IMAGE-11).
//! * [`Behavior::LoserReturnsEarly`] joins the winner without waiting, returning an image that
//!   is still building (IMAGE-11).
//! * [`Behavior::AlwaysRebuild`] treats a ready image as stale and deletes it, pulling the
//!   image out from under a sibling that just reused it (IMAGE-10).
//! * [`Behavior::RecreateFailedInPlace`] creates over a failed image without deleting it
//!   first; the service refuses the create, so a failed image can never be rebuilt (IMAGE-10).
//!
//! # Every always-property has a sometimes-property beside it
//!
//! As in [`crate::output`]: a safety property over a space that never reaches the interesting
//! state measures nothing, so each claim is paired with a witness that the checker got there.

use stateright::{Model, Property};

/// The platform's image under the one name.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Platform {
    Absent,
    /// `CREATING` or `UPDATING`: a build is running.
    Building,
    /// `CREATED` or `UPDATED`: usable.
    Ready,
    /// `CREATE_FAILED`, `UPDATE_FAILED`, `DELETE_FAILED`.
    Failed,
    Deleting,
}

/// What a describe found, as the decision table reads it.
pub type Found = Platform;

/// What a caller does next with what a describe found.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Plan {
    /// Return the ready image; nothing is uploaded or created.
    Reuse,
    /// Wait for the running build to settle, then describe again.
    Wait,
    /// Delete the image, wait until it is gone, then build.
    Delete,
    /// A deletion is under way: wait until the name is free, then build.
    AwaitAbsent,
    /// Upload the artifact and create the image.
    Build,
}

/// **The specification** of the decision `ensure_image` makes after each describe.
///
/// A forced caller waits for a running build to settle before deleting it, because the
/// service refuses to delete an image in `CREATING`. A failed image is deleted whether or not
/// the caller forced, because the name is content-addressed: the only way to a fresh build
/// under it is to free it first.
///
/// **Falsification** — 2026-09-24. Answering `Reuse` for a ready image under force made
/// `the_specified_ensure_satisfies_every_property` fail with a counterexample to
/// `IMAGE-10 a forced caller deletes the ready image it finds`; restored.
pub fn plan(found: Found, force: bool) -> Plan {
    match (found, force) {
        (Platform::Absent, _) => Plan::Build,
        (Platform::Ready, false) => Plan::Reuse,
        (Platform::Ready, true) | (Platform::Failed, _) => Plan::Delete,
        (Platform::Building, _) => Plan::Wait,
        (Platform::Deleting, _) => Plan::AwaitAbsent,
    }
}

/// How a caller ended.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum End {
    /// Returned an image, and whether this caller's own create built it.
    Returned { reused: bool },
    /// The build it waited on failed; the error names the image and its reason.
    BuildFailed,
    /// The name was freed while this caller waited on it.
    Vanished,
    /// A refused create surfaced as this caller's failure.
    CreateRefused,
}

/// Where one caller is.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Pc {
    /// About to describe the image.
    Describe,
    /// Waiting for a build to settle. `then_delete` is a forced caller that will delete what
    /// the build settles to; otherwise the caller returns it.
    Waiting {
        then_delete: bool,
    },
    /// Its delete was accepted or found a deletion under way; waiting for the name to free.
    AwaitingAbsent,
    /// The name is free as far as this caller knows: upload and create.
    Creating,
    /// Its create was refused: describe again, then join whatever is there.
    Redescribe,
    Done(End),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Caller {
    pub force: bool,
    pub pc: Pc,
    /// This caller's create was accepted, for the image generation it is now waiting on.
    pub created: bool,
    /// This caller uploaded an artifact.
    pub uploaded: bool,
    /// This caller issued a delete.
    pub deleted: bool,
    /// This caller saw the image failed, and has not yet deleted it.
    pub owes_delete: bool,
    /// What the platform held when this caller returned.
    pub returned_state: Option<Platform>,
    /// This caller's create was refused.
    pub lost_race: bool,
    /// This caller went back to the describe after the image it waited on disappeared.
    pub redescribed: bool,
    /// What this caller's first describe found.
    pub first_found: Option<Platform>,
    /// Caller-identity calls this caller made to resolve the image ARN.
    pub account_calls: u8,
}

impl Caller {
    fn new(force: bool) -> Self {
        Self {
            force,
            pc: Pc::Describe,
            created: false,
            uploaded: false,
            deleted: false,
            owes_delete: false,
            returned_state: None,
            lost_race: false,
            redescribed: false,
            first_found: None,
            account_calls: 0,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    pub platform: Platform,
    pub callers: [Caller; 2],
    /// Creates the platform accepted over the whole run.
    pub accepted_creates: u8,
    /// Deletions the platform completed over the whole run.
    pub completed_deletes: u8,
    /// A ready image was deleted by a caller that did not force.
    pub unforced_ready_delete: bool,
    /// A caller created over an image it had seen failed without deleting it first.
    pub failed_recreated_in_place: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    /// Caller `i` takes its next step.
    Step(usize),
    /// The running build settles.
    BuildSettles { ready: bool },
    /// The deletion under way finishes.
    DeletionFinishes,
}

/// Which policies the model runs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// [`plan`] and the race-loss join.
    Specified,
    FailOnRefusedCreate,
    LoserReturnsEarly,
    AlwaysRebuild,
    RecreateFailedInPlace,
}

impl Behavior {
    fn plan(self, found: Found, force: bool) -> Plan {
        match (self, found) {
            (Behavior::AlwaysRebuild, Platform::Ready) => Plan::Delete,
            (Behavior::RecreateFailedInPlace, Platform::Failed) => Plan::Build,
            _ => plan(found, force),
        }
    }
}

/// The model's configuration: the policy, what the name held before either caller started,
/// and whether each caller forces.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Config {
    pub behavior: Behavior,
}

#[derive(Clone, Debug)]
pub struct EnsureModel {
    pub cfg: Config,
}

impl EnsureModel {
    pub fn new(behavior: Behavior) -> Self {
        Self {
            cfg: Config { behavior },
        }
    }

    /// One caller's step against the platform. Returns `None` when the caller cannot move
    /// until the platform does.
    fn step(&self, state: &mut State, i: usize) -> Option<()> {
        let behavior = self.cfg.behavior;
        let mut caller = state.callers[i];
        match caller.pc {
            Pc::Describe => {
                // The ARN is needed for the describe; it is resolved once per sandbox.
                if caller.account_calls == 0 {
                    caller.account_calls = 1;
                }
                let found = state.platform;
                caller.first_found.get_or_insert(found);
                if found == Platform::Failed {
                    caller.owes_delete = true;
                }
                // Force is one-shot: a caller that has deleted once joins what it finds after.
                caller.pc = match behavior.plan(found, caller.force && !caller.deleted) {
                    Plan::Reuse => {
                        caller.returned_state = Some(state.platform);
                        Pc::Done(End::Returned { reused: true })
                    }
                    Plan::Wait => Pc::Waiting {
                        then_delete: caller.force && !caller.deleted,
                    },
                    Plan::Delete => {
                        self.delete(state, &mut caller);
                        Pc::AwaitingAbsent
                    }
                    Plan::AwaitAbsent => Pc::AwaitingAbsent,
                    Plan::Build => Pc::Creating,
                };
            }
            Pc::Waiting { then_delete } => match state.platform {
                Platform::Building => return None,
                Platform::Ready | Platform::Failed if then_delete => {
                    // A forced caller deletes what the build settled to.
                    if state.platform == Platform::Failed {
                        caller.owes_delete = true;
                    }
                    self.delete(state, &mut caller);
                    caller.pc = Pc::AwaitingAbsent;
                }
                Platform::Ready => {
                    caller.returned_state = Some(state.platform);
                    caller.pc = Pc::Done(End::Returned {
                        reused: !caller.created,
                    });
                }
                Platform::Failed => caller.pc = Pc::Done(End::BuildFailed),
                // The image was deleted while this caller waited: a sibling is rebuilding a
                // failure, or forcing. Describe once more and join what is there.
                Platform::Absent | Platform::Deleting => caller.pc = self.redescribe(&mut caller),
            },
            Pc::AwaitingAbsent => match state.platform {
                Platform::Deleting => return None,
                // Free, or already rebuilt by a sibling: build, and join the sibling if the
                // create is refused.
                _ => caller.pc = Pc::Creating,
            },
            Pc::Creating => {
                caller.uploaded = true;
                if caller.owes_delete {
                    state.failed_recreated_in_place = true;
                }
                if state.platform == Platform::Absent {
                    state.platform = Platform::Building;
                    state.accepted_creates += 1;
                    caller.created = true;
                    caller.pc = Pc::Waiting { then_delete: false };
                } else {
                    caller.lost_race = true;
                    caller.pc = match behavior {
                        Behavior::FailOnRefusedCreate => Pc::Done(End::CreateRefused),
                        _ => Pc::Redescribe,
                    };
                }
            }
            Pc::Redescribe => {
                caller.pc = match state.platform {
                    Platform::Ready | Platform::Building
                        if behavior == Behavior::LoserReturnsEarly =>
                    {
                        caller.returned_state = Some(state.platform);
                        Pc::Done(End::Returned { reused: true })
                    }
                    Platform::Ready => {
                        caller.returned_state = Some(state.platform);
                        Pc::Done(End::Returned { reused: true })
                    }
                    Platform::Building => Pc::Waiting { then_delete: false },
                    Platform::Failed => Pc::Done(End::BuildFailed),
                    Platform::Absent | Platform::Deleting => self.redescribe(&mut caller),
                };
            }
            Pc::Done(_) => return None,
        }
        state.callers[i] = caller;
        Some(())
    }

    /// Back to the describe, once: a second disappearance ends the caller.
    fn redescribe(&self, caller: &mut Caller) -> Pc {
        if caller.redescribed {
            return Pc::Done(End::Vanished);
        }
        caller.redescribed = true;
        // Whatever this caller built is gone; the image it returns now is someone's new one.
        caller.created = false;
        Pc::Describe
    }

    fn delete(&self, state: &mut State, caller: &mut Caller) {
        let forced = caller.force && !caller.deleted;
        caller.deleted = true;
        caller.owes_delete = false;
        match state.platform {
            Platform::Ready | Platform::Failed => {
                if state.platform == Platform::Ready && !forced {
                    state.unforced_ready_delete = true;
                }
                state.platform = Platform::Deleting;
            }
            // Refused (`CREATING` cannot be deleted), already going, or already gone: the
            // caller waits for the name either way.
            Platform::Building | Platform::Deleting | Platform::Absent => {}
        }
    }
}

impl Model for EnsureModel {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        let mut states = Vec::new();
        for platform in [
            Platform::Absent,
            Platform::Building,
            Platform::Ready,
            Platform::Failed,
        ] {
            for (first, second) in [(false, false), (true, false)] {
                states.push(State {
                    platform,
                    callers: [Caller::new(first), Caller::new(second)],
                    accepted_creates: 0,
                    completed_deletes: 0,
                    unforced_ready_delete: false,
                    failed_recreated_in_place: false,
                });
            }
        }
        states
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        for i in 0..2 {
            let mut probe = *state;
            if self.step(&mut probe, i).is_some() {
                actions.push(Action::Step(i));
            }
        }
        match state.platform {
            Platform::Building => {
                actions.push(Action::BuildSettles { ready: true });
                actions.push(Action::BuildSettles { ready: false });
            }
            Platform::Deleting => actions.push(Action::DeletionFinishes),
            _ => {}
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = *last;
        match action {
            Action::Step(i) => self.step(&mut next, i)?,
            Action::BuildSettles { ready } => {
                next.platform = if ready {
                    Platform::Ready
                } else {
                    Platform::Failed
                };
            }
            Action::DeletionFinishes => {
                next.platform = Platform::Absent;
                next.completed_deletes += 1;
            }
        }
        Some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        fn returned(c: &Caller) -> Option<bool> {
            match c.pc {
                Pc::Done(End::Returned { reused }) => Some(reused),
                _ => None,
            }
        }
        vec![
            // ── IMAGE-8 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "IMAGE-8 a caller resolves its account once however often it describes",
                |_, s| s.callers.iter().all(|c| c.account_calls <= 1),
            ),
            Property::<Self>::sometimes(
                "IMAGE-8 witness: a caller describes twice on one account lookup",
                |_, s| {
                    s.callers
                        .iter()
                        .any(|c| c.redescribed && c.account_calls == 1)
                },
            ),
            // ── IMAGE-9 ───────────────────────────────────────────────────────
            Property::<Self>::always("IMAGE-9 a returned image is ready", |_, s| {
                s.callers
                    .iter()
                    .all(|c| returned(c).is_none() || c.returned_state == Some(Platform::Ready))
            }),
            Property::<Self>::always(
                "IMAGE-9 reused is true exactly when this caller's create was not accepted",
                |_, s| {
                    s.callers
                        .iter()
                        .all(|c| returned(c).is_none_or(|reused| reused == !c.created))
                },
            ),
            Property::<Self>::always(
                "IMAGE-9 an unforced caller that finds a ready image returns it and uploads nothing",
                |_, s| {
                    s.callers.iter().all(|c| {
                        c.force
                            || c.first_found != Some(Platform::Ready)
                            || (returned(c) == Some(true) && !c.uploaded && !c.created)
                    })
                },
            ),
            Property::<Self>::sometimes(
                "IMAGE-9 witness: a caller reuses a ready image",
                |_, s| {
                    s.callers
                        .iter()
                        .any(|c| returned(c) == Some(true) && !c.uploaded)
                },
            ),
            Property::<Self>::sometimes(
                "IMAGE-9 witness: a caller waits out a sibling's build and reuses it",
                |_, s| {
                    s.callers[0].created
                        && returned(&s.callers[1]) == Some(true)
                        && returned(&s.callers[0]) == Some(false)
                },
            ),
            // ── IMAGE-10 ──────────────────────────────────────────────────────
            Property::<Self>::always(
                "IMAGE-10 a ready image is deleted only by a forced caller",
                |_, s| !s.unforced_ready_delete,
            ),
            Property::<Self>::always(
                "IMAGE-10 a forced caller deletes the ready image it finds",
                |_, s| {
                    s.callers.iter().all(|c| {
                        !c.force
                            || c.first_found != Some(Platform::Ready)
                            || c.pc == Pc::Describe
                            || c.deleted
                    })
                },
            ),
            Property::<Self>::always(
                "IMAGE-10 a failed image is deleted before it is rebuilt",
                |_, s| !s.failed_recreated_in_place,
            ),
            Property::<Self>::sometimes(
                "IMAGE-10 witness: a failed image is deleted and rebuilt",
                |_, s| {
                    s.completed_deletes > 0
                        && s.callers
                            .iter()
                            .any(|c| c.created && returned(c) == Some(false))
                },
            ),
            Property::<Self>::sometimes(
                "IMAGE-10 witness: a forced caller rebuilds a ready image",
                |_, s| {
                    s.callers
                        .iter()
                        .any(|c| c.deleted && c.created && returned(c) == Some(false))
                        && s.completed_deletes > 0
                },
            ),
            // ── IMAGE-11 ──────────────────────────────────────────────────────
            Property::<Self>::always(
                "IMAGE-11 at most one create is accepted per generation of the name",
                |_, s| s.accepted_creates <= 1 + s.completed_deletes,
            ),
            Property::<Self>::always(
                "IMAGE-11 a refused create never fails the caller",
                |_, s| {
                    s.callers
                        .iter()
                        .all(|c| c.pc != Pc::Done(End::CreateRefused))
                },
            ),
            Property::<Self>::always(
                "IMAGE-11 a caller that lost the race returns only a ready image",
                |_, s| {
                    s.callers.iter().all(|c| {
                        !c.lost_race
                            || returned(c).is_none()
                            || c.returned_state == Some(Platform::Ready)
                    })
                },
            ),
            Property::<Self>::sometimes(
                "IMAGE-11 witness: the loser of a create race joins the winner's build",
                |_, s| {
                    s.callers
                        .iter()
                        .any(|c| c.lost_race && returned(c) == Some(true))
                        && s.callers.iter().any(|c| returned(c) == Some(false))
                },
            ),
            // ── liveness ──────────────────────────────────────────────────────
            //
            // Sound here because the model is acyclic: the platform leaves `Absent` only
            // through a caller's create, and each caller creates and deletes a bounded number
            // of times.
            Property::<Self>::eventually("every caller ends", |_, s| {
                s.callers.iter().all(|c| matches!(c.pc, Pc::Done(_)))
            }),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<EnsureModel> {
        EnsureModel::new(behavior).checker().spawn_bfs().join()
    }

    /// The headline: the specified `ensure_image` satisfies IMAGE-9, IMAGE-10 and IMAGE-11
    /// over every interleaving of two callers and the platform, and witnesses each case.
    #[test]
    fn the_specified_ensure_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 200,
            "a space this small could not reach the races: {}",
            checker.unique_state_count()
        );
    }

    /// **The race, lost loudly.** A sibling's create a moment earlier fails this trial.
    #[test]
    fn failing_on_a_refused_create_fails_the_slower_sibling() {
        let checker = checked(Behavior::FailOnRefusedCreate);
        checker.assert_any_discovery("IMAGE-11 a refused create never fails the caller");
    }

    /// **The race, joined too early.** The loser returns an image still building.
    #[test]
    fn a_loser_that_does_not_wait_returns_an_image_still_building() {
        let checker = checked(Behavior::LoserReturnsEarly);
        checker.assert_any_discovery(
            "IMAGE-11 a caller that lost the race returns only a ready image",
        );
        checker.assert_any_discovery("IMAGE-9 a returned image is ready");
    }

    /// **Rebuild on sight.** A ready image is deleted by a caller that did not force.
    #[test]
    fn rebuilding_a_ready_image_deletes_it_without_force() {
        let checker = checked(Behavior::AlwaysRebuild);
        checker.assert_any_discovery("IMAGE-10 a ready image is deleted only by a forced caller");
    }

    /// **Recreate over a failure.** The create is refused against the failed name, so the
    /// failure can never be rebuilt.
    #[test]
    fn recreating_a_failed_image_in_place_never_rebuilds_it() {
        let checker = checked(Behavior::RecreateFailedInPlace);
        checker.assert_any_discovery("IMAGE-10 a failed image is deleted before it is rebuilt");
    }

    /// The table `microvms-core`'s `control::ensure::plan` mirrors.
    #[test]
    fn the_plan_table() {
        for (found, force, expected) in [
            (Platform::Absent, false, Plan::Build),
            (Platform::Absent, true, Plan::Build),
            (Platform::Ready, false, Plan::Reuse),
            (Platform::Ready, true, Plan::Delete),
            (Platform::Building, false, Plan::Wait),
            (Platform::Building, true, Plan::Wait),
            (Platform::Failed, false, Plan::Delete),
            (Platform::Failed, true, Plan::Delete),
            (Platform::Deleting, false, Plan::AwaitAbsent),
            (Platform::Deleting, true, Plan::AwaitAbsent),
        ] {
            assert_eq!(plan(found, force), expected, "{found:?} force={force}");
        }
    }
}
