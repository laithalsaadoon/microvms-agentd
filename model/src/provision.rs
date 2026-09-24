// SPDX-License-Identifier: Apache-2.0
//! A checked model of how `microvms-core` provisions the `agentd` daemon binary.
//!
//! Fourth sibling beside the daemon model in [`crate`], the client model in
//! [`crate::client`], and the output model in [`crate::output`]. It specifies BIND-17
//! through BIND-20 in `spec/core.symspec.json`, which issue #219 asked for when it moved
//! the CLI's provisioning chain into `microvms-core` so the Python and Node bindings can
//! call it.
//!
//! # What one request can be answered from
//!
//! A request names a version (or none, which means the core's own version) and may carry
//! a caller-supplied binary. It is answered from, in order:
//!
//! 1. the caller's binary, which must exist and be an aarch64 ELF (BIND-20), and which
//!    never falls through to the cache or a fetch when it is refused (BIND-17);
//! 2. the cache entry for exactly the requested version, served only when its bytes still
//!    match the digest recorded when they were verified (BIND-19);
//! 3. a fetch of the release asset for that version, verified by `gh attestation verify`
//!    or, when `gh` cannot download, by the release's `SHA256SUMS` entry (BIND-18), and
//!    checked for aarch64 before it is installed (BIND-20).
//!
//! # The policy the checker compares against five others
//!
//! [`Behavior::Specified`] is the policy `microvms-core/src/provision.rs` implements. The
//! others are the ways it could plausibly be written instead, and each breaks a property
//! the checker names:
//!
//! * [`Behavior::Today`] is the CLI when #219 was filed: a cache entry was served because
//!   it existed, and `$MICROVM_AGENTD` because it existed. A corrupted cache entry and an
//!   x86 binary in the override were both handed to the image build.
//! * [`Behavior::Unversioned`] keys the cache by name alone, so a request for one version
//!   is answered with another version's binary.
//! * [`Behavior::LaunderAttestation`] falls through to the `curl` checksum when
//!   `gh attestation verify` refuses bytes `gh` downloaded, so bytes that failed
//!   provenance are served under a weaker check that cannot see what was wrong with them.
//! * [`Behavior::CacheBeforeVerify`] downloads straight into the cache path. A process that
//!   dies between the download and the verification leaves unverified bytes where the next
//!   request trusts them.
//! * [`Behavior::WarnOnUnverified`] serves a `curl` download whose release has no
//!   `SHA256SUMS`, with a warning. Issue #219 rules that out: a fetch that cannot be
//!   verified is an error, never a warning.
//!
//! # Tampering is a digest mismatch, not an attacker model
//!
//! [`Action::Tamper`] changes a cache entry's bytes after it was recorded: a truncated
//! write, a disk error, a user copying a different binary over it. The digest record sits
//! beside the entry in the same directory, so a writer who can replace both is not
//! stopped, and the model does not pretend otherwise; that writer could replace the
//! caller's binary too. What the model settles is that nothing the cache holds is served
//! unless it is still the bytes that were verified.
//!
//! # Every always-property has a sometimes-property beside it
//!
//! As in [`crate::client`]: a safety property over a space that never reaches the
//! interesting state measures nothing, so each claim is paired with a witness that the
//! checker got there.

use stateright::{Model, Property};

/// How many versions the model distinguishes. Version 0 is the core's own version, which
/// is what a request without a version asks for.
pub const VERSIONS: u8 = 2;

/// The core's own version, the default.
pub const CORE_VERSION: u8 = 0;

/// The version a request names.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Wanted {
    /// No version given: the core's own.
    Default,
    /// This version.
    Explicit(u8),
}

impl Wanted {
    /// The version a request is resolved against.
    pub fn resolve(self) -> u8 {
        match self {
            Wanted::Default => CORE_VERSION,
            Wanted::Explicit(version) => version,
        }
    }
}

/// A caller-supplied binary, reduced to what the policy can observe about it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Supplied {
    /// No binary given: the request is answered from the cache or a fetch.
    Nothing,
    /// An aarch64 ELF executable.
    Arm,
    /// An ELF binary for another machine, or not an ELF file at all.
    NotArm,
    /// A path that names nothing.
    Missing,
}

