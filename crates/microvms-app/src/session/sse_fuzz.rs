// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for BIND-22: [`SseParser::feed`] and [`decode`] over hostile stream bytes.
//!
//! A root workload can reach the daemon, so the bytes of an event stream are untrusted input
//! to this client. `bolero::check!` runs the harness as an ordinary `#[test]` under stable
//! `cargo test`, and as a coverage-guided target under
//! `cargo +nightly bolero test session::sse_fuzz::hostile_stream_bytes_stay_bounded_and_every_event_round_trips -p microvms-app -T 120s`
//! (the `sse` job in `.github/workflows/fuzz.yml`).
//!
//! # What an input is
//!
//! A list of pieces, each a whole frame the daemon could send (`output`, `gap`, `exit`, with
//! every number the input's, so a gap can end before it starts), or one it never sends: an
//! event named for one of those whose fields have the wrong JSON types, undecodable base64,
//! an unknown event name, a keepalive comment, lines whose field names (`event`, `data`, `id`,
//! `retry` or the input's own) and values the input picks, or arbitrary bytes. The three
//! line-ending spellings are mixed. Every piece ends in a blank line, so a later piece starts a
//! frame of its own. Then, in one input in eight, a flood: one byte repeated with no
//! terminator, sized to land a few bytes either side of [`MAX_PENDING_BYTES`] (mostly over
//! it). The pieces are fed in chunks of at most 64 bytes, so frame terminators land across
//! chunk boundaries, and a flood in chunks large enough that it takes about a thousand feeds;
//! the input picks each size.
//!
//! # What it checks
//!
//! * No panic, whatever the bytes.
//! * After every feed that succeeds, [`SseParser::pending`] is at most [`MAX_PENDING_BYTES`].
//!   A feed fails exactly when the bytes no terminator ends pass it; the error is a protocol
//!   error that a reconnect wouldn't cure, and the parser holds nothing after it.
//! * The chunked stream yields the frames each piece yields alone, in order, and one read of
//!   every piece yields the same. A refused stream yields a prefix of them.
//! * Every frame round-trips its framing: written back as `event:` and `data:` lines, it
//!   parses to itself. Every decoded event round-trips too: encoded the way the daemon encodes
//!   it, it decodes to the same event. Only an `output` frame can fail to decode.
//! * Each well-formed piece decodes to the event it was built from, and each piece of chosen
//!   lines parses to the frame the SSE field rules make of them.

use base64::Engine as _;
use protocol::exec::{
    EVENT_EXIT, EVENT_GAP, EVENT_OUTPUT, ExitEvent, GapEvent, OutputEvent, StreamKind,
};

use super::sse::{ExecEvent, Frame, MAX_PENDING_BYTES, SseParser, decode};
use crate::error::ErrorKind;

/// The three spellings of a line ending. A frame's blank line is its ending twice.
const LINE_ENDINGS: [&str; 3] = ["\n", "\r\n", "\r"];

/// One piece of the stream, and what the parser should make of it alone.
enum Piece {
    /// A frame the daemon sends, and the event it must decode to.
    Event {
        wire: Vec<u8>,
        event: serde_json::Value,
    },
    /// An `output` frame whose base64 doesn't decode, at this offset.
    BadBase64 { wire: Vec<u8>, offset: u64 },
    /// A frame with data under an event name this client doesn't dispatch.
    Unknown { wire: Vec<u8> },
    /// A frame named for an event this client dispatches, whose data has a field of the wrong
    /// JSON type, so it's dropped.
    WrongShape { wire: Vec<u8> },
    /// Lines the input chose, and the frame the SSE field rules make of them, if any.
    Fields { wire: Vec<u8>, frame: Option<Frame> },
    /// A comment, which is no frame at all.
    Keepalive { wire: Vec<u8> },
    /// Whatever the input said, then a blank line.
    Junk { wire: Vec<u8> },
}

