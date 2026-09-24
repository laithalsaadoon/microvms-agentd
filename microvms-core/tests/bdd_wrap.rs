// SPDX-License-Identifier: Apache-2.0
//! The Gherkin behavior spec for IMAGE-1 through IMAGE-4, run against the real functions.
//!
//! The scenarios live in `tests/features/wrap_dockerfile.feature`, tagged with the requirement
//! each one verifies; this file is their step definitions and runner. It is a `harness = false`
//! test, so `cargo test` runs it everywhere, and it writes a JUnit report when
//! `CUCUMBER_JUNIT` names a file (give that path absolutely: `cargo test` runs this binary from
//! the package directory).
//!
//! The create preflight is [`ControlPlane::preflight`], the list `create_image` runs before
//! the wire, over a transport that refuses every call: preflight makes none, so a scenario
//! that reached the transport would fail loudly rather than pass against AWS.

use std::sync::Arc;

use cucumber::{World, WriterExt, gherkin::Step, given, then, when, writer};
use microvms_core::control::artifact::{
    BaseImage, WrapOptions, default_dockerfile, wrap_dockerfile,
};
use microvms_core::control::transport::{Call, Reply, Transport};
use microvms_core::control::{ControlPlane, CreateImageRequest, DEFAULT_AGENT_PORT, SystemClock};
use microvms_core::{Error, ErrorKind, Region};

/// A transport for a preflight, which makes no call.
struct NoCalls;

impl Transport for NoCalls {
    fn send(
        &self,
        call: Call,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Reply, Error>> + Send + '_>>
    {
        panic!("a preflight makes no call, but {} was sent", call.operation)
    }
}

#[derive(Debug, Default, World)]
struct Wrap {
    task: String,
    options: WrapOptions,
    wrapped: Option<Result<String, Error>>,
    derived: Option<Result<BaseImage, Error>>,
}

impl Wrap {
    fn result(&self) -> &str {
        match self.wrapped.as_ref().expect("the task was wrapped") {
            Ok(text) => text,
            Err(error) => panic!("the wrap was refused: {error}"),
        }
    }

