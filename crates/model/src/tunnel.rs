// SPDX-License-Identifier: Apache-2.0
//! A checked model of the identity-verified tunnel: the Noise KK handshake and its two pins,
//! the relayed frames, and the end of stream (#297, #342).
//!
//! `microvm tunnel --verify-identity` runs a KK handshake inside the WebSocket before any byte
//! moves (`crates/protocol/src/identity.rs`): the client pins the VM's public key, the daemon
//! pins the launching host's, and every frame after the handshake is one Noise transport
//! message whose nonce is its position in the stream. The integration tests drive the daemon
//! and the client with a hand-written peer and a scripted on-path party; this is where every
//! interleaving of the two sides and an attacker is explored.
//!
//! # The attacker
//!
//! The endpoint proxy terminates TLS, so whatever sits on the path between the client and the
//! daemon reads every frame and can drop one, replay one it saw, swap two, send a plaintext
//! close frame of its own, or hang up. It holds no session key, so it can't seal a frame; it
//! can hold a static key of its own, which [`FarEnd::Impostor`] uses to answer the client's
//! handshake itself. [`Caller::TokenHolder`] is the other side's attacker: a caller holding the
//! agent token and its own key, not the launching host's. The attacker acts at most
//! [`Config::budget`] times, which bounds the state space and still reaches every one-cut and
//! two-cut attack on a two-byte stream.
//!
//! # Keys are symbols, and the pins are equality
//!
//! Nothing here computes a DH. Under KK a responder decrypts the initiator's first message only
//! when the message was made for its own static key by the static key it pinned, and the
//! initiator decrypts the reply only when it came from the static key it pinned; both are
//! mixed into the handshake hash, so a wrong key fails decryption rather than a check that
//! could be skipped. The model states that as two comparisons ([`Daemon::accepts`] and the
//! client's reply check), and each can be switched off ([`Variant::DaemonPinOff`],
//! [`Variant::ClientPinOff`]) to show what it's for. In real KK neither can be switched off
//! from one side, which is why the pin's seeded fault lives here.
//!
//! # Ends
//!
//! The daemon ends a stream the guest finished with its end of stream and then a plaintext
//! close; the client ends one its local side finished the same way. A side that offered the end
//! of stream in its handshake payload is held to it: the client reads a close or a hangup
//! without the daemon's end as [`End::Truncated`], and the daemon resets the guest connection
//! when the caller's end is missing. [`Variant::OlderDaemon`] and [`Variant::OlderClient`] are
//! the releases from before #342, which offer nothing and send no end of stream.

use stateright::{Model, Property};

/// How many bytes the guest sends before its EOF.
pub const GUEST_BYTES: u8 = 2;
/// How many bytes the caller's local side sends before its EOF.
pub const CALLER_BYTES: u8 = 1;
/// The first byte an impostor far end writes of its own, told apart from the guest's.
pub const IMPOSTOR_BYTE: u8 = 100;

/// A caller's static key.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum HostKey {
    /// The launching host's, which the daemon pinned from the run hook.
    Launcher,
    /// Anyone else's.
    Other,
}

/// A far end's static key.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum VmKey {
    /// The VM's own, which the client pinned in its name record.
    Pinned,
    /// Another VM's, or an attacker's.
    Other,
}

/// Who dials the tunnel.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Caller {
    /// The launching host, holding its own secret.
    Launcher,
    /// A caller with the agent token and a key of its own (AGENTD-17's attacker).
    TokenHolder,
}

impl Caller {
    fn key(self) -> HostKey {
        match self {
            Caller::Launcher => HostKey::Launcher,
            Caller::TokenHolder => HostKey::Other,
        }
    }
}

/// Who answers the handshake.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum FarEnd {
    /// The daemon in the VM the record was made for.
    Daemon,
    /// A far end holding another key: a VM relaunched with a fresh seed, a record replayed from
    /// another VM, or the path answering the handshake itself (BIND-21's attacker).
    Impostor,
}

/// Which session a sealed frame belongs to. Only its two parties can seal or open it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Session {
    /// Between the client and the daemon.
    Real,
    /// Between the client and an impostor that it accepted.
    Impostor,
}

/// A sealed frame's plaintext.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Body {
    /// One relayed byte, by its index in the sender's stream.
    Byte(u8),
    /// The end of stream: the message with nothing in it.
    End,
}

/// A plaintext close frame's code, as the side that reads it sees it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Code {
    /// 1000, or no code.
    Normal,
    /// 4403: the handshake was refused.
    Refused,
}

/// A WebSocket frame.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Frame {
    /// The initiator's handshake message: its static, the static it was made for, its offer.
    Hello {
        from: HostKey,
        to: VmKey,
        offer: bool,
    },
    /// The responder's reply: its static, its offer, and the session the two now share.
    Reply {
        from: VmKey,
        offer: bool,
        session: Session,
    },
    /// A transport message at position `seq` of its sender's stream.
    Sealed {
        session: Session,
        seq: u8,
        body: Body,
    },
    /// A plaintext WebSocket close.
    Close(Code),
}

