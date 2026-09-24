// SPDX-License-Identifier: Apache-2.0
//! A checked model of wrapping a task Dockerfile with the agentd stanza, and of the base-image
//! policy that pairs the result with the create call's `FROM` guard.
//!
//! It specifies IMAGE-1 through IMAGE-4 in `spec/core.symspec.json`, which issue #220 asked
//! for: a harness whose tasks bring their own Dockerfile carried the stanza as a string literal
//! of its own, and inverted `require_matching_from` by hand to get past the guard.
//!
//! # What has states here
//!
//! The function under test is pure, so the states are its inputs and the two decisions made
//! with its output: whether the wrap is accepted, and whether the create call's `FROM` guard
//! then accepts the wrapped Dockerfile under the base policy the caller picked. A task
//! Dockerfile is reduced to the five features the decisions read: its first `FROM`, how its
//! text ends, its last `USER`, whether it declares a `WORKDIR`, and whether it sets a keepalive
//! the client cannot tolerate. The Rust tests in `microvms-core` render each feature
//! combination as real Dockerfile text and check `wrap_dockerfile` against [`specified`].
//!
//! # The specification and the policies it rejects
//!
//! [`specified`] is the table. The rejected policies are other functions of the same shape:
//!
//! * [`Behavior::LiteralAppend`] is the harvester's approach: a string literal appended to the
//!   task text. The literal is a second source that drifts (IMAGE-1), a task ending in a line
//!   continuation swallows the stanza's first line (IMAGE-2), and a task with no `FROM` is
//!   wrapped into a Dockerfile no build can run (IMAGE-3).
//! * [`Behavior::AlwaysUserRoot`] writes `USER root` whatever the task set. It keeps the
//!   bootstrap invariant, but a task that never changes user no longer wraps to the default
//!   stanza, so the default Dockerfile and the wrapped one stop being one text (IMAGE-1).
//! * [`Behavior::ManagedBaseOnly`] is the state before #220: the only base a caller can name
//!   without writing the pairing by hand is the managed one, so a task on another base is
//!   refused by the `FROM` guard (IMAGE-4).
//!
//! # Every always-property has a sometimes-property beside it
//!
//! As in [`crate::output`]: a safety property over a space that never reaches the interesting
//! state measures nothing, so each claim is paired with a witness that the checker got there.

use stateright::{Model, Property};

/// A task Dockerfile's first `FROM`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum From {
    /// No `FROM` at all.
    Absent,
    /// The managed base's own registry ref.
    Managed,
    /// The managed base's ref pinned by digest.
    ManagedPinned,
    /// Any other image, which is what a task Dockerfile usually names.
    Other,
}

/// How the task text ends.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Tail {
    /// On a finished instruction, with or without a trailing newline.
    Complete,
    /// Inside a line continuation: the last instruction line ends with the escape character.
    Continuation,
    /// Inside a heredoc whose terminator never appears.
    OpenHeredoc,
}

/// The task's last `USER`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum User {
    /// No `USER` instruction.
    Unset,
    /// `USER root` or `USER 0`.
    Root,
    /// Any other user.
    Other,
}

/// The workdir option a caller passes.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Workdir {
    /// None given.
    Unset,
    /// One absolute path.
    Absolute,
    /// Anything else: relative, or carrying a line break that would inject an instruction.
    Invalid,
}

/// A task Dockerfile, reduced to what the decisions read.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Task {
    pub from: From,
    pub tail: Tail,
    pub user: User,
    pub declares_workdir: bool,
    /// `AGENTD_SSE_KEEPALIVE_SECS` at or over the client's stream idle timeout.
    pub keepalive_too_long: bool,
}

/// The wrap options.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Opts {
    pub workdir: Workdir,
    /// The caller relies on the image `WORKDIR`, so one must be declared somewhere.
    pub inherit_workdir: bool,
}

/// Why a wrap was refused.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Refusal {
    NoFrom,
    Unfinished,
    Keepalive,
    BadWorkdir,
    NothingToInherit,
}