    /// Everything the wrap added after the task text.
    fn added(&self) -> &str {
        let result = self.result();
        let task = self.task.as_str();
        let rest = result
            .strip_prefix(task)
            .unwrap_or_else(|| panic!("the result starts with the task text:\n{result}"));
        // The task text may have lacked its trailing newline, which the wrap supplies.
        rest.strip_prefix('\n')
            .filter(|_| !task.ends_with('\n'))
            .unwrap_or(rest)
    }
}

fn preflight(dockerfile: &str, base: BaseImage) -> Result<(), Error> {
    let plane = ControlPlane::with_transport(
        Arc::new(NoCalls),
        Region::UsEast1,
        Arc::new(SystemClock::new()),
    );
    let mut request = CreateImageRequest::new(
        "bdd-wrap",
        b"\x7fELF".to_vec(),
        "s3://bucket/key.zip",
        "arn:aws:iam::123456789012:role/build",
    );
    request.base_image = base;
    request.dockerfile = Some(dockerfile.to_string());
    plane.preflight(&request)
}

fn refusal(result: Option<&Result<impl std::fmt::Debug, Error>>) -> &Error {
    match result.expect("the step ran") {
        Ok(value) => panic!("expected a refusal, got {value:?}"),
        Err(error) => error,
    }
}

#[given(expr = "the task Dockerfile {string}")]
fn task_line(world: &mut Wrap, text: String) {
    world.task = format!("{text}\n");
}

#[given(expr = "the task Dockerfile {string} with no trailing newline")]
fn task_line_bare(world: &mut Wrap, text: String) {
    world.task = text;
}

#[given(expr = "the task Dockerfile:")]
fn task_block(world: &mut Wrap, step: &Step) {
    let text = step.docstring.as_ref().expect("a docstring");
    // The docstring starts after the opening delimiter's newline and ends before the closing
    // one; a Dockerfile on disk ends with a newline.
    world.task = format!("{}\n", text.trim_start_matches('\n').trim_end_matches('\n'));
}

#[given(expr = "the wrap option workdir {string}")]
fn option_workdir(world: &mut Wrap, workdir: String) {
    world.options.workdir = Some(workdir);
}

#[given("the wrap option to inherit the workdir")]
fn option_inherit(world: &mut Wrap) {
    world.options.inherit_workdir = true;
}

#[when("the task Dockerfile is wrapped")]
fn wrap(world: &mut Wrap) {
    world.wrapped = Some(wrap_dockerfile(&world.task, &world.options));
}

#[when("a base image is derived from the task Dockerfile")]
fn derive(world: &mut Wrap) {
    world.derived = Some(BaseImage::from_dockerfile(&world.task));
}

#[then("the wrap succeeds")]
fn wrap_succeeds(world: &mut Wrap) {
    world.result();
}

#[then(expr = "the result is the default Dockerfile for base {string} with no workdir")]
fn is_default(world: &mut Wrap, docker_ref: String) {
    let base = BaseImage {
        docker_ref,
        ..BaseImage::al2023()
    };
    assert_eq!(
        world.result(),
        default_dockerfile(DEFAULT_AGENT_PORT, None, &base, None)
    );
}

#[then(expr = "the result is the default Dockerfile for base {string} with workdir {string}")]
fn is_default_with_workdir(world: &mut Wrap, docker_ref: String, workdir: String) {
    let base = BaseImage {
        docker_ref,
        ..BaseImage::al2023()
    };
    assert_eq!(
        world.result(),
        default_dockerfile(DEFAULT_AGENT_PORT, Some(&workdir), &base, None)
    );
}

#[then("the result ends with the lines:")]
fn ends_with(world: &mut Wrap, step: &Step) {
    let expected = step.docstring.as_ref().expect("a docstring");
    let expected = format!(
        "{}\n",
        expected.trim_start_matches('\n').trim_end_matches('\n')
    );
    let result = world.result();
    assert!(result.ends_with(&expected), "{result}");
}

#[then(expr = "{string} is the first line after the task text")]
fn first_added_line(world: &mut Wrap, line: String) {
    let added = world.added();
    assert_eq!(added.lines().next(), Some(line.as_str()), "{added}");
}

#[then(expr = "the line after the task text is {string}")]
fn line_after(world: &mut Wrap, line: String) {
    first_added_line(world, line);
}

#[then("the result adds no USER line")]
fn no_user_line(world: &mut Wrap) {
    let added = world.added();
    assert!(
        !added.lines().any(|line| line.starts_with("USER")),
        "{added}"
    );
}

#[then(expr = "the wrap is refused naming {string}")]
fn wrap_refused(world: &mut Wrap, cause: String) {
    let error = refusal(world.wrapped.as_ref());
    assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
    assert!(error.to_string().contains(&cause), "{error}");
}

#[then(expr = "the derivation is refused naming {string}")]
fn derive_refused(world: &mut Wrap, cause: String) {
    let error = refusal(world.derived.as_ref());
    assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
    assert!(error.to_string().contains(&cause), "{error}");
}

#[then("the create preflight accepts the result under the from-Dockerfile base")]
fn accepted_derived(world: &mut Wrap) {
    let result = world.result().to_string();
    let base = BaseImage::from_dockerfile(&result).expect("a wrapped Dockerfile has a FROM");
    preflight(&result, base).expect("the derived base pairs with its own FROM");
}

#[then("the create preflight accepts the result under the managed base")]
fn accepted_managed(world: &mut Wrap) {
    let result = world.result().to_string();
    preflight(&result, BaseImage::al2023()).expect("the managed base accepts its own ref");
}

#[then(expr = "the create preflight refuses the result under the managed base naming {string}")]
fn refused_managed(world: &mut Wrap, found: String) {
    let result = world.result().to_string();
    let error = preflight(&result, BaseImage::al2023()).expect_err("the FROM disagrees");
    assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
    assert!(error.to_string().contains(&found), "{error}");
}

#[then(expr = "the derived base's docker_ref is {string}")]
fn derived_ref(world: &mut Wrap, docker_ref: String) {
    let base = world
        .derived
        .as_ref()
        .expect("derived")
        .as_ref()
        .expect("a FROM");
    assert_eq!(base.docker_ref, docker_ref);
}

#[then("the derived base's name is the managed base's")]
fn derived_name(world: &mut Wrap) {
    let base = world
        .derived
        .as_ref()
        .expect("derived")
        .as_ref()
        .expect("a FROM");
    assert_eq!(base.name, BaseImage::al2023().name);
    assert_eq!(base.working_dir, "");
}

fn main() {
    let features = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/features/wrap_dockerfile.feature"
    );
    // `cargo test <filter>` passes a libtest filter to every test target. A filter naming
    // something else selects nothing here, as libtest would.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let filters: Vec<&str> = args
        .iter()
        .map(String::as_str)
        .filter(|arg| !arg.starts_with('-'))
        .collect();
    if !filters.is_empty()
        && !filters
            .iter()
            .any(|filter| "bdd_wrap wrap_dockerfile".contains(filter))
    {
        return;
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .expect("a runtime");
    runtime.block_on(async {
        let cucumber = Wrap::cucumber().fail_on_skipped();
        match std::env::var_os("CUCUMBER_JUNIT") {
            Some(path) => {
                let report = std::fs::File::create(&path).expect("the JUnit report file");
                cucumber
                    .with_writer(
                        writer::Basic::raw(std::io::stdout(), writer::Coloring::Never, 0)
                            .summarized()
                            .tee::<Wrap, _>(writer::JUnit::for_tee(report, 0))
                            .normalized(),
                    )
                    .with_cli(cucumber::cli::Opts::<_, _, _, cucumber::cli::Empty>::default())
                    .run_and_exit(features)
                    .await;
            }
            None => {
                cucumber
                    .with_cli(cucumber::cli::Opts::<_, _, _, cucumber::cli::Empty>::default())
                    .run_and_exit(features)
                    .await;
            }
        }
    });
}
