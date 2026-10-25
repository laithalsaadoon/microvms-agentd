// SPDX-License-Identifier: Apache-2.0
//! The one door to AWS, as a trait — which is what makes CLI-2's behavioral guard possible.
//!
//! # Why a trait, where the Python had monkeypatching
//!
//! `cli.py` names two module-level factories, `open_sandbox` (`cli.py:645`) and
//! `attach_session` (`cli.py:661`), and its behavioral guard patches both to raise. Its own
//! comment says why the *names* matter rather than just the failure: a handler that
//! constructed its own `Sandbox(...)` would still fail the patched test — the library's
//! client factory is patched too — so "it failed" and even "it failed with the sentinel"
//! both stay true while the seam has been bypassed. What distinguishes them is whether the
//! CLI-level seam was *entered*, which is what `test_cli.py:398` asserts.
//!
//! Rust has no monkeypatching, so the seam is a trait every handler takes by `&dyn`. That
//! is strictly better than the thing it replaces: the fake is passed in rather than swapped
//! into a module, there is no global mutable state, and a handler that wanted to reach
//! around it would have to call a core constructor, which the crate's `clippy.toml` refuses
//! outside this file: `ControlPlane::new`, `Sandbox::new`, `adopt_in` and `from_name`,
//! `Session::connect`, `direct`, `attach` and `builder`, core's `preflight`, and the production
//! transport and adapters by type. The two `#[expect]` sites below, one per door call, are
//! listed in `LINT_EXCEPTIONS` in `tools/test_ratchet.py`.
//!
//! # There are exactly three methods, and each is a whole capability
//!
//! `control_plane`, `open_sandbox`, `attach_session`. Not one method per operation: a seam
//! with twenty methods is a seam a handler can be half-way through, and the guard's
//! question is "did this command reach AWS through the library at all". Three, because they
//! are the three shapes — a plane for the read-only listing calls, a sandbox for the
//! lifecycle, a session for a VM this invocation did not launch.
//!
//! # `Region` resolution lives here too
//!
//! Because it must agree with the SDK the library calls: `AWS_REGION` then
//! `AWS_DEFAULT_REGION` then `us-east-1`, which is boto3's own order and
//! `aws-config`'s. A CLI that resolved differently would derive a connector ARN for one
//! region and a client for another, and the failure reads as a malformed connector rather
//! than as a region disagreement.
//!
//! (cli.py line numbers resolve at `git show 'c4d396e^:clients/python/src/microvms_agentd/cli.py'` — the retired oracle.)

use std::path::PathBuf;
use std::sync::Arc;

use futures_util_shim::BoxFuture;
use microvms_core::control::ControlPlane;
use microvms_core::prelude::*;
use microvms_core::sandbox::{ControlPlaneMinter, Sandbox};
use microvms_core::session::{Session, SessionBuilder};
use microvms_core::{Error, ErrorKind, Region};

/// A boxed future, spelled locally rather than by depending on `futures-util`.
///
/// One type alias against one more direct dependency in a manifest whose exact contents are
/// the requirement under test. `futures-util` is harmless, but the guard asserts an exact
/// set and every addition to that set has to be worth writing a paragraph about — this one
/// is not.
pub mod futures_util_shim {
    /// A pinned, boxed, `Send` future — the shape a `&dyn` trait method must return.
    pub type BoxFuture<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + Send + 'a>>;
}

/// Which of the three doors a command went through, for the guard's third assertion.
///
/// `cfg(test)`: production has no reason to ask which door it went through, and a field on
/// [`AwsSeam`] recording it would be state the shipped binary maintains for a test's benefit.
#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Door {
    ControlPlane,
    OpenSandbox,
    AttachSession,
}

#[cfg(test)]
impl Door {
    pub fn as_str(self) -> &'static str {
        match self {
            Door::ControlPlane => "control_plane",
            Door::OpenSandbox => "open_sandbox",
            Door::AttachSession => "attach_session",
        }
    }
}

/// A seam that panics if any door is entered, for tests of commands that must not touch AWS.
///
/// One shared fake rather than a copy per test module: the four panicking bodies are the
/// whole implementation, and three private copies of them is three places for a fifth seam
/// method to be stubbed differently. `cfg(test)` for the reason [`Door`] gives — nothing in
/// the shipped binary refuses AWS on purpose.
#[cfg(test)]
pub struct PanickingSeam;

#[cfg(test)]
impl CoreSeam for PanickingSeam {
    fn control_plane(&self, _region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        panic!("this command must not reach the control plane")
    }

    fn open_sandbox(
        &self,
        _region: Region,
        _port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        panic!("this command must not open a sandbox")
    }

    fn attach_session(
        &self,
        _region: Region,
        _attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        panic!("this command must not attach a session")
    }
}