/// What `gh` does for one fetch.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Gh {
    /// `gh` cannot download: not installed, or refusing to run unauthenticated.
    Unavailable,
    /// `gh` downloads the asset and `gh attestation verify` accepts it.
    Attested,
    /// `gh` downloads the asset and `gh attestation verify` refuses it.
    Unattested,
}

/// What `curl` finds on the release for one fetch.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Curl {
    /// The download fails.
    Fails,
    /// The asset downloads, and the release has no `SHA256SUMS` (every tag before v0.5.0).
    NoSums,
    /// The asset downloads and its `SHA256SUMS` entry matches.
    SumsMatch,
    /// The asset downloads and its `SHA256SUMS` entry does not match.
    SumsMismatch,
}

/// The release, as one fetch sees it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Release {
    pub gh: Gh,
    pub curl: Curl,
    /// The asset is an aarch64 ELF.
    pub arm: bool,
}

/// How a fetch's bytes were proven, or why they were not.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Verdict {
    /// `gh attestation verify` accepted them.
    Attestation,
    /// The `SHA256SUMS` entry matched.
    Checksum,
    /// Served with a warning, unproven. Only [`Behavior::WarnOnUnverified`] answers this.
    Unverified,
    /// Refused: the bytes are discarded.
    Refused,
}

/// Why a request was refused.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Refusal {
    /// The caller's path names nothing.
    MissingBinary,
    /// The binary is not an aarch64 ELF.
    NotArm,
    /// The fetch could not be verified.
    Unverified,
}

/// Where a served binary came from.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Source {
    Caller,
    Cache,
    Fetched,
}

/// A binary handed back to the caller, with what the properties need to know about it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Served {
    pub source: Source,
    /// The version the bytes are, which a property compares with the version requested.
    pub version: u8,
    pub arm: bool,
    /// The bytes were proven by attestation or checksum. False for a caller's binary,
    /// whose provenance is the caller's business.
    pub verified: bool,
    /// `gh attestation verify` refused these bytes at some point.
    pub attestation_failed: bool,
    /// A cache hit's bytes still match their digest record, and a record exists.
    pub matches_record: bool,
    /// A fetch verified by the `SHA256SUMS` entry rather than by attestation.
    pub checksum: bool,
}

/// How one request ended.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Outcome {
    Served(Served),
    Refused(Refusal),
    /// The process died mid-fetch.
    Crashed,
}

/// One version's cache entry.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Entry {
    /// The version the bytes are. Equal to the slot's version except under
    /// [`Behavior::Unversioned`], which has one slot for every version.
    pub version: u8,
    pub arm: bool,
    /// The bytes were proven before they were written here.
    pub verified: bool,
    /// A digest record was written beside the entry. False for an entry an older client
    /// installed before digest records existed.
    pub recorded: bool,
    /// The bytes still match the digest record.
    pub intact: bool,
    /// `gh attestation verify` refused these bytes.
    pub attestation_failed: bool,
}

impl Entry {
    /// What the specified policy serves from the cache.
    fn trusted(self) -> bool {
        self.recorded && self.intact
    }
}

/// Where the current request is.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Phase {
    /// No request in flight.
    Idle,
    /// The cache could not answer; the release is about to be read.
    Fetching { version: u8 },
    /// The asset is on disk (in a partial file, or in the cache under
    /// [`Behavior::CacheBeforeVerify`]) and not yet verified.
    Downloaded { version: u8, release: Release },
}

/// The provisioning policy the model runs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Behavior {
    /// `microvms-core/src/provision.rs`.
    Specified,
    /// The CLI when #219 was filed.
    Today,
    /// A cache keyed by name alone.
    Unversioned,
    /// A refused attestation falls through to the checksum.
    LaunderAttestation,
    /// Downloads go straight into the cache path.
    CacheBeforeVerify,
    /// An unverifiable `curl` download is served with a warning.
    WarnOnUnverified,
}

