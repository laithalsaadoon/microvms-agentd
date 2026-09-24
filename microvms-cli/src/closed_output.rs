// SPDX-License-Identifier: Apache-2.0
//! What the CLI does when a reader closes its stdout or stderr: CLI-7, CLI-8, and CLI-9.
//!
//! This is the binary's copy of `specified()` in `model/src/output.rs`, the Stateright model
//! that checks these requirements. The model rejects the two alternatives: resetting SIGPIPE
//! to its default (the process dies by signal and skips its teardown) and reporting a closed
//! reader as a failure (a `run` that launched a VM would exit non-zero and invite a second
//! launch). `closed_output_fuzz::MODEL_TABLE` pins this module to the model's table.
//!
//! Every write in the CLI goes through [`crate::envelope::Output`], which records a
//! `BrokenPipe` on either stream and never writes to that stream again. Windows needs no
//! separate path: std maps `ERROR_BROKEN_PIPE` and `ERROR_NO_DATA` to `ErrorKind::BrokenPipe`.

use std::io;

use crate::exit::Exit;

/// What the CLI was asked to do, reduced to the shapes that treat output differently.
///
/// The binary only asks about [`Command::Stream`] and [`Channel::Stdout`]; the other shapes
/// are the model's, kept so `closed_output_fuzz::MODEL_TABLE` checks the whole table.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(not(test), allow(dead_code))]
pub enum Command {
    /// `--help` or `--version`: clap's text, then exit 0.
    Help,
    /// A command that writes one document to stdout.
    OneShot,
    /// A command that launched a VM and owes its teardown before it exits.
    Launch,
    /// `exec --stream`: events on stdout until the remote exec ends.
    Stream,
}

/// The two streams a reader can close.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(not(test), allow(dead_code))]
pub enum Channel {
    Stdout,
    Stderr,
}

/// What to do after a write found its reader gone.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Decision {
    /// Drop the bytes and carry on with the command (CLI-7, CLI-8).
    Continue,
    /// Stop streaming, leave the remote exec running, and exit `ERR_INTERRUPTED` (CLI-9).
    StopStream,
}

/// The answer for one failed write. `stream_event` marks a write of stream output, as opposed
/// to a document, a progress line, or a note.
pub fn on_failed_write(command: Command, channel: Channel, stream_event: bool) -> Decision {
    // CLI-9: only a stream event that cannot reach stdout's reader stops anything.
    match (command, channel, stream_event) {
        (Command::Stream, Channel::Stdout, true) => Decision::StopStream,
        // CLI-7, CLI-8: every other failed write is dropped; the command runs to its end.
        _ => Decision::Continue,
    }
}

/// The exit code for a command that reached `outcome`.
///
/// CLI-8: the readers' state is taken as parameters so it is visible that it is ignored. A
/// closed reader is the consumer's choice, not an outcome of the command.
pub fn exit_code(outcome: Exit, _stdout_open: bool, _stderr_open: bool) -> Exit {
    outcome
}

/// Whether an I/O error means the reader of the stream has gone (CLI-7).
pub fn is_closed_reader(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::BrokenPipe
}

#[cfg(test)]
mod tests {
    use super::*;

    /// CLI-7: only `BrokenPipe` counts as a closed reader; a full disk is a different failure.
    #[test]
    fn only_a_broken_pipe_is_a_closed_reader() {
        assert!(is_closed_reader(&io::ErrorKind::BrokenPipe.into()));
        assert!(!is_closed_reader(&io::ErrorKind::StorageFull.into()));
        assert!(!is_closed_reader(&io::ErrorKind::Interrupted.into()));
    }
}
