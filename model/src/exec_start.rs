// SPDX-License-Identifier: Apache-2.0
//! A checked model of how the daemon turns one `POST /v1/exec/start` into a child process.
//!
//! Fourth sibling beside the bootstrap model in [`crate`], the client model in
//! [`crate::client`] and the output model in [`crate::output`]. It specifies AGENTD-7 through
//! AGENTD-16 in `spec/agentd.symspec.json`: the three protocol additions of issues #224, #225
//! and #226, which let a start request name its user and group, inherit the image's `ENV`,
//! and name its shell.
//!
//! # What the model settles
//!
//! Three questions a unit test answers only for the inputs somebody thought to write:
//!
//! * **Validation before spawn.** A request that names a user, group or shell the guest does
//!   not have is answered 400, and no child is spawned for it. The predecessor validated its
//!   timeout inside the waiter, after the child was already running, and a 400 for a running
//!   child leaves an orphan nobody can see. [`Behavior::SpawnBeforeResolve`] is that order,
//!   and the checker finds the orphan.
//! * **Environment precedence.** Five layers can define one variable, lowest first: the
//!   image environment the daemon inherited at startup (only with `inherit_image_env`), the
//!   passwd identity (`HOME`, `USER`, `LOGNAME` from the resolved row), the launch
//!   environment, and the request's own `env`. The identity sits above the image so a user
//!   demoted from root does not keep the image's root `HOME`, and below the launch and the
//!   request so a caller who sets one of the three wins.
//! * **The token never reaches a child.** It arrives in the run-hook payload, after the
//!   snapshot is taken, and the snapshot drops every `AGENTD_` variable besides. The model
//!   includes a daemon that exports the token into its own environment
//!   ([`Behavior::LiveUnfilteredWithExportedToken`]), to show which of the two defences each
//!   rejected design lacks.
//!
//! # Abstraction
//!
//! A variable's value is its [`Source`], not its bytes: what matters is which layer won.
//! The guest's databases are reduced to the cases that behave differently: a user given as
//! an integer with and without a passwd row, a name that resolves, a name that does not, and
//! an all-digit string with no row (read as a uid, the way `docker exec -u 1000` reads it).
//!
//! # Every always-property has a sometimes-property beside it
//!
//! As in [`crate::client`] and [`crate::output`]: a safety property over a space that never
//! reaches the interesting state measures nothing, so each claim is paired with a witness.

use stateright::{Model, Property};

/// Where a variable in a child's environment came from.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Source {
    /// The image's `ENV`, inherited by the daemon as the container `CMD`.
    Image,
    /// The daemon's own `AGENTD_*` configuration.
    Config,
    /// The agent token.
    Token,
    /// The resolved passwd row.
    Passwd,
    /// The launch environment from the run-hook payload.
    Launch,
    /// The start request's own `env`.
    Request,
}

/// The variables the model tracks.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Key {
    /// `HOME`: defined by the image, the passwd row, the launch and the request.
    Home,
    /// `USER` (and `LOGNAME`, which always travels with it): the passwd row only.
    User,
    /// `PATH`: defined by the image, the launch and the request.
    Path,
    /// A variable only the image defines, such as a `JAVA_HOME`.
    ImageOnly,
    /// `AGENTD_PORT`: daemon configuration present in its environment from startup.
    AgentdConfig,
    /// `AGENTD_TOKEN`: present only in a daemon that exports the token into its own
    /// environment, which the specified daemon never does.
    AgentdToken,
}

impl Key {
    /// Every key, in index order.
    pub const ALL: [Key; 6] = [
        Key::Home,
        Key::User,
        Key::Path,
        Key::ImageOnly,
        Key::AgentdConfig,
        Key::AgentdToken,
    ];

    fn index(self) -> usize {
        self as usize
    }

    /// Whether the name starts with `AGENTD_`, the prefix the snapshot drops.
    pub fn is_agentd(self) -> bool {
        matches!(self, Key::AgentdConfig | Key::AgentdToken)
    }
}

/// An environment: for each key, the layer that set it, if any.
#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct Env([Option<Source>; 6]);

impl Env {
    pub fn get(&self, key: Key) -> Option<Source> {
        self.0[key.index()]
    }

    pub fn set(&mut self, key: Key, source: Source) {
        self.0[key.index()] = Some(source);
    }

    /// Overlays `other` on `self`: each key `other` defines replaces this one's.
    ///
    /// The daemon's `Command::envs` in call order, one layer at a time.
    pub fn overlay(&mut self, other: &Env) {
        for key in Key::ALL {
            if let Some(source) = other.get(key) {
                self.set(key, source);
            }
        }
    }

