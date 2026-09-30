// SPDX-License-Identifier: Apache-2.0
//! The fuzz harnesses for the client's verified-tunnel reads: the daemon's handshake reply
//! (BIND-21), and each frame after it over a hostile path (BIND-23, BIND-24).
//!
//! `bolero::check!` runs each as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test session::tunnel_fuzz::a_reply_opens_only_from_the_pinned_vm -p microvms-edges -T 60s`
//! and `... session::tunnel_fuzz::frames_open_in_order_and_the_daemons_end_only_after_every_chunk ...`
//! (the `daemon-edges-app` job in `.github/workflows/fuzz.yml`).
//!
//! # What an input is
//!
//! For the reply: the pinned VM's answer to this client's handshake, with the offer or a
//! payload the input picks; the same VM's answer to another handshake, replayed; another VM's
//! answer; the pinned VM's with one bit flipped; or whatever bytes. For the frames: chunks of
//! one to eight bytes the guest sends, sealed by the daemon in one real session and followed by
//! its end of stream, and the steps of a path that delivers the next frame, drops it, replays
//! any frame of the session, swaps the next two, flips a bit, or sends bytes of its own. The
//! steps the input gives run first (half the paths take none), and the rest of the frames are
//! then delivered in order.
//!
//! # What it checks
//!
//! * No panic, whatever the bytes.
//! * The reply opens a session exactly when the pinned VM made it for this handshake, and the
//!   offer read is the payload's (BIND-21).
//! * Each frame that opens is the guest's next chunk, whole and in order (BIND-24), so nothing
//!   replayed, reordered or forged reaches the local connection.
//! * The daemon's end of stream opens only after every chunk (BIND-23), and a path that did
//!   nothing reaches it.

use bolero::TypeGenerator;

use microvms_app::identity::{LaunchIdentity, TunnelIdentity};

use super::tunnel::{Frame, Verified, open_frame, read_reply};

const VM_SEED: [u8; 32] = [7; 32];
const HOST_SEED: [u8; 32] = [9; 32];
const OTHER_SEED: [u8; 32] = [11; 32];
/// Caps that keep an input's work small without narrowing what a frame can hold.
const MAX_CHUNKS: usize = 8;
const MAX_JUNK_BYTES: usize = 256;

fn identity() -> TunnelIdentity {
    LaunchIdentity::from_seeds(VM_SEED, HOST_SEED)
        .expect("valid seeds")
        .keep()
}

/// The host's public key, which the VM pins.
fn host_public() -> [u8; 32] {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD
        .decode(
            LaunchIdentity::from_seeds(VM_SEED, HOST_SEED)
                .expect("valid seeds")
                .host_public_field(),
        )
        .expect("base64")
        .try_into()
        .expect("32 bytes")
}

/// A responder holding `vm_seed`'s key and pinning the host.
fn responder(vm_seed: [u8; 32]) -> snow::HandshakeState {
    // The builder borrows both keys until it builds, so they need a home that outlives it.
    let host = host_public();
    snow::Builder::new(protocol::identity::NOISE_PATTERN.parse().expect("parses"))
        .local_private_key(&vm_seed)
        .and_then(|builder| builder.remote_public_key(&host))
        .and_then(|builder| builder.build_responder())
        .expect("the responder builds")
}

/// This client's initiator and its first handshake message.
fn hello() -> (snow::HandshakeState, Vec<u8>) {
    let mut initiator = crate::identity::initiator(&identity()).expect("the initiator builds");
    let mut scratch = vec![0_u8; 65535];
    let written = initiator
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut scratch)
        .expect("writes");
    (initiator, scratch[..written].to_vec())
}

/// `responder`'s reply to `first`, carrying `payload`.
fn answer(mut responder: snow::HandshakeState, first: &[u8], payload: &[u8]) -> Vec<u8> {
    let mut scratch = vec![0_u8; 65535];
    responder
        .read_message(first, &mut scratch)
        .expect("the handshake message opens");
    let written = responder
        .write_message(payload, &mut scratch)
        .expect("the reply writes");
    scratch[..written].to_vec()
}

fn offer_payload(offer: bool) -> &'static [u8] {
    if offer {
        &protocol::identity::HANDSHAKE_PAYLOAD
    } else {
        &[]
    }
}

/// A far end's handshake reply.
#[derive(Clone, Debug, TypeGenerator)]
enum Reply {
    /// The pinned VM's, to this handshake, offering the end of stream or not.
    Pinned { offer: bool },
    /// The pinned VM's, to this handshake, with a payload the input picks.
    Payload(Vec<u8>),
    /// The pinned VM's answer to another handshake, replayed.
    Replayed { offer: bool },
    /// Another VM's answer to a handshake made for it.
    OtherVm { offer: bool },
    /// The pinned VM's, with one bit flipped.
    Flipped { offer: bool, at: u16, bit: u8 },
    /// Whatever bytes.
    Bytes(Vec<u8>),
}