/// Where a wrapped Dockerfile's stanza lines came from.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Source {
    /// The function the default Dockerfile generator also uses.
    Generator,
    /// A copy of it, kept somewhere else.
    Literal,
}

/// A wrapped Dockerfile, reduced to what the properties read.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Wrapped {
    pub source: Source,
    /// Every stanza line is its own instruction: none was joined into the task's last one.
    pub stanza_intact: bool,
    /// `USER root` sits between the task text and the stanza.
    pub user_root: bool,
    /// The first `FROM`, which wrapping never changes.
    pub from: From,
}

/// What [`specified`] (or a rejected policy) answers for a task and its options.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Wrap {
    Refused(Refusal),
    Wrapped(Wrapped),
}

/// The base image a caller hands the create call with the wrapped Dockerfile.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Policy {
    /// `BaseImage::al2023()`.
    Managed,
    /// `BaseImage::from_dockerfile(wrapped)`.
    FromDockerfile,
}

/// The create call's `FROM` guard (`require_matching_from`).
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Guard {
    Accepted,
    Refused,
}

/// **The specification** of `wrap_dockerfile`: refusals first, in the order the function
/// checks them, then the wrap.
///
/// **Falsification** — 2026-09-24. Dropping the `Tail` refusal made
/// `the_specified_wrap_satisfies_every_property` fail with a two-state counterexample to
/// `IMAGE-2 a wrapped Dockerfile keeps every stanza line its own instruction`; restored.
pub fn specified(task: Task, opts: Opts) -> Wrap {
    if task.from == From::Absent {
        return Wrap::Refused(Refusal::NoFrom);
    }
    if task.tail != Tail::Complete {
        return Wrap::Refused(Refusal::Unfinished);
    }
    if task.keepalive_too_long {
        return Wrap::Refused(Refusal::Keepalive);
    }
    if opts.workdir == Workdir::Invalid {
        return Wrap::Refused(Refusal::BadWorkdir);
    }
    if opts.inherit_workdir && !task.declares_workdir && opts.workdir == Workdir::Unset {
        return Wrap::Refused(Refusal::NothingToInherit);
    }
    Wrap::Wrapped(Wrapped {
        source: Source::Generator,
        stanza_intact: true,
        user_root: task.user == User::Other,
        from: task.from,
    })
}

/// The harvester's `_HARNESS_STANZA`: a literal appended after a newline, no refusals.
fn literal_append(task: Task, _opts: Opts) -> Wrap {
    Wrap::Wrapped(Wrapped {
        source: Source::Literal,
        stanza_intact: task.tail == Tail::Complete,
        user_root: true,
        from: task.from,
    })
}

/// The specification with `USER root` written unconditionally.
fn always_user_root(task: Task, opts: Opts) -> Wrap {
    match specified(task, opts) {
        Wrap::Wrapped(wrapped) => Wrap::Wrapped(Wrapped {
            user_root: true,
            ..wrapped
        }),
        refused => refused,
    }
}

/// **The specification** of the `FROM` guard under a base policy.
///
/// The managed base accepts its own ref, its digest pin, and a Dockerfile with no `FROM`
/// (left to the build). The from-Dockerfile policy takes its ref from the Dockerfile, so the
/// guard's comparison is the ref against itself.
pub fn guard(policy: Policy, from: From) -> Guard {
    match (policy, from) {
        (Policy::FromDockerfile, _) => Guard::Accepted,
        (Policy::Managed, From::Other) => Guard::Refused,
        (Policy::Managed, _) => Guard::Accepted,
    }
}

/// Which policies the model runs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// [`specified`] and [`guard`].
    Specified,
    /// The harvester's appended literal.
    LiteralAppend,
    /// `USER root` whatever the task set.
    AlwaysUserRoot,
    /// No from-Dockerfile policy: the caller's only base is the managed one.
    ManagedBaseOnly,
}

impl Behavior {
    fn wrap(self, task: Task, opts: Opts) -> Wrap {
        match self {
            Behavior::LiteralAppend => literal_append(task, opts),
            Behavior::AlwaysUserRoot => always_user_root(task, opts),
            Behavior::Specified | Behavior::ManagedBaseOnly => specified(task, opts),
        }
    }