impl Piece {
    fn wire(&self) -> &[u8] {
        match self {
            Piece::Event { wire, .. }
            | Piece::BadBase64 { wire, .. }
            | Piece::Unknown { wire }
            | Piece::WrongShape { wire }
            | Piece::Fields { wire, .. }
            | Piece::Keepalive { wire }
            | Piece::Junk { wire } => wire,
        }
    }
}

/// The bytes as text with no line breaks, so they stay inside one field.
fn one_line(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).replace(['\r', '\n'], " ")
}

fn frame_wire(event: &str, data: &str, ending: &str) -> Vec<u8> {
    format!("event: {event}{ending}data: {data}{ending}{ending}").into_bytes()
}

/// The JSON types a protocol event's fields have, for writing a value of some other type.
#[derive(Clone, Copy)]
enum Field {
    U64,
    OptionI32,
    Bool,
    Text,
    Stream,
}

/// Values that don't deserialize as `field`, at the edges of each type where there's one.
fn wrong(field: Field, pick: u64) -> &'static str {
    let choices: &[&str] = match field {
        Field::U64 => &["-1", "1.5", "18446744073709551616", "\"7\"", "null", "[1]"],
        Field::OptionI32 => &["2147483648", "-2147483649", "1.5", "\"7\"", "[1]"],
        Field::Bool => &["1", "\"true\"", "null", "[]"],
        Field::Text => &["7", "null", "[\"a\"]", "{}"],
        Field::Stream => &["\"stdin\"", "7", "null", "\"STDOUT\""],
    };
    choices[usize::try_from(pick % choices.len() as u64).expect("a small index")]
}

/// A value that does deserialize as `field`.
fn right(field: Field) -> &'static str {
    match field {
        Field::U64 => "7",
        Field::OptionI32 => "null",
        Field::Bool => "true",
        Field::Text => "\"\"",
        Field::Stream => "\"stdout\"",
    }
}

/// An `output`, `gap` or `exit` object with each field right or wrong by a bit of `bits`, one
/// of them always wrong, and each other field sometimes left out.
fn wrong_shape(number: u64, bits: u64) -> (&'static str, String) {
    let (event, fields): (&str, &[(&str, Field)]) = match number % 3 {
        0 => (
            EVENT_OUTPUT,
            &[
                ("offset", Field::U64),
                ("stream", Field::Stream),
                ("output", Field::Text),
            ],
        ),
        1 => (EVENT_GAP, &[("from", Field::U64), ("to", Field::U64)]),
        _ => (
            EVENT_EXIT,
            &[
                ("exit_code", Field::OptionI32),
                ("signal", Field::OptionI32),
                ("timed_out", Field::Bool),
                ("truncated", Field::Bool),
                ("writers_may_be_alive", Field::Bool),
                ("offset", Field::U64),
            ],
        ),
    };
    let forced = usize::try_from(number / 3 % fields.len() as u64).expect("a small index");
    let mut members = Vec::new();
    for (at, (name, field)) in fields.iter().enumerate() {
        let shift = 3 * u32::try_from(at).expect("a small index");
        let value = if at == forced || bits >> shift & 1 == 1 {
            wrong(*field, bits >> (shift + 1) ^ number)
        } else if bits >> (shift + 2) & 1 == 1 {
            continue;
        } else {
            right(*field)
        };
        members.push(format!("\"{name}\":{value}"));
    }
    (event, format!("{{{}}}", members.join(",")))
}

/// The field names a chosen line can carry; any other pick uses the input's bytes as the name.
const FIELD_NAMES: [&str; 4] = ["event", "data", "id", "retry"];