/// Which way a frame travels.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Dir {
    ToFar,
    ToClient,
}

/// How the client's relay ended: `TunnelEnd`, plus the error arm.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum End {
    Closed,
    Truncated,
    ClosedUnproven,
    Refused,
    /// The relay returned an error: a reply that didn't verify, or a frame that didn't
    /// authenticate.
    Failed,
}

/// What the guest's read of its connection ended with.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum GuestRead {
    Open,
    Eof,
    Reset,
}

/// A defense turned off, or a peer from before #342.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Variant {
    /// Both sides as shipped.
    Specified,
    /// The daemon completes a handshake from any caller key.
    DaemonPinOff,
    /// The client accepts a reply from any far-end key.
    ClientPinOff,
    /// The daemon dials the guest when the handshake arrives, before deciding it.
    DialBeforeHandshake,
    /// Receivers open a frame of their session at any position: no counter nonce.
    NoNonce,
    /// The daemon closes the guest's connection without its end of stream.
    NoEndOfStream,
    /// The client reads any close or hangup as a clean end, as it did before #342.
    CloseIsClean,
    /// The daemon closes the guest's connection on any caller end, as it did before #342.
    CloseEndsGuest,
    /// The offers travel outside the handshake's encryption, where the path can strip them.
    OfferInClear,
    /// The daemon predates the end of stream.
    OlderDaemon,
    /// The client predates the end of stream.
    OlderClient,
}

impl Variant {
    fn daemon_offers(self) -> bool {
        self != Variant::OlderDaemon
    }

    fn client_offers(self) -> bool {
        self != Variant::OlderClient
    }

    /// Both sides send and check the end of stream. The end-of-stream properties are about
    /// these releases: one from before #342 has no end to check, which is its documented limit.
    fn current_on_both_ends(self) -> bool {
        self.daemon_offers() && self.client_offers()
    }
}

/// The model's knobs.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Config {
    pub variant: Variant,
    pub caller: Caller,
    pub far_end: FarEnd,
    /// How many times the attacker may act.
    pub budget: u8,
}

impl Config {
    /// The launching host tunnelling to its own VM through a hostile path.
    pub fn path_attacker(variant: Variant) -> Self {
        Self {
            variant,
            caller: Caller::Launcher,
            far_end: FarEnd::Daemon,
            budget: 2,
        }
    }

    /// A caller with the token but not the host key, against the real daemon.
    pub fn token_holder(variant: Variant) -> Self {
        Self {
            caller: Caller::TokenHolder,
            ..Self::path_attacker(variant)
        }
    }

    /// The launching host, whose handshake another key answers.
    pub fn impostor(variant: Variant) -> Self {
        Self {
            far_end: FarEnd::Impostor,
            ..Self::path_attacker(variant)
        }
    }
}

/// Where the client's relay is.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ClientPhase {
    Idle,
    /// Its handshake message is out; it waits for the reply.
    Hello,
    Relaying,
    Done(End),
}

/// The client's relay and the local connection behind it.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Client {
    pub phase: ClientPhase,
    pub session: Option<Session>,
    /// Whether the far end's reply offered the end of stream.
    pub far_offered: bool,
    /// Local bytes sealed and sent.
    pub sent: u8,
    pub next_out: u8,
    pub next_in: u8,
    /// What the relay wrote to the local connection, in order.
    pub received: Vec<u8>,
    /// Whether the local side ended the tunnel.
    pub ended_first: bool,
}

/// The daemon, or an impostor answering in its place, and the guest behind it.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Daemon {
    /// The handshake completed.
    pub established: bool,
    /// The daemon connected to the guest port.
    pub dialed: bool,
    /// The daemon closed the tunnel with 4403.
    pub refused: bool,
    /// Whether the caller's handshake offered the end of stream.
    pub caller_offered: bool,
    /// The tunnel is over on this side.
    pub done: bool,
    /// Guest bytes relayed to the caller.
    pub relayed: u8,
    pub next_out: u8,
    pub next_in: u8,
    /// What reached the guest, in order.
    pub guest_received: Vec<u8>,
    pub guest: GuestRead,
    /// The guest reached its EOF, which ended the tunnel from this side.
    pub guest_ended_first: bool,
    /// The daemon sent its end of stream.
    pub sent_end: bool,
    /// What an impostor opened of the client's local bytes.
    pub stolen: Vec<u8>,
}