/// **The specification of a fetch's verification.** One pure function over what the
/// release does, mirrored by the table test in `microvms-core/src/provision.rs`.
///
/// * `gh` downloads and attestation passes: [`Verdict::Attestation`].
/// * `gh` downloads and attestation fails: [`Verdict::Refused`], without trying `curl`.
/// * `gh` cannot download: `curl`, and only a matching `SHA256SUMS` entry passes.
///
/// **Falsification** — 2026-09-24. Returning `verify(Behavior::Specified, Release { gh:
/// Gh::Unavailable, curl, arm })` for the `Unattested` arm (the laundering fall-through)
/// made `the_specified_policy_satisfies_every_property` fail with a ten-entry
/// counterexample to `BIND-18 bytes that failed attestation are never served`; restored
/// after.
pub fn verify(behavior: Behavior, release: Release) -> Verdict {
    match release.gh {
        Gh::Attested => Verdict::Attestation,
        Gh::Unattested if behavior == Behavior::LaunderAttestation => verify(
            behavior,
            Release {
                gh: Gh::Unavailable,
                ..release
            },
        ),
        Gh::Unattested => Verdict::Refused,
        Gh::Unavailable => match release.curl {
            Curl::SumsMatch => Verdict::Checksum,
            Curl::NoSums if behavior == Behavior::WarnOnUnverified => Verdict::Unverified,
            Curl::Fails | Curl::NoSums | Curl::SumsMismatch => Verdict::Refused,
        },
    }
}

/// The model's knobs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Config {
    pub behavior: Behavior,
    /// Requests in one run. Three is the least that gets a fetch, a tamper, and the
    /// request that must notice it into one path, with a caller-supplied request beside.
    pub max_requests: u8,
    /// Cache entries the environment may change behind the client's back.
    pub max_tampers: u8,
}

impl Config {
    pub fn specified() -> Self {
        Self::with(Behavior::Specified)
    }

    pub fn with(behavior: Behavior) -> Self {
        Self {
            behavior,
            max_requests: 3,
            max_tampers: 1,
        }
    }
}

/// One run: a machine's cache across a few requests.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct State {
    /// One slot per version.
    pub cache: [Option<Entry>; VERSIONS as usize],
    pub phase: Phase,
    pub requests: u8,
    pub tampers: u8,
    /// The request in flight or last answered.
    pub request: Option<(Wanted, Supplied)>,
    /// How that request ended, once it has.
    pub outcome: Option<Outcome>,
    /// The request found a cache entry it would not serve and discarded it (BIND-19).
    pub discarded: bool,
    /// The request fetched although the cache held a trusted entry for its version.
    pub fetched_over_trusted: bool,
}

/// What can happen.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    /// A caller asks for a binary.
    Request(Wanted, Supplied),
    /// The release is read: `gh`, then `curl` if `gh` could not download.
    Download(Release),
    /// The downloaded bytes are verified, checked for aarch64, and installed or discarded.
    Verify,
    /// The process dies between the download and the verification.
    Crash,
    /// A cache entry's bytes change after they were recorded.
    Tamper(u8),
}

/// The model.
#[derive(Clone, Debug)]
pub struct Provisioning {
    pub cfg: Config,
}

impl Provisioning {
    pub fn new(cfg: Config) -> Self {
        Self { cfg }
    }

    fn slot(&self, version: u8) -> usize {
        match self.cfg.behavior {
            Behavior::Unversioned => 0,
            _ => version as usize,
        }
    }