    fn guard(self, policy: Policy, from: From) -> Guard {
        match self {
            Behavior::ManagedBaseOnly => guard(Policy::Managed, from),
            _ => guard(policy, from),
        }
    }
}

/// Where one task is in its trip from text to a create call.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Phase {
    /// The task and options are chosen; nothing has run.
    Composed,
    /// `wrap_dockerfile` answered.
    Wrapped(Wrap),
    /// The wrapped Dockerfile met the `FROM` guard under a base policy.
    Checked(Wrapped, Policy, Guard),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    pub task: Task,
    pub opts: Opts,
    pub phase: Phase,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    /// Call `wrap_dockerfile`.
    Wrap,
    /// Hand the wrapped Dockerfile to the create call's preflight under `Policy`.
    Check(Policy),
}

/// The model.
#[derive(Clone, Debug)]
pub struct WrapModel {
    pub behavior: Behavior,
}

impl WrapModel {
    pub fn new(behavior: Behavior) -> Self {
        Self { behavior }
    }
}

/// Every task the model enumerates.
pub fn tasks() -> Vec<Task> {
    let mut tasks = Vec::new();
    for from in [
        From::Absent,
        From::Managed,
        From::ManagedPinned,
        From::Other,
    ] {
        for tail in [Tail::Complete, Tail::Continuation, Tail::OpenHeredoc] {
            for user in [User::Unset, User::Root, User::Other] {
                for declares_workdir in [false, true] {
                    for keepalive_too_long in [false, true] {
                        tasks.push(Task {
                            from,
                            tail,
                            user,
                            declares_workdir,
                            keepalive_too_long,
                        });
                    }
                }
            }
        }
    }
    tasks
}

/// Every option set the model enumerates.
pub fn options() -> Vec<Opts> {
    let mut options = Vec::new();
    for workdir in [Workdir::Unset, Workdir::Absolute, Workdir::Invalid] {
        for inherit_workdir in [false, true] {
            options.push(Opts {
                workdir,
                inherit_workdir,
            });
        }
    }
    options
}

impl Model for WrapModel {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        let mut states = Vec::new();
        for task in tasks() {
            for opts in options() {
                states.push(State {
                    task,
                    opts,
                    phase: Phase::Composed,
                });
            }
        }
        states
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        match state.phase {
            Phase::Composed => actions.push(Action::Wrap),
            Phase::Wrapped(Wrap::Wrapped(_)) => {
                actions.push(Action::Check(Policy::Managed));
                actions.push(Action::Check(Policy::FromDockerfile));
            }
            Phase::Wrapped(Wrap::Refused(_)) | Phase::Checked(..) => {}
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let phase = match (last.phase, action) {
            (Phase::Composed, Action::Wrap) => {
                Phase::Wrapped(self.behavior.wrap(last.task, last.opts))
            }
            (Phase::Wrapped(Wrap::Wrapped(wrapped)), Action::Check(policy)) => {
                Phase::Checked(wrapped, policy, self.behavior.guard(policy, wrapped.from))
            }
            _ => return None,
        };
        Some(State { phase, ..*last })
    }