/// The whole system.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct State {
    pub client: Client,
    pub daemon: Daemon,
    pub to_far: Vec<Frame>,
    pub to_client: Vec<Frame>,
    /// Every frame the attacker saw go each way, for replay.
    pub seen_to_far: Vec<Frame>,
    pub seen_to_client: Vec<Frame>,
    /// The attacker dropped the connection.
    pub hung_up: bool,
    /// Attacker actions left.
    pub budget: u8,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Action {
    ClientHello,
    /// The local side's next byte, sealed and sent.
    ClientSend,
    /// The local side's EOF: the client's end of stream, then its close.
    ClientLocalEof,
    /// The guest's next byte, relayed by the daemon.
    GuestSend,
    /// The guest's EOF: the daemon's end of stream, then its close.
    GuestEof,
    /// An impostor far end writes a byte of its own.
    ImpostorSend,
    /// The network delivers the next frame one way, or the hangup once nothing is left.
    Deliver(Dir),
    /// The attacker drops the next frame one way.
    Drop(Dir),
    /// The attacker sends again a frame it saw go one way, by its index.
    Replay(Dir, u8),
    /// The attacker swaps the next two frames one way.
    Swap(Dir),
    /// The attacker sends a plaintext close of its own one way.
    ForgeClose(Dir),
    /// The attacker drops the connection.
    HangUp,
    /// The attacker clears the offer in a handshake frame in flight ([`Variant::OfferInClear`]).
    StripOffer(Dir),
}

/// The model.
#[derive(Clone, Debug)]
pub struct TunnelModel {
    pub cfg: Config,
}

impl TunnelModel {
    pub fn new(cfg: Config) -> Self {
        Self { cfg }
    }
}

impl Daemon {
    /// The pin (AGENTD-17): under KK the first message opens only when it was made for this
    /// VM's key by the host key the run hook pinned.
    fn accepts(from: HostKey, to: VmKey, variant: Variant) -> bool {
        to == VmKey::Pinned && (from == HostKey::Launcher || variant == Variant::DaemonPinOff)
    }
}

fn queue(state: &mut State, dir: Dir, frame: Frame) {
    match dir {
        Dir::ToFar => {
            state.to_far.push(frame);
            if !state.seen_to_far.contains(&frame) {
                state.seen_to_far.push(frame);
            }
        }
        Dir::ToClient => {
            state.to_client.push(frame);
            if !state.seen_to_client.contains(&frame) {
                state.seen_to_client.push(frame);
            }
        }
    }
}

fn channel(state: &mut State, dir: Dir) -> &mut Vec<Frame> {
    match dir {
        Dir::ToFar => &mut state.to_far,
        Dir::ToClient => &mut state.to_client,
    }
}

impl TunnelModel {
    /// The client's reading of a close or a hangup it got before any end of stream.
    fn unproven_end(&self, client: &Client, code: Code) -> End {
        if code == Code::Refused {
            return End::Refused;
        }
        if client.phase == ClientPhase::Hello {
            return End::Failed;
        }
        let variant = self.cfg.variant;
        if matches!(variant, Variant::CloseIsClean | Variant::OlderClient) {
            End::Closed
        } else if client.far_offered {
            End::Truncated
        } else {
            End::ClosedUnproven
        }
    }

    fn client_reads(&self, next: &mut State, frame: Option<Frame>) {
        let variant = self.cfg.variant;
        let client = &mut next.client;
        match (client.phase, frame) {
            (ClientPhase::Done(_) | ClientPhase::Idle, _) => {}
            (
                ClientPhase::Hello,
                Some(Frame::Reply {
                    from,
                    offer,
                    session,
                }),
            ) => {
                // BIND-21: the reply opens only when it came from the pinned VM key.
                if from == VmKey::Pinned || variant == Variant::ClientPinOff {
                    client.phase = ClientPhase::Relaying;
                    client.session = Some(session);
                    client.far_offered = offer && variant.client_offers();
                } else {
                    client.phase = ClientPhase::Done(End::Failed);
                }
            }
            (ClientPhase::Relaying, Some(Frame::Sealed { session, seq, body })) => {
                let opens = client.session == Some(session)
                    && (seq == client.next_in || variant == Variant::NoNonce);
                if !opens {
                    // BIND-24: a frame that doesn't open at its position fails the tunnel.
                    client.phase = ClientPhase::Done(End::Failed);
                    return;
                }
                client.next_in = seq + 1;
                match body {
                    Body::Byte(byte) => client.received.push(byte),
                    // An older client writes the empty message's nothing and keeps reading.
                    Body::End if variant.client_offers() => {
                        client.phase = ClientPhase::Done(End::Closed);
                    }
                    Body::End => {}
                }
            }
            (ClientPhase::Hello | ClientPhase::Relaying, Some(Frame::Close(code))) => {
                client.phase = ClientPhase::Done(self.unproven_end(client, code));
            }
            (ClientPhase::Hello | ClientPhase::Relaying, None) => {
                client.phase = ClientPhase::Done(self.unproven_end(client, Code::Normal));
            }
            // Anything else mid-handshake or mid-relay is a frame out of place.
            (ClientPhase::Hello | ClientPhase::Relaying, Some(_)) => {
                client.phase = ClientPhase::Done(End::Failed);
            }
        }
    }