    /// Answers a request from the caller's binary or the cache, or starts a fetch.
    fn request(&self, next: &mut State, wanted: Wanted, supplied: Supplied) {
        let version = wanted.resolve();
        let behavior = self.cfg.behavior;
        next.request = Some((wanted, supplied));
        next.outcome = None;
        next.discarded = false;
        next.fetched_over_trusted = false;
        next.requests += 1;

        match supplied {
            Supplied::Nothing => {}
            Supplied::Missing => {
                next.outcome = Some(Outcome::Refused(Refusal::MissingBinary));
                return;
            }
            // The CLI when #219 was filed checked only that the override existed.
            Supplied::NotArm if behavior != Behavior::Today => {
                next.outcome = Some(Outcome::Refused(Refusal::NotArm));
                return;
            }
            Supplied::Arm | Supplied::NotArm => {
                next.outcome = Some(Outcome::Served(Served {
                    source: Source::Caller,
                    version,
                    arm: supplied == Supplied::Arm,
                    verified: false,
                    attestation_failed: false,
                    matches_record: false,
                    checksum: false,
                }));
                return;
            }
        }

        let slot = self.slot(version);
        if let Some(entry) = next.cache[slot] {
            let served = match behavior {
                // Served because it existed.
                Behavior::Today | Behavior::Unversioned => true,
                _ => entry.trusted(),
            };
            if served {
                next.outcome = Some(Outcome::Served(Served {
                    source: Source::Cache,
                    version: entry.version,
                    arm: entry.arm,
                    verified: entry.verified,
                    attestation_failed: entry.attestation_failed,
                    matches_record: entry.trusted(),
                    checksum: false,
                }));
                return;
            }
            next.cache[slot] = None;
            next.discarded = true;
        }
        next.fetched_over_trusted = next.cache[version as usize]
            .is_some_and(|entry| entry.version == version && entry.trusted());
        next.phase = Phase::Fetching { version };
    }

    fn verified(&self, next: &mut State, version: u8, release: Release) {
        let behavior = self.cfg.behavior;
        let slot = self.slot(version);
        next.phase = Phase::Idle;
        let verdict = verify(behavior, release);
        let refusal = match verdict {
            Verdict::Refused => Some(Refusal::Unverified),
            _ if !release.arm => Some(Refusal::NotArm),
            _ => None,
        };
        if let Some(refusal) = refusal {
            // The partial file is discarded. Under CacheBeforeVerify there is no partial:
            // the bytes are already where the next request looks, and stay there.
            next.outcome = Some(Outcome::Refused(refusal));
            return;
        }
        let entry = Entry {
            version,
            arm: release.arm,
            verified: verdict != Verdict::Unverified,
            recorded: true,
            intact: true,
            attestation_failed: release.gh == Gh::Unattested,
        };
        next.cache[slot] = Some(entry);
        next.outcome = Some(Outcome::Served(Served {
            source: Source::Fetched,
            version,
            arm: entry.arm,
            verified: entry.verified,
            attestation_failed: entry.attestation_failed,
            matches_record: true,
            checksum: verdict == Verdict::Checksum,
        }));
    }

    const RELEASES: [Release; 24] = {
        let gh = [Gh::Unavailable, Gh::Attested, Gh::Unattested];
        let curl = [
            Curl::Fails,
            Curl::NoSums,
            Curl::SumsMatch,
            Curl::SumsMismatch,
        ];
        let mut out = [Release {
            gh: Gh::Unavailable,
            curl: Curl::Fails,
            arm: true,
        }; 24];
        let mut i = 0;
        while i < 24 {
            out[i] = Release {
                gh: gh[i / 8],
                curl: curl[(i / 2) % 4],
                arm: i % 2 == 0,
            };
            i += 1;
        }
        out
    };
}

/// The served binary and the request it answered, when the last request was served.
fn served(state: &State) -> Option<(Served, Wanted, Supplied)> {
    match (state.outcome, state.request) {
        (Some(Outcome::Served(served)), Some((wanted, supplied))) => {
            Some((served, wanted, supplied))
        }
        _ => None,
    }
}