    /// How many keys are defined.
    pub fn len(&self) -> u8 {
        self.0.iter().filter(|slot| slot.is_some()).count() as u8
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Whether any key carries `source`.
    pub fn carries(&self, source: Source) -> bool {
        self.0.contains(&Some(source))
    }

    /// The environment without its `AGENTD_*` keys.
    pub fn without_agentd(&self) -> Env {
        let mut kept = *self;
        for key in Key::ALL {
            if key.is_agentd() {
                kept.0[key.index()] = None;
            }
        }
        kept
    }
}

/// The request's `user`, reduced to the cases that behave differently.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum UserReq {
    /// Omitted: the child runs as the daemon's own user.
    Absent,
    /// An integer uid with a passwd row.
    IdWithRow,
    /// An integer uid with no passwd row.
    IdNoRow,
    /// A string naming a passwd row.
    NameKnown,
    /// A string naming nothing, not all digits.
    NameUnknown,
    /// An all-digit string naming no row: read as a uid.
    DigitsNoRow,
}

/// The request's `group`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum GroupReq {
    Absent,
    /// An integer gid.
    Id,
    /// A string naming a group row.
    NameKnown,
    /// A string naming nothing.
    NameUnknown,
}

/// The request's `shell`.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ShellReq {
    /// `false`: `command` is an argv.
    Off,
    /// `true`: `/bin/sh -c`.
    On,
    /// A name the guest has as an executable file on the searched directories.
    NamedPresent,
    /// A name the guest does not have.
    NamedMissing,
}

/// One start request.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Request {
    pub user: UserReq,
    pub group: GroupReq,
    pub shell: ShellReq,
    pub inherit_image_env: bool,
    /// The request's `env` sets `HOME`.
    pub sets_home: bool,
    /// The request's `env` sets `PATH`.
    pub sets_path: bool,
}

impl Request {
    /// Every request the model sends.
    pub fn all() -> Vec<Request> {
        let mut all = Vec::new();
        for user in [
            UserReq::Absent,
            UserReq::IdWithRow,
            UserReq::IdNoRow,
            UserReq::NameKnown,
            UserReq::NameUnknown,
            UserReq::DigitsNoRow,
        ] {
            for group in [
                GroupReq::Absent,
                GroupReq::Id,
                GroupReq::NameKnown,
                GroupReq::NameUnknown,
            ] {
                for shell in [
                    ShellReq::Off,
                    ShellReq::On,
                    ShellReq::NamedPresent,
                    ShellReq::NamedMissing,
                ] {
                    for inherit_image_env in [false, true] {
                        for sets_home in [false, true] {
                            for sets_path in [false, true] {
                                all.push(Request {
                                    user,
                                    group,
                                    shell,
                                    inherit_image_env,
                                    sets_home,
                                    sets_path,
                                });
                            }
                        }
                    }
                }
            }
        }
        all
    }

    fn env(&self) -> Env {
        let mut env = Env::default();
        if self.sets_home {
            env.set(Key::Home, Source::Request);
        }
        if self.sets_path {
            env.set(Key::Path, Source::Request);
        }
        env
    }

    /// Whether the request is in protocol 1's original vocabulary: integer ids and a
    /// boolean shell (AGENTD-16).
    pub fn is_protocol_one(&self) -> bool {
        matches!(
            self.user,
            UserReq::Absent | UserReq::IdWithRow | UserReq::IdNoRow
        ) && matches!(self.group, GroupReq::Absent | GroupReq::Id)
            && matches!(self.shell, ShellReq::Off | ShellReq::On)
    }

    /// Whether the user resolves to a passwd row.
    pub fn has_row(&self) -> bool {
        matches!(self.user, UserReq::IdWithRow | UserReq::NameKnown)
    }
}

/// The uid a child runs as.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Uid {
    /// The daemon's own (root).
    Daemon,
    /// The integer the request gave, or the digits it spelled.
    Requested,
    /// The uid of the passwd row the name resolved to.
    Row,
}

/// The gid a child runs as.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Gid {
    Daemon,
    Requested,
    /// The primary gid of the passwd row a *named* user resolved to.
    RowPrimary,
    /// The gid of the group row a named group resolved to.
    GroupRow,
}

/// What the child execs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Program {
    /// `command[0]` with `command[1..]`.
    Argv,
    /// `/bin/sh -c <script>`.
    BinSh,
    /// The resolved named shell, `-c <script>`.
    Resolved,
}

/// The stable 400 codes, each `ErrorBody::error` on the wire.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Code {
    /// `unknown_user`.
    UnknownUser,
    /// `unknown_group`.
    UnknownGroup,
    /// `unknown_shell`.
    UnknownShell,
}

