# microvms-core

Run agents, shell commands, builds, and tests inside AWS Lambda MicroVMs from Rust.
Your application controls a remote sandbox through `Sandbox` and `Session`: launch
a VM, copy a workspace, execute tools, collect results, and terminate it. The
`agents` module also provisions Claude Code or Codex with Amazon Bedrock access.

## Run your first command

You need Rust, AWS credentials with MicroVM access, a VM execution role, and an
image containing `agentd`. Complete the [one-time AWS setup](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/)
if this is your first use. AWS usage is billed; a first image build takes several
minutes. Once an image exists, the example below is the full launch-to-cleanup path.

Use the CLI to prepare a reusable image, then copy `data.imageIdentifier` from
its JSON output:

```sh
microvm build --name agent-tools --region us-east-1 --json
export MICROVM_IMAGE='arn:aws:lambda:us-east-1:123456789012:microvm-image:REPLACE_ME'
export MICROVM_EXECUTION_ROLE_ARN='arn:aws:iam::123456789012:role/REPLACE_ME'
cargo new sandbox-demo
cd sandbox-demo
```

The image and this example use `us-east-1`. Set `MICROVM_IMAGE` to the actual ARN;
the SDK does not perform the CLI's image-name lookup. Set
`MICROVM_EXECUTION_ROLE_ARN` to the actual execution role ARN from setup.
AWS credentials come from
the standard credential chain, including `AWS_PROFILE` and environment variables.

Replace the empty `[dependencies]` section in `Cargo.toml` with:

```toml
[dependencies]
microvms-core = "0.11"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

Replace `src/main.rs` with:

```rust,no_run
use std::time::Duration;
use microvms_core::prelude::*;
use microvms_core::{Region, protocol::exec::{Shell, StartRequest}};
use microvms_core::sandbox::{RunRequest, Sandbox, TeardownOpts};
use microvms_core::session::{DEFAULT_READY_TIMEOUT, mint_exec_id};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let image = std::env::var("MICROVM_IMAGE")?;
    let role = std::env::var("MICROVM_EXECUTION_ROLE_ARN")?;
    let mut sandbox = Sandbox::new(Region::UsEast1).await?;
    let work = async {
        let mut request = RunRequest::new().with_image(&image);
        request.execution_role_arn = Some(role);
        let session = sandbox.run(request).await?;
        session.wait_until_ready(DEFAULT_READY_TIMEOUT).await?;
        session.run_sync(StartRequest {
            exec_id: mint_exec_id(),
            command: vec!["printf 'hello from a sandbox\\n'".into()],
            shell: Shell::Flag(true),
            cwd: None,
            env: Default::default(),
            user: None,
            group: None,
            timeout_sec: Some(30.0),
            stdin: false,
            reap_group_on_exit: true,
            inherit_image_env: false,
        }, Duration::from_secs(35)).await
    }.await;

    // Runs even when launch, readiness, or execution returns an error.
    let cleanup = sandbox.terminate(TeardownOpts::default()).await;
    for failure in &cleanup.failures {
        eprintln!("cleanup: {failure}");
    }
    for resource in &cleanup.undeleted {
        eprintln!("not deleted: {resource}");
    }
    let result = work?;
    print!("{}", result.stdout());
    eprint!("{}", result.stderr());
    println!("exit: {:?}", result.exit_code());
    if !result.succeeded() {
        return Err("guest command failed".into());
    }
    if !cleanup.failures.is_empty() || !cleanup.undeleted.is_empty() {
        return Err("VM cleanup failed; see errors above".into());
    }
    Ok(())
}
```

```sh
cargo run
```

Expected output includes `hello from a sandbox` and `exit: Some(0)`.
`use microvms_core::prelude::*;` brings the constructors that wire in AWS, such as
`Sandbox::new`, into scope.
`cwd: None` uses the image's working directory. Set a different directory only
after creating it or uploading files there; a generic image need not contain
`/workspace`.
Cleanup requests VM termination and retains the image for reuse. Dropping a
`Sandbox` does **not** terminate it; always call `terminate` and inspect its report.
Use `TeardownOpts::default().waiting_for_terminated()` when you must wait for
AWS to report termination complete.

## Give an agent its own sandbox

Use a separate VM for each task or workspace, upload only the files it needs,
run tools through the session, and download the results before cleanup.
`Session::upload_file`, `upload_tar`, `download_file`, and `download_tar` handle
transfers; `Session::run` returns an `ExecHandle` for streaming and cancellation.

For built-in coding agents, start with
[running Claude Code or Codex](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/run-coding-agents-on-bedrock/).
Rust's `agents::AgentVm` composes image preparation, launch, Bedrock credential
installation, and prompts. Agent prompts run as uid/gid 1000; the generic command
example above runs as the daemon's user, normally root.

The VM separates workload execution from your host. Processes within one guest
share that VM; uid demotion is not a separate security boundary. Guest workloads
can access the VM execution role through metadata, so scope that role accordingly.
Agent helpers request internet access. Omitting egress does not block the default
network, and proxy-deny variables are advisory. Enforced internet isolation needs
a VPC connector and verified VPC routing without internet access. See
[trust boundaries](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/TRUST.md)
and [networking](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/NETWORKING.md).

## Next steps

- [API documentation](https://docs.rs/microvms-core): lifecycle, sessions, agents, sizing, and costs.
- [Use the SDKs](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/from-code/): Rust, Python, and Node examples.
- [Custom guest images](https://laithalsaadoon.github.io/microvms-agentd/learn/operations/write-a-guest-dockerfile/): install your own agent and tools.
- [Embedding guide](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/EMBEDDING.md): longer-running applications and harnesses.

Apache-2.0