    fn properties(&self) -> Vec<Property<Self>> {
        fn wrapped(s: &State) -> Option<Wrapped> {
            match s.phase {
                Phase::Wrapped(Wrap::Wrapped(w)) | Phase::Checked(w, ..) => Some(w),
                _ => None,
            }
        }
        fn refused(s: &State) -> Option<Refusal> {
            match s.phase {
                Phase::Wrapped(Wrap::Refused(r)) => Some(r),
                _ => None,
            }
        }
        vec![
            // ── IMAGE-1 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "IMAGE-1 a wrapped Dockerfile's stanza is the default generator's",
                |_, s| wrapped(s).is_none_or(|w| w.source == Source::Generator),
            ),
            Property::<Self>::always(
                "IMAGE-1 a task that sets no other user wraps to the default stanza alone",
                |_, s| wrapped(s).is_none_or(|w| s.task.user == User::Other || !w.user_root),
            ),
            Property::<Self>::sometimes(
                "IMAGE-1 witness: a bare FROM wraps to the default stanza",
                |_, s| {
                    s.task.user == User::Unset
                        && !s.task.declares_workdir
                        && wrapped(s).is_some_and(|w| !w.user_root)
                },
            ),
            // ── IMAGE-2 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "IMAGE-2 a wrapped Dockerfile keeps every stanza line its own instruction",
                |_, s| wrapped(s).is_none_or(|w| w.stanza_intact),
            ),
            Property::<Self>::always(
                "IMAGE-2 a task that sets another user has USER root restored",
                |_, s| wrapped(s).is_none_or(|w| s.task.user != User::Other || w.user_root),
            ),
            Property::<Self>::always("IMAGE-2 wrapping keeps the task's first FROM", |_, s| {
                wrapped(s).is_none_or(|w| w.from == s.task.from)
            }),
            Property::<Self>::sometimes(
                "IMAGE-2 witness: a task on another user is wrapped",
                |_, s| s.task.user == User::Other && wrapped(s).is_some(),
            ),
            // ── IMAGE-3 ───────────────────────────────────────────────────────
            Property::<Self>::always("IMAGE-3 a task with no FROM is never wrapped", |_, s| {
                s.task.from != From::Absent || wrapped(s).is_none()
            }),
            Property::<Self>::always("IMAGE-3 an unfinished task is never wrapped", |_, s| {
                s.task.tail == Tail::Complete || wrapped(s).is_none()
            }),
            Property::<Self>::always(
                "IMAGE-3 a refusal names a cause the task or options carry",
                |_, s| match refused(s) {
                    None => true,
                    Some(Refusal::NoFrom) => s.task.from == From::Absent,
                    Some(Refusal::Unfinished) => s.task.tail != Tail::Complete,
                    Some(Refusal::Keepalive) => s.task.keepalive_too_long,
                    Some(Refusal::BadWorkdir) => s.opts.workdir == Workdir::Invalid,
                    Some(Refusal::NothingToInherit) => {
                        s.opts.inherit_workdir
                            && !s.task.declares_workdir
                            && s.opts.workdir == Workdir::Unset
                    }
                },
            ),
            Property::<Self>::sometimes("IMAGE-3 witness: an open heredoc is refused", |_, s| {
                s.task.tail == Tail::OpenHeredoc && refused(s) == Some(Refusal::Unfinished)
            }),
            Property::<Self>::sometimes(
                "IMAGE-3 witness: inheriting a workdir nothing declares is refused",
                |_, s| refused(s) == Some(Refusal::NothingToInherit),
            ),
            // ── IMAGE-4 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "IMAGE-4 the from-Dockerfile policy passes every wrapped Dockerfile",
                |_, s| match s.phase {
                    Phase::Checked(_, Policy::FromDockerfile, guard) => guard == Guard::Accepted,
                    _ => true,
                },
            ),
            Property::<Self>::sometimes(
                "IMAGE-4 witness: the managed base refuses a task on another base",
                |_, s| {
                    matches!(
                        s.phase,
                        Phase::Checked(w, Policy::Managed, Guard::Refused) if w.from == From::Other
                    )
                },
            ),
            Property::<Self>::sometimes(
                "IMAGE-4 witness: a digest-pinned task passes under the from-Dockerfile policy",
                |_, s| {
                    matches!(
                        s.phase,
                        Phase::Checked(w, Policy::FromDockerfile, Guard::Accepted)
                            if w.from == From::ManagedPinned
                    )
                },
            ),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<WrapModel> {
        WrapModel::new(behavior).checker().spawn_bfs().join()
    }

    /// The headline: the specified wrap and guard satisfy IMAGE-1 through IMAGE-4 over every
    /// task and option set, and witness every case that makes that meaningful.
    #[test]
    fn the_specified_wrap_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 1000,
            "a space this small could not reach every combination: {}",
            checker.unique_state_count()
        );
    }

    /// **The harvester's literal.** A second source of the stanza, a continuation that
    /// swallows its first line, and a Dockerfile with no `FROM` wrapped anyway.
    #[test]
    fn a_literal_append_drifts_swallows_and_wraps_what_cannot_build() {
        let checker = checked(Behavior::LiteralAppend);
        checker.assert_any_discovery(
            "IMAGE-1 a wrapped Dockerfile's stanza is the default generator's",
        );
        let path = checker
            .assert_any_discovery(
                "IMAGE-2 a wrapped Dockerfile keeps every stanza line its own instruction",
            )
            .into_states();
        assert_ne!(
            path.first().expect("a path starts somewhere").task.tail,
            Tail::Complete,
            "the swallow needs an unfinished tail"
        );
        checker.assert_any_discovery("IMAGE-3 a task with no FROM is never wrapped");
    }

    /// **The rejected unconditional `USER root`.** The invariant holds, and the default
    /// Dockerfile stops being the wrap of a bare `FROM`.
    #[test]
    fn an_unconditional_user_root_splits_the_one_stanza_in_two() {
        let checker = checked(Behavior::AlwaysUserRoot);
        checker.assert_no_discovery(
            "IMAGE-2 a wrapped Dockerfile keeps every stanza line its own instruction",
        );
        checker.assert_any_discovery(
            "IMAGE-1 a task that sets no other user wraps to the default stanza alone",
        );
    }

    /// **#220 in the model.** With only the managed base to hand, a task on another base is
    /// refused by the guard — the inversion every harness wrote by hand.
    #[test]
    fn with_only_the_managed_base_a_task_on_another_base_is_refused() {
        let checker = checked(Behavior::ManagedBaseOnly);
        let path = checker
            .assert_any_discovery(
                "IMAGE-4 the from-Dockerfile policy passes every wrapped Dockerfile",
            )
            .into_states();
        assert_eq!(
            path.first().expect("a path starts somewhere").task.from,
            From::Other
        );
    }

    /// The table `microvms-core`'s wrap tests mirror: the refusal each feature produces on
    /// its own, against an otherwise wrappable task.
    #[test]
    fn the_specification_table() {
        let plain = Task {
            from: From::Other,
            tail: Tail::Complete,
            user: User::Unset,
            declares_workdir: false,
            keepalive_too_long: false,
        };
        let none = Opts {
            workdir: Workdir::Unset,
            inherit_workdir: false,
        };
        assert!(matches!(specified(plain, none), Wrap::Wrapped(w) if !w.user_root));
        for (task, opts, refusal) in [
            (
                Task {
                    from: From::Absent,
                    ..plain
                },
                none,
                Refusal::NoFrom,
            ),
            (
                Task {
                    tail: Tail::Continuation,
                    ..plain
                },
                none,
                Refusal::Unfinished,
            ),
            (
                Task {
                    tail: Tail::OpenHeredoc,
                    ..plain
                },
                none,
                Refusal::Unfinished,
            ),
            (
                Task {
                    keepalive_too_long: true,
                    ..plain
                },
                none,
                Refusal::Keepalive,
            ),
            (
                plain,
                Opts {
                    workdir: Workdir::Invalid,
                    ..none
                },
                Refusal::BadWorkdir,
            ),
            (
                plain,
                Opts {
                    inherit_workdir: true,
                    ..none
                },
                Refusal::NothingToInherit,
            ),
        ] {
            assert_eq!(
                specified(task, opts),
                Wrap::Refused(refusal),
                "{task:?} {opts:?}"
            );
        }
        let absolute = Opts {
            inherit_workdir: true,
            workdir: Workdir::Absolute,
        };
        assert!(matches!(specified(plain, absolute), Wrap::Wrapped(_)));
        assert!(matches!(
            specified(
                Task {
                    declares_workdir: true,
                    ..plain
                },
                Opts {
                    inherit_workdir: true,
                    ..none
                }
            ),
            Wrap::Wrapped(_)
        ));
        assert!(matches!(
            specified(Task { user: User::Other, ..plain }, none),
            Wrap::Wrapped(w) if w.user_root
        ));
        assert!(matches!(
            specified(Task { user: User::Root, ..plain }, none),
            Wrap::Wrapped(w) if !w.user_root
        ));
    }
}
