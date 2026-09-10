# Agent VMs: the L3 helpers over the primitives

Status: specified and built 2026-09-10. This document is the specification the code in
`microvms-core/src/agents/` and the `agent-up` / `agent-prompt` commands implement, and
the record of the scope decision it changed.

## The three layers, named

The repository has always had two layers and never named them. This document names three.

**L1, the primitives.** The daemon's wire routes (`exec`, `fs`, `health`, the lifecycle
hooks) and the control-plane operations (`CreateMicrovmImage`, `RunMicrovm`, suspend,
resume, terminate, the auth-token mint). Every one is generic, measured against the real
service, and closed against the platform's traps. `docs/PROTOCOL.md` and
`docs/PLATFORM.md` own this layer.

**L2, the lifecycle.** `Sandbox` and `Session` in `microvms-core`, and the `microvm`
commands over them: `run`, `build --reuse --project`, `exec`, `cp`, `sync`, named VMs,
`attach`, `suspend`, `resume`, `terminate`. Still generic: nothing here knows what a
coding agent is.

**L3, the agent VMs.** One call that gets you a VM with a coding agent installed, model
access wired, running as a non-root user, and a second call that hands the agent a task.
Everything L3 does is a composition of L2 calls that `examples/coding-agents-on-bedrock/`
already performed in a shell script; what L3 adds is that the composition lives in the
library, so the CLI, the bindings, and a harness call one function instead of
re-deriving the seven-step recipe and its four measured traps.

## The rule this changes, and how the cost is bounded

Until this document the rule was: platform code exposes only generic primitives, and
agent-specific detail (CLI installs, model ids, credential wiring, config files) lives
only in `examples/` and docs. `docs/HARNESS-CAPABILITIES.md` lists "harness provider
classes" as an explicit non-goal, and `docs/STRATEGY.md` says "not an orchestrator".
The reason was churn: an agent CLI's flags, config format, and model ids move on a
vendor's cadence, and a platform library that hardcodes them decays.

L3 crosses that line on purpose, for two agents, with the churn contained:

1. **Every agent-specific fact is data in one table**, `agents::profile`, not code
   spread across the crate. Each entry carries the date and version it was verified
   against, the way `docs/PLATFORM.md` dates a platform claim. Updating a profile is a
   one-file edit plus a live run.
2. **Every default is overridable at the call site.** The model id, the agent CLI's
   install line, the headless command template, and the environment file are each
   parameters with a default; a caller whose vendor moved first passes the new value
   and does not wait for a release.
3. **The generic layer stays generic.** L1 and L2 gained one read-only accessor for
   L3 (`Sandbox::port`) and nothing else. `agents` depends on `control`, `session`,
   and `sandbox`; nothing depends on `agents`.
4. **This is still not an orchestrator.** L3 provisions one VM and runs one prompt in
   it. Scheduling, retries across VMs, multi-agent coordination, and turn loops stay
   outside this repository, per `docs/STRATEGY.md`.

The non-goals paragraph in `docs/HARNESS-CAPABILITIES.md` still holds for harness
*provider classes* (a Harbor `BaseEnvironment`, an eve backend): those import the
harness's packages and live in its ecosystem. L3 is the layer such a class would call.

## Requirements

Written in the EARS shapes the rest of `spec/` uses. `AGENT-n` is the id.

- **AGENT-1.** The `agents` module shall expose exactly two agent profiles,
  `ClaudeCode` and `Codex`, as a closed enum, so a caller cannot name an agent the
  library has no recipe for.
- **AGENT-2.** For any non-empty set of profiles, `agents::dockerfile` shall produce a
  Dockerfile that starts with the client's own agentd stanza lines (`FROM` the managed
  base's `docker_ref`, `COPY agentd`, `chmod`), adds the union of the profiles' install
  layers, creates uid and gid 1000 by appending to `/etc/passwd` and `/etc/group`,
  creates and hands `/workspace` to that uid, sets `WORKDIR /workspace`, and ends with
  the stanza's `ENV`, `EXPOSE`, `ENTRYPOINT []`, `CMD ["/agentd"]` lines. The output
  shall pass every local guard `Sandbox::preflight` runs.