impl Reply {
    /// The bytes on the wire for `first`, and the offer the client should read, when it
    /// should open.
    fn wire(&self, first: &[u8]) -> (Vec<u8>, Option<bool>) {
        match self {
            Reply::Pinned { offer } => (
                answer(responder(VM_SEED), first, offer_payload(*offer)),
                Some(*offer),
            ),
            Reply::Payload(payload) => {
                let payload = &payload[..payload.len().min(MAX_JUNK_BYTES)];
                (
                    answer(responder(VM_SEED), first, payload),
                    Some(protocol::identity::offers_end_of_stream(payload)),
                )
            }
            Reply::Replayed { offer } => {
                let (_, another) = hello();
                (
                    answer(responder(VM_SEED), &another, offer_payload(*offer)),
                    None,
                )
            }
            Reply::OtherVm { offer } => {
                let other = LaunchIdentity::from_seeds(OTHER_SEED, HOST_SEED)
                    .expect("valid seeds")
                    .keep();
                let mut initiator =
                    crate::identity::initiator(&other).expect("the initiator builds");
                let mut scratch = vec![0_u8; 65535];
                let written = initiator
                    .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut scratch)
                    .expect("writes");
                (
                    answer(
                        responder(OTHER_SEED),
                        &scratch[..written],
                        offer_payload(*offer),
                    ),
                    None,
                )
            }
            Reply::Flipped { offer, at, bit } => {
                let mut wire = answer(responder(VM_SEED), first, offer_payload(*offer));
                let at = usize::from(*at) % wire.len();
                wire[at] ^= 1 << (bit % 8);
                (wire, None)
            }
            // A random reply that verifies against the pin is a 2^-128 event; the harness would
            // report it rather than hide it.
            Reply::Bytes(bytes) => (bytes[..bytes.len().min(MAX_JUNK_BYTES)].to_vec(), None),
        }
    }
}

/// One reply, checked against the pin's rule.
fn check_reply(case: &Reply) {
    let (initiator, first) = hello();
    let (wire, expected) = case.wire(&first);
    let mut scratch = vec![0_u8; 65535];
    match (read_reply(initiator, &wire, &mut scratch, 8080), expected) {
        (Ok(verified), Some(offer)) => {
            assert_eq!(verified.daemon_proves_end, offer, "{case:?}");
        }
        (Err(error), None) => {
            assert!(
                error.to_string().contains("pinned key"),
                "{case:?}: {error}"
            );
        }
        (read, expected) => panic!(
            "{case:?} read as {:?}, and the pin's rule says {expected:?}",
            read.map(|verified| verified.daemon_proves_end)
        ),
    }
}

/// **BIND-21.** Hostile bytes as the daemon's handshake reply open a session exactly when the
/// pinned VM made them for this handshake, with the offer the payload carries.
///
/// Every run checks one reply of each kind before the drawn ones. Under `cargo test` bolero
/// draws for a fixed time, and a handshake's crypto keeps that to a few dozen inputs, so a
/// rule only a draw reaches can go unchecked in a run (#297's seeded faults did).
#[test]
fn a_reply_opens_only_from_the_pinned_vm() {
    for case in [
        Reply::Pinned { offer: false },
        Reply::Pinned { offer: true },
        Reply::Payload(vec![0x02]),
        Reply::Replayed { offer: true },
        Reply::OtherVm { offer: true },
        Reply::Flipped {
            offer: true,
            at: 0,
            bit: 0,
        },
        Reply::Bytes(vec![0; 48]),
    ] {
        check_reply(&case);
    }
    bolero::check!().with_type::<Reply>().for_each(check_reply);
}

/// One thing the path does.
#[derive(Clone, Debug, TypeGenerator)]
enum Step {
    Deliver,
    Drop,
    /// Any frame of the session, by index.
    Replay(u8),
    Swap,
    Flip {
        at: u16,
        bit: u8,
    },
    Junk(Vec<u8>),
}

/// The guest's chunks, and a path.
///
/// Half the paths are faithful, since a random list of steps almost always attacks before the
/// end, and the end of stream's own rule needs a stream that gets there. Each chunk's length
/// comes from its first byte, one to eight bytes, so short chunks (a one-byte chunk is the
/// likeliest to be mistaken for the empty end) are common.
#[derive(Clone, Debug, TypeGenerator)]
struct Path {
    chunks: Vec<Vec<u8>>,
    steps: Vec<Step>,
    faithful: bool,
}