    /// The daemon ends the tunnel on a caller end with nothing proving the caller finished: a
    /// close, a hangup, or a frame that doesn't open.
    ///
    /// Its own close goes back either way. The daemon answers a close with one and drops the
    /// connection on a transport failure, and the client reads both the same.
    fn caller_vanished(&self, next: &mut State) {
        let variant = self.cfg.variant;
        // AGENTD-20: a caller that offered the end of stream and ended without it may have been
        // cut short, so the guest gets a reset.
        next.daemon.guest = if next.daemon.caller_offered
            && variant.daemon_offers()
            && variant != Variant::CloseEndsGuest
        {
            GuestRead::Reset
        } else {
            GuestRead::Eof
        };
        next.daemon.done = true;
        queue(next, Dir::ToClient, Frame::Close(Code::Normal));
    }

    fn daemon_reads(&self, next: &mut State, frame: Option<Frame>) {
        let variant = self.cfg.variant;
        if next.daemon.done {
            return;
        }
        if self.cfg.far_end == FarEnd::Impostor {
            match frame {
                Some(Frame::Hello { .. }) if !next.daemon.established => {
                    next.daemon.established = true;
                    queue(
                        next,
                        Dir::ToClient,
                        Frame::Reply {
                            from: VmKey::Other,
                            offer: true,
                            session: Session::Impostor,
                        },
                    );
                }
                Some(Frame::Sealed {
                    session: Session::Impostor,
                    body: Body::Byte(byte),
                    ..
                }) => next.daemon.stolen.push(byte),
                Some(Frame::Close(_)) | None => next.daemon.done = true,
                Some(_) => {}
            }
            return;
        }
        match frame {
            Some(Frame::Hello { from, to, offer }) if !next.daemon.established => {
                if variant == Variant::DialBeforeHandshake {
                    next.daemon.dialed = true;
                }
                if Daemon::accepts(from, to, variant) {
                    next.daemon.established = true;
                    next.daemon.caller_offered = offer && variant.daemon_offers();
                    // AGENTD-18: the dial comes after the handshake decided.
                    next.daemon.dialed = true;
                    queue(
                        next,
                        Dir::ToClient,
                        Frame::Reply {
                            from: VmKey::Pinned,
                            offer: variant.daemon_offers(),
                            session: Session::Real,
                        },
                    );
                } else {
                    next.daemon.refused = true;
                    next.daemon.done = true;
                    queue(next, Dir::ToClient, Frame::Close(Code::Refused));
                }
            }
            Some(Frame::Sealed { session, seq, body }) if next.daemon.established => {
                let opens = session == Session::Real
                    && (seq == next.daemon.next_in || variant == Variant::NoNonce);
                if !opens {
                    // AGENTD-21: a frame that doesn't open at its position ends the tunnel
                    // without relaying it.
                    self.caller_vanished(next);
                    return;
                }
                next.daemon.next_in = seq + 1;
                match body {
                    Body::Byte(byte) => next.daemon.guest_received.push(byte),
                    Body::End => {
                        next.daemon.guest = GuestRead::Eof;
                        next.daemon.done = true;
                        queue(next, Dir::ToClient, Frame::Close(Code::Normal));
                    }
                }
            }
            Some(Frame::Close(_)) | None if next.daemon.established => {
                self.caller_vanished(next);
            }
            Some(Frame::Close(_)) | None => next.daemon.done = true,
            // A frame out of place: before the handshake, or a second handshake.
            Some(_) => {
                if next.daemon.established {
                    self.caller_vanished(next);
                } else {
                    next.daemon.done = true;
                }
            }
        }
    }
}

impl Model for TunnelModel {
    type State = State;
    type Action = Action;

    fn init_states(&self) -> Vec<State> {
        vec![State {
            client: Client {
                phase: ClientPhase::Idle,
                session: None,
                far_offered: false,
                sent: 0,
                next_out: 0,
                next_in: 0,
                received: Vec::new(),
                ended_first: false,
            },
            daemon: Daemon {
                established: false,
                dialed: false,
                refused: false,
                caller_offered: false,
                done: false,
                relayed: 0,
                next_out: 0,
                next_in: 0,
                guest_received: Vec::new(),
                guest: GuestRead::Open,
                guest_ended_first: false,
                sent_end: false,
                stolen: Vec::new(),
            },
            to_far: Vec::new(),
            to_client: Vec::new(),
            seen_to_far: Vec::new(),
            seen_to_client: Vec::new(),
            hung_up: false,
            budget: self.cfg.budget,
        }]
    }