- **AGENT-3.** When a profile set is unchanged and the daemon binary is unchanged, the
  derived image name shall be unchanged, so `build --reuse` semantics hold: the name is
  `agent-vm-<profiles>-<12 hex of the artifact content hash>`.
- **AGENT-4.** `agents::bedrock::mint` shall produce a Bedrock bearer token from the
  caller's AWS credential chain by SigV4 query presigning, with the lifetime the caller
  asks for, capped at the service's 12-hour ceiling, and shall never write the token to
  a log, a `Debug` impl, or a command line.
- **AGENT-5.** `install_access` shall deliver model credentials to a running VM as
  files only (`/workspace/.agent-env` mode `0600`, `/workspace/.codex/config.toml` when
  Codex is present), through the authenticated file route, then run one root exec that
  hands `/workspace` to uid 1000. The environment file shall set `PATH` explicitly.
- **AGENT-6.** `install_access` shall write `/workspace/.agent-vm.json` naming the
  installed profiles and their models, so a later process that holds only the VM's
  identifiers can learn which agent to prompt without a local record.
- **AGENT-7.** `prompt` shall run the agent's headless command as uid 1000 and gid
  1000, with `/workspace` as the working directory, sourcing the environment file first,
  and shall refuse locally (zero wire calls) a task string that is empty.
- **AGENT-8.** `agent-up` against a name that is already registered shall not build or
  launch; it shall attach, mint a fresh token, re-run `install_access`, and report
  `reused: true`. This is how a 12-hour token is refreshed on a long-lived VM.
- **AGENT-9.** `agent-up` shall launch with egress requested, since neither agent can
  reach Bedrock without it, and shall register the name only after the launch succeeds
  and the credentials are installed.
- **AGENT-10.** `agent-prompt` without `--agent` shall read the marker from AGENT-6 and
  choose the sole installed profile; when two are installed it shall refuse and name
  both.
- **AGENT-11.** Neither command shall introduce a new exit row; failures map onto the
  existing table (`ERR_PRECONDITION`, `ERR_INVALID_ARG`, `ERR_EXEC_FAILED`,
  `ERR_NAME_TAKEN`, and the wire rows).

## The profile table

`agents/profile.rs`. Each row is a `const` with the fields below; the values are the ones
`examples/coding-agents-on-bedrock/` measured working on 2026-09-02 in us-east-1.

| Field | ClaudeCode | Codex |
| --- | --- | --- |
| `id` | `claude-code` | `codex` |
| `install_lines` | `dnf install nodejs22 nodejs22-npm python3 git tar gzip which findutils procps-ng`; `npm install -g @anthropic-ai/claude-code` | the same `dnf` line; `npm install -g @openai/codex` |
| `default_model` | `global.anthropic.claude-opus-5` | `global.openai.gpt-5.6-sol` |
| `env` | `CLAUDE_CODE_USE_BEDROCK=1`, `ANTHROPIC_MODEL=<model>`, `AWS_BEARER_TOKEN_BEDROCK=<token>` | `OPENAI_API_KEY=<token>` |
| `config_files` | none | `/workspace/.codex/config.toml`: provider `bedrock`, `model_reasoning_effort = medium` (Codex has no metadata for a Bedrock model id and otherwise sends none; a no-effort run declined a task once in five on 2026-09-10), `base_url = https://bedrock-runtime.<region>.amazonaws.com/openai/v1`, `web_search = disabled` (Codex advertises hosted web search by default and bedrock-runtime fails the turn), `env_key = OPENAI_API_KEY`, `wire_api = responses`, `model = <model>` |
| `headless_command(task)` | `claude -p <task> --allowedTools Bash,Read,Edit,Write,Grep,Glob` | `codex exec --skip-git-repo-check -s workspace-write <task>` |
| `verified` | 2026-09-10, us-east-1, `@anthropic-ai/claude-code` latest on that date | 2026-09-10, us-east-1, `@openai/codex` 0.154.0, bedrock-runtime host |