/// What a command needs to attach to a VM it did not launch.
///
/// A struct rather than four parameters because three of the four are opaque identifiers of
/// the same shape, and `attach_session(a, b, c)` is how an endpoint ends up where a token
/// was meant.
#[derive(Clone, Debug)]
pub struct Attach {
    pub endpoint: String,
    pub agent_token: String,
    pub microvm_id: String,
    pub port: Option<u16>,
}

/// The one door to AWS.
///
/// `&dyn` rather than a generic parameter, so a handler signature does not change shape
/// between production and the guard — a monomorphised handler is a second compiled handler,
/// and then the guard is testing a copy.
pub trait CoreSeam: Send + Sync {
    /// A control plane for the read-only calls (`ls --remote`, a state read).
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>>;

    /// A sandbox with nothing launched, for the lifecycle commands.
    fn open_sandbox(
        &self,
        region: Region,
        port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>>;

    /// A session for a VM this invocation did not launch.
    fn attach_session(
        &self,
        region: Region,
        attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>>;
}

/// The production seam.
pub struct AwsSeam;

// The two door calls, each alone in a function that carries its own `#[expect]`. An `expect`
// on the `impl` below would also turn off the transport and environment bans for every line of
// it, so a `std::env::var` or a hand-built `Call` added to a seam method would pass lint.

/// Core's production control plane for `region`.
#[expect(
    clippy::disallowed_methods,
    reason = "the one door to AWS: the CLI's clippy.toml refuses core's constructors everywhere else"
)]
async fn production_plane(region: Region) -> Result<ControlPlane, Error> {
    ControlPlane::new(region).await
}

/// Core's session builder over the production HTTP backend.
#[expect(
    clippy::disallowed_methods,
    reason = "the one door to AWS: the CLI's clippy.toml refuses core's constructors everywhere else"
)]
fn production_session(endpoint: String, agent_token: String) -> SessionBuilder {
    Session::builder(endpoint, agent_token)
}

impl CoreSeam for AwsSeam {
    fn control_plane(&self, region: Region) -> BoxFuture<'_, Result<ControlPlane, Error>> {
        Box::pin(production_plane(region))
    }

    fn open_sandbox(
        &self,
        region: Region,
        port: Option<u16>,
    ) -> BoxFuture<'_, Result<Sandbox, Error>> {
        Box::pin(async move {
            let mut plane = production_plane(region).await?;
            if let Some(port) = port {
                // `?` rather than a check in the CLI: `--port 0` is refused by core against the
                // model's `min: 1`, and a second message here would be a second message to keep
                // right. Still zero control-plane calls — `ControlPlane::new` only resolves
                // credentials.
                plane = plane.with_port(port)?;
            }
            Ok(Sandbox::with_control_plane(plane))
        })
    }

    fn attach_session(
        &self,
        region: Region,
        attach: Attach,
    ) -> BoxFuture<'_, Result<Session, Error>> {
        Box::pin(async move {
            // Core's minter over the same `ControlPlane` the launch path uses, so an attached
            // session mints proxy tokens exactly as a launched one does (TRAP-9), port scopes
            // included.
            let mut plane = production_plane(region).await?;
            if let Some(port) = attach.port {
                plane = plane.with_port(port)?;
            }
            let port = plane.port();
            let minter = Arc::new(ControlPlaneMinter::new(Arc::new(plane), attach.microvm_id));
            production_session(attach.endpoint, attach.agent_token)
                .with_minter(minter)
                .with_port(port)
                .build()
        })
    }
}