    fn actions(&self, state: &State, actions: &mut Vec<Action>) {
        let client = &state.client;
        let daemon = &state.daemon;
        if client.phase == ClientPhase::Idle {
            actions.push(Action::ClientHello);
        }
        if client.phase == ClientPhase::Relaying && !state.hung_up {
            if client.sent < CALLER_BYTES {
                actions.push(Action::ClientSend);
            } else {
                actions.push(Action::ClientLocalEof);
            }
        }
        if !daemon.done && !state.hung_up {
            match self.cfg.far_end {
                FarEnd::Daemon if daemon.established && daemon.dialed => {
                    if daemon.relayed < GUEST_BYTES {
                        actions.push(Action::GuestSend);
                    } else {
                        actions.push(Action::GuestEof);
                    }
                }
                FarEnd::Impostor if daemon.established && daemon.next_out == 0 => {
                    actions.push(Action::ImpostorSend);
                }
                _ => {}
            }
        }
        for dir in [Dir::ToFar, Dir::ToClient] {
            let (queued, seen) = match dir {
                Dir::ToFar => (&state.to_far, &state.seen_to_far),
                Dir::ToClient => (&state.to_client, &state.seen_to_client),
            };
            let reader_done = match dir {
                Dir::ToFar => daemon.done,
                Dir::ToClient => matches!(client.phase, ClientPhase::Done(_)),
            };
            if !queued.is_empty() || (state.hung_up && !reader_done) {
                actions.push(Action::Deliver(dir));
            }
            if state.budget == 0 || state.hung_up {
                continue;
            }
            if !queued.is_empty() {
                actions.push(Action::Drop(dir));
            }
            if queued.len() >= 2 {
                actions.push(Action::Swap(dir));
            }
            for index in 0..seen.len() {
                actions.push(Action::Replay(dir, index as u8));
            }
            actions.push(Action::ForgeClose(dir));
            if self.cfg.variant == Variant::OfferInClear
                && queued.iter().any(|frame| {
                    matches!(
                        frame,
                        Frame::Hello { offer: true, .. } | Frame::Reply { offer: true, .. }
                    )
                })
            {
                actions.push(Action::StripOffer(dir));
            }
        }
        if state.budget > 0 && !state.hung_up && client.phase != ClientPhase::Idle {
            actions.push(Action::HangUp);
        }
    }

    fn next_state(&self, last: &State, action: Action) -> Option<State> {
        let mut next = last.clone();
        let variant = self.cfg.variant;
        match action {
            Action::ClientHello => {
                next.client.phase = ClientPhase::Hello;
                queue(
                    &mut next,
                    Dir::ToFar,
                    Frame::Hello {
                        from: self.cfg.caller.key(),
                        to: VmKey::Pinned,
                        offer: variant.client_offers(),
                    },
                );
            }
            Action::ClientSend => {
                let session = next.client.session?;
                let seq = next.client.next_out;
                let byte = next.client.sent;
                next.client.next_out += 1;
                next.client.sent += 1;
                queue(
                    &mut next,
                    Dir::ToFar,
                    Frame::Sealed {
                        session,
                        seq,
                        body: Body::Byte(byte),
                    },
                );
            }
            Action::ClientLocalEof => {
                let session = next.client.session?;
                let seq = next.client.next_out;
                next.client.next_out += 1;
                next.client.phase = ClientPhase::Done(End::Closed);
                next.client.ended_first = true;
                if variant.client_offers() {
                    queue(
                        &mut next,
                        Dir::ToFar,
                        Frame::Sealed {
                            session,
                            seq,
                            body: Body::End,
                        },
                    );
                }
                queue(&mut next, Dir::ToFar, Frame::Close(Code::Normal));
            }
            Action::GuestSend => {
                let seq = next.daemon.next_out;
                let byte = next.daemon.relayed;
                next.daemon.next_out += 1;
                next.daemon.relayed += 1;
                queue(
                    &mut next,
                    Dir::ToClient,
                    Frame::Sealed {
                        session: Session::Real,
                        seq,
                        body: Body::Byte(byte),
                    },
                );
            }
            Action::GuestEof => {
                let seq = next.daemon.next_out;
                next.daemon.next_out += 1;
                next.daemon.done = true;
                next.daemon.guest_ended_first = true;
                next.daemon.guest = GuestRead::Eof;
                // AGENTD-19: the end of stream goes out before the close.
                if variant.daemon_offers() && variant != Variant::NoEndOfStream {
                    next.daemon.sent_end = true;
                    queue(
                        &mut next,
                        Dir::ToClient,
                        Frame::Sealed {
                            session: Session::Real,
                            seq,
                            body: Body::End,
                        },
                    );
                }
                queue(&mut next, Dir::ToClient, Frame::Close(Code::Normal));
            }
            Action::ImpostorSend => {
                let seq = next.daemon.next_out;
                next.daemon.next_out += 1;
                queue(
                    &mut next,
                    Dir::ToClient,
                    Frame::Sealed {
                        session: Session::Impostor,
                        seq,
                        body: Body::Byte(IMPOSTOR_BYTE),
                    },
                );
            }
            Action::Deliver(dir) => {
                let queued = channel(&mut next, dir);
                let frame = if queued.is_empty() {
                    None
                } else {
                    Some(queued.remove(0))
                };
                match dir {
                    Dir::ToFar => self.daemon_reads(&mut next, frame),
                    Dir::ToClient => self.client_reads(&mut next, frame),
                }
            }
            Action::Drop(dir) => {
                next.budget -= 1;
                channel(&mut next, dir).remove(0);
            }
            Action::Swap(dir) => {
                next.budget -= 1;
                channel(&mut next, dir).swap(0, 1);
            }
            Action::Replay(dir, index) => {
                next.budget -= 1;
                let frame = match dir {
                    Dir::ToFar => next.seen_to_far[usize::from(index)],
                    Dir::ToClient => next.seen_to_client[usize::from(index)],
                };
                channel(&mut next, dir).push(frame);
            }
            Action::ForgeClose(dir) => {
                next.budget -= 1;
                channel(&mut next, dir).push(Frame::Close(Code::Normal));
            }
            Action::HangUp => {
                next.budget -= 1;
                next.hung_up = true;
                next.to_far.clear();
                next.to_client.clear();
            }
            Action::StripOffer(dir) => {
                next.budget -= 1;
                for frame in channel(&mut next, dir).iter_mut() {
                    match frame {
                        Frame::Hello { offer, .. } | Frame::Reply { offer, .. } => *offer = false,
                        _ => {}
                    }
                }
            }
        }
        (next != *last).then_some(next)
    }

