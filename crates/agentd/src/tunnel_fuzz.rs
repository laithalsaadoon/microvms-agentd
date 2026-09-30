// SPDX-License-Identifier: Apache-2.0
//! The fuzz harnesses for the daemon's verified-tunnel reads: the caller's handshake message
//! (AGENTD-17), and each frame after it over a hostile path (AGENTD-20, AGENTD-21).
//!
//! `bolero::check!` runs each as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test tunnel_fuzz::a_handshake_opens_only_from_the_launching_host -p agentd -T 60s`
//! and `... tunnel_fuzz::frames_open_in_order_and_the_callers_end_only_after_every_chunk ...`
//! (the `daemon-edges-app` job in `.github/workflows/fuzz.yml`).
//!
//! # What an input is
//!
//! For the handshake: the launching host's first message for this VM, with the offer or a
//! payload the input picks; one from another host key, or made for another VM's key; the
//! launching host's with one bit flipped; or whatever bytes. For the frames: chunks of one to
//! eight bytes the caller sends, sealed in one real session and followed by its end of stream,
//! and the steps of a path that delivers the next frame, drops it, replays any frame of the
//! session, swaps the next two, flips a bit, or sends bytes of its own. The steps the input
//! gives run first (half the paths take none), and the rest of the frames are then delivered in
//! order, so a path that did nothing ends clean.
//!
//! # What it checks
//!
//! * No panic, whatever the bytes.
//! * The handshake opens exactly when the launching host made it for this VM, and the offer
//!   read is the payload's (AGENTD-17).
//! * Each frame that opens is the caller's next chunk, whole and in order, so a replayed, a
//!   reordered and a forged frame never reach the guest (AGENTD-21).
//! * The caller's end of stream opens only after every chunk the caller sent (AGENTD-20), and
//!   a path that did nothing reaches it.

use bolero::TypeGenerator;

use crate::tunnel::Inbound;
use crate::tunnel::handshake::{open, read_hello};

const VM_SEED: [u8; 32] = [7; 32];
const HOST_SEED: [u8; 32] = [9; 32];
const OTHER_SEED: [u8; 32] = [11; 32];
/// Caps that keep an input's work small without narrowing what a frame can hold.
const MAX_CHUNKS: usize = 8;
const MAX_JUNK_BYTES: usize = 256;

fn public_of(seed: [u8; 32]) -> [u8; 32] {
    *x25519_dalek::PublicKey::from(&x25519_dalek::StaticSecret::from(seed)).as_bytes()
}

/// The VM's responder, from the material a launch with these seeds delivers.
fn responder() -> snow::HandshakeState {
    use base64::Engine as _;
    let b64 = base64::engine::general_purpose::STANDARD;
    let hook = protocol::hook::RunHook {
        agent_token: "tunnel-fuzz".to_string(),
        env: std::collections::HashMap::new(),
        identity_seed: Some(b64.encode(VM_SEED)),
        identity_host_public_key: Some(b64.encode(public_of(HOST_SEED))),
    };
    crate::tunnel_identity::Material::from_payload(&hook)
        .expect("valid material")
        .expect("present")
        .responder()
        .expect("the responder builds")
}

/// An initiator with `host_seed`'s key, pinning `vm_public`.
fn initiator(host_seed: [u8; 32], vm_public: [u8; 32]) -> snow::HandshakeState {
    snow::Builder::new(protocol::identity::NOISE_PATTERN.parse().expect("parses"))
        .local_private_key(&host_seed)
        .and_then(|builder| builder.remote_public_key(&vm_public))
        .and_then(|builder| builder.build_initiator())
        .expect("the initiator builds")
}

fn hello(host_seed: [u8; 32], vm_public: [u8; 32], payload: &[u8]) -> Vec<u8> {
    let mut scratch = vec![0_u8; 65535];
    let written = initiator(host_seed, vm_public)
        .write_message(payload, &mut scratch)
        .expect("the handshake message writes");
    scratch[..written].to_vec()
}

/// A caller's first handshake message.
#[derive(Clone, Debug, TypeGenerator)]
enum Hello {
    /// The launching host's, for this VM, offering the end of stream or not.
    Launcher { offer: bool },
    /// The launching host's, for this VM, with a payload the input picks.
    Payload(Vec<u8>),
    /// Another host key's, for this VM.
    OtherHost { offer: bool },
    /// The launching host's, made for another VM's key.
    OtherVm { offer: bool },
    /// The launching host's, with one bit flipped.
    Flipped { offer: bool, at: u16, bit: u8 },
    /// Whatever bytes.
    Bytes(Vec<u8>),
}

fn offer_payload(offer: bool) -> &'static [u8] {
    if offer {
        &protocol::identity::HANDSHAKE_PAYLOAD
    } else {
        &[]
    }
}