/// A child as spawned.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Child {
    pub uid: Uid,
    pub gid: Gid,
    pub program: Program,
    pub env: Env,
}

/// The answer to the start request.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Answer {
    /// 200, `phase: running`.
    Started,
    /// 400 with this code.
    Refused(Code),
}

/// What the daemon knows when it plans a child.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct World {
    /// The image-env layer, as this daemon reads it.
    pub image: Env,
    pub launch: Env,
}

/// **The specification.** What the daemon spawns for a request, or which 400 it answers.
///
/// Resolution runs in the order user, group, shell, so the first unresolvable field names
/// the code. The environment is built one layer at a time, lowest first: the image (only
/// with `inherit_image_env`), the passwd identity, the launch, the request.
///
/// **Falsification** — 2026-09-24. Swapping the identity and launch layers made
/// `the_specified_daemon_satisfies_every_property` fail with a seven-state counterexample to
/// `AGENTD-9 HOME and USER follow request over launch over passwd`: a launch `HOME` under a
/// user with a passwd row was overwritten by the row. Restored after; BFS then explores
/// 21,323 unique states with every property holding.
pub fn specified(request: &Request, world: &World) -> Result<Child, Code> {
    plan(request, world, Layering::Specified, false, false)
}

/// Which layer order a daemon uses.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Layering {
    /// image < passwd < launch < request.
    Specified,
    /// image < launch < request < passwd: the identity applied last.
    IdentityOnTop,
    /// passwd < launch < request < image: an `extend` in the wrong direction.
    ImageOnTop,
}

fn plan(
    request: &Request,
    world: &World,
    layering: Layering,
    numeric_primary_group: bool,
    shell_fallback: bool,
) -> Result<Child, Code> {
    let uid = match request.user {
        UserReq::Absent => Uid::Daemon,
        UserReq::IdWithRow | UserReq::IdNoRow | UserReq::DigitsNoRow => Uid::Requested,
        UserReq::NameKnown => Uid::Row,
        UserReq::NameUnknown => return Err(Code::UnknownUser),
    };
    let gid = match request.group {
        GroupReq::Id => Gid::Requested,
        GroupReq::NameKnown => Gid::GroupRow,
        GroupReq::NameUnknown => return Err(Code::UnknownGroup),
        GroupReq::Absent => match request.user {
            UserReq::NameKnown => Gid::RowPrimary,
            UserReq::IdWithRow if numeric_primary_group => Gid::RowPrimary,
            _ => Gid::Daemon,
        },
    };
    let program = match request.shell {
        ShellReq::Off => Program::Argv,
        ShellReq::On => Program::BinSh,
        ShellReq::NamedPresent => Program::Resolved,
        ShellReq::NamedMissing if shell_fallback => Program::BinSh,
        ShellReq::NamedMissing => return Err(Code::UnknownShell),
    };

    let image = if request.inherit_image_env {
        world.image
    } else {
        Env::default()
    };
    let mut identity = Env::default();
    if request.has_row() {
        identity.set(Key::Home, Source::Passwd);
        identity.set(Key::User, Source::Passwd);
    }
    let layers = match layering {
        Layering::Specified => [image, identity, world.launch, request.env()],
        Layering::IdentityOnTop => [image, world.launch, request.env(), identity],
        Layering::ImageOnTop => [identity, world.launch, request.env(), image],
    };
    let mut env = Env::default();
    for layer in &layers {
        env.overlay(layer);
    }
    Ok(Child {
        uid,
        gid,
        program,
        env,
    })
}

/// Which daemon the model runs: the specification, or one deviation from it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// [`specified`], with the snapshot taken at startup and filtered.
    Specified,
    /// Spawns first and resolves after, answering 400 for a running child.
    SpawnBeforeResolve,
    /// Applies the passwd identity above the launch and request environments.
    IdentityOnTop,
    /// Applies the image environment above everything else.
    ImageOnTop,
    /// Applies the image environment whether or not the request asked.
    ImageAlways,
    /// Keeps `AGENTD_*` in the startup snapshot.
    UnfilteredSnapshot,
    /// Exports the token into its own environment at the run hook, and reads the image layer
    /// from its live environment at spawn without filtering. Both defences gone.
    LiveUnfilteredWithExportedToken,
    /// The same export and live read, with the `AGENTD_` filter kept: the filter alone
    /// still holds the token back.
    LiveFilteredWithExportedToken,
    /// Gives an integer user with a passwd row that row's primary gid.
    NumericPrimaryGroup,
    /// Runs `/bin/sh` when a named shell is missing.
    ShellFallback,
    /// Reports the live environment's key count on health, `AGENTD_*` included.
    HealthCountsLiveEnv,
}