Shared, not per profile: `HOME=/workspace`, `PATH=/usr/local/bin:/usr/bin:/bin`,
`AWS_REGION=<region>`, uid and gid 1000, `WORKDIR /workspace`. The `dnf` line is
emitted once when both profiles are present.

The install lines pin nothing, so an image built today carries today's CLI. That is a
choice, not an oversight: the `--reuse` hash covers the Dockerfile text, and a pinned
version would make every vendor release a Dockerfile edit and a rebuild. A caller who
wants a pin passes `--claude-version` or `--codex-version`, which appends `@<version>`
to the npm install and therefore changes the hash.

## The Bedrock bearer token

`agents/bedrock.rs` ports the `aws-bedrock-token-generator` recipe to Rust on the
`aws-sigv4` crate core already carries, so the CLI drops its `uvx` dependency for this
path and the token never touches a subprocess's argv.

The recipe, verbatim from the reference implementation, is recorded in that file's
module docs with the source permalink and the date it was read. In outline: presign a
`POST` to `https://bedrock.amazonaws.com/` (the host is fixed and not regional; the
region enters only the credential scope) with `Action=CallWithBearerToken` in the query,
service `bedrock`, signed header `host` only, `X-Amz-Expires` at the requested lifetime (default and
ceiling 43200 seconds), then base64-encode the presigned URL with its scheme stripped and
`&Version=1` appended, and prefix `bedrock-api-key-`. The unit test asserts the shape
(prefix, base64 alphabet, the decoded URL's host, action, and expiry) against fixed
credentials and a fixed clock; the live conformance check asserts the token is accepted
by the real service, because a presign that is one canonical byte off still looks like
a token.

The token is a `BearerToken` newtype whose `Debug` prints its length only, following
`.erpaval/solutions/best-practices/credential-structs-never-derive-debug.md`.

## The core API

```rust
pub mod agents {
    pub enum Agent { ClaudeCode, Codex }            // AGENT-1
    pub struct AgentSpec { agent: Agent, model: Option<String>, cli_version: Option<String> }
    pub struct Profile { /* the table row */ }
    impl Agent { pub fn profile(self) -> &'static Profile; pub fn as_str(self) -> &'static str; }

    pub const AGENT_UID: u32 = 1000;  pub const AGENT_GID: u32 = 1000;
    pub const WORKDIR: &str = "/workspace";
    pub fn dockerfile(specs: &[AgentSpec], base: &BaseImage, port: u16) -> String;   // AGENT-2
    pub fn image_stem(specs: &[AgentSpec]) -> String;                               // AGENT-3

    pub mod bedrock {
        pub struct BearerToken(..);   // Debug = length only
        pub struct Minted { token: BearerToken, expires_at: SystemTime }
        pub async fn mint(region: &Region, lifetime: Duration) -> Result<Minted, Error>;  // AGENT-4
        pub fn mint_with(credentials, region, lifetime, now) -> Result<Minted, Error>;    // pure, tested
    }

    pub struct BedrockAccess { region: Region, token: BearerToken }
    pub struct GuestFile { path: String, contents: Vec<u8>, mode: &'static str }
    pub fn provisioning_files(specs, access) -> Vec<GuestFile>;                    // AGENT-5, AGENT-6
    pub async fn install_access(session: &Session, specs, access) -> Result<(), Error>;
    pub async fn installed_agents(session: &Session) -> Result<Vec<AgentSpec>, Error>;  // AGENT-10
    pub fn prompt_request(spec: &AgentSpec, task: &str, opts: &PromptOptions) -> Result<StartRequest, Error>;  // AGENT-7
    pub async fn prompt(session: &Session, spec, task, opts) -> Result<ExecHandle, Error>;

    pub struct AgentVm { sandbox: Sandbox, specs: Vec<AgentSpec> }
    impl AgentVm {
        pub fn new(sandbox: Sandbox, specs: Vec<AgentSpec>) -> Result<Self, Error>;  // refuses an empty set
        pub fn claude_code(sandbox: Sandbox) -> Self;   // the names the request used
        pub fn codex(sandbox: Sandbox) -> Self;
        pub fn image_request(&self, binary: Vec<u8>, artifact_uri, build_role_arn, size) -> CreateImageRequest;
        pub fn launch_request(&self, image_identifier, execution_role_arn) -> RunRequest;  // egress on
        pub async fn build(&mut self, request) -> Result<&Image, Error>;
        pub async fn launch(&mut self, request) -> Result<&Session, Error>;
        pub async fn install_access(&self, access: &BedrockAccess) -> Result<(), Error>;
        pub async fn prompt(&self, agent: Agent, task: &str, opts) -> Result<ExecHandle, Error>;
        pub async fn terminate(&mut self, opts: TeardownOpts) -> TeardownReport;
        pub fn sandbox(&self) -> &Sandbox;  pub fn session(&self) -> Option<&Session>;
    }
}
```

The artifact upload stays the caller's, exactly as it is for `Sandbox::build_image`:
S3 is not in core's dependency set, and `AgentVm::image_request` returns the request
whose `code_artifact_uri` the caller fills before calling `build`. The free functions
exist beside the struct because the CLI's refresh and prompt paths hold an attached
`Session` and no `Sandbox`; `AgentVm`'s methods delegate to them.

## The CLI surface

Two flat commands, in the tree beside `run` and `exec`. Flat rather than a nested
`agent` group because the manifest generator, its cross-check test, and the docs site's
Reference generator all read one level of subcommands, and a nested group would be a
second command grammar for two commands. `port-forward` set the hyphenated precedent.

**`microvm agent-up [BINARY] --vm-name NAME [--agent claude-code|codex]... [--claude-model ID]
[--codex-model ID] [--claude-version V] [--codex-version V] [--project DIR] [--memory MIB]
[--token-ttl-hours H] [--max-idle-sec S] [--suspended-sec S] [--auto-resume]
[--max-duration-sec S] [--port P] [--state-dir DIR] <InfraFlags> <RegionFlags>`**

Builds the profile image under its content-hash name (reusing an existing one), launches
a kept, named VM with egress, mints a Bedrock token, installs it, optionally uploads
`--project`'s tree to `/workspace` (packed by the same `sync` module `run <DIR>` uses),
and registers the name. Against a registered name it takes the AGENT-8 refresh path.
`--agent` defaults to `claude-code`; both may be given. `--memory` defaults to 1024,
the example's measured choice for peaky agent sessions. Envelope type
`microvm.agent`; keys `vmName`, `microvmId`, `endpoint`, `agentToken`,
`imageIdentifier`, `imageName`, `imageReused`, `vmReused`, `agents`
(`[{agent, model, cliVersion, headlessCommand}]`), `credentialExpiresAt` (epoch seconds),
`workdir`, `project` (`{workdir, uploadedBytes, uploadedMembers}` or null), `agentd`.

**`microvm agent-prompt TASK [--agent A] [--timeout SEC] [--detach] [--exec-id ID] <AttachFlags> <RegionFlags>`**

Runs the headless agent over the task as uid 1000 in `/workspace`. Without `--agent`,
reads the guest marker (AGENT-10). `--detach` starts and returns the exec id for
`exec --poll`; the default waits up to `--timeout` (900 s, client-side; the daemon-side
exec carries no budget, as `exec` does) and acks. On the refresh path a `--project` tree is
uploaded before the credential install, so the same `chown` hands it to the agent. Envelope type
`microvm.agent.prompt`; keys `execId`, `agent`, `model`, `phase`, `exitCode`, `stdout`,
`stderr`, `truncated`. A non-zero agent exit earns `ERR_EXEC_FAILED` on a success
envelope, as `exec` does.

Teardown is `microvm terminate NAME`, which already exists. Streaming is
`microvm exec --stream --name NAME --user 1000 --group 1000 '<headlessCommand>'`, and the
`agents[].headlessCommand` key in the `agent-up` envelope is there so a caller can do
that without knowing the template.

## Verification

`mise run check` covers: the Dockerfile derivation against every preflight guard, the
image-name stability property (same specs and binary give the same name; a version pin
changes it), the token shape under fixed credentials and clock, the provisioning file
set per profile combination, `prompt_request` field-by-field, the marker round trip
through the session recorder, the CLI guards (both commands fail closed through the
seam and name their door; a registered name skips the build door; an empty task is
refused with zero doors), and the manifest count.

The live half, per `CLAUDE.md`'s rule, is `drive_agent_vm` in `conformance/run_rs.py`:
`agent-up` with both profiles builds or reuses the image and launches; the marker names
both; `agent-prompt --agent claude-code` completes a Bash task with exit 0 and a
tool-call in its transcript; `agent-prompt --agent codex` creates a file that a following
`exec` can `cat`; a second `agent-up` against the same name reports `vmReused: true`
and a later `credentialExpiresAt`; `terminate NAME` releases the name. That section
needs Bedrock access to both default models in the conformance account and is the only
part of the suite that does; it reports the model ids it used.

## The bindings

`microvms-py` and `microvms-js` carry the layer as `AgentVm`, `AgentSpec`, and
`BearerToken`, plus four module functions for a caller who holds only a session:
`installed_agents`, `install_agent_access`, `prompt_agent`, and `mint_bedrock_token`
(`installedAgents`, `installAgentAccess`, `promptAgent`, `mintBedrockToken`). The
sequence is the CLI's, one method per step, and the upload stays the caller's because
S3 is not in the core's dependency set:

```python
vm = microvms.AgentVm(microvms.Region.us_east_1(),
                      [microvms.AgentSpec.claude_code(), microvms.AgentSpec.codex()])
image = vm.find_image(binary=agentd, build_role_arn=build_role)
if image is None:
    name = vm.image_name(binary=agentd, build_role_arn=build_role)
    s3.put_object(Bucket=bucket, Key=f"{name}.zip",
                  Body=vm.build_artifact(binary=agentd, build_role_arn=build_role))
    image = vm.build_image(binary=agentd, code_artifact_uri=f"s3://{bucket}/{name}.zip",
                           build_role_arn=build_role).identifier
vm.launch(image_identifier=image, execution_role_arn=exec_role)
token = vm.install_access()                      # minted in process; token.expires_at
result = vm.prompt_sync("codex", "Create hello.py that prints hello, run it.")
vm.terminate(delete_image=False)
```

The binding holds the same lock the sandbox and every session it hands out hold, so a
`terminate` and a session call cannot interleave; `vm.sandbox` and `vm.session` reach
the same VM for suspend, resume, and file transfer. `terminate(delete_image=True)` deletes
only an image the object itself built, the sandbox's existing rule; a reused image is
reported `image_deleted: false` with no failure, and the caller deletes it. The core's `AgentVm` owns its
sandbox, which one binding class cannot share, so the bindings drive the layer through
the core's free functions (`image_request_for`, `launch_request_for`, `install_access`,
`prompt`, `spec_for`) with the specs kept beside the lock. No refusal lives in a
binding: an unknown agent name, an empty or repeated spec set, a prompt for an agent
the VM does not carry, a blank task, and a token lifetime past the ceiling are all the
core's messages. A `BearerToken` has no constructor and shows only its length; `expose()`
is the one door to the text, for a caller writing it into an environment themselves.

The Python stub is regenerated from the compiled module (`mise run stubs`) and the Node
`index.d.ts` from the napi surface, so both type surfaces follow the Rust.

## Phase 2, deliberately out of this change

- `--stream` on `agent-prompt`. The `exec --stream` path exists and the envelope
  publishes the command to stream.
- `microvm.toml` keys for the agent flags. `merge_config` is `run`-shaped and the
  agent flags would need their own precedence table.
- Other model access paths (a vendor API key, an AgentCore gateway). `BedrockAccess`
  is a struct rather than an enum until a second variant exists to name.
