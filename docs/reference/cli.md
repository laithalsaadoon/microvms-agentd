# microvms-agentd · CLI

The `microvm` CLI provides the following subcommands (`crates/microvms-cli/Cargo.toml:21-23`).

## run

```
microvm run [OPTIONS] [BINARY_OR_DIR]
```

Build an image, launch a VM, run a command, report the cost, tear it down.

`crates/microvms-cli/src/commands/lifecycle.rs:475`

Flags:

- `--image` — Launch this existing image instead of building one. Takes an ARN or a name. `crates/microvms-cli/src/cli.rs:839`.
- `--image-version` — Launch this exact image version instead of the image's latest active one. `crates/microvms-cli/src/cli.rs:857`.
- `--artifact-uri` — Where the build artifact already is, as an s3:// URI. `crates/microvms-cli/src/cli.rs:865`.
- `--exec` — A shell command to run in the VM. `crates/microvms-cli/src/cli.rs:871`.
- `--name` — Image name. Defaults to a per-invocation name, because reusing one is how a clientToken replay wedges an image. `crates/microvms-cli/src/cli.rs:876`.
- `--memory` — Baseline MiB, which selects a documented size class. `crates/microvms-cli/src/cli.rs:884`.
- `--cpus` — vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:506`.
- `--memory-mib` — MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:511`.
- `--dockerfile` — A Dockerfile to use instead of the library's default. Its FROM must match the base. `crates/microvms-cli/src/cli.rs:891`.
- `--log-group` — CloudWatch log group for the build's logs, instead of the service-created `/aws/lambda-microvms/<image-name>`. `crates/microvms-cli/src/cli.rs:900`.
- `--log-stream` — Log stream name prefix inside --log-group. The client appends `/<16 hex>` per build attempt, and the resolved exact name is on the envelope as `logStream`. `crates/microvms-cli/src/cli.rs:912`.
- `--repair-identity` — Widen the guest so `sethostname` and the boot_id bind mount work. `crates/microvms-cli/src/cli.rs:918`.
- `--egress` — Request the managed INTERNET_EGRESS connector. `crates/microvms-cli/src/cli.rs:925`.
- `--egress-network-connector` — Existing VPC network connector ARN. Repeat for multiple connectors. `crates/microvms-cli/src/cli.rs:934`.
- `--deny-egress` — Set proxy environment variables that discourage outbound HTTP requests. `crates/microvms-cli/src/cli.rs:942`.
- `--shell` — Launch shell-capable, so `microvm shell` can attach later. `crates/microvms-cli/src/cli.rs:951`.
- `--launch-env` — Set one launch-environment variable for every exec in the VM, as KEY=VALUE. Repeatable. `crates/microvms-cli/src/cli.rs:972`.
- `--user` — User to run --exec's command as: a name or a numeric uid. Omitted runs as the daemon's own user. `crates/microvms-cli/src/cli.rs:983`.
- `--group` — Group to run --exec's command as: a name or a numeric gid. Omitted keeps the daemon's own group, or a named user's primary group. `crates/microvms-cli/src/cli.rs:990`.
- `--keep` — Leave the VM and image running. You are then paying for them. `crates/microvms-cli/src/cli.rs:994`.
- `--no-wait` — Return once the launch is accepted, with the VM still PENDING, instead of waiting for RUNNING and for its daemon. `microvm wait` finishes the launch later. `crates/microvms-cli/src/cli.rs:1004`.
- `--identity` — Generate a per-VM identity, so `tunnel --verify-identity` can prove the far end. `crates/microvms-cli/src/cli.rs:1020`.
- `--vm-name` — Register a local name for the kept VM, so later commands can say `--name <NAME>` instead of pasting the endpoint/agent-token/microvm-id triple. `crates/microvms-cli/src/cli.rs:1038`.
- `--client-token` — Idempotency key for this launch: 1-128 printable ASCII characters. `crates/microvms-cli/src/cli.rs:750`.
- `--vm-log-group` — CloudWatch log group for the VM's own logs (not the image build's). `crates/microvms-cli/src/cli.rs:757`.
- `--vm-log-stream` — Exact log stream inside --vm-log-group. `crates/microvms-cli/src/cli.rs:761`.
- `--no-vm-logs` — Turn the VM's own logging off. `crates/microvms-cli/src/cli.rs:765`.
- `--timeout` — How long to wait for the exec, in seconds. `crates/microvms-cli/src/cli.rs:1045`.
- `--max-idle-sec` — Suspend the VM after this much inbound-traffic idleness. `crates/microvms-cli/src/cli.rs:1049`.
- `--suspended-sec` — Terminate the VM after this long suspended. A resume past it cannot work. `crates/microvms-cli/src/cli.rs:1053`.
- `--auto-resume` — Let the platform resume a suspended VM on an incoming request. `crates/microvms-cli/src/cli.rs:1057`.
- `--max-duration-sec` — Hard ceiling on the VM's life. Refused above 28800 (eight hours) before any call. `crates/microvms-cli/src/cli.rs:1061`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:1065`.
- `--state-dir` — Where the run ledger is written. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:1069`.
- `--config` — Read this project config file instead of ./microvm.toml. `crates/microvms-cli/src/cli.rs:675`.
- `--no-config` — Ignore any microvm.toml, even a malformed one. Flags and defaults apply. `crates/microvms-cli/src/cli.rs:679`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--bucket` — S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:722`.
- `--build-role-arn` — Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:726`.
- `--execution-role-arn` — Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:730`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## quickstart

```
microvm quickstart [OPTIONS]
```

Zero to a live exec: provision the daemon, build, launch, run, report, tear down.

`crates/microvms-cli/src/commands/lifecycle.rs:990`

Flags:

- `--exec` — The command to run inside the first VM. `crates/microvms-cli/src/cli.rs:1097`.
- `--state-dir` — Where the run ledger and the provisioned-daemon cache live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:1106`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--bucket` — S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:722`.
- `--build-role-arn` — Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:726`.
- `--execution-role-arn` — Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:730`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## build