    fn properties(&self) -> Vec<Property<Self>> {
        vec![
            // ── the handshake and its pins ──────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-17 the daemon completes a handshake only with the launching host's key",
                |m, s| {
                    !s.daemon.established
                        || m.cfg.far_end == FarEnd::Impostor
                        || m.cfg.caller == Caller::Launcher
                },
            ),
            Property::<Self>::always(
                "AGENTD-18 the daemon dials the guest only after the handshake completes",
                |_, s| !s.daemon.dialed || s.daemon.established,
            ),
            Property::<Self>::always(
                "BIND-21 no local byte reaches a far end without the pinned VM key",
                |_, s| s.daemon.stolen.is_empty(),
            ),
            Property::<Self>::always(
                "BIND-21 the client writes no byte a far end without the pinned VM key sealed",
                |_, s| !s.client.received.contains(&IMPOSTOR_BYTE),
            ),
            // ── the frames ─────────────────────────────────────────────────────
            Property::<Self>::always(
                "BIND-24 the client writes the guest's bytes in order, each once",
                |_, s| is_prefix(&s.client.received),
            ),
            Property::<Self>::always(
                "AGENTD-21 the guest reads the caller's bytes in order, each once",
                |_, s| is_prefix(&s.daemon.guest_received),
            ),
            // ── the end of stream ─────────────────────────────────────────────
            Property::<Self>::always(
                "AGENTD-19 the daemon's close after a guest EOF follows its end of stream",
                |m, s| {
                    !s.daemon.guest_ended_first
                        || !m.cfg.variant.daemon_offers()
                        || s.daemon.sent_end
                },
            ),
            Property::<Self>::always(
                "BIND-23 the client reads a daemon's end as Closed only with every byte it sent",
                |m, s| {
                    s.client.phase != ClientPhase::Done(End::Closed)
                        || s.client.ended_first
                        || !m.cfg.variant.current_on_both_ends()
                        || s.client.received == all(GUEST_BYTES)
                },
            ),
            Property::<Self>::always(
                "BIND-23 a tunnel ends ClosedUnproven only into a daemon that offered nothing",
                |m, s| {
                    s.client.phase != ClientPhase::Done(End::ClosedUnproven)
                        || !m.cfg.variant.daemon_offers()
                },
            ),
            Property::<Self>::always(
                "AGENTD-20 the guest reads EOF only after every byte of a caller that offered the end",
                |m, s| {
                    s.daemon.guest != GuestRead::Eof
                        || s.daemon.guest_ended_first
                        || !m.cfg.variant.current_on_both_ends()
                        || s.daemon.guest_received == all(CALLER_BYTES)
                },
            ),
            // ── witnesses ────────────────────────────────────────────────────
            Property::<Self>::sometimes(
                "witness: a guest's whole stream arrives and the tunnel ends Closed",
                |_, s| {
                    s.client.phase == ClientPhase::Done(End::Closed)
                        && !s.client.ended_first
                        && s.client.received == all(GUEST_BYTES)
                },
            ),
            Property::<Self>::sometimes(
                "witness: the caller's whole upload reaches the guest as an EOF",
                |_, s| {
                    s.daemon.guest == GuestRead::Eof
                        && !s.daemon.guest_ended_first
                        && s.daemon.guest_received == all(CALLER_BYTES)
                },
            ),
            Property::<Self>::sometimes("witness: a cut stream ends Truncated", |_, s| {
                s.client.phase == ClientPhase::Done(End::Truncated)
                    && s.client.received.len() < usize::from(GUEST_BYTES)
            }),
            Property::<Self>::sometimes("witness: a cut upload resets the guest", |_, s| {
                s.daemon.guest == GuestRead::Reset
            }),
            Property::<Self>::sometimes(
                "witness: a replayed or reordered frame fails the tunnel",
                |_, s| {
                    s.client.phase == ClientPhase::Done(End::Failed) && s.client.session.is_some()
                },
            ),
        ]
    }
}

/// `0..len` as bytes: a whole stream, by index.
fn all(len: u8) -> Vec<u8> {
    (0..len).collect()
}