/// Lines built from `bytes`, split at each zero byte. A line's first byte picks its name and
/// whether it's `name: value`, `name:value` or a bare `name`; the rest is its value.
fn chosen_lines(bytes: &[u8]) -> Vec<String> {
    bytes
        .split(|byte| *byte == 0)
        .map(|segment| {
            let (pick, rest) = segment.split_first().map_or((0, &[][..]), |(p, r)| (*p, r));
            let value = one_line(rest);
            let name = FIELD_NAMES
                .get(usize::from(pick % 5))
                .map_or_else(|| value.replace(':', "_"), |name| (*name).to_string());
            let line = match pick / 5 % 3 {
                0 => format!("{name}: {value}"),
                1 => format!("{name}:{value}"),
                _ => name,
            };
            // An empty line would end the frame early; a lone colon is a comment instead.
            if line.is_empty() {
                ":".to_string()
            } else {
                line
            }
        })
        .collect()
}

/// The frame the SSE field rules make of `lines`: a line starting with a colon is a comment, a
/// field's name runs to the first colon and its value drops one leading space, the last `event`
/// names the frame, every `data` adds a line, and a frame with no `data` isn't one.
fn frame_of(lines: &[String]) -> Option<Frame> {
    let mut event = String::new();
    let mut data = Vec::new();
    for line in lines {
        if line.starts_with(':') {
            continue;
        }
        let (name, value) = line.split_once(':').unwrap_or((line, ""));
        let value = value.strip_prefix(' ').unwrap_or(value);
        match name {
            "event" => event = value.to_string(),
            "data" => data.push(value),
            _ => {}
        }
    }
    (!data.is_empty()).then(|| Frame {
        event,
        data: data.join("\n"),
    })
}

fn piece(kind: u8, number: u64, other: u64, bytes: &[u8]) -> Piece {
    let ending = LINE_ENDINGS[usize::from(kind / 9) % LINE_ENDINGS.len()];
    match kind % 9 {
        0 => {
            let stream = StreamKind::ALL[usize::from(number % 2 == 1)];
            let output = OutputEvent {
                offset: number,
                stream,
                output: base64::engine::general_purpose::STANDARD.encode(bytes),
            };
            Piece::Event {
                wire: frame_wire(EVENT_OUTPUT, &json(&output), ending),
                event: output_value(stream, number, bytes),
            }
        }
        1 => {
            // Each end the input's own, so `to` can come before `from`.
            let gap = GapEvent {
                from: number,
                to: other,
            };
            Piece::Event {
                wire: frame_wire(EVENT_GAP, &json(&gap), ending),
                event: gap_value(gap.from, gap.to),
            }
        }
        2 => {
            let bit = |at: u32| number >> at & 1 == 1;
            let [a, b, c, d, e, f, g, h] = other.to_le_bytes();
            let exit = ExitEvent {
                exit_code: bit(0).then_some(i32::from_le_bytes([a, b, c, d])),
                signal: bit(1).then_some(i32::from_le_bytes([e, f, g, h])),
                timed_out: bit(2),
                truncated: bit(3),
                writers_may_be_alive: bit(4),
                offset: number,
            };
            Piece::Event {
                wire: frame_wire(EVENT_EXIT, &json(&exit), ending),
                event: exit_value(&exit),
            }
        }
        3 => {
            // `!` is outside the base64 alphabet, so no text around it decodes.
            let output = OutputEvent {
                offset: number,
                stream: StreamKind::Stdout,
                output: format!("{}!", one_line(bytes)),
            };
            Piece::BadBase64 {
                wire: frame_wire(EVENT_OUTPUT, &json(&output), ending),
                offset: number,
            }
        }
        4 => Piece::Keepalive {
            wire: format!(":{}{ending}{ending}", one_line(bytes)).into_bytes(),
        },
        5 => Piece::Unknown {
            wire: frame_wire(&format!("x-{}", one_line(bytes)), &one_line(bytes), ending),
        },
        6 => {
            let (event, data) = wrong_shape(number, other);
            Piece::WrongShape {
                wire: frame_wire(event, &data, ending),
            }
        }
        7 => {
            let lines = chosen_lines(bytes);
            let mut wire = lines.join(ending);
            wire.push_str(ending);
            wire.push_str(ending);
            Piece::Fields {
                wire: wire.into_bytes(),
                frame: frame_of(&lines),
            }
        }
        _ => {
            let mut wire = bytes.to_vec();
            wire.extend_from_slice(b"\n\n");
            Piece::Junk { wire }
        }
    }
}