impl Model for Provisioning {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        let empty = State {
            cache: [None; VERSIONS as usize],
            phase: Phase::Idle,
            requests: 0,
            tampers: 0,
            request: None,
            outcome: None,
            discarded: false,
            fetched_over_trusted: false,
        };
        // A machine an older client provisioned: verified bytes with no digest record.
        let mut legacy = empty;
        legacy.cache[CORE_VERSION as usize] = Some(Entry {
            version: CORE_VERSION,
            arm: true,
            verified: true,
            recorded: false,
            intact: true,
            attestation_failed: false,
        });
        vec![empty, legacy]
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        match state.phase {
            Phase::Idle => {
                if state.requests < self.cfg.max_requests {
                    for supplied in [
                        Supplied::Nothing,
                        Supplied::Arm,
                        Supplied::NotArm,
                        Supplied::Missing,
                    ] {
                        actions.push(Action::Request(Wanted::Default, supplied));
                    }
                    for version in 0..VERSIONS {
                        actions.push(Action::Request(
                            Wanted::Explicit(version),
                            Supplied::Nothing,
                        ));
                    }
                }
                if state.tampers < self.cfg.max_tampers {
                    for slot in 0..VERSIONS {
                        if state.cache[slot as usize].is_some_and(|entry| entry.intact) {
                            actions.push(Action::Tamper(slot));
                        }
                    }
                }
            }
            Phase::Fetching { .. } => {
                for release in Self::RELEASES {
                    actions.push(Action::Download(release));
                }
            }
            Phase::Downloaded { .. } => {
                actions.push(Action::Verify);
                actions.push(Action::Crash);
            }
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = *last;
        match action {
            Action::Request(wanted, supplied) => self.request(&mut next, wanted, supplied),
            Action::Download(release) => {
                let Phase::Fetching { version } = last.phase else {
                    return None;
                };
                next.phase = Phase::Downloaded { version, release };
                if self.cfg.behavior == Behavior::CacheBeforeVerify {
                    // The download lands where a cache hit reads, and its digest is taken
                    // on the way in, before anything proved the bytes.
                    next.cache[version as usize] = Some(Entry {
                        version,
                        arm: release.arm,
                        verified: false,
                        recorded: true,
                        intact: true,
                        attestation_failed: release.gh == Gh::Unattested,
                    });
                }
            }
            Action::Verify => {
                let Phase::Downloaded { version, release } = last.phase else {
                    return None;
                };
                self.verified(&mut next, version, release);
            }
            Action::Crash => {
                next.phase = Phase::Idle;
                next.outcome = Some(Outcome::Crashed);
            }
            Action::Tamper(slot) => {
                let entry = next.cache[slot as usize].as_mut()?;
                entry.intact = false;
                next.tampers += 1;
            }
        }
        (next != *last).then_some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            // ── BIND-17 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-17 a caller-supplied binary is served or refused, never replaced",
                |_, s| match s.request {
                    Some((_, supplied)) if supplied != Supplied::Nothing => {
                        s.phase == Phase::Idle
                            && matches!(
                                s.outcome,
                                Some(Outcome::Refused(_))
                                    | Some(Outcome::Served(Served {
                                        source: Source::Caller,
                                        ..
                                    }))
                            )
                    }
                    _ => true,
                },
            ),
            Property::<Self>::always(
                "BIND-17 a served binary is the version requested, the core's by default",
                |_, s| {
                    served(s).is_none_or(|(served, wanted, _)| {
                        served.version == wanted.resolve()
                            && (wanted != Wanted::Default || served.version == CORE_VERSION)
                    })
                },
            ),
            Property::<Self>::always(
                "BIND-17 a trusted cache entry is served rather than fetched over",
                |_, s| !s.fetched_over_trusted,
            ),
            Property::<Self>::sometimes("BIND-17 witness: a caller's binary is served", |_, s| {
                served(s).is_some_and(|(served, _, _)| served.source == Source::Caller)
            }),
            Property::<Self>::sometimes(
                "BIND-17 witness: a default request is a cache hit",
                |_, s| {
                    served(s).is_some_and(|(served, wanted, _)| {
                        served.source == Source::Cache && wanted == Wanted::Default
                    })
                },
            ),
            Property::<Self>::sometimes(
                "BIND-17 witness: another version is fetched beside the core's cached one",
                |_, s| {
                    served(s).is_some_and(|(served, _, _)| {
                        served.source == Source::Fetched && served.version != CORE_VERSION
                    }) && s.cache[CORE_VERSION as usize].is_some()
                },
            ),
            // ── BIND-18 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-18 fetched bytes are served only after verification",
                |_, s| {
                    served(s).is_none_or(|(served, _, _)| {
                        served.source == Source::Caller || served.verified
                    })
                },
            ),
            Property::<Self>::always(
                "BIND-18 bytes that failed attestation are never served",
                |_, s| served(s).is_none_or(|(served, _, _)| !served.attestation_failed),
            ),
            Property::<Self>::always("BIND-18 the cache holds only verified bytes", |_, s| {
                s.cache.iter().flatten().all(|entry| entry.verified)
            }),
            Property::<Self>::sometimes(
                "BIND-18 witness: a failed attestation is refused",
                |_, s| {
                    s.outcome == Some(Outcome::Refused(Refusal::Unverified))
                        && s.cache.iter().all(Option::is_none)
                },
            ),
            Property::<Self>::sometimes(
                "BIND-18 witness: a checksum verifies a fetch gh could not make",
                |_, s| {
                    served(s).is_some_and(|(served, _, _)| {
                        served.source == Source::Fetched && served.checksum
                    })
                },
            ),
            Property::<Self>::sometimes(
                "BIND-18 witness: a crash mid-fetch leaves the cache empty",
                |_, s| s.outcome == Some(Outcome::Crashed) && s.cache.iter().all(Option::is_none),
            ),
            // ── BIND-19 ───────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-19 a cache hit still matches its digest record",
                |_, s| {
                    served(s).is_none_or(|(served, _, _)| {
                        served.source != Source::Cache || served.matches_record
                    })
                },
            ),
            Property::<Self>::sometimes(
                "BIND-19 witness: a tampered entry is discarded and fetched again",
                |_, s| {
                    s.discarded
                        && s.tampers > 0
                        && served(s).is_some_and(|(served, _, _)| served.source == Source::Fetched)
                },
            ),
            Property::<Self>::sometimes(
                "BIND-19 witness: an entry with no digest record is fetched again",
                |_, s| s.discarded && s.tampers == 0 && s.requests == 1,
            ),
            // ── BIND-20 ───────────────────────────────────────────────────────
            Property::<Self>::always("BIND-20 nothing served is a non-aarch64 binary", |_, s| {
                served(s).is_none_or(|(served, _, _)| served.arm)
            }),
            Property::<Self>::always(
                "BIND-20 the cache never holds a non-aarch64 binary",
                |_, s| s.cache.iter().flatten().all(|entry| entry.arm),
            ),
            Property::<Self>::sometimes(
                "BIND-20 witness: a caller's non-aarch64 binary is refused",
                |_, s| {
                    s.outcome == Some(Outcome::Refused(Refusal::NotArm))
                        && matches!(s.request, Some((_, Supplied::NotArm)))
                },
            ),
            Property::<Self>::sometimes(
                "BIND-20 witness: a fetched non-aarch64 asset is refused",
                |_, s| {
                    s.outcome == Some(Outcome::Refused(Refusal::NotArm))
                        && matches!(s.request, Some((_, Supplied::Nothing)))
                },
            ),
            // ── liveness ──────────────────────────────────────────────────────
            //
            // Sound because the model is acyclic: `requests` and `tampers` only grow, and
            // within a request the phase only moves forward.
            Property::<Self>::eventually("BIND-17 every request is answered", |m, s| {
                s.requests == m.cfg.max_requests && s.phase == Phase::Idle
            }),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Model};

    fn checked(behavior: Behavior) -> impl Checker<Provisioning> {
        Provisioning::new(Config::with(behavior))
            .checker()
            .spawn_bfs()
            .join()
    }

    /// The headline: the specified policy satisfies BIND-17 through BIND-20 over every
    /// sequence of requests, release behaviors, crashes, and cache changes, and witnesses
    /// every case that makes that meaningful.
    #[test]
    fn the_specified_policy_satisfies_every_property() {
        let checker = checked(Behavior::Specified);
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 1_000,
            "a space this small could not reach the interleavings: {}",
            checker.unique_state_count()
        );
    }

    /// **#219 in the model.** The CLI as filed served a changed cache entry because it
    /// existed, and a non-aarch64 `$MICROVM_AGENTD` because it existed.
    #[test]
    fn todays_cli_serves_a_tampered_cache_entry_and_a_non_arm_override() {
        let checker = checked(Behavior::Today);
        let steps = checker
            .assert_any_discovery("BIND-19 a cache hit still matches its digest record")
            .into_actions();
        assert!(
            steps
                .iter()
                .any(|a| matches!(a, Action::Request(_, Supplied::Nothing))),
            "the stale hit needs a cache lookup, got {steps:?}"
        );
        let steps = checker
            .assert_any_discovery("BIND-20 nothing served is a non-aarch64 binary")
            .into_actions();
        assert!(
            steps.contains(&Action::Request(Wanted::Default, Supplied::NotArm)),
            "the non-arm serve is the caller's binary, got {steps:?}"
        );
        checker.assert_no_discovery("BIND-18 bytes that failed attestation are never served");
    }

    /// A cache keyed by name alone answers one version's request with another's binary.
    #[test]
    fn an_unversioned_cache_serves_another_versions_binary() {
        let checker = checked(Behavior::Unversioned);
        checker.assert_any_discovery(
            "BIND-17 a served binary is the version requested, the core's by default",
        );
    }

    /// Falling through to `curl` after a refused attestation serves bytes that failed
    /// provenance, which is exactly the laundering the CLI's comment forbids.
    #[test]
    fn falling_through_after_a_refused_attestation_launders_the_bytes() {
        let checker = checked(Behavior::LaunderAttestation);
        let steps = checker
            .assert_any_discovery("BIND-18 bytes that failed attestation are never served")
            .into_actions();
        assert!(
            steps.iter().any(|a| matches!(
                a,
                Action::Download(Release {
                    gh: Gh::Unattested,
                    ..
                })
            )),
            "the laundering needs a refused attestation, got {steps:?}"
        );
    }

    /// Downloading into the cache path leaves unverified bytes where the next request
    /// trusts them once the process dies before the verification.
    #[test]
    fn downloading_into_the_cache_leaves_unverified_bytes_behind() {
        let checker = checked(Behavior::CacheBeforeVerify);
        checker.assert_any_discovery("BIND-18 the cache holds only verified bytes");
        checker.assert_any_discovery("BIND-18 fetched bytes are served only after verification");
        checker.assert_any_discovery("BIND-20 the cache never holds a non-aarch64 binary");
    }

    /// A warning instead of an error serves bytes nothing proved.
    #[test]
    fn a_warning_for_an_unverifiable_fetch_serves_unproven_bytes() {
        let checker = checked(Behavior::WarnOnUnverified);
        let steps = checker
            .assert_any_discovery("BIND-18 fetched bytes are served only after verification")
            .into_actions();
        assert!(
            steps.iter().any(|a| matches!(
                a,
                Action::Download(Release {
                    gh: Gh::Unavailable,
                    curl: Curl::NoSums,
                    ..
                })
            )),
            "the unproven serve needs a release with no SHA256SUMS, got {steps:?}"
        );
    }

    /// The table `microvms-core`'s fake-release tests mirror: the specified verdict for
    /// every combination of what `gh` and `curl` do.
    #[test]
    fn the_verification_table() {
        for gh in [Gh::Unavailable, Gh::Attested, Gh::Unattested] {
            for curl in [
                Curl::Fails,
                Curl::NoSums,
                Curl::SumsMatch,
                Curl::SumsMismatch,
            ] {
                let expected = match (gh, curl) {
                    (Gh::Attested, _) => Verdict::Attestation,
                    (Gh::Unattested, _) => Verdict::Refused,
                    (Gh::Unavailable, Curl::SumsMatch) => Verdict::Checksum,
                    (Gh::Unavailable, _) => Verdict::Refused,
                };
                let release = Release {
                    gh,
                    curl,
                    arm: true,
                };
                assert_eq!(
                    verify(Behavior::Specified, release),
                    expected,
                    "{gh:?} {curl:?}"
                );
            }
        }
    }
}