/// Whether `received` is the start of a stream, each byte once and in order.
fn is_prefix(received: &[u8]) -> bool {
    received
        .iter()
        .enumerate()
        .all(|(index, byte)| usize::from(*byte) == index)
}

#[cfg(test)]
mod tests {
    use super::*;
    use stateright::{Checker, Expectation, Model};

    fn checked(cfg: Config) -> impl Checker<TunnelModel> {
        TunnelModel::new(cfg).checker().spawn_bfs().join()
    }

    /// Every `always` property holds over `cfg`'s whole space.
    fn assert_safe(cfg: Config) -> impl Checker<TunnelModel> {
        let checker = checked(cfg);
        for property in TunnelModel::new(cfg).properties() {
            if property.expectation == Expectation::Always {
                assert!(
                    checker.discovery(property.name).is_none(),
                    "the tunnel as configured ({cfg:?}) breaks {:?}: {:?}",
                    property.name,
                    checker
                        .discovery(property.name)
                        .map(|path| path.into_actions())
                );
            }
        }
        checker
    }

    /// The actions of the first path that breaks `name` under `cfg`.
    fn attack(cfg: Config, name: &'static str) -> Vec<Action> {
        checked(cfg).assert_any_discovery(name).into_actions()
    }

    /// Applies `actions` from the initial state.
    fn run(cfg: Config, actions: &[Action]) -> State {
        let model = TunnelModel::new(cfg);
        actions
            .iter()
            .fold(model.init_states().remove(0), |state, action| {
                model
                    .next_state(&state, *action)
                    .unwrap_or_else(|| panic!("{action:?} changes nothing in {state:?}"))
            })
    }

    /// The handshake, as far as the client relaying.
    const HANDSHAKE: [Action; 3] = [
        Action::ClientHello,
        Action::Deliver(Dir::ToFar),
        Action::Deliver(Dir::ToClient),
    ];

    /// **The headline** (AGENTD-19, AGENTD-20, AGENTD-21, BIND-23, BIND-24): against a path that
    /// drops, replays, swaps, forges a close or hangs up, twice, every property holds, and the
    /// clean transfer, the cut stream, the cut upload and the refused replay are all reached.
    #[test]
    fn the_specified_tunnel_holds_every_property_against_a_path_attacker() {
        let checker = assert_safe(Config::path_attacker(Variant::Specified));
        checker.assert_properties();
        assert!(
            checker.unique_state_count() > 10_000,
            "a space this small could not reach the two-cut attacks: {}",
            checker.unique_state_count()
        );
    }

    /// **A caller holding the agent token but not the host key never completes a handshake**
    /// (AGENTD-17, AGENTD-18): the daemon refuses it with 4403 and never dials the guest.
    #[test]
    fn agentd_17_a_token_holder_without_the_host_key_is_refused_before_any_dial() {
        assert_safe(Config::token_holder(Variant::Specified));
        let refused = run(Config::token_holder(Variant::Specified), &HANDSHAKE);
        assert!(
            refused.daemon.refused && !refused.daemon.dialed,
            "{refused:?}"
        );
        assert_eq!(refused.client.phase, ClientPhase::Done(End::Refused));
    }

    /// **The pin is what refuses it** (AGENTD-17): with the daemon's pin off, the model finds
    /// the token holder completing a handshake.
    #[test]
    fn agentd_17_the_model_finds_the_token_holder_when_the_daemons_pin_is_off() {
        let steps = attack(
            Config::token_holder(Variant::DaemonPinOff),
            "AGENTD-17 the daemon completes a handshake only with the launching host's key",
        );
        assert!(steps.contains(&Action::Deliver(Dir::ToFar)), "{steps:?}");
    }

    /// **A dial decided before the handshake reaches the guest for a refused caller**
    /// (AGENTD-18).
    #[test]
    fn agentd_18_the_model_finds_a_dial_before_the_handshake() {
        attack(
            Config::token_holder(Variant::DialBeforeHandshake),
            "AGENTD-18 the daemon dials the guest only after the handshake completes",
        );
    }

    /// **A far end without the pinned VM key gets no local byte and writes none** (BIND-21).
    #[test]
    fn bind_21_an_impostor_far_end_gets_no_local_byte_and_writes_none() {
        assert_safe(Config::impostor(Variant::Specified));
        let refused = run(Config::impostor(Variant::Specified), &HANDSHAKE);
        assert_eq!(refused.client.phase, ClientPhase::Done(End::Failed));
    }

    /// **The client's pin is what stops it** (BIND-21): with it off, the model finds the
    /// impostor reading the upload and writing into the local connection.
    #[test]
    fn bind_21_the_model_finds_the_impostor_when_the_clients_pin_is_off() {
        let cfg = Config::impostor(Variant::ClientPinOff);
        attack(
            cfg,
            "BIND-21 no local byte reaches a far end without the pinned VM key",
        );
        attack(
            cfg,
            "BIND-21 the client writes no byte a far end without the pinned VM key sealed",
        );
    }