/// A completed session: this client's, and the daemon's transport.
fn session() -> (Verified, snow::TransportState) {
    let (initiator, first) = hello();
    let mut daemon = responder(VM_SEED);
    let mut scratch = vec![0_u8; 65535];
    daemon
        .read_message(&first, &mut scratch)
        .expect("the handshake message opens");
    let written = daemon
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut scratch)
        .expect("the reply writes");
    let reply = scratch[..written].to_vec();
    let verified = read_reply(initiator, &reply, &mut scratch, 8080).expect("the reply opens");
    (verified, daemon.into_transport_mode().expect("transport"))
}

/// The paths every run checks before the drawn ones, for the reason
/// [`a_reply_opens_only_from_the_pinned_vm`] gives: a faithful path whose first chunk is one
/// byte, and a path that flips, drops, replays or swaps.
fn anchor_paths() -> Vec<Path> {
    let chunks = vec![vec![0], vec![1, 2], vec![3, 4, 5]];
    let path = |steps: Vec<Step>, faithful| Path {
        chunks: chunks.clone(),
        steps,
        faithful,
    };
    vec![
        path(Vec::new(), true),
        path(vec![Step::Flip { at: 0, bit: 0 }], false),
        path(vec![Step::Drop], false),
        path(vec![Step::Deliver, Step::Replay(0)], false),
        path(vec![Step::Swap], false),
        path(vec![Step::Junk(vec![0; 32])], false),
    ]
}

/// **BIND-23 and BIND-24.** Over a path that drops, replays, swaps, flips and forges, each frame
/// the client opens is the guest's next chunk, whole and in order, and the daemon's end of
/// stream opens only after every chunk; a path that did nothing reaches it.
#[test]
fn frames_open_in_order_and_the_daemons_end_only_after_every_chunk() {
    for path in anchor_paths() {
        check_path(&path);
    }
    bolero::check!().with_type::<Path>().for_each(check_path);
}

/// One path, checked against the frames' rules.
fn check_path(path: &Path) {
    let chunks: Vec<Vec<u8>> = path
        .chunks
        .iter()
        .filter(|chunk| !chunk.is_empty())
        .take(MAX_CHUNKS)
        .map(|chunk| chunk[..chunk.len().min(1 + usize::from(chunk[0] % 8))].to_vec())
        .collect();
    let (mut client, mut daemon) = session();
    let mut scratch = vec![0_u8; 65535];
    // Every frame of the session, the end of stream last: what the path saw.
    let frames: Vec<Vec<u8>> = chunks
        .iter()
        .map(Vec::as_slice)
        .chain([&[][..]])
        .map(|plain| {
            let written = daemon.write_message(plain, &mut scratch).expect("seals");
            scratch[..written].to_vec()
        })
        .collect();
    let mut queue: std::collections::VecDeque<Vec<u8>> = frames.iter().cloned().collect();
    let mut opened = 0;
    let mut attacked = false;

    let given: &[Step] = if path.faithful { &[] } else { &path.steps };
    let steps = given.iter().map(Some).chain(std::iter::repeat(None));
    for step in steps {
        let frame = match step {
            None | Some(Step::Deliver) => queue.pop_front(),
            Some(Step::Drop) => {
                attacked |= queue.pop_front().is_some();
                continue;
            }
            Some(Step::Replay(index)) => {
                attacked = true;
                Some(frames[usize::from(*index) % frames.len()].clone())
            }
            Some(Step::Swap) => {
                if queue.len() >= 2 {
                    queue.swap(0, 1);
                    attacked = true;
                }
                continue;
            }
            Some(Step::Flip { at, bit }) => queue.pop_front().map(|mut frame| {
                let at = usize::from(*at) % frame.len();
                frame[at] ^= 1 << (bit % 8);
                attacked = true;
                frame
            }),
            Some(Step::Junk(bytes)) => {
                attacked = true;
                Some(bytes[..bytes.len().min(MAX_JUNK_BYTES)].to_vec())
            }
        };
        let Some(frame) = frame else {
            assert!(
                attacked,
                "a path that did nothing ran out before the end: {path:?}"
            );
            return;
        };
        match open_frame(&mut client, &frame, &mut scratch) {
            Ok(Frame::Chunk(count)) => {
                assert!(
                    chunks.get(opened).map(Vec::as_slice) == Some(&scratch[..count]),
                    "frame {opened} opened as {:?}, not the guest's next chunk: {path:?}",
                    &scratch[..count]
                );
                opened += 1;
            }
            Ok(Frame::End) => {
                assert_eq!(
                    opened,
                    chunks.len(),
                    "the daemon's end opened before every chunk: {path:?}"
                );
                return;
            }
            // The relay fails here, so nothing after it reaches the local connection.
            Err(error) => {
                assert!(
                    attacked,
                    "an untouched frame failed to open: {error}: {path:?}"
                );
                assert!(
                    error.to_string().contains("did not authenticate"),
                    "{error}"
                );
                return;
            }
        }
    }
}