impl Behavior {
    fn exports_token(self) -> bool {
        matches!(
            self,
            Behavior::LiveUnfilteredWithExportedToken | Behavior::LiveFilteredWithExportedToken
        )
    }

    /// Whether the image layer is read from the live environment at spawn.
    fn live_image(self) -> bool {
        self.exports_token()
    }

    /// The snapshot taken at startup, if this daemon takes one.
    fn snapshot(self, boot_env: &Env) -> Option<Env> {
        match self {
            Behavior::UnfilteredSnapshot => Some(*boot_env),
            _ if self.live_image() => None,
            _ => Some(boot_env.without_agentd()),
        }
    }

    /// The image layer as this daemon reads it at plan time.
    fn image_layer(self, state: &State) -> Env {
        match self {
            Behavior::LiveUnfilteredWithExportedToken => state.daemon_env,
            Behavior::LiveFilteredWithExportedToken => state.daemon_env.without_agentd(),
            _ => state.snapshot.unwrap_or_default(),
        }
    }

    fn decide(self, request: &Request, world: &World) -> Result<Child, Code> {
        let mut request = *request;
        if self == Behavior::ImageAlways {
            request.inherit_image_env = true;
        }
        let layering = match self {
            Behavior::IdentityOnTop => Layering::IdentityOnTop,
            Behavior::ImageOnTop => Layering::ImageOnTop,
            _ => Layering::Specified,
        };
        plan(
            &request,
            world,
            layering,
            self == Behavior::NumericPrimaryGroup,
            self == Behavior::ShellFallback,
        )
    }

    /// What `GET /v1/health` reports as the snapshot's key count.
    fn health_report(self, state: &State) -> Option<u8> {
        match self {
            Behavior::HealthCountsLiveEnv => Some(state.daemon_env.len()),
            _ => state.snapshot.map(|snapshot| snapshot.len()),
        }
    }
}

/// Where the one start request is in its handling.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Step {
    /// No request yet.
    Idle,
    /// Received, nothing decided.
    Received,
    /// Resolved: the plan or the refusal is known.
    Resolved,
    /// Answered.
    Answered,
}

/// The daemon from startup through one start request.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    /// The process has started and taken its snapshot.
    pub booted: bool,
    /// The daemon's own process environment right now.
    pub daemon_env: Env,
    /// The startup snapshot, when this daemon takes one.
    pub snapshot: Option<Env>,
    /// The run hook installed the token and the launch environment.
    pub token_installed: bool,
    pub launch: Env,
    pub request: Option<Request>,
    pub step: Step,
    /// The resolution, once made.
    pub resolution: Option<Result<Child, Code>>,
    /// The child, once spawned. At most one per request.
    pub child: Option<Child>,
    pub answer: Option<Answer>,
    /// What health reported, once asked.
    pub health: Option<Option<u8>>,
}

/// What can happen.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    /// The daemon starts as the container `CMD` with the image environment and its own
    /// `AGENTD_*` configuration.
    Boot,
    /// The platform's run hook, carrying the token and a launch environment.
    RunHook { home: bool, path: bool },
    /// `POST /v1/exec/start`.
    Receive(Request),
    /// Resolution of user, group and shell, and the environment plan.
    Resolve,
    /// The spawn.
    Spawn,
    /// The response.
    Answer,
    /// `GET /v1/health`.
    Health,
}

/// The model.
#[derive(Clone, Debug)]
pub struct ExecStart {
    pub behavior: Behavior,
}

impl ExecStart {
    pub fn new(behavior: Behavior) -> Self {
        Self { behavior }
    }

    /// The daemon's environment at startup: the image's `HOME`, `PATH` and one image-only
    /// variable, plus `AGENTD_PORT`. Never the token, which does not exist yet.
    pub fn boot_env() -> Env {
        let mut env = Env::default();
        env.set(Key::Home, Source::Image);
        env.set(Key::Path, Source::Image);
        env.set(Key::ImageOnly, Source::Image);
        env.set(Key::AgentdConfig, Source::Config);
        env
    }

    fn world(&self, state: &State) -> World {
        World {
            image: self.behavior.image_layer(state),
            launch: state.launch,
        }
    }

    /// Spawns whatever a spawn-first daemon can run before it has resolved anything: the
    /// command under `/bin/sh` as the daemon's own user.
    fn blind_child(&self, state: &State, request: &Request) -> Child {
        let mut unresolved = *request;
        unresolved.user = UserReq::Absent;
        unresolved.group = GroupReq::Absent;
        unresolved.shell = ShellReq::On;
        plan(
            &unresolved,
            &self.world(state),
            Layering::Specified,
            false,
            false,
        )
        .expect("an unresolved request always plans")
    }
}

