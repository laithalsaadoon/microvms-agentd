// SPDX-License-Identifier: Apache-2.0
//! Resolve CLI credential inputs before any command starts work or consumes stdin.

use std::io::Read;
use std::path::Path;

use crate::cli::{AttachFlags, Command};
use crate::exit::{CliError, Exit};

type Input<'a> = (&'a mut Option<String>, Option<&'a Path>, &'static str);

fn token_input(flags: &mut AttachFlags) -> Input<'_> {
    (
        &mut flags.agent_token,
        flags.agent_token_file.as_deref(),
        "agent-token",
    )
}

pub(crate) fn prepare(
    command: &mut Command,
    stdin: &mut impl Read,
    mut warn: impl FnMut(&str),
) -> Result<(), CliError> {
    // A credential consumes stdin through EOF. Refuse a second reader before either
    // reader runs, so command input is never silently consumed as a credential.
    let command_reads_stdin = matches!(command, Command::Exec(args) if args.stdin)
        || matches!(command, Command::Stdin(args) if args.data.as_deref() == Some("-"));
    let inputs = match command {
        Command::Exec(args) => vec![token_input(&mut args.attach)],
        Command::Wait(args) => vec![token_input(&mut args.attach)],
        Command::Health(args) => vec![token_input(&mut args.attach)],
        Command::Keepalive(args) => vec![token_input(&mut args.attach)],
        Command::Ack(args) => vec![token_input(&mut args.attach)],
        Command::Kill(args) => vec![token_input(&mut args.attach)],
        Command::Ps(args) => vec![token_input(&mut args.attach)],
        Command::Stdin(args) => vec![token_input(&mut args.attach)],
        Command::Cp(args) => vec![token_input(&mut args.attach)],
        Command::Exists(args) => vec![token_input(&mut args.attach)],
        Command::Sync(args) => vec![token_input(&mut args.attach)],
        Command::Adopt(args) => vec![token_input(&mut args.attach)],
        Command::PortForward(args) => vec![token_input(&mut args.attach)],
        Command::AgentPrompt(args) => vec![token_input(&mut args.attach)],
        Command::Tunnel(args) => vec![
            token_input(&mut args.attach),
            (
                &mut args.identity_host_seed,
                args.identity_host_seed_file.as_deref(),
                "identity-host-seed",
            ),
        ],
        Command::Attach(args) => vec![
            (
                &mut args.agent_token,
                args.agent_token_file.as_deref(),
                "agent-token",
            ),
            (
                &mut args.identity_host_seed,
                args.identity_host_seed_file.as_deref(),
                "identity-host-seed",
            ),
        ],
        _ => Vec::new(),
    };
    let stdin_readers = inputs
        .iter()
        .filter(|(_, path, _)| *path == Some(Path::new("-")))
        .count()
        + usize::from(command_reads_stdin);
    if stdin_readers > 1 {
        return Err(CliError::new(
            Exit::InvalidArg,
            "stdin can supply only one input; use a protected file for the other input",
        ));
    }
    for (value, path, flag) in inputs {
        if let Some(path) = path {
            *value = Some(read_secret(path, stdin, flag)?);
        } else if value.is_some() {
            warn(&format!(
                "--{flag} supplies a secret on the command line; prefer --name, \
                 attach --from, or --{flag}-file"
            ));
        }
    }
    Ok(())
}

fn read_secret(path: &Path, stdin: &mut impl Read, flag: &str) -> Result<String, CliError> {
    let result = if path == Path::new("-") {
        microvms_core::secret_input::read_secret(stdin)
    } else {
        microvms_core::secret_input::read_secret_file(path)
    };
    result.map_err(|error| {
        CliError::new(
            Exit::InvalidArg,
            format!("could not read --{flag}-file input: {error}"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::Cli;
    use clap::Parser;
    use std::io::Write;

    #[test]
    fn every_attached_command_refuses_an_unreadable_token_file() {
        let directory = tempfile::tempdir().unwrap();
        let missing = directory.path().join("missing-token");
        for arguments in [
            vec!["exec", "true"],
            vec!["wait"],
            vec!["health"],
            vec!["keepalive"],
            vec!["ack", "exec-example"],
            vec!["kill", "exec-example"],
            vec!["ps"],
            vec!["stdin", "exec-example"],
            vec!["cp", "local", "vm:/remote"],
            vec!["exists", "/remote"],
            vec!["sync", "."],
            vec!["adopt"],
            vec!["port-forward", "8080"],
            vec!["agent-prompt", "task"],
            vec!["tunnel", "5432"],
            vec!["attach", "--name", "example"],
        ] {
            let name = arguments[0];
            let mut argv = vec!["microvm"];
            argv.extend(arguments);
            argv.extend([
                "--endpoint",
                "https://example.invalid",
                "--microvm-id",
                "mvm-example",
                "--agent-token-file",
                missing.to_str().unwrap(),
            ]);
            let mut command = Cli::try_parse_from(argv).unwrap().command;
            let mut stdin = &b"untouched"[..];
            let mut warnings = Vec::new();
            let error = prepare(&mut command, &mut stdin, |warning| {
                warnings.push(warning.to_owned())
            })
            .expect_err(name);
            assert_eq!(error.exit, Exit::InvalidArg, "{name}");
            assert!(
                error
                    .message
                    .contains("could not read --agent-token-file input"),
                "{name}: {}",
                error.message
            );
            assert_eq!(stdin, b"untouched", "{name}");
            assert!(warnings.is_empty(), "{name}: {warnings:?}");
        }
    }

    #[test]
    fn file_inputs_preserve_credential_values() {
        let mut token = tempfile::NamedTempFile::new().unwrap();
        token.write_all(b"example-token\r\n").unwrap();
        let mut seed = tempfile::NamedTempFile::new().unwrap();
        seed.write_all(b"example-seed\n").unwrap();
        let mut command = Cli::try_parse_from([
            "microvm",
            "tunnel",
            "5432",
            "--endpoint",
            "https://example.invalid",
            "--microvm-id",
            "mvm-example",
            "--verify-identity",
            "--agent-token-file",
            token.path().to_str().unwrap(),
            "--identity-host-seed-file",
            seed.path().to_str().unwrap(),
            "--identity-vm-public-key",
            "example-pin",
        ])
        .unwrap()
        .command;
        let mut warnings = Vec::new();
        prepare(&mut command, &mut &b""[..], |warning| {
            warnings.push(warning.to_owned())
        })
        .unwrap();
        let Command::Tunnel(args) = command else {
            panic!("expected tunnel");
        };
        assert_eq!(args.attach.agent_token.as_deref(), Some("example-token"));
        assert_eq!(args.identity_host_seed.as_deref(), Some("example-seed"));
        assert!(warnings.is_empty());
    }
}