```
microvm build [OPTIONS] [BINARY]
```

Build a MicroVM image and wait for it to be usable.

`crates/microvms-cli/src/commands/lifecycle.rs:1464`

Flags:

- `--state-dir` — Where the provisioned-daemon cache lives. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs — the same directory `run`'s ledger uses, so the two commands share one cache. `crates/microvms-cli/src/cli.rs:1139`.
- `--artifact-uri` — Where the build artifact already is, as an s3:// URI. See `run --artifact-uri`. `crates/microvms-cli/src/cli.rs:1143`.
- `--name` — Image name. Defaults to a per-invocation name. `crates/microvms-cli/src/cli.rs:1147`.
- `--memory` — Baseline MiB, selecting a documented size class. `crates/microvms-cli/src/cli.rs:1151`.
- `--cpus` — vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:506`.
- `--memory-mib` — MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:511`.
- `--dockerfile` — A Dockerfile to use instead of the library's default. `crates/microvms-cli/src/cli.rs:1158`.
- `--project` — A project directory whose dependency files bake an environment layer into the image (#74). `crates/microvms-cli/src/cli.rs:1172`.
- `--base-image-version` — Pin the managed base image to one version instead of taking the service's default. `crates/microvms-cli/src/cli.rs:1190`.
- `--log-group` — CloudWatch log group for the build's logs, instead of the service-created `/aws/lambda-microvms/<image-name>`. See `run --log-group`. `crates/microvms-cli/src/cli.rs:1195`.
- `--log-stream` — Log stream name prefix inside --log-group. The client appends `/<16 hex>` per build attempt and the envelope reports the resolved exact name as `logStream`. See `run --log-stream`. Requires --log-group — `build` reads no config file, so the flag is the only place a group can come from. `crates/microvms-cli/src/cli.rs:1202`.
- `--repair-identity` — Widen the guest so `sethostname` and the boot_id bind mount work. `crates/microvms-cli/src/cli.rs:1206`.
- `--tag` — A tag on the image, repeatable. Split at the first `=`, so a VALUE may hold one, and checked against the model's `TagKey` and `TagValue` at parse time; an empty VALUE is legal. A KEY given twice is refused rather than one of its values dropped. `crates/microvms-cli/src/cli.rs:1212`.
- `--base-image` — The managed base image `baseImageArn` names, instead of `al2023-1`. `crates/microvms-cli/src/cli.rs:1221`.
- `--inherit-workdir` — Rely on the image's `WORKDIR`: an exec with no cwd runs there. `crates/microvms-cli/src/cli.rs:1229`.
- `--run-hook-timeout-sec` — The timeout of the `run`, `resume`, `suspend` and `terminate` hooks, in whole seconds. `crates/microvms-cli/src/cli.rs:1237`.
- `--build-hook-timeout-sec` — The timeout of the `ready` and `validate` build hooks, in whole seconds. `crates/microvms-cli/src/cli.rs:1244`.
- `--reuse` — Reuse an existing image whose build inputs match, instead of building. `crates/microvms-cli/src/cli.rs:1265`.
- `--s3-key-prefix` — Under --reuse, a key prefix inside the bucket for the artifact. `crates/microvms-cli/src/cli.rs:1269`.
- `--force` — Under --reuse, delete a ready image under the derived name and build it afresh. `crates/microvms-cli/src/cli.rs:1273`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:1277`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--bucket` — S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:722`.
- `--build-role-arn` — Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:726`.
- `--execution-role-arn` — Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:730`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## agent-up

```
microvm agent-up [OPTIONS] --vm-name <NAME> [BINARY]
```

Bring up a VM with a coding agent in it: image, launch, model credentials, non-root user.

`crates/microvms-cli/src/commands/agent.rs:235`

Flags:

- `--vm-name` — The local name to register for the VM, and the handle every later command uses. `crates/microvms-cli/src/cli.rs:2423`.
- `--agent` — Which agent to install. Repeatable; defaults to `claude-code`. `crates/microvms-cli/src/cli.rs:2427`.
- `--claude-model` — The Bedrock inference-profile id Claude Code uses. Defaults to the profile's. `crates/microvms-cli/src/cli.rs:2431`.
- `--codex-model` — The Bedrock model id Codex uses. Defaults to the profile's. `crates/microvms-cli/src/cli.rs:2435`.
- `--claude-version` — Pin the Claude Code npm package to this version. Changes the image's reuse hash. `crates/microvms-cli/src/cli.rs:2439`.
- `--codex-version` — Pin the Codex npm package to this version. Changes the image's reuse hash. `crates/microvms-cli/src/cli.rs:2443`.
- `--project` — A local directory to upload into /workspace after launch, packed the way `run <DIR>` packs. `crates/microvms-cli/src/cli.rs:2451`.
- `--memory` — Baseline MiB. Default 1024: a 4 GiB always-present ceiling at half the floor cost of 2048, which fits peaky agent sessions (see `docs/AGENT-VMS.md`). `crates/microvms-cli/src/cli.rs:2456`.
- `--cpus` — vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:506`.
- `--memory-mib` — MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:511`.
- `--token-ttl-hours` — How long the Bedrock bearer token lives, in hours. Default and ceiling 12. `crates/microvms-cli/src/cli.rs:2463`.
- `--max-idle-sec` — Suspend the VM after this much inbound-traffic idleness. `crates/microvms-cli/src/cli.rs:2467`.
- `--suspended-sec` — Terminate the VM after this long suspended. A resume past it cannot work. `crates/microvms-cli/src/cli.rs:2471`.
- `--auto-resume` — Let the platform resume a suspended VM on an incoming request. `crates/microvms-cli/src/cli.rs:2475`.
- `--max-duration-sec` — Hard ceiling on the VM's life. Refused above 28800 (eight hours) before any call. `crates/microvms-cli/src/cli.rs:2479`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:2483`.
- `--state-dir` — Where the run ledger and name registry live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2487`.
- `--client-token` — Idempotency key for this launch: 1-128 printable ASCII characters. `crates/microvms-cli/src/cli.rs:750`.
- `--vm-log-group` — CloudWatch log group for the VM's own logs (not the image build's). `crates/microvms-cli/src/cli.rs:757`.
- `--vm-log-stream` — Exact log stream inside --vm-log-group. `crates/microvms-cli/src/cli.rs:761`.
- `--no-vm-logs` — Turn the VM's own logging off. `crates/microvms-cli/src/cli.rs:765`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--bucket` — S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:722`.
- `--build-role-arn` — Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:726`.
- `--execution-role-arn` — Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:730`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## agent-prompt

```
microvm agent-prompt [OPTIONS] <TASK>
```

Hand a coding agent in a running agent VM one task, headless, as the non-root user.

`crates/microvms-cli/src/commands/agent.rs:764`

Flags:

- `--agent` — Which installed agent to prompt. Omitted reads the guest marker and takes the one it names. `crates/microvms-cli/src/cli.rs:2524`.
- `--permission-mode` — Guest agent permission policy. Unrestricted bypasses agent approvals, still as uid 1000. `crates/microvms-cli/src/cli.rs:2528`.
- `--execution-timeout` — Remote execution deadline in seconds; persists after detach. Must be positive and finite. `crates/microvms-cli/src/cli.rs:2532`.
- `--reap-group-on-exit` — Terminate residual process-group children when the main agent exits. `crates/microvms-cli/src/cli.rs:2536`.
- `--timeout` — Caller wait limit in seconds. Does not stop the agent when the wait expires. `crates/microvms-cli/src/cli.rs:2540`.
- `--detach` — Start the agent and return immediately, without waiting and without acking. `crates/microvms-cli/src/cli.rs:2547`.
- `--exec-id` — Use this exec id instead of a fresh one, making a retry idempotent. `crates/microvms-cli/src/cli.rs:2551`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## exec

```
microvm exec [OPTIONS] [COMMAND]
```

Run one command in a MicroVM that is already running.

`crates/microvms-cli/src/commands/attached.rs:243`

Flags:

- `--timeout` — How long to wait for the command, in seconds. `crates/microvms-cli/src/cli.rs:1297`.
- `--timeout-sec` — The daemon's deadline for the command, in seconds: `timeout_sec` on the start request. `crates/microvms-cli/src/cli.rs:1305`.
- `--complete` — Wait for exactly one result, the way the SDKs' `run_to_completion` does (BIND-8 to BIND-10). `crates/microvms-cli/src/cli.rs:1316`.
- `--client-grace` — With --complete, the seconds past --timeout-sec before the kill, and then for its result. `crates/microvms-cli/src/cli.rs:1324`.
- `--cwd` — Working directory. `crates/microvms-cli/src/cli.rs:1331`.
- `--env` — Set one environment variable for the command, as KEY=VALUE. Repeatable. `crates/microvms-cli/src/cli.rs:1348`.
- `--user` — User to run the command as: a name or a numeric uid. Omitted runs as the daemon's own user. `crates/microvms-cli/src/cli.rs:1360`.
- `--group` — Group to run the command as: a name (resolved against the guest's `/etc/group`) or a numeric gid. Omitted keeps the daemon's own group, or a named user's primary group. `crates/microvms-cli/src/cli.rs:1365`.
- `--shell` — Run the command under this shell instead of `/bin/sh`, for example `bash` (AGENTD-14). `crates/microvms-cli/src/cli.rs:1375`.
- `--inherit-image-env` — Start the command's environment from the image's `ENV` rather than from nothing (AGENTD-11). `crates/microvms-cli/src/cli.rs:1386`.
- `--exec-id` — Use this exec id instead of a fresh one, making a retry idempotent. `crates/microvms-cli/src/cli.rs:1406`.
- `--poll` — Read an existing exec's status and output instead of starting anything. `crates/microvms-cli/src/cli.rs:1415`.
- `--detach` — Start the command and return immediately, without waiting and **without acking**. `crates/microvms-cli/src/cli.rs:1435`.
- `--stream` — Stream output as it arrives rather than waiting for the whole thing. `crates/microvms-cli/src/cli.rs:1445`.
- `--from-offset` — Resume a stream at this byte offset. Only with --stream. `crates/microvms-cli/src/cli.rs:1453`.
- `--stdin` — Give the command a stdin pipe and feed it this process's stdin, then close it. `crates/microvms-cli/src/cli.rs:1461`.
- `--reap` — Signal the command's whole process group once its own child exits. `crates/microvms-cli/src/cli.rs:1473`.
- `--kill-on-timeout` — On `ERR_TIMEOUT`, stop the exec rather than leaving it running. `crates/microvms-cli/src/cli.rs:1484`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## wait

```
microvm wait [OPTIONS]
```

Wait until a kept MicroVM answers: RUNNING, and then its daemon.

`crates/microvms-cli/src/commands/lifecycle.rs:2207`

Flags:

- `--timeout` — How long to wait for RUNNING, in seconds. The daemon wait after it is core's own bootstrap bound. `crates/microvms-cli/src/cli.rs:1567`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## health

```
microvm health [OPTIONS]
```

Ask a running MicroVM's daemon whether it is up, and what its identity repair did.

`crates/microvms-cli/src/commands/attached.rs:915`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## keepalive

```
microvm keepalive [OPTIONS]
```

Keep a running MicroVM awake while work runs inside it, by polling its health.

`crates/microvms-cli/src/commands/attached.rs:806`

Flags:

- `--interval` — Seconds between health polls. Default: a third of the idle window, at most 20. `crates/microvms-cli/src/cli.rs:1497`.
- `--while-busy` — Stop once the daemon reports no running exec. `crates/microvms-cli/src/cli.rs:1501`.
- `--for` — Stop after this many seconds, busy or not. `crates/microvms-cli/src/cli.rs:1505`.
- `--idle-window` — The VM's `maxIdleDurationSeconds`. Read from the control plane when omitted. `crates/microvms-cli/src/cli.rs:1509`.
- `--tolerated-errors` — Retryable poll failures in a row to retry, a second apart, before giving up. `crates/microvms-cli/src/cli.rs:1513`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## ack

```
microvm ack [OPTIONS] <EXEC_ID>
```

Release a finished exec's buffered output, which starts its collection clock.

`crates/microvms-cli/src/commands/attached.rs:1123`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## kill

```
microvm kill [OPTIONS] <EXEC_ID>
```

Stop a running exec: SIGTERM its whole process group, SIGKILL after the daemon's grace.

`crates/microvms-cli/src/commands/attached.rs:1164`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## ps

```
microvm ps [OPTIONS]
```

List what is running in a MicroVM: every exec's process group and its live pids.

`crates/microvms-cli/src/commands/attached.rs:1216`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## stdin

```
microvm stdin [OPTIONS] <EXEC_ID>
```

Write to a running exec's stdin, and optionally close it.

`crates/microvms-cli/src/commands/attached.rs:1307`

Flags:

- `--data` — What to write. `-` reads this process's stdin; omitted writes nothing. `crates/microvms-cli/src/cli.rs:1808`.
- `--eof` — Close stdin after any --data is written. `crates/microvms-cli/src/cli.rs:1816`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## cp

```
microvm cp [OPTIONS] <SRC> <DST>
```

Copy a file or a tar archive between here and a running MicroVM.

`crates/microvms-cli/src/commands/attached.rs:1473`

Flags:

- `--tar` — Move a whole directory tree, as an uncompressed tar archive. `crates/microvms-cli/src/cli.rs:1856`.
- `--mode` — Permissions for an uploaded file, octal as a string (`644`, `0755`). `crates/microvms-cli/src/cli.rs:1864`.
- `--lines` — Read only lines START through END of a file in the VM, 1-based and inclusive. `crates/microvms-cli/src/cli.rs:1874`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## exists

```
microvm exists [OPTIONS] <PATH>
```

Say whether a path exists in a running MicroVM.

`crates/microvms-cli/src/commands/attached.rs:1625`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## sync

```
microvm sync [OPTIONS] <DIR>
```

Sync a project directory into a running MicroVM's /workspace, uploading only what changed.

`crates/microvms-cli/src/commands/attached.rs:1784`

Flags:

- `--watch` — Keep syncing on filesystem changes until Ctrl-C. `crates/microvms-cli/src/cli.rs:1898`.
- `--full` — Upload the whole tree even when the guest manifest claims members are unchanged. `crates/microvms-cli/src/cli.rs:1906`.
- `--timeout` — How long the in-guest deletion of locally-removed paths may take, in seconds. `crates/microvms-cli/src/cli.rs:1910`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## attach

```
microvm attach [OPTIONS]
```

Register a name for a running MicroVM this state directory did not launch.

`crates/microvms-cli/src/commands/attached.rs:1973`

Flags:

- `--from` — A name record as the registry writes it (`<state-dir>/names/<name>.json`), or `-` for stdin. `crates/microvms-cli/src/cli.rs:1937`.
- `--name` — The name to register. Required with the explicit triple; renames a `--from` record. `crates/microvms-cli/src/cli.rs:1950`.
- `--endpoint` — The VM's endpoint, as reported by `run` on the machine that launched it. `crates/microvms-cli/src/cli.rs:1954`.
- `--agent-token` — The agent token delivered to the VM at launch. Never echoed in the envelope. `crates/microvms-cli/src/cli.rs:1958`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:1962`.
- `--identity-host-seed` — The launching host's identity secret, base64, from `run --identity`'s envelope. `crates/microvms-cli/src/cli.rs:1969`.
- `--identity-vm-public-key` — The VM's public key, base64, from `run --identity`'s envelope. The pin. `crates/microvms-cli/src/cli.rs:1973`.
- `--verify-identity` — Prove the far end is the VM the record was created for, before writing anything. `crates/microvms-cli/src/cli.rs:1985`.
- `--port` — The daemon's port inside the guest, for the probe. Not recorded. `crates/microvms-cli/src/cli.rs:1989`.
- `--state-dir` — Where the local state lives — the registry this record is written into. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:1994`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## adopt

```
microvm adopt [OPTIONS]
```

Adopt a MicroVM this invocation didn't launch, through core's lifecycle guards, and report what they read.

`crates/microvms-cli/src/commands/attached.rs:124`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## tunnel

```
microvm tunnel [OPTIONS] <LOCAL[:GUEST]>
```

Tunnel arbitrary TCP to a guest port, so `psql` or `ssh` here reaches a server in the VM.

`crates/microvms-cli/src/commands/attached.rs:2465`

Flags:

- `--bind` — The local address to bind. Loopback by default, deliberately. `crates/microvms-cli/src/cli.rs:1615`.
- `--max-connections` — Stop after this many connections, instead of running until Ctrl-C. `crates/microvms-cli/src/cli.rs:1622`.
- `--verify-identity` — Prove the far end is the VM this record was created for, before relaying a byte. `crates/microvms-cli/src/cli.rs:1648`.
- `--identity-host-seed` — The launching host's identity secret, base64, from `run --identity`'s envelope. `crates/microvms-cli/src/cli.rs:1655`.
- `--identity-vm-public-key` — The VM's public key, base64, from `run --identity`'s envelope. The pin. `crates/microvms-cli/src/cli.rs:1659`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## port-forward

```
microvm port-forward [OPTIONS] <LOCAL[:GUEST]>
```

Serve a guest port on localhost, so a browser here reaches a server in the VM.

`crates/microvms-cli/src/commands/attached.rs:2319`

Flags:

- `--bind` — The local address to bind. Loopback by default, deliberately. `crates/microvms-cli/src/cli.rs:1719`.
- `--max-connections` — Stop after serving this many connections, instead of running until Ctrl-C. `crates/microvms-cli/src/cli.rs:1726`.
- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:625`.
- `--agent-token` — The agent token delivered to the VM at launch. `crates/microvms-cli/src/cli.rs:629`.
- `--microvm-id` — The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:633`.
- `--name` — The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:644`.
- `--port` — The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:648`.
- `--state-dir` — Where the local state lives — the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:657`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## shell

```
microvm shell [OPTIONS]
```

Open an interactive root shell in a running MicroVM — a real PTY, with job control, signals, and resize.

`crates/microvms-cli/src/commands/attached.rs:2753`

Flags:

- `--endpoint` — The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:1679`.
- `--microvm-id` — The MicroVM id, needed to mint the shell token. `crates/microvms-cli/src/cli.rs:1683`.
- `--name` — The name `run --keep --vm-name` registered, standing in for endpoint and id. `crates/microvms-cli/src/cli.rs:1691`.
- `--state-dir` — Where the local state lives — the name registry. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs, the same resolution the attached commands use. `crates/microvms-cli/src/cli.rs:1697`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## suspend

```
microvm suspend [OPTIONS] <MICROVM_ID>
```

Freeze a MicroVM.

`crates/microvms-cli/src/commands/lifecycle.rs:2052`

Flags:

- `--timeout` — How long to wait for the state transition, in seconds. `crates/microvms-cli/src/cli.rs:2011`.
- `--state-dir` — Where the VM's history is appended. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2015`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## resume

```
microvm resume [OPTIONS] <MICROVM_ID>
```

Thaw a suspended MicroVM and report its endpoint.

`crates/microvms-cli/src/commands/lifecycle.rs:2131`

Flags:

- `--timeout` — How long to wait for RUNNING, in seconds. `crates/microvms-cli/src/cli.rs:2029`.
- `--state-dir` — Where the VM's history is appended. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2033`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## terminate

```
microvm terminate [OPTIONS] <MICROVM_ID>
```

Tear down a MicroVM, and optionally its image and build log group.

`crates/microvms-cli/src/commands/lifecycle.rs:2252`

Flags:

- `--image-identifier` — The image to delete, if --delete-image is given. Omitted, the image is read off the kept run's ledger record in the state directory. `crates/microvms-cli/src/cli.rs:2048`.
- `--image-name` — The image's name, needed to name its build log group. Omitted, it is read off the same ledger record when one names the image. `crates/microvms-cli/src/cli.rs:2055`.
- `--delete-image` — Also delete the image, and name its build log group. `crates/microvms-cli/src/cli.rs:2064`.
- `--wait` — Wait for TERMINATED rather than returning as soon as the call is accepted. `crates/microvms-cli/src/cli.rs:2068`.
- `--wait-sec` — Wait for TERMINATED for at most this many seconds. Implies --wait. `crates/microvms-cli/src/cli.rs:2072`.
- `--state-dir` — Where the VM's history is appended. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2076`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## ls

```
microvm ls [OPTIONS]
```

List what this CLI created and could not confirm it deleted; `--remote` asks the account too.

`crates/microvms-cli/src/commands/local.rs:52`

Flags:

- `--state-dir` — Where the ledgers live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2086`.
- `--watch` — Re-read the ledger on an interval until Ctrl-C, port-forward style (#78). `crates/microvms-cli/src/cli.rs:2096`.
- `--interval-sec` — Seconds between ledger re-reads under `--watch`; default 2. `crates/microvms-cli/src/cli.rs:2105`.
- `--max-refreshes` — Stop after this many snapshots instead of on Ctrl-C. `crates/microvms-cli/src/cli.rs:2113`.
- `--remote` — Ask the account too: list live MicroVMs and images and mark each ledger entry live, gone, or unjudged. `crates/microvms-cli/src/cli.rs:2126`.
- `--prune` — Remove the ledger files of `gone` entries. Requires `--remote`. `crates/microvms-cli/src/cli.rs:2133`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## history

```
microvm history [OPTIONS] <VM_ID>
```

Print what was asked of one MicroVM and what the platform reported back.

`crates/microvms-cli/src/commands/local.rs:547`

Flags:

- `--state-dir` — Where the histories live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2147`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## names

```
microvm names [OPTIONS]
```

List the names this machine's registry holds, or delete one whose VM is gone.

`crates/microvms-cli/src/commands/local.rs:633`

Flags:

- `--delete` — Delete this name's record first; repeat for more. A name the registry doesn't hold is refused, and then nothing is deleted. `crates/microvms-cli/src/cli.rs:2155`.
- `--state-dir` — Where the registry lives. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2159`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## logs

```
microvm logs [OPTIONS] <IMAGE_NAME>
```

Name an image's build log group and print the `aws logs tail` command that reads it.

`crates/microvms-cli/src/commands/local.rs:778`

Flags:

- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## image-versions

```
microvm image-versions [OPTIONS] <IMAGE>
```

List an image's versions: each one's build state, whether `RunMicrovm` launches it, and what it was built with.

`crates/microvms-cli/src/commands/image.rs:18`

Flags:

- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## image-set-status

```
microvm image-set-status [OPTIONS] <IMAGE> <VERSION> <STATUS>
```

Retire an image version (`INACTIVE`) or restore it (`ACTIVE`), and read the change back.

`crates/microvms-cli/src/commands/image.rs:50`

Flags:

- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## image-builds

```
microvm image-builds [OPTIONS] <IMAGE> <VERSION>
```

List an image version's builds, one per Graviton generation, or read one with `--build-id`.

`crates/microvms-cli/src/commands/image.rs:83`

Flags:

- `--build-id` — Read this one build, with its snapshot sizes, rather than listing them all. `crates/microvms-cli/src/cli.rs:2212`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## cost

```
microvm cost [OPTIONS]
```

Estimate the cost of a run or a plan.

`crates/microvms-cli/src/commands/cost.rs:29`

Flags:

- `--estimate` — Treat the durations as a plan rather than as timings. `crates/microvms-cli/src/cli.rs:2225`.
- `--compare` — Also print running versus suspended for the same hold, with the break-even. `crates/microvms-cli/src/cli.rs:2229`.
- `--memory` — Baseline MiB, selecting a documented size class. `crates/microvms-cli/src/cli.rs:2233`.
- `--cpus` — vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:506`.
- `--memory-mib` — MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:511`.
- `--running-sec` — Seconds the VM spent, or will spend, RUNNING. `crates/microvms-cli/src/cli.rs:2243`.
- `--suspended-sec` — Seconds spent suspended. Storage only — no compute line at all. `crates/microvms-cli/src/cli.rs:2247`.
- `--build-sec` — Seconds the image build took. `crates/microvms-cli/src/cli.rs:2254`.
- `--image-gb` — Image size in GB. Adds storage with its one-week minimum retention. `crates/microvms-cli/src/cli.rs:2258`.
- `--cycles` — Suspend/resume cycles, each paying a snapshot write plus a read. `crates/microvms-cli/src/cli.rs:2266`.
- `--hold-sec` — The hold to compare running against suspended over, in seconds. `crates/microvms-cli/src/cli.rs:2270`.
- `--max-cost` — A budget in USD the report's total is checked against (#77). `crates/microvms-cli/src/cli.rs:2280`.
- `--on-breach` — What a `--max-cost` breach does: warn and exit 0, or abort with ERR_PRECONDITION (exit 12). `crates/microvms-cli/src/cli.rs:2289`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## egress-posture

```
microvm egress-posture [OPTIONS]
```

The egress posture `run` with these options would report, without launching.

`crates/microvms-cli/src/commands/local.rs:862`

Flags:

- `--egress` — As `run --egress`: request the managed INTERNET_EGRESS connector. `crates/microvms-cli/src/cli.rs:1532`.
- `--egress-network-connector` — As `run --egress-network-connector`: an existing VPC network connector ARN. Repeatable. `crates/microvms-cli/src/cli.rs:1536`.
- `--deny-egress` — As `run --deny-egress`: the advisory proxy variables. Not isolation. `crates/microvms-cli/src/cli.rs:1540`.
- `--config` — Read this project config file instead of ./microvm.toml. `crates/microvms-cli/src/cli.rs:675`.
- `--no-config` — Ignore any microvm.toml, even a malformed one. Flags and defaults apply. `crates/microvms-cli/src/cli.rs:679`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## doctor

```
microvm doctor [OPTIONS]
```

Check every prerequisite and say which one is wrong.

`crates/microvms-cli/src/commands/doctor.rs:33`

Flags:

- `--binary` — The agentd binary to check the architecture of. `crates/microvms-cli/src/cli.rs:2312`.
- `--infra-dir` — The Terraform stack directory. Defaults to ./conformance/infra. `crates/microvms-cli/src/cli.rs:2316`.
- `--config` — Read this project config file instead of ./microvm.toml. `crates/microvms-cli/src/cli.rs:675`.
- `--no-config` — Ignore any microvm.toml, even a malformed one. Flags and defaults apply. `crates/microvms-cli/src/cli.rs:679`.
- `--region` — AWS region. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:587`.
- `--unlisted-region` — Use a region this client has not seen carry MicroVMs. Costs you the diagnostic. `crates/microvms-cli/src/cli.rs:594`.
- `--bucket` — S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:722`.
- `--build-role-arn` — Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:726`.
- `--execution-role-arn` — Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:730`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## manifest

```
microvm manifest [OPTIONS]
```

Emit the whole command surface, its exit codes, and its envelope schema.

`crates/microvms-cli/src/commands/local.rs:931`

Flags:

- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## constants

```
microvm constants [OPTIONS]
```

Emit every service constraint this client believes, for the drift gate.

`crates/microvms-cli/src/commands/local.rs:954`

Flags:

- `--emit-json` — Emit the raw constants object, unwrapped by an envelope. `crates/microvms-cli/src/cli.rs:2337`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## dockerfile

```
microvm dockerfile [OPTIONS]
```

Print the Dockerfile stanza that wraps any base image with agentd.

`crates/microvms-cli/src/commands/local.rs:992`

Flags:

- `--from` — The image ref for the FROM line. Defaults to the managed al2023 base's pair. `crates/microvms-cli/src/cli.rs:2349`.
- `--port` — The port agentd listens on inside the guest. `crates/microvms-cli/src/cli.rs:2353`.
- `--workdir` — A working directory to create and set. Strongly recommended. `crates/microvms-cli/src/cli.rs:2361`.
- `--wrap` — Append the agentd stanza to this task Dockerfile rather than print the default one. `crates/microvms-cli/src/cli.rs:2369`.
- `--inherit-workdir` — With --wrap: the exec working directory is the image's own `WORKDIR`, so one must be declared, by the task or by --workdir. `crates/microvms-cli/src/cli.rs:2374`.
- `--agent` — Print the Dockerfile of an agent VM image carrying this agent, the one `agent-up` builds. Repeatable. `crates/microvms-cli/src/cli.rs:2379`.
- `--claude-version` — With --agent: pin the Claude Code npm package to this version. `crates/microvms-cli/src/cli.rs:2383`.
- `--codex-version` — With --agent: pin the Codex npm package to this version. `crates/microvms-cli/src/cli.rs:2387`.
- `--json` — Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:73`.
- `--dense` — Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:77`.
- `--quiet` — Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:81`.
- `-h, --help` — Print help. `crates/microvms-cli/src/cli.rs:49`.
- `-V, --version` — Print version. `crates/microvms-cli/src/cli.rs:52-53`.

## See also

- [processes](../behavior/processes.md) — 7 shared source citations
- [contract map](../insights/contract-map.md) — 6 shared source citations
- [module map](../architecture/module-map.md) — 5 shared source citations
- [impact analysis](../insights/impact-analysis.md) — 5 shared source citations
- [risk hotspots](../analysis/risk-hotspots.md) — 3 shared source citations