impl Hello {
    /// The bytes on the wire, and the offer the daemon should read, when it should open.
    fn wire(&self) -> (Vec<u8>, Option<bool>) {
        let vm = public_of(VM_SEED);
        match self {
            Hello::Launcher { offer } => {
                (hello(HOST_SEED, vm, offer_payload(*offer)), Some(*offer))
            }
            Hello::Payload(payload) => {
                let payload = &payload[..payload.len().min(MAX_JUNK_BYTES)];
                (
                    hello(HOST_SEED, vm, payload),
                    Some(protocol::identity::offers_end_of_stream(payload)),
                )
            }
            Hello::OtherHost { offer } => (hello(OTHER_SEED, vm, offer_payload(*offer)), None),
            Hello::OtherVm { offer } => (
                hello(HOST_SEED, public_of(OTHER_SEED), offer_payload(*offer)),
                None,
            ),
            Hello::Flipped { offer, at, bit } => {
                let mut wire = hello(HOST_SEED, vm, offer_payload(*offer));
                let at = usize::from(*at) % wire.len();
                wire[at] ^= 1 << (bit % 8);
                (wire, None)
            }
            // A random message that happens to be a valid KK message for these keys is a
            // 2^-128 event; the harness would report it rather than hide it.
            Hello::Bytes(bytes) => (bytes[..bytes.len().min(MAX_JUNK_BYTES)].to_vec(), None),
        }
    }
}

/// **AGENTD-17.** Hostile bytes as the caller's first handshake message open the handshake
/// exactly when the launching host made them for this VM, with the offer the payload carries.
#[test]
fn a_handshake_opens_only_from_the_launching_host() {
    bolero::check!().with_type::<Hello>().for_each(|case| {
        let (wire, expected) = case.wire();
        let mut scratch = vec![0_u8; 65535];
        let read = read_hello(&mut responder(), &wire, &mut scratch);
        match (read, expected) {
            (Ok(offered), Some(offer)) => assert_eq!(offered, offer, "{case:?}"),
            (Err(_), None) => {}
            (read, expected) => {
                panic!("{case:?} read as {read:?}, and the launching host's rule says {expected:?}")
            }
        }
    });
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

/// A caller's chunks, and a path.
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

/// A completed session: the caller's transport and the daemon's.
fn session() -> (snow::TransportState, snow::TransportState) {
    let mut caller = initiator(HOST_SEED, public_of(VM_SEED));
    let mut daemon = responder();
    let mut scratch = vec![0_u8; 65535];
    let mut reply = vec![0_u8; 65535];
    let first = caller
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut scratch)
        .expect("writes");
    daemon
        .read_message(&scratch[..first], &mut reply)
        .expect("opens");
    let second = daemon
        .write_message(&protocol::identity::HANDSHAKE_PAYLOAD, &mut scratch)
        .expect("writes");
    caller
        .read_message(&scratch[..second], &mut reply)
        .expect("opens");
    (
        caller.into_transport_mode().expect("transport"),
        daemon.into_transport_mode().expect("transport"),
    )
}

/// **AGENTD-20 and AGENTD-21.** Over a path that drops, replays, swaps, flips and forges, each
/// frame the daemon opens is the caller's next chunk, whole and in order, and the caller's end
/// of stream opens only after every chunk; a path that did nothing reaches it.
#[test]
fn frames_open_in_order_and_the_callers_end_only_after_every_chunk() {
    bolero::check!().with_type::<Path>().for_each(|path| {
        let chunks: Vec<Vec<u8>> = path
            .chunks
            .iter()
            .filter(|chunk| !chunk.is_empty())
            .take(MAX_CHUNKS)
            .map(|chunk| chunk[..chunk.len().min(1 + usize::from(chunk[0] % 8))].to_vec())
            .collect();
        let (mut caller, mut daemon) = session();
        let mut scratch = vec![0_u8; 65535];
        // Every frame of the session, the end of stream last: what the path saw.
        let frames: Vec<Vec<u8>> = chunks
            .iter()
            .map(Vec::as_slice)
            .chain([&[][..]])
            .map(|plain| {
                let written = caller.write_message(plain, &mut scratch).expect("seals");
                scratch[..written].to_vec()
            })
            .collect();
        let mut queue: std::collections::VecDeque<Vec<u8>> = frames.iter().cloned().collect();
        let mut opened = 0;
        let mut attacked = false;
        let mut plain = vec![0_u8; 65535];

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
                // Nothing left to deliver: only an attack gets here without the end.
                assert!(
                    attacked,
                    "a path that did nothing ran out before the end: {path:?}"
                );
                return;
            };
            match open(&mut daemon, &frame, &mut plain) {
                Inbound::Bytes(bytes) => {
                    assert!(
                        chunks.get(opened) == Some(&bytes),
                        "frame {opened} opened as {bytes:?}, not the caller's next chunk: {path:?}"
                    );
                    opened += 1;
                }
                Inbound::Ended => {
                    assert_eq!(
                        opened,
                        chunks.len(),
                        "the caller's end opened before every chunk: {path:?}"
                    );
                    return;
                }
                // The pump ends the tunnel here, so nothing after it reaches the guest.
                Inbound::Failed(_) => {
                    assert!(attacked, "an untouched frame failed to open: {path:?}");
                    return;
                }
                Inbound::Closed => unreachable!("open never reads a close"),
            }
        }
    });
}