/// A protocol event as the JSON a `data:` line carries. None of them can fail to serialize.
fn json(event: &impl serde::Serialize) -> String {
    serde_json::to_string(event).expect("a protocol event serializes")
}

fn output_value(stream: StreamKind, offset: u64, data: &[u8]) -> serde_json::Value {
    serde_json::json!({ "output": { "stream": stream, "offset": offset, "data": data } })
}

fn gap_value(from: u64, to: u64) -> serde_json::Value {
    serde_json::json!({ "gap": { "from": from, "to": to } })
}

fn exit_value(exit: &ExitEvent) -> serde_json::Value {
    serde_json::json!({ "exit": exit })
}

/// A decoded event as a value two events can be compared by (`ExecEvent` has no `PartialEq`).
fn value(event: &ExecEvent) -> serde_json::Value {
    match event {
        ExecEvent::Output {
            stream,
            offset,
            data,
        } => output_value(*stream, *offset, data),
        ExecEvent::Gap { from, to } => gap_value(*from, *to),
        ExecEvent::Exit(exit) => exit_value(exit),
    }
}

/// The frame the daemon would write for `event`.
fn encode(event: &ExecEvent) -> Frame {
    let (name, data) = match event {
        ExecEvent::Output {
            stream,
            offset,
            data,
        } => (
            EVENT_OUTPUT,
            json(&OutputEvent {
                offset: *offset,
                stream: *stream,
                output: base64::engine::general_purpose::STANDARD.encode(data),
            }),
        ),
        ExecEvent::Gap { from, to } => (
            EVENT_GAP,
            json(&GapEvent {
                from: *from,
                to: *to,
            }),
        ),
        ExecEvent::Exit(exit) => (EVENT_EXIT, json(exit)),
    };
    Frame {
        event: name.to_string(),
        data,
    }
}

/// `frame` written back as the lines that carry it.
fn render(frame: &Frame) -> Vec<u8> {
    let mut wire = format!("event: {}\n", frame.event);
    for line in frame.data.split('\n') {
        wire.push_str("data: ");
        wire.push_str(line);
        wire.push('\n');
    }
    wire.push('\n');
    wire.into_bytes()
}

/// Checks one piece against the event it was built from, alone in a fresh parser.
fn check_piece(piece: &Piece) {
    let frames = SseParser::new()
        .feed(piece.wire())
        .expect("one piece is far under the ceiling");
    match piece {
        Piece::Event { event, .. } => {
            assert_eq!(frames.len(), 1, "BIND-22: a daemon frame is one frame");
            let decoded = decode(&frames[0])
                .expect("BIND-22: a daemon frame decodes")
                .expect("BIND-22: a daemon frame is an event");
            assert_eq!(
                &value(&decoded),
                event,
                "BIND-22: the event it was built from"
            );
        }
        Piece::BadBase64 { offset, .. } => {
            assert_eq!(
                frames.len(),
                1,
                "BIND-22: a corrupt output frame is still a frame"
            );
            let error = decode(&frames[0]).expect_err("BIND-22: corrupt output is an error");
            assert!(
                error.to_string().contains(&offset.to_string()),
                "BIND-22: the error names the offset: {error}"
            );
        }
        Piece::Unknown { .. } => {
            assert_eq!(
                frames.len(),
                1,
                "BIND-22: an unknown event is still a frame"
            );
            assert!(
                decode(&frames[0])
                    .expect("an unknown event isn't an error")
                    .is_none(),
                "BIND-22: an unknown event is dropped"
            );
        }
        Piece::WrongShape { .. } => {
            assert_eq!(
                frames.len(),
                1,
                "BIND-22: a mistyped event is still a frame"
            );
            assert!(
                decode(&frames[0])
                    .expect("a mistyped event isn't an error")
                    .is_none(),
                "BIND-22: a mistyped event is dropped: {:?}",
                frames[0]
            );
        }
        Piece::Fields { frame, .. } => {
            assert_eq!(
                frames.as_slice(),
                frame.as_slice(),
                "BIND-22: chosen lines parse by the SSE field rules"
            );
        }
        Piece::Keepalive { .. } => {
            assert!(frames.is_empty(), "BIND-22: a comment is not a frame");
        }
        Piece::Junk { .. } => {}
    }
}