impl Model for ExecStart {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        vec![State {
            booted: false,
            daemon_env: Env::default(),
            snapshot: None,
            token_installed: false,
            launch: Env::default(),
            request: None,
            step: Step::Idle,
            resolution: None,
            child: None,
            answer: None,
            health: None,
        }]
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        if !state.booted {
            actions.push(Action::Boot);
            return;
        }
        if state.health.is_none() {
            actions.push(Action::Health);
        }
        if !state.token_installed {
            for home in [false, true] {
                for path in [false, true] {
                    actions.push(Action::RunHook { home, path });
                }
            }
            return;
        }
        let spawn_first = self.behavior == Behavior::SpawnBeforeResolve;
        match state.step {
            Step::Idle => {
                for request in Request::all() {
                    actions.push(Action::Receive(request));
                }
            }
            Step::Received if spawn_first && state.child.is_none() => actions.push(Action::Spawn),
            Step::Received => actions.push(Action::Resolve),
            Step::Resolved => match state.resolution {
                Some(Ok(_)) if state.child.is_none() => actions.push(Action::Spawn),
                _ => actions.push(Action::Answer),
            },
            Step::Answered => {}
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = *last;
        match action {
            Action::Boot => {
                next.booted = true;
                next.daemon_env = Self::boot_env();
                next.snapshot = self.behavior.snapshot(&next.daemon_env);
            }
            Action::RunHook { home, path } => {
                next.token_installed = true;
                if home {
                    next.launch.set(Key::Home, Source::Launch);
                }
                if path {
                    next.launch.set(Key::Path, Source::Launch);
                }
                if self.behavior.exports_token() {
                    next.daemon_env.set(Key::AgentdToken, Source::Token);
                }
            }
            Action::Receive(request) => {
                next.request = Some(request);
                next.step = Step::Received;
            }
            Action::Resolve => {
                let request = last.request.expect("resolved only once received");
                next.resolution = Some(self.behavior.decide(&request, &self.world(last)));
                next.step = Step::Resolved;
            }
            Action::Spawn => {
                let request = last.request.expect("spawned only once received");
                next.child = Some(match last.resolution {
                    Some(Ok(child)) => child,
                    _ => self.blind_child(last, &request),
                });
            }
            Action::Answer => {
                next.answer = Some(match last.resolution {
                    Some(Ok(_)) => Answer::Started,
                    Some(Err(code)) => Answer::Refused(code),
                    None => unreachable!("answered only once resolved"),
                });
                next.step = Step::Answered;
            }
            Action::Health => next.health = Some(self.behavior.health_report(last)),
        }
        Some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        // Stated over what was spawned and answered, never over the resolution: a property
        // that read the plan would pass for a daemon that planned correctly and spawned
        // something else.
        fn started(s: &State) -> Option<(&Request, &Child)> {
            match (&s.request, &s.child, s.answer) {
                (Some(request), Some(child), Some(Answer::Started)) => Some((request, child)),
                _ => None,
            }
        }
        fn refused(s: &State) -> Option<(&Request, Code)> {
            match (&s.request, s.answer) {
                (Some(request), Some(Answer::Refused(code))) => Some((request, code)),
                _ => None,
            }
        }
        vec![
            // ── AGENTD-7 ──────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-7 a named user or group runs as its row's id",
                |_, s| match started(s) {
                    Some((r, c)) => {
                        (r.user != UserReq::NameKnown || c.uid == Uid::Row)
                            && (r.group != GroupReq::NameKnown || c.gid == Gid::GroupRow)
                    }
                    None => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-7 witness: a child runs as a user named by string",
                |_, s| started(s).is_some_and(|(r, _)| r.user == UserReq::NameKnown),
            ),
            Property::<Self>::sometimes(
                "AGENTD-7 witness: a child runs in a group named by string",
                |_, s| started(s).is_some_and(|(r, _)| r.group == GroupReq::NameKnown),
            ),
            // ── AGENTD-8 ──────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-8 a request refused for an unknown user or group spawned no child",
                |_, s| match refused(s) {
                    Some((_, Code::UnknownUser | Code::UnknownGroup)) => s.child.is_none(),
                    _ => true,
                },
            ),
            Property::<Self>::always(
                "AGENTD-8 an unknown user or group is never started",
                |_, s| {
                    started(s).is_none_or(|(r, _)| {
                        r.user != UserReq::NameUnknown && r.group != GroupReq::NameUnknown
                    })
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-8 witness: an unknown user is refused unknown_user",
                |_, s| {
                    refused(s).is_some_and(|(r, code)| {
                        r.user == UserReq::NameUnknown && code == Code::UnknownUser
                    })
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-8 witness: an unknown group is refused unknown_group",
                |_, s| {
                    refused(s).is_some_and(|(r, code)| {
                        r.group == GroupReq::NameUnknown && code == Code::UnknownGroup
                    })
                },
            ),
            // ── AGENTD-9 ──────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-9 HOME and USER follow request over launch over passwd",
                |_, s| match started(s) {
                    Some((r, c)) if r.has_row() => {
                        let home = if r.sets_home {
                            Source::Request
                        } else if s.launch.get(Key::Home).is_some() {
                            Source::Launch
                        } else {
                            Source::Passwd
                        };
                        c.env.get(Key::Home) == Some(home)
                            && c.env.get(Key::User) == Some(Source::Passwd)
                    }
                    Some((_, c)) => c.env.get(Key::User).is_none(),
                    None => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-9 witness: the passwd HOME reaches a child",
                |_, s| {
                    started(s).is_some_and(|(_, c)| c.env.get(Key::Home) == Some(Source::Passwd))
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-9 witness: a request HOME overrides the passwd HOME",
                |_, s| {
                    started(s).is_some_and(|(r, c)| {
                        r.has_row() && c.env.get(Key::Home) == Some(Source::Request)
                    })
                },
            ),
            // ── AGENTD-10 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-10 without inherit_image_env no daemon variable reaches a child",
                |_, s| match started(s) {
                    Some((r, c)) if !r.inherit_image_env => {
                        !c.env.carries(Source::Image)
                            && !c.env.carries(Source::Config)
                            && !c.env.carries(Source::Token)
                    }
                    _ => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-10 witness: a child starts without inheriting beside a non-empty snapshot",
                |_, s| {
                    started(s).is_some_and(|(r, _)| !r.inherit_image_env)
                        && s.snapshot.is_some_and(|snapshot| !snapshot.is_empty())
                },
            ),
            // ── AGENTD-11 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-11 with inherit_image_env each key takes its highest layer",
                |_, s| match started(s) {
                    Some((r, c)) if r.inherit_image_env => {
                        let launch = |key| s.launch.get(key).is_some();
                        let path = if r.sets_path {
                            Source::Request
                        } else if launch(Key::Path) {
                            Source::Launch
                        } else {
                            Source::Image
                        };
                        let home = if r.sets_home {
                            Source::Request
                        } else if launch(Key::Home) {
                            Source::Launch
                        } else if r.has_row() {
                            Source::Passwd
                        } else {
                            Source::Image
                        };
                        c.env.get(Key::ImageOnly) == Some(Source::Image)
                            && c.env.get(Key::Path) == Some(path)
                            && c.env.get(Key::Home) == Some(home)
                    }
                    _ => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-11 witness: an image-only variable reaches a child",
                |_, s| {
                    started(s)
                        .is_some_and(|(_, c)| c.env.get(Key::ImageOnly) == Some(Source::Image))
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-11 witness: a launch PATH overrides the image PATH",
                |_, s| {
                    started(s).is_some_and(|(r, c)| {
                        r.inherit_image_env && c.env.get(Key::Path) == Some(Source::Launch)
                    })
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-11 witness: a demoted user's passwd HOME overrides the image HOME",
                |_, s| {
                    started(s).is_some_and(|(r, c)| {
                        r.inherit_image_env && c.env.get(Key::Home) == Some(Source::Passwd)
                    })
                },
            ),
            // ── AGENTD-12 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-12 no child carries the agent token or AGENTD_ configuration",
                |_, s| {
                    s.child.is_none_or(|c| {
                        !c.env.carries(Source::Token) && !c.env.carries(Source::Config)
                    })
                },
            ),
            Property::<Self>::always(
                "AGENTD-12 the snapshot holds no AGENTD_ variable",
                |_, s| {
                    s.snapshot.is_none_or(|snapshot| {
                        Key::ALL
                            .iter()
                            .all(|key| !key.is_agentd() || snapshot.get(*key).is_none())
                    })
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-12 witness: a child inherits the image env after the token is installed",
                |_, s| s.token_installed && started(s).is_some_and(|(r, _)| r.inherit_image_env),
            ),
            // ── AGENTD-13 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-13 health reports the snapshot's key count",
                |_, s| match s.health {
                    Some(report) => report == s.snapshot.map(|snapshot| snapshot.len()),
                    None => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-13 witness: health reports a non-empty snapshot",
                |_, s| matches!(s.health, Some(Some(keys)) if keys > 0),
            ),
            // ── AGENTD-14 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-14 a named shell runs as the resolved shell",
                |_, s| {
                    started(s).is_none_or(|(r, c)| {
                        r.shell != ShellReq::NamedPresent || c.program == Program::Resolved
                    })
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-14 witness: a child runs under a named shell",
                |_, s| started(s).is_some_and(|(_, c)| c.program == Program::Resolved),
            ),
            // ── AGENTD-15 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-15 a missing shell is never started and spawns no child",
                |_, s| match (&s.request, s.answer) {
                    (Some(r), Some(_)) if r.shell == ShellReq::NamedMissing => {
                        s.child.is_none() && s.answer != Some(Answer::Started)
                    }
                    _ => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-15 witness: a missing shell is refused unknown_shell",
                |_, s| refused(s).is_some_and(|(_, code)| code == Code::UnknownShell),
            ),
            // ── AGENTD-16 ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-16 integer ids and a boolean shell keep their protocol-1 meaning",
                |_, s| match started(s) {
                    Some((r, c)) if r.is_protocol_one() => {
                        let uid = match r.user {
                            UserReq::Absent => Uid::Daemon,
                            _ => Uid::Requested,
                        };
                        let gid = match r.group {
                            GroupReq::Id => Gid::Requested,
                            _ => Gid::Daemon,
                        };
                        let program = match r.shell {
                            ShellReq::On => Program::BinSh,
                            _ => Program::Argv,
                        };
                        c.uid == uid && c.gid == gid && c.program == program
                    }
                    _ => true,
                },
            ),
            Property::<Self>::sometimes(
                "AGENTD-16 witness: an integer user with a passwd row runs",
                |_, s| started(s).is_some_and(|(r, _)| r.user == UserReq::IdWithRow),
            ),
            // ── liveness ──────────────────────────────────────────────────────
            //
            // Sound because the model is acyclic: every action moves a state that no action
            // moves back.
            Property::<Self>::eventually("every received request is answered", |_, s| {
                s.answer.is_some()
            }),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<ExecStart> {
        ExecStart::new(behavior).checker().spawn_bfs().join()
    }

    /// The headline: the specified daemon satisfies AGENTD-7 through AGENTD-16 for every
    /// request against every launch environment, and witnesses each case.
    #[test]
    fn the_specified_daemon_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 3_000,
            "a space this small could not hold every request: {}",
            checker.unique_state_count()
        );
    }

    /// **The predecessor's order.** Spawning before resolving answers 400 for a child that
    /// is already running.
    #[test]
    fn spawning_before_resolving_orphans_a_child_behind_a_400() {
        let checker = checked(Behavior::SpawnBeforeResolve);
        let path = checker
            .assert_any_discovery(
                "AGENTD-8 a request refused for an unknown user or group spawned no child",
            )
            .into_actions();
        assert!(
            path.iter().position(|a| *a == Action::Spawn)
                < path.iter().position(|a| *a == Action::Resolve),
            "the orphan needs the spawn first, got {path:?}"
        );
        checker
            .assert_any_discovery("AGENTD-15 a missing shell is never started and spawns no child");
    }

    /// The identity layer above the caller's: a request that set `HOME` loses it.
    #[test]
    fn the_identity_on_top_overwrites_a_callers_home() {
        checked(Behavior::IdentityOnTop)
            .assert_any_discovery("AGENTD-9 HOME and USER follow request over launch over passwd");
    }

    /// The image layer above the caller's: a launch `PATH` loses to the image's.
    #[test]
    fn the_image_on_top_overwrites_the_launch_path() {
        let checker = checked(Behavior::ImageOnTop);
        checker.assert_any_discovery(
            "AGENTD-11 with inherit_image_env each key takes its highest layer",
        );
        checker.assert_no_discovery(
            "AGENTD-10 without inherit_image_env no daemon variable reaches a child",
        );
    }

    /// Inheriting whether or not asked is the default property broken: the image env
    /// reaches a child that asked for the exact launch map.
    #[test]
    fn inheriting_unasked_breaks_the_exact_environment() {
        checked(Behavior::ImageAlways).assert_any_discovery(
            "AGENTD-10 without inherit_image_env no daemon variable reaches a child",
        );
    }

    /// An unfiltered snapshot carries `AGENTD_PORT` into a child that inherits.
    #[test]
    fn an_unfiltered_snapshot_leaks_configuration() {
        let checker = checked(Behavior::UnfilteredSnapshot);
        checker.assert_any_discovery("AGENTD-12 the snapshot holds no AGENTD_ variable");
        checker.assert_any_discovery(
            "AGENTD-12 no child carries the agent token or AGENTD_ configuration",
        );
    }

    /// **Both defences gone.** A daemon that exports the token and reads its live
    /// environment unfiltered hands the token to the first child that inherits.
    #[test]
    fn a_live_unfiltered_read_of_an_exported_token_leaks_it() {
        let checker = checked(Behavior::LiveUnfilteredWithExportedToken);
        let path = checker
            .assert_any_discovery(
                "AGENTD-12 no child carries the agent token or AGENTD_ configuration",
            )
            .into_actions();
        assert!(
            path.iter().any(|a| matches!(a, Action::RunHook { .. })),
            "the token exists only after the run hook, got {path:?}"
        );
    }

    /// **One defence left.** The same live read with the `AGENTD_` filter keeps the token
    /// out: the filter alone holds, which is why the specification keeps it even though the
    /// startup snapshot is taken before any token exists.
    #[test]
    fn the_agentd_filter_alone_holds_the_token_back() {
        checked(Behavior::LiveFilteredWithExportedToken).assert_no_discovery(
            "AGENTD-12 no child carries the agent token or AGENTD_ configuration",
        );
    }

    /// An integer uid that picked up its row's primary gid changes what protocol 1 meant.
    #[test]
    fn a_numeric_user_taking_its_rows_group_breaks_protocol_one() {
        checked(Behavior::NumericPrimaryGroup).assert_any_discovery(
            "AGENTD-16 integer ids and a boolean shell keep their protocol-1 meaning",
        );
    }

    /// Falling back to `/bin/sh` is the exit 127 the issue wanted gone, one step removed: a
    /// caller who asked for bash gets dash's semantics and no signal.
    #[test]
    fn falling_back_to_sh_starts_a_missing_shell() {
        checked(Behavior::ShellFallback)
            .assert_any_discovery("AGENTD-15 a missing shell is never started and spawns no child");
    }

    /// Counting the live environment reports `AGENTD_PORT` as an image key.
    #[test]
    fn counting_the_live_environment_misreports_the_snapshot() {
        checked(Behavior::HealthCountsLiveEnv)
            .assert_any_discovery("AGENTD-13 health reports the snapshot's key count");
    }

    /// The table the daemon's tests mirror: the specification's answer for the cases the
    /// issues name.
    #[test]
    fn the_specification_table() {
        let mut launch = Env::default();
        launch.set(Key::Path, Source::Launch);
        let world = World {
            image: ExecStart::boot_env().without_agentd(),
            launch,
        };
        let base = Request {
            user: UserReq::Absent,
            group: GroupReq::Absent,
            shell: ShellReq::Off,
            inherit_image_env: false,
            sets_home: false,
            sets_path: false,
        };

        // Flag unset, no user: exactly the launch map.
        let child = specified(&base, &world).expect("starts");
        assert_eq!(child.env, launch);

        // A named user: its row's uid and primary gid, HOME and USER from the row.
        let named = Request {
            user: UserReq::NameKnown,
            ..base
        };
        let child = specified(&named, &world).expect("starts");
        assert_eq!((child.uid, child.gid), (Uid::Row, Gid::RowPrimary));
        assert_eq!(child.env.get(Key::Home), Some(Source::Passwd));

        // The request's HOME wins over the row's.
        let overridden = Request {
            sets_home: true,
            ..named
        };
        let child = specified(&overridden, &world).expect("starts");
        assert_eq!(child.env.get(Key::Home), Some(Source::Request));

        // The three refusals, in resolution order.
        for (request, code) in [
            (
                Request {
                    user: UserReq::NameUnknown,
                    shell: ShellReq::NamedMissing,
                    ..base
                },
                Code::UnknownUser,
            ),
            (
                Request {
                    group: GroupReq::NameUnknown,
                    shell: ShellReq::NamedMissing,
                    ..base
                },
                Code::UnknownGroup,
            ),
            (
                Request {
                    shell: ShellReq::NamedMissing,
                    ..base
                },
                Code::UnknownShell,
            ),
        ] {
            assert_eq!(specified(&request, &world), Err(code), "{request:?}");
        }

        // Flag set: image < passwd < launch < request.
        let inheriting = Request {
            inherit_image_env: true,
            ..named
        };
        let child = specified(&inheriting, &world).expect("starts");
        assert_eq!(child.env.get(Key::ImageOnly), Some(Source::Image));
        assert_eq!(child.env.get(Key::Home), Some(Source::Passwd));
        assert_eq!(child.env.get(Key::Path), Some(Source::Launch));
        assert_eq!(child.env.get(Key::AgentdConfig), None);
    }
}