    /// **Without the counter nonce, a replayed frame reaches both sides twice** (AGENTD-21,
    /// BIND-24): the position in the stream is what refuses a replay, a drop and a swap.
    #[test]
    fn bind_24_the_model_finds_a_replay_when_frames_carry_no_position() {
        let cfg = Config::path_attacker(Variant::NoNonce);
        let steps = attack(
            cfg,
            "BIND-24 the client writes the guest's bytes in order, each once",
        );
        assert!(
            steps
                .iter()
                .any(|step| matches!(step, Action::Replay(..) | Action::Swap(_) | Action::Drop(_))),
            "{steps:?}"
        );
        attack(
            cfg,
            "AGENTD-21 the guest reads the caller's bytes in order, each once",
        );
    }

    /// **A daemon that closes after the guest's EOF without its end of stream is found**
    /// (AGENTD-19), and with it no download ever ends `Closed`.
    #[test]
    fn agentd_19_the_model_finds_a_daemon_that_closes_without_its_end() {
        let cfg = Config::path_attacker(Variant::NoEndOfStream);
        attack(
            cfg,
            "AGENTD-19 the daemon's close after a guest EOF follows its end of stream",
        );
        assert!(
            checked(cfg)
                .discovery("witness: a guest's whole stream arrives and the tunnel ends Closed")
                .is_none(),
            "without the daemon's end, the client can't prove a download finished"
        );
    }

    /// **#342 in the model** (BIND-23): a client that reads a close as the end reads a stream
    /// the path cut short as a finished one.
    #[test]
    fn bind_23_the_model_finds_a_cut_stream_read_as_finished_before_342() {
        let steps = attack(
            Config::path_attacker(Variant::CloseIsClean),
            "BIND-23 the client reads a daemon's end as Closed only with every byte it sent",
        );
        assert!(
            steps.iter().any(|step| matches!(
                step,
                Action::ForgeClose(_) | Action::HangUp | Action::Drop(_)
            )),
            "the cut is the path's doing: {steps:?}"
        );
    }

    /// **#342's daemon side in the model** (AGENTD-20): a daemon that closes the guest's
    /// connection on any caller end hands a cut upload an EOF.
    #[test]
    fn agentd_20_the_model_finds_a_cut_upload_read_as_finished_before_342() {
        attack(
            Config::path_attacker(Variant::CloseEndsGuest),
            "AGENTD-20 the guest reads EOF only after every byte of a caller that offered the end",
        );
    }

    /// **The offer is in the handshake because the path could strip it anywhere else**
    /// (BIND-23, AGENTD-20): stripped, a current pair reads as older releases, and a cut
    /// stream ends unproven while a cut upload closes the guest.
    #[test]
    fn bind_23_the_model_finds_an_offer_stripped_outside_the_handshake() {
        let cfg = Config::path_attacker(Variant::OfferInClear);
        let steps = attack(
            cfg,
            "BIND-23 a tunnel ends ClosedUnproven only into a daemon that offered nothing",
        );
        assert!(
            steps
                .iter()
                .any(|step| matches!(step, Action::StripOffer(_))),
            "{steps:?}"
        );
        attack(
            cfg,
            "AGENTD-20 the guest reads EOF only after every byte of a caller that offered the end",
        );
    }

    /// **An older daemon's tunnels end unproven, and nothing else breaks** (BIND-23): the skew
    /// rule, so a current client doesn't fail every tunnel into an older image.
    #[test]
    fn bind_23_a_daemon_that_offers_nothing_ends_unproven_and_breaks_nothing_else() {
        let cfg = Config::path_attacker(Variant::OlderDaemon);
        assert_safe(cfg);
        let mut steps = HANDSHAKE.to_vec();
        for _ in 0..GUEST_BYTES {
            steps.extend([Action::GuestSend, Action::Deliver(Dir::ToClient)]);
        }
        steps.extend([Action::GuestEof, Action::Deliver(Dir::ToClient)]);
        let ended = run(cfg, &steps);
        assert_eq!(ended.client.phase, ClientPhase::Done(End::ClosedUnproven));
        assert_eq!(ended.client.received, all(GUEST_BYTES));
    }

    /// **An older client still reads a cut stream as finished**, which is its documented limit:
    /// it never reads the daemon's offer, so nothing holds it to the end of stream.
    #[test]
    fn an_older_client_still_reads_a_cut_stream_as_finished() {
        let mut steps = HANDSHAKE.to_vec();
        steps.extend([
            Action::GuestSend,
            Action::Deliver(Dir::ToClient),
            Action::ForgeClose(Dir::ToClient),
            Action::Deliver(Dir::ToClient),
        ]);
        let cut = run(Config::path_attacker(Variant::OlderClient), &steps);
        assert_eq!(cut.client.phase, ClientPhase::Done(End::Closed));
        assert_eq!(cut.client.received, vec![0]);
        let current = run(Config::path_attacker(Variant::Specified), &steps);
        assert_eq!(current.client.phase, ClientPhase::Done(End::Truncated));
    }
}