/// Checks that `frame` and its decoded event each survive being written back out.
fn check_round_trip(frame: &Frame) {
    let mut parser = SseParser::new();
    let again = parser
        .feed(&render(frame))
        .expect("one frame is far under the ceiling");
    assert_eq!(
        again,
        std::slice::from_ref(frame),
        "BIND-22: the frame round-trips"
    );
    assert_eq!(
        parser.pending(),
        0,
        "BIND-22: a whole frame leaves nothing held"
    );

    match decode(frame) {
        Ok(Some(event)) => {
            let reencoded = decode(&encode(&event))
                .expect("BIND-22: a re-encoded event decodes")
                .expect("BIND-22: a re-encoded event is an event");
            assert_eq!(
                value(&reencoded),
                value(&event),
                "BIND-22: the event round-trips"
            );
        }
        Ok(None) => {}
        Err(error) => assert_eq!(
            frame.event, EVENT_OUTPUT,
            "BIND-22: only output fails to decode, not {frame:?}: {error}"
        ),
    }
}

/// **BIND-22.** Hostile stream bytes never panic the parser or leave more than
/// `MAX_PENDING_BYTES` held between reads, a flood past the ceiling is refused exactly when the
/// undelimited bytes pass it, and every frame and event round-trips its framing.
///
/// Seeded as `sse-fuzz-ceiling-removed` in `verify/guards/faults/sse.toml`: delete the ceiling check in
/// `SseParser::feed` and the first flood over the ceiling holds more than it, which fails the
/// bound after that feed. One input in eight carries a flood and all but a few floods pass the
/// ceiling, so the `ITERATIONS` inputs a stable `cargo test` runs meet about sixteen, and the
/// chance they meet none is under one in ten million. `sse-fuzz-gap-backwards` and
/// `sse-fuzz-straddle-missed` seed a panic on a gap that ends before it starts and a scan that
/// misses a terminator split across two feeds.
#[test]
fn hostile_stream_bytes_stay_bounded_and_every_event_round_trips() {
    // A count rather than bolero's default one-second budget, which ran out before the first
    // flood when the machine was loaded, so the ceiling fault's firing depended on the load.
    // `cargo bolero test` ignores it and runs for its own `-T`.
    const ITERATIONS: usize = 128;
    bolero::check!()
        .with_iterations(ITERATIONS)
        // No shrinking. Every shrink step replays the input, a flood in it costs a quarter of a
        // second in a debug build, and shrinking one failing input took up to 40 s. The failure
        // prints the input and its `BOLERO_RANDOM_SEED`, which replays it.
        .with_shrink_time(std::time::Duration::ZERO)
        .with_type::<(Vec<(u8, u64, u64, Vec<u8>)>, Vec<u16>, Option<(u8, u8, u8)>)>()
        .for_each(|(pieces, splits, flood)| {
            let pieces: Vec<Piece> = pieces
                .iter()
                .map(|(kind, number, other, bytes)| piece(*kind, *number, *other, bytes))
                .collect();

            let mut expected = Vec::new();
            let mut whole = Vec::new();
            for piece in &pieces {
                check_piece(piece);
                expected.extend(
                    SseParser::new()
                        .feed(piece.wire())
                        .expect("one piece is far under the ceiling"),
                );
                whole.extend_from_slice(piece.wire());
            }
            let mut one_read = SseParser::new();
            let frames = one_read
                .feed(&whole)
                .expect("the pieces are far under the ceiling");
            assert_eq!(
                frames, expected,
                "BIND-22: one read yields each piece's frames"
            );
            let residue = one_read.pending();
            for frame in &expected {
                check_round_trip(frame);
            }

            // The pieces alone in the chunks the input sizes, before any flood. The flood run
            // below cuts the pieces at the same places, so a terminator it misses across a
            // chunk boundary this run misses too, and fails here. Checked only after a flood, a
            // missed terminator could end in the refused branch instead, whose prefix check
            // holds a stream that lost its last frame, and `sse-fuzz-straddle-missed` fired
            // with either message by the seed. A missed terminator after a comment or junk loses
            // no frame, only the bytes held, so both asserts carry the entry's message.
            let mut sizes = splits.iter().cycle();
            let mut parser = SseParser::new();
            let mut emitted = Vec::new();
            let mut at = 0;
            while at < whole.len() {
                let size = sizes
                    .next()
                    .map_or(whole.len(), |size| usize::from(*size) % 64 + 1);
                let end = at + size.min(whole.len() - at);
                emitted.extend(
                    parser
                        .feed(&whole[at..end])
                        .expect("the pieces are far under the ceiling"),
                );
                at = end;
            }
            assert_eq!(
                emitted, expected,
                "BIND-22: the chunks yield each piece's frames"
            );
            assert_eq!(
                parser.pending(),
                residue,
                "BIND-22: the chunks yield each piece's frames and hold what one read holds"
            );

            // A byte that ends no line, repeated to within a few bytes of the ceiling. One input
            // in eight carries one: a flood costs as much as a thousand small inputs, and the
            // fuzzer's time is better spent mostly on the frames.
            let flood = flood.filter(|(gate, ..)| gate % 4 == 0).map_or_else(
                Vec::new,
                |(_, byte, over)| {
                    let byte = if byte == b'\n' || byte == b'\r' {
                        b'x'
                    } else {
                        byte
                    };
                    vec![byte; MAX_PENDING_BYTES - 4 + usize::from(over)]
                },
            );
            let refusal_due = residue + flood.len() > MAX_PENDING_BYTES;
            let framed = whole.len();
            whole.extend_from_slice(&flood);

            // Chunks the input sizes: at most 64 bytes over the pieces, so a terminator often
            // straddles two feeds, and never so small over a flood that it takes millions.
            let floor = flood.len() / 1024;
            let mut parser = SseParser::new();
            let mut emitted = Vec::new();
            let mut refused = false;
            let mut at = 0;
            let mut sizes = splits.iter().cycle();
            while at < whole.len() {
                let size = match sizes.next() {
                    None => whole.len(),
                    Some(size) if at < framed => usize::from(*size) % 64 + 1,
                    Some(size) => (usize::from(*size) + 1).max(floor),
                };
                let end = at + size.min(whole.len() - at);
                match parser.feed(&whole[at..end]) {
                    Ok(frames) => {
                        assert!(
                            parser.pending() <= MAX_PENDING_BYTES,
                            "BIND-22: {} bytes held, over the {MAX_PENDING_BYTES}-byte ceiling",
                            parser.pending()
                        );
                        emitted.extend(frames);
                    }
                    Err(error) => {
                        assert_eq!(error.kind(), ErrorKind::Protocol, "{error}");
                        assert!(
                            !error.retryable(),
                            "BIND-22: a reconnect refills it: {error}"
                        );
                        assert_eq!(parser.pending(), 0, "BIND-22: refused bytes are dropped");
                        refused = true;
                        break;
                    }
                }
                at = end;
            }

            assert_eq!(
                refused,
                refusal_due,
                "BIND-22: {residue} + {} undelimited bytes against the ceiling",
                flood.len()
            );
            if refused {
                assert!(
                    expected.starts_with(&emitted),
                    "BIND-22: a refused stream yields a prefix of its frames"
                );
            } else {
                assert_eq!(
                    emitted, expected,
                    "BIND-22: a flood under the ceiling leaves the frames whole"
                );
                assert_eq!(parser.pending(), residue + flood.len());
            }
        });
}