/// The region every ARN in a request is derived for.
///
/// Flags win over the environment, and the environment order is `AWS_REGION` then
/// `AWS_DEFAULT_REGION` — boto3's and aws-config's own. See the module docs on why agreeing
/// with the SDK matters more than picking the better order.
/// `unlisted` is the deprecated `--unlisted-region`, read by [`unlisted_region`].
pub fn resolve_region(
    flag: Option<Region>,
    unlisted: Option<&str>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<Region, Error> {
    if let Some(region) = flag.or_else(|| unlisted.map(unlisted_region)) {
        return Ok(region);
    }
    // The environment, parsed in core so the bindings' `preflight` reads it the same way; a
    // malformed name is refused with where it came from.
    Region::from_env(env)
}

/// The region the deprecated `--unlisted-region` names: the name as typed, without
/// `--region`'s name check, as 1.2 took it (#743).
#[allow(deprecated)]
pub fn unlisted_region(name: &str) -> Region {
    Region::unlisted(name)
}

/// The three account-specific values, resolved before any call.
///
/// A missing bucket discovered halfway through a build has already spent the caller's
/// attention. `doctor` reports the same three.
#[derive(Clone, Debug, Default)]
pub struct Infra {
    pub bucket: Option<String>,
    pub build_role_arn: Option<String>,
    pub execution_role_arn: Option<String>,
}

impl Infra {
    /// Flags win over the environment.
    pub fn resolve(
        bucket: Option<String>,
        build_role_arn: Option<String>,
        execution_role_arn: Option<String>,
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Self {
        Self {
            bucket: bucket.or_else(|| env("MICROVM_BUCKET")),
            build_role_arn: build_role_arn.or_else(|| env("MICROVM_BUILD_ROLE_ARN")),
            execution_role_arn: execution_role_arn.or_else(|| env("MICROVM_EXECUTION_ROLE_ARN")),
        }
    }

    /// Rejects a command that cannot possibly succeed, naming **every** gap at once.
    ///
    /// Every gap rather than the first, because these arrive together from one Terraform
    /// apply and reporting them one per attempt costs the caller three round trips to learn
    /// one fact.
    pub fn require(&self, names: &[&str]) -> Result<(), Error> {
        let missing: Vec<&str> = names
            .iter()
            .copied()
            .filter(|name| self.get(name).is_none())
            .collect();
        if missing.is_empty() {
            return Ok(());
        }
        let flags: Vec<String> = missing
            .iter()
            .map(|name| format!("--{} (or ${})", name.replace('_', "-"), env_var_for(name)))
            .collect();
        Err(Error::new(
            ErrorKind::Precondition,
            format!(
                "missing required infrastructure: {}. \
                 `terraform -chdir=conformance/infra output` prints all three, and `microvm \
                 doctor` checks them alongside credentials and the daemon binary.",
                flags.join(", ")
            ),
        ))
    }

    fn get(&self, name: &str) -> Option<&str> {
        let value = match name {
            "bucket" => self.bucket.as_deref(),
            "build_role_arn" => self.build_role_arn.as_deref(),
            "execution_role_arn" => self.execution_role_arn.as_deref(),
            _ => None,
        };
        value.filter(|text| !text.is_empty())
    }
}

/// The environment variable each infrastructure value falls back to.
fn env_var_for(name: &str) -> &'static str {
    match name {
        "bucket" => "MICROVM_BUCKET",
        "build_role_arn" => "MICROVM_BUILD_ROLE_ARN",
        "execution_role_arn" => "MICROVM_EXECUTION_ROLE_ARN",
        _ => "",
    }
}

/// Resolve a caller-selected root or the shared absolute default before any state I/O.
pub fn try_state_dir(
    flag: Option<PathBuf>,
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<PathBuf, microvms_core::error::Error> {
    match flag {
        Some(path) => Ok(path),
        None => microvms_core::names::state_root(env),
    }
}

#[cfg(test)]
/// Where the run ledger lives: `$MICROVM_STATE_DIR`, else `~/.microvm/runs`.
pub fn state_dir(flag: Option<PathBuf>, env: &dyn Fn(&str) -> Option<String>) -> PathBuf {
    if let Some(path) = flag {
        return path;
    }
    // Core's resolution, so the bindings' default registry is the CLI's.
    microvms_core::names::default_state_root(env)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> + use<> {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |name: &str| {
            owned
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
        }
    }

    /// `AWS_REGION` before `AWS_DEFAULT_REGION`, which is boto3's own order.
    ///
    /// Asserted with *both* set, because that is the only case that distinguishes the two
    /// orders — and the consequence of getting it backwards is a connector ARN for one
    /// region and a client for another, reported as a malformed connector.
    #[test]
    fn aws_region_wins_over_aws_default_region() {
        let env = env_of(&[
            ("AWS_REGION", "us-west-2"),
            ("AWS_DEFAULT_REGION", "eu-west-1"),
        ]);
        assert_eq!(
            resolve_region(None, None, &env).expect("resolves"),
            Region::US_WEST_2
        );
    }

    /// A flag wins over both, and the documented default is us-east-1.
    #[test]
    fn a_flag_wins_and_the_default_is_us_east_one() {
        let env = env_of(&[("AWS_REGION", "us-west-2")]);
        assert_eq!(
            resolve_region(Some(Region::EU_WEST_1), None, &env).expect("resolves"),
            Region::EU_WEST_1
        );
        let empty = env_of(&[]);
        assert_eq!(
            resolve_region(None, None, &empty).expect("resolves"),
            Region::US_EAST_1
        );
    }

    /// A well-formed region from the environment resolves, measured or not, and a malformed
    /// one is refused saying where it came from, because "not a region name" alone sends
    /// someone to the flags they did not type.
    #[test]
    fn the_environment_region_resolves_and_a_malformed_one_names_its_source() {
        let env = env_of(&[("AWS_REGION", "us-west-1")]);
        let region = resolve_region(None, None, &env).expect("a well-formed name resolves");
        assert_eq!(region.as_str(), "us-west-1");
        assert!(!region.is_measured());

        let env = env_of(&[("AWS_REGION", "US-WEST-1")]);
        let error = resolve_region(None, None, &env).expect_err("a case change is a typo");
        assert_eq!(error.kind(), ErrorKind::InvalidArg);
        let message = error.to_string();
        assert!(message.contains("not a region name"), "{message}");
        assert!(message.contains("$AWS_REGION"), "{message}");
    }

    /// The deprecated `--unlisted-region` keeps its 1.2 behavior: a named region's name comes
    /// back equal to its constant, any other name is taken as typed, without `--region`'s
    /// name check, and the flag wins over the environment.
    #[test]
    fn the_deprecated_unlisted_flag_keeps_its_one_two_behavior() {
        let env = env_of(&[("AWS_REGION", "eu-west-1")]);
        assert_eq!(
            resolve_region(None, Some("us-east-1"), &env).expect("resolves"),
            Region::US_EAST_1
        );
        let unlisted = resolve_region(None, Some("us-west-1"), &env).expect("resolves");
        assert_eq!(unlisted.as_str(), "us-west-1");
        let unchecked = resolve_region(None, Some("US-WEST-1"), &env).expect("resolves");
        assert_eq!(unchecked.as_str(), "US-WEST-1");
    }

    /// Every missing infrastructure value is named at once, with its flag and its variable.
    ///
    /// One per attempt would cost the caller three round trips to learn one fact — they come
    /// from one `terraform apply`.
    #[test]
    fn every_missing_infrastructure_value_is_named_in_one_message() {
        let infra = Infra::default();
        let error = infra
            .require(&["bucket", "build_role_arn", "execution_role_arn"])
            .expect_err("nothing is set");
        assert_eq!(error.kind(), ErrorKind::Precondition);
        let message = error.to_string();
        for expected in [
            "--bucket",
            "$MICROVM_BUCKET",
            "--build-role-arn",
            "$MICROVM_BUILD_ROLE_ARN",
            "--execution-role-arn",
            "$MICROVM_EXECUTION_ROLE_ARN",
        ] {
            assert!(message.contains(expected), "{expected} missing: {message}");
        }
    }

    /// An empty string counts as unset.
    ///
    /// `MICROVM_BUCKET=` in a sourced env file is the shape this catches, and it would
    /// otherwise pass `require` and fail as a malformed S3 URI three minutes into a build.
    #[test]
    fn an_empty_infrastructure_value_counts_as_missing() {
        let env = env_of(&[("MICROVM_BUCKET", "")]);
        let infra = Infra::resolve(None, None, None, &env);
        assert!(infra.require(&["bucket"]).is_err());
    }

    /// A flag wins over the environment for each of the three.
    #[test]
    fn infrastructure_flags_win_over_the_environment() {
        let env = env_of(&[
            ("MICROVM_BUCKET", "from-env"),
            ("MICROVM_BUILD_ROLE_ARN", "arn:env"),
        ]);
        let infra = Infra::resolve(Some("from-flag".into()), None, None, &env);
        assert_eq!(infra.bucket.as_deref(), Some("from-flag"));
        assert_eq!(infra.build_role_arn.as_deref(), Some("arn:env"));
        assert_eq!(infra.execution_role_arn, None);
        infra
            .require(&["bucket", "build_role_arn"])
            .expect("both present");
    }

    /// The state directory follows the flag, then `$MICROVM_STATE_DIR`, then `~/.microvm/runs`.
    #[test]
    fn the_state_directory_follows_the_flag_then_the_variable_then_the_home_default() {
        let env = env_of(&[("MICROVM_STATE_DIR", "/tmp/ledgers"), ("HOME", "/home/x")]);
        assert_eq!(
            state_dir(Some(PathBuf::from("/flag")), &env),
            PathBuf::from("/flag")
        );
        assert_eq!(state_dir(None, &env), PathBuf::from("/tmp/ledgers"));
        let home_only = env_of(&[("HOME", "/home/x")]);
        assert_eq!(
            state_dir(None, &home_only),
            PathBuf::from("/home/x/.microvm/runs")
        );
    }

    /// Every door has a stable name, which is what the behavioral guard asserts on.
    #[test]
    fn the_three_doors_have_stable_names() {
        use super::Door;
        assert_eq!(Door::ControlPlane.as_str(), "control_plane");
        assert_eq!(Door::OpenSandbox.as_str(), "open_sandbox");
        assert_eq!(Door::AttachSession.as_str(), "attach_session");
    }
}
