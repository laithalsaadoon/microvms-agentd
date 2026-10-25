# microvms-agentd · CLI

The `microvm` CLI provides the following subcommands (`crates/microvms-cli/Cargo.toml:21-23`).

## Credential inputs

Use `--name` for a VM registered locally. To transfer a record from another
machine, pipe it into `microvm attach --from -`, then use its registered name.
Attached commands also accept `--agent-token-file <FILE|->`. The `attach` and
`tunnel` commands accept `--identity-host-seed-file <FILE|->` for the host seed.

The credential file flags read a regular file or stdin with `-`, remove trailing
line endings, and refuse an empty value. The group and other permission bits
must be clear on Unix. Only one input may read stdin, including `exec --stdin` and
`stdin --data -`. Literal secret flags remain supported and print a warning
recommending these input paths.

## run

```
microvm run [OPTIONS] [BINARY_OR_DIR]
```

Build an image, launch a VM, run a command, report the cost, tear it down.

`crates/microvms-cli/src/commands/lifecycle.rs:437`

Flags:

- `--image`: Launch this existing image instead of building one. Takes an ARN or a name. `crates/microvms-cli/src/cli.rs:823`.
- `--image-version`: Launch this exact image version instead of the image's latest active one. `crates/microvms-cli/src/cli.rs:841`.
- `--artifact-uri`: Where the build artifact already is, as an s3:// URI. `crates/microvms-cli/src/cli.rs:849`.
- `--exec`: A shell command to run in the VM. `crates/microvms-cli/src/cli.rs:855`.
- `--name`: Image name. Defaults to a per-invocation name, because reusing one is how a clientToken replay wedges an image. `crates/microvms-cli/src/cli.rs:860`.
- `--memory`: Baseline MiB, which selects a documented size class. `crates/microvms-cli/src/cli.rs:868`.
- `--cpus`: vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:515`.
- `--memory-mib`: MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:520`.
- `--dockerfile`: A Dockerfile to use instead of the library's default. Its FROM must match the base. `crates/microvms-cli/src/cli.rs:875`.
- `--log-group`: CloudWatch log group for the build's logs; overrides the service-created `/aws/lambda-microvms/<image-name>`. `crates/microvms-cli/src/cli.rs:884`.
- `--log-stream`: Log stream name prefix inside --log-group. The client appends `/<16 hex>` per build attempt, and the resolved exact name is on the envelope as `logStream`. `crates/microvms-cli/src/cli.rs:896`.
- `--repair-identity`: Widen the guest so `sethostname` and the boot_id bind mount work. `crates/microvms-cli/src/cli.rs:905`.
- `--egress`: Request the managed INTERNET_EGRESS connector. `crates/microvms-cli/src/cli.rs:912`.
- `--egress-network-connector`: Existing VPC network connector ARN. Repeat for multiple connectors. `crates/microvms-cli/src/cli.rs:921`.
- `--deny-egress`: Set proxy environment variables that discourage outbound HTTP requests. `crates/microvms-cli/src/cli.rs:929`.
- `--shell`: Launch shell-capable, so `microvm shell` can attach later. `crates/microvms-cli/src/cli.rs:938`.
- `--launch-env`: Set one launch-environment variable for every exec in the VM, as KEY=VALUE. Repeatable. `crates/microvms-cli/src/cli.rs:959`.
- `--user`: User to run --exec's command as: a name or a numeric uid. Omitted runs as the daemon's own user. `crates/microvms-cli/src/cli.rs:970`.
- `--group`: Group to run --exec's command as: a name or a numeric gid. Omitted keeps the daemon's own group, or a named user's primary group. `crates/microvms-cli/src/cli.rs:977`.
- `--keep`: Leave the VM and image running. You are then paying for them. `crates/microvms-cli/src/cli.rs:981`.
- `--no-wait`: Return once the launch is accepted, with the VM still PENDING. `microvm wait` finishes the launch later. `crates/microvms-cli/src/cli.rs:991`.
- `--identity`: Generate a per-VM identity, so `tunnel --verify-identity` can prove the far end. `crates/microvms-cli/src/cli.rs:1007`.
- `--vm-name`: Register a local name for the kept VM, so later commands can say `--name <NAME>` instead of pasting the endpoint/agent-token/microvm-id triple. `crates/microvms-cli/src/cli.rs:1025`.
- `--client-token`: Idempotency key for this launch: 1-128 printable ASCII characters. `crates/microvms-cli/src/cli.rs:734`.
- `--vm-log-group`: CloudWatch log group for the VM's own logs (not the image build's). `crates/microvms-cli/src/cli.rs:741`.
- `--vm-log-stream`: Exact log stream inside --vm-log-group. `crates/microvms-cli/src/cli.rs:745`.
- `--no-vm-logs`: Turn the VM's own logging off. `crates/microvms-cli/src/cli.rs:749`.
- `--timeout`: How long to wait for the exec, in seconds. `crates/microvms-cli/src/cli.rs:1032`.
- `--max-idle-sec`: Suspend the VM after this much inbound-traffic idleness. `crates/microvms-cli/src/cli.rs:1036`.
- `--suspended-sec`: Terminate a VM the idle policy suspended once it's been suspended this long. An explicit suspend outlasted it when measured (2026-10-12), which AWS doesn't promise. `crates/microvms-cli/src/cli.rs:1039`.
- `--auto-resume`: Let the platform resume a suspended VM on an incoming request. `crates/microvms-cli/src/cli.rs:1045`.
- `--max-duration-sec`: Hard ceiling on the VM's life. Refused above 28800 (eight hours) before any call. `crates/microvms-cli/src/cli.rs:1049`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:1053`.
- `--state-dir`: Where the run ledger is written. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:1057`.
- `--config`: Read this project config file instead of ./microvm.toml. `crates/microvms-cli/src/cli.rs:659`.
- `--no-config`: Ignore any microvm.toml, even a malformed one. Flags and defaults apply. `crates/microvms-cli/src/cli.rs:663`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--bucket`: S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:706`.
- `--build-role-arn`: Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:710`.
- `--execution-role-arn`: Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:714`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## quickstart

```
microvm quickstart [OPTIONS]
```

Zero to a live exec: provision the daemon, build, launch, run, report, tear down.

`crates/microvms-cli/src/commands/lifecycle.rs:962`

Flags:

- `--exec`: The command to run inside the first VM. `crates/microvms-cli/src/cli.rs:1085`.
- `--state-dir`: Where the run ledger and the provisioned-daemon cache live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:1094`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--bucket`: S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:706`.
- `--build-role-arn`: Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:710`.
- `--execution-role-arn`: Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:714`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## build

```
microvm build [OPTIONS] [BINARY]
```

Build a MicroVM image and wait for it to be usable.

`crates/microvms-cli/src/commands/lifecycle.rs:1436`

Flags:

- `--state-dir`: Where the provisioned-daemon cache lives. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs: the same directory `run`'s ledger uses, so the two commands share one cache. `crates/microvms-cli/src/cli.rs:1127`.
- `--artifact-uri`: Where the build artifact already is, as an s3:// URI. See `run --artifact-uri`. `crates/microvms-cli/src/cli.rs:1131`.
- `--name`: Image name. Defaults to a per-invocation name. `crates/microvms-cli/src/cli.rs:1135`.
- `--memory`: Baseline MiB, selecting a documented size class. `crates/microvms-cli/src/cli.rs:1139`.
- `--cpus`: vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:515`.
- `--memory-mib`: MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:520`.
- `--dockerfile`: A Dockerfile to use instead of the library's default. `crates/microvms-cli/src/cli.rs:1146`.
- `--project`: A project directory whose dependency files bake an environment layer into the image (#74). `crates/microvms-cli/src/cli.rs:1160`.
- `--base-image-version`: Pin the managed base image to one version instead of taking the service's default. `crates/microvms-cli/src/cli.rs:1178`.
- `--log-group`: CloudWatch log group for the build's logs; overrides the service-created `/aws/lambda-microvms/<image-name>`. See `run --log-group`. `crates/microvms-cli/src/cli.rs:1183`.
- `--log-stream`: Log stream name prefix inside --log-group. The client appends `/<16 hex>` per build attempt and the envelope reports the resolved exact name as `logStream`. See `run --log-stream`. Requires --log-group: `build` reads no config file, so the flag is the only place a group can come from. `crates/microvms-cli/src/cli.rs:1190`.
- `--repair-identity`: Widen the guest so `sethostname` and the boot_id bind mount work. `crates/microvms-cli/src/cli.rs:1194`.
- `--tag`: A tag on the image, repeatable. Split at the first `=`, so a VALUE may hold one, and checked against the model's `TagKey` and `TagValue` at parse time; an empty VALUE is legal. A KEY given twice is refused rather than one of its values dropped. `crates/microvms-cli/src/cli.rs:1200`.
- `--base-image`: The managed base image `baseImageArn` names, instead of `al2023-1`. `crates/microvms-cli/src/cli.rs:1209`.
- `--inherit-workdir`: Rely on the image's `WORKDIR`: an exec with no cwd runs there. `crates/microvms-cli/src/cli.rs:1217`.
- `--run-hook-timeout-sec`: The timeout of the `run`, `resume`, `suspend` and `terminate` hooks, in whole seconds. `crates/microvms-cli/src/cli.rs:1225`.
- `--build-hook-timeout-sec`: The timeout of the `ready` and `validate` build hooks, in whole seconds. `crates/microvms-cli/src/cli.rs:1232`.
- `--reuse`: Reuse an existing image whose build inputs match. `crates/microvms-cli/src/cli.rs:1253`.
- `--s3-key-prefix`: Under --reuse, a key prefix inside the bucket for the artifact. `crates/microvms-cli/src/cli.rs:1257`.
- `--force`: Under --reuse, delete a ready image under the derived name and build it afresh. `crates/microvms-cli/src/cli.rs:1261`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:1265`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--bucket`: S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:706`.
- `--build-role-arn`: Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:710`.
- `--execution-role-arn`: Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:714`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## agent-up

```
microvm agent-up [OPTIONS] --vm-name <NAME> [BINARY]
```

Bring up a VM with a coding agent in it: image, launch, model credentials, non-root user.

`crates/microvms-cli/src/commands/agent.rs:237`

Flags:

- `--vm-name`: The local name to register for the VM, and the handle every later command uses. `crates/microvms-cli/src/cli.rs:2468`.
- `--agent`: Which agent to install. Repeatable; defaults to `claude-code`. `crates/microvms-cli/src/cli.rs:2472`.
- `--claude-model`: The Bedrock inference-profile id Claude Code uses. Defaults to the profile's. `crates/microvms-cli/src/cli.rs:2476`.
- `--codex-model`: The Bedrock model id Codex uses. Defaults to the profile's. `crates/microvms-cli/src/cli.rs:2480`.
- `--claude-version`: Pin the Claude Code npm package to this version. Changes the image's reuse hash. `crates/microvms-cli/src/cli.rs:2484`.
- `--codex-version`: Pin the Codex npm package to this version. Changes the image's reuse hash. `crates/microvms-cli/src/cli.rs:2488`.
- `--project`: A local directory to upload into /workspace after launch, packed the way `run <DIR>` packs. `crates/microvms-cli/src/cli.rs:2496`.
- `--memory`: Baseline MiB. Default 1024: a 4 GiB always-present ceiling at half the floor cost of 2048, which fits peaky agent sessions (see `docs/AGENT-VMS.md`). `crates/microvms-cli/src/cli.rs:2501`.
- `--cpus`: vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:515`.
- `--memory-mib`: MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:520`.
- `--token-ttl-hours`: How long the Bedrock bearer token lives, in hours. Default and ceiling 12. `crates/microvms-cli/src/cli.rs:2508`.
- `--max-idle-sec`: Suspend the VM after this much inbound-traffic idleness. `crates/microvms-cli/src/cli.rs:2512`.
- `--suspended-sec`: Terminate a VM the idle policy suspended once it's been suspended this long. An explicit suspend outlasted it when measured (2026-10-12), which AWS doesn't promise. `crates/microvms-cli/src/cli.rs:2515`.
- `--auto-resume`: Let the platform resume a suspended VM on an incoming request. `crates/microvms-cli/src/cli.rs:2521`.
- `--max-duration-sec`: Hard ceiling on the VM's life. Refused above 28800 (eight hours) before any call. `crates/microvms-cli/src/cli.rs:2525`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:2529`.
- `--state-dir`: Where the run ledger and name registry live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2533`.
- `--client-token`: Idempotency key for this launch: 1-128 printable ASCII characters. `crates/microvms-cli/src/cli.rs:734`.
- `--vm-log-group`: CloudWatch log group for the VM's own logs (not the image build's). `crates/microvms-cli/src/cli.rs:741`.
- `--vm-log-stream`: Exact log stream inside --vm-log-group. `crates/microvms-cli/src/cli.rs:745`.
- `--no-vm-logs`: Turn the VM's own logging off. `crates/microvms-cli/src/cli.rs:749`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--bucket`: S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:706`.
- `--build-role-arn`: Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:710`.
- `--execution-role-arn`: Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:714`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## agent-prompt

```
microvm agent-prompt [OPTIONS] <TASK>
```

Hand a coding agent in a running agent VM one task, headless, as the non-root user.

`crates/microvms-cli/src/commands/agent.rs:766`

Flags:

- `--agent`: Which installed agent to prompt. Omitted reads the guest marker and takes the one it names. `crates/microvms-cli/src/cli.rs:2570`.
- `--permission-mode`: Guest agent permission policy. Unrestricted bypasses agent approvals, still as uid 1000. `crates/microvms-cli/src/cli.rs:2574`.
- `--execution-timeout`: Remote execution deadline in seconds; persists after detach. Must be positive and finite. `crates/microvms-cli/src/cli.rs:2578`.
- `--reap-group-on-exit`: Terminate residual process-group children when the main agent exits. `crates/microvms-cli/src/cli.rs:2582`.
- `--timeout`: Caller wait limit in seconds. Does not stop the agent when the wait expires. `crates/microvms-cli/src/cli.rs:2586`.
- `--detach`: Start the agent and return immediately, without waiting and without acking. `crates/microvms-cli/src/cli.rs:2593`.
- `--exec-id`: Use this exec id instead of a fresh one, making a retry idempotent. `crates/microvms-cli/src/cli.rs:2597`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## exec

```
microvm exec [OPTIONS] [COMMAND]
```

Run one command in a MicroVM that is already running.

`crates/microvms-cli/src/commands/attached.rs:245`

Flags:

- `--timeout`: How long to wait for the command, in seconds. `crates/microvms-cli/src/cli.rs:1285`.
- `--timeout-sec`: The daemon's deadline for the command, in seconds: `timeout_sec` on the start request. `crates/microvms-cli/src/cli.rs:1293`.
- `--complete`: Wait for exactly one result, the way the SDKs' `run_to_completion` does (BIND-8 to BIND-10). `crates/microvms-cli/src/cli.rs:1304`.
- `--client-grace`: With --complete, the seconds past --timeout-sec before the kill, and then for its result. `crates/microvms-cli/src/cli.rs:1312`.
- `--cwd`: Working directory. `crates/microvms-cli/src/cli.rs:1319`.
- `--env`: Set one environment variable for the command, as KEY=VALUE. Repeatable. `crates/microvms-cli/src/cli.rs:1336`.
- `--user`: User to run the command as: a name or a numeric uid. Omitted runs as the daemon's own user. `crates/microvms-cli/src/cli.rs:1348`.
- `--group`: Group to run the command as: a name (resolved against the guest's `/etc/group`) or a numeric gid. Omitted keeps the daemon's own group, or a named user's primary group. `crates/microvms-cli/src/cli.rs:1353`.
- `--shell`: Run the command under this shell instead of `/bin/sh`, for example `bash` (AGENTD-14). `crates/microvms-cli/src/cli.rs:1363`.
- `--inherit-image-env`: Start the command's environment from the image's `ENV` rather than from nothing (AGENTD-11). `crates/microvms-cli/src/cli.rs:1374`.
- `--exec-id`: Use this exec id instead of a fresh one, making a retry idempotent. `crates/microvms-cli/src/cli.rs:1394`.
- `--poll`: Read an existing exec's status and output instead of starting anything. `crates/microvms-cli/src/cli.rs:1403`.
- `--detach`: Start the command and return immediately, without waiting and **without acking**. `crates/microvms-cli/src/cli.rs:1423`.
- `--stream`: Stream output as it arrives rather than waiting for the whole thing. `crates/microvms-cli/src/cli.rs:1433`.
- `--from-offset`: Resume a stream at this byte offset. Only with --stream. `crates/microvms-cli/src/cli.rs:1441`.
- `--stdin`: Give the command a stdin pipe and feed it this process's stdin, then close it. `crates/microvms-cli/src/cli.rs:1449`.
- `--reap`: Signal the command's whole process group once its own child exits. `crates/microvms-cli/src/cli.rs:1461`.
- `--poweroff-on-exit` powers off the guest after this exec's original process group exits. It needs a daemon that supports it and runs with CAP_SYS_BOOT. Other work in the guest ends too, and the start acknowledgment and the results can race the shutdown. It doesn't prevent idle suspend. `crates/microvms-cli/src/cli.rs:1467`.
- `--kill-on-timeout`: On `ERR_TIMEOUT`, stop the exec rather than leaving it running. `crates/microvms-cli/src/cli.rs:1478`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## wait

```
microvm wait [OPTIONS]
```

Wait until a kept MicroVM answers: RUNNING, and then its daemon.

`crates/microvms-cli/src/commands/lifecycle.rs:2184`

Flags:

- `--timeout`: How long to wait for RUNNING, in seconds. The daemon wait after it is core's own bootstrap bound. `crates/microvms-cli/src/cli.rs:1561`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## health

```
microvm health [OPTIONS]
```

Ask a running MicroVM's daemon whether it is up, and what its identity repair did.

`crates/microvms-cli/src/commands/attached.rs:947`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## keepalive

```
microvm keepalive [OPTIONS]
```

Keep a running MicroVM awake while work runs inside it, by polling its health.

`crates/microvms-cli/src/commands/attached.rs:838`

Flags:

- `--interval`: Seconds between health polls. Default: a third of the idle window, at most 20. `crates/microvms-cli/src/cli.rs:1491`.
- `--while-busy`: Stop once the daemon reports no running exec. `crates/microvms-cli/src/cli.rs:1495`.
- `--for`: Stop after this many seconds, busy or not. `crates/microvms-cli/src/cli.rs:1499`.
- `--idle-window`: The VM's `maxIdleDurationSeconds`. Read from the control plane when omitted. `crates/microvms-cli/src/cli.rs:1503`.
- `--tolerated-errors`: Retryable poll failures in a row to retry, a second apart, before giving up. `crates/microvms-cli/src/cli.rs:1507`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## ack

```
microvm ack [OPTIONS] <EXEC_ID>
```

Release a finished exec's buffered output, which starts its collection clock.

`crates/microvms-cli/src/commands/attached.rs:1152`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## kill

```
microvm kill [OPTIONS] <EXEC_ID>
```

Stop a running exec: SIGTERM its whole process group, SIGKILL after the daemon's grace.

`crates/microvms-cli/src/commands/attached.rs:1193`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## ps

```
microvm ps [OPTIONS]
```

List what is running in a MicroVM: every exec's process group and its live pids.

`crates/microvms-cli/src/commands/attached.rs:1245`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## stdin

```
microvm stdin [OPTIONS] <EXEC_ID>
```

Write to a running exec's stdin, and optionally close it.

`crates/microvms-cli/src/commands/attached.rs:1336`

Flags:

- `--data`: What to write. `-` reads this process's stdin; omitted writes nothing. `crates/microvms-cli/src/cli.rs:1811`.
- `--eof`: Close stdin after any --data is written. `crates/microvms-cli/src/cli.rs:1819`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## cp

```
microvm cp [OPTIONS] <SRC> <DST>
```

Copy a file or a tar archive between here and a running MicroVM.

`crates/microvms-cli/src/commands/attached.rs:1502`

Flags:

- `--tar`: Move a whole directory tree, as an uncompressed tar archive. `crates/microvms-cli/src/cli.rs:1859`.
- `--mode`: Permissions for an uploaded file, octal as a string (`644`, `0755`). `crates/microvms-cli/src/cli.rs:1867`.
- `--lines`: Read only lines START through END of a file in the VM, 1-based and inclusive. `crates/microvms-cli/src/cli.rs:1877`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## exists

```
microvm exists [OPTIONS] <PATH>
```

Say whether a path exists in a running MicroVM.

`crates/microvms-cli/src/commands/attached.rs:1654`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## sync

```
microvm sync [OPTIONS] <DIR>
```

Sync a project directory into a running MicroVM's /workspace, uploading only what changed.

`crates/microvms-cli/src/commands/attached.rs:1813`

Flags:

- `--watch`: Keep syncing on filesystem changes until Ctrl-C. `crates/microvms-cli/src/cli.rs:1901`.
- `--full`: Upload the whole tree even when the guest manifest claims members are unchanged. `crates/microvms-cli/src/cli.rs:1909`.
- `--timeout`: How long the in-guest deletion of locally-removed paths may take, in seconds. `crates/microvms-cli/src/cli.rs:1913`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## attach

```
microvm attach --from <FILE|-> [--name <NEW_NAME>] [--verify-identity]
microvm attach --name <NAME> --microvm-id <ID> --endpoint <URL> --agent-token-file <FILE|-> [--identity-host-seed-file <FILE|-> --identity-vm-public-key <BASE64>] [--verify-identity]
```

Register a name for a running MicroVM this state directory did not launch.

`crates/microvms-cli/src/commands/attached.rs:2002`

Flags:

- `--from`: A name record as the registry writes it (`<state-dir>/names/<name>.json`), or `-` for stdin. `crates/microvms-cli/src/cli.rs:1940`.
- `--name`: The name to register. Required with the explicit triple; renames a `--from` record. `crates/microvms-cli/src/cli.rs:1953`.
- `--endpoint`: The VM's endpoint, as reported by `run` on the machine that launched it. `crates/microvms-cli/src/cli.rs:1957`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:1967`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--from` or `--agent-token-file`. Never echoed in the envelope. `crates/microvms-cli/src/cli.rs:1961`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:1970`.
- `--identity-host-seed-file` reads the host identity secret from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:1995`.
- `--identity-host-seed`: Prefer `--identity-host-seed-file`. The launching host's identity secret, base64, from `run --identity`'s envelope. `crates/microvms-cli/src/cli.rs:1977`.
- `--identity-vm-public-key`: The VM's public key, base64, from `run --identity`'s envelope. The pin. `crates/microvms-cli/src/cli.rs:1998`.
- `--verify-identity`: Prove the far end is the VM the record was created for, before writing anything. `crates/microvms-cli/src/cli.rs:2010`.
- `--port`: The daemon's port inside the guest, for the probe; it is not recorded. `crates/microvms-cli/src/cli.rs:2014`.
- `--state-dir`: Where the local state lives: the registry this record is written into. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2019`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## adopt

```
microvm adopt [OPTIONS]
```

Adopt a MicroVM this invocation didn't launch, through core's lifecycle guards, and report what they read.

`crates/microvms-cli/src/commands/attached.rs:126`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## tunnel

```
microvm tunnel [OPTIONS] <LOCAL[:GUEST]>
```

Tunnel arbitrary TCP to a guest port, so `psql` or `ssh` here reaches a server in the VM.

`crates/microvms-cli/src/commands/attached.rs:2494`

Flags:

- `--bind`: The local address to bind. Loopback by default, deliberately. `crates/microvms-cli/src/cli.rs:1609`.
- `--max-connections`: Stop after this many connections; without this limit, runs until Ctrl-C. `crates/microvms-cli/src/cli.rs:1616`.
- `--verify-identity`: Prove the far end is the VM this record was created for, before relaying a byte. `crates/microvms-cli/src/cli.rs:1642`.
- `--identity-host-seed-file` reads the host identity secret from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:1659`.
- `--identity-host-seed`: Prefer `--identity-host-seed-file`. The launching host's identity secret, base64, from `run --identity`'s envelope. `crates/microvms-cli/src/cli.rs:1649`.
- `--identity-vm-public-key`: The VM's public key, base64, from `run --identity`'s envelope. The pin. `crates/microvms-cli/src/cli.rs:1662`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## port-forward

```
microvm port-forward [OPTIONS] <LOCAL[:GUEST]>
```

Serve a guest port on localhost, so a browser here reaches a server in the VM.

`crates/microvms-cli/src/commands/attached.rs:2348`

Flags:

- `--bind`: The local address to bind. Loopback by default, deliberately. `crates/microvms-cli/src/cli.rs:1722`.
- `--max-connections`: Stop after serving this many connections; without this limit, runs until Ctrl-C. `crates/microvms-cli/src/cli.rs:1729`.
- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:604`.
- `--agent-token-file` reads the agent token from a protected file, or stdin with `-`. `crates/microvms-cli/src/cli.rs:614`.
- `--agent-token`: The agent token delivered to the VM at launch. Prefer `--name` or `--agent-token-file`. `crates/microvms-cli/src/cli.rs:608`.
- `--microvm-id`: The MicroVM id, needed to mint the endpoint proxy token. `crates/microvms-cli/src/cli.rs:617`.
- `--name`: The name `run --keep --vm-name` registered, standing in for the whole triple. `crates/microvms-cli/src/cli.rs:628`.
- `--port`: The daemon's port inside the guest. `crates/microvms-cli/src/cli.rs:632`.
- `--state-dir`: Where the local state lives: the name registry, and exec's per-VM history. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:641`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## shell

```
microvm shell [OPTIONS]
```

Open an interactive root shell in a running MicroVM, a real PTY, with job control, signals, and resize.

`crates/microvms-cli/src/commands/attached.rs:2782`

Flags:

- `--endpoint`: The VM's endpoint, as reported by `run`. `crates/microvms-cli/src/cli.rs:1682`.
- `--microvm-id`: The MicroVM id, needed to mint the shell token. `crates/microvms-cli/src/cli.rs:1686`.
- `--name`: The name `run --keep --vm-name` registered, standing in for endpoint and id. `crates/microvms-cli/src/cli.rs:1694`.
- `--state-dir`: Where the local state lives: the name registry. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs, the same resolution the attached commands use. `crates/microvms-cli/src/cli.rs:1700`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## suspend

```
microvm suspend [OPTIONS] <MICROVM_ID>
```

Freeze a MicroVM.

`crates/microvms-cli/src/commands/lifecycle.rs:2027`

Flags:

- `--timeout`: How long to wait for the state transition, in seconds. `crates/microvms-cli/src/cli.rs:2036`.
- `--state-dir`: Where the VM's history is appended. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2040`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## resume

```
microvm resume [OPTIONS] <MICROVM_ID>
```

Thaw a suspended MicroVM and report its endpoint.

`crates/microvms-cli/src/commands/lifecycle.rs:2108`

Flags:

- `--timeout`: How long to wait for RUNNING, in seconds. `crates/microvms-cli/src/cli.rs:2054`.
- `--state-dir`: Where the VM's history is appended. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2058`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## terminate

```
microvm terminate [OPTIONS] <MICROVM_ID>
```

Tear down a MicroVM, and optionally its image and build log group.

`crates/microvms-cli/src/commands/lifecycle.rs:2229`

Flags:

- `--image-identifier`: The image to delete, if --delete-image is given. Omitted, the image is read off the kept run's ledger record in the state directory. `crates/microvms-cli/src/cli.rs:2073`.
- `--image-name`: The image's name, needed to name its build log group. Omitted, it is read off the same ledger record when one names the image. `crates/microvms-cli/src/cli.rs:2080`.
- `--delete-image`: Also delete the image, and name its build log group. `crates/microvms-cli/src/cli.rs:2089`.
- `--wait`: Wait for TERMINATED rather than returning as soon as the call is accepted. `crates/microvms-cli/src/cli.rs:2093`.
- `--wait-sec`: Wait for TERMINATED for at most this many seconds. Implies --wait. `crates/microvms-cli/src/cli.rs:2097`.
- `--state-dir`: Where the VM's history is appended. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2101`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## ls

```
microvm ls [OPTIONS]
```

List what this CLI created and could not confirm it deleted; `--remote` asks the account too.

`crates/microvms-cli/src/commands/local.rs:52`

Flags:

- `--state-dir`: Where the ledgers live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2111`.
- `--watch`: Re-read the ledger on an interval until Ctrl-C, port-forward style (#78). `crates/microvms-cli/src/cli.rs:2121`.
- `--interval-sec`: Seconds between ledger re-reads under `--watch`; default 2. `crates/microvms-cli/src/cli.rs:2130`.
- `--max-refreshes`: Stop after this many snapshots instead of on Ctrl-C. `crates/microvms-cli/src/cli.rs:2138`.
- `--remote`: Ask the account too: list live MicroVMs and images and mark each ledger entry live, gone, or unjudged. `crates/microvms-cli/src/cli.rs:2151`.
- `--prune`: Remove the ledger files of `gone` entries. Requires `--remote`. `crates/microvms-cli/src/cli.rs:2158`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## history

```
microvm history [OPTIONS] <VM_ID>
```

Print what was asked of one MicroVM and what the platform reported back.

`crates/microvms-cli/src/commands/local.rs:549`

Flags:

- `--state-dir`: Where the histories live. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2172`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## names

```
microvm names [OPTIONS]
```

List the names this machine's registry holds, or delete one whose VM is gone.

`crates/microvms-cli/src/commands/local.rs:635`

Flags:

- `--delete`: Delete this name's record first; repeat for more. A name the registry doesn't hold is refused, and then nothing is deleted. `crates/microvms-cli/src/cli.rs:2180`.
- `--state-dir`: Where the registry lives. Defaults to $MICROVM_STATE_DIR or ~/.microvm/runs. `crates/microvms-cli/src/cli.rs:2184`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## logs

```
microvm logs [OPTIONS] <IMAGE_NAME>
```

Name an image's build log group and print the `aws logs tail` command that reads it.

`crates/microvms-cli/src/commands/local.rs:780`

Flags:

- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## image-versions

```
microvm image-versions [OPTIONS] <IMAGE>
```

List an image's versions: each one's build state, whether `RunMicrovm` launches it, and what it was built with.

`crates/microvms-cli/src/commands/image.rs:18`

Flags:

- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## image-set-status

```
microvm image-set-status [OPTIONS] <IMAGE> <VERSION> <STATUS>
```

Retire an image version (`INACTIVE`) or restore it (`ACTIVE`), and read the change back.

`crates/microvms-cli/src/commands/image.rs:50`

Flags:

- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## image-builds

```
microvm image-builds [OPTIONS] <IMAGE> <VERSION>
```

List an image version's builds, one per Graviton generation, or read one with `--build-id`.

`crates/microvms-cli/src/commands/image.rs:83`

Flags:

- `--build-id`: Read this one build, with its snapshot sizes. `crates/microvms-cli/src/cli.rs:2252`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## cost

```
microvm cost [OPTIONS]
```

Estimate the cost of a run or a plan.

`crates/microvms-cli/src/commands/cost.rs:29`

Flags:

- `--estimate`: Treat the durations as a plan rather than as timings. `crates/microvms-cli/src/cli.rs:2265`.
- `--compare`: Also print running versus suspended for the same hold, with the break-even. `crates/microvms-cli/src/cli.rs:2269`.
- `--memory`: Baseline MiB, selecting a documented size class. `crates/microvms-cli/src/cli.rs:2273`.
- `--cpus`: vCPUs the workload needs, instead of --memory: the smallest size class whose baseline covers the request is used. Takes a fraction (0.5) and combines with --memory-mib. `crates/microvms-cli/src/cli.rs:515`.
- `--memory-mib`: MiB of memory the workload needs, instead of --memory: any figure, which selects the smallest class whose baseline covers it (1500 selects 2048). Combines with --cpus. `crates/microvms-cli/src/cli.rs:520`.
- `--running-sec`: Seconds the VM spent, or will spend, RUNNING. `crates/microvms-cli/src/cli.rs:2283`.
- `--suspended-sec`: Seconds spent suspended. Storage only: no compute line at all. `crates/microvms-cli/src/cli.rs:2287`.
- `--build-sec`: Seconds the image build took. `crates/microvms-cli/src/cli.rs:2294`.
- `--image-gb`: Image size in GB. Adds storage with its one-week minimum retention. `crates/microvms-cli/src/cli.rs:2298`.
- `--cycles`: Suspend/resume cycles, each paying a snapshot write plus a read. `crates/microvms-cli/src/cli.rs:2306`.
- `--hold-sec`: The hold to compare running against suspended over, in seconds. `crates/microvms-cli/src/cli.rs:2310`.
- `--max-cost`: A budget in USD the report's total is checked against (#77). `crates/microvms-cli/src/cli.rs:2320`.
- `--on-breach`: Action on a `--max-cost` breach: warn and exit 0, or abort with ERR_PRECONDITION (exit 12). `crates/microvms-cli/src/cli.rs:2329`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## egress-posture

```
microvm egress-posture [OPTIONS]
```

The egress posture `run` with these options would report, without launching.

`crates/microvms-cli/src/commands/local.rs:864`

Flags:

- `--egress`: As `run --egress`: request the managed INTERNET_EGRESS connector. `crates/microvms-cli/src/cli.rs:1526`.
- `--egress-network-connector`: As `run --egress-network-connector`: an existing VPC network connector ARN. Repeatable. `crates/microvms-cli/src/cli.rs:1530`.
- `--deny-egress`: As `run --deny-egress`: the advisory proxy variables; these do not enforce isolation. `crates/microvms-cli/src/cli.rs:1534`.
- `--config`: Read this project config file instead of ./microvm.toml. `crates/microvms-cli/src/cli.rs:659`.
- `--no-config`: Ignore any microvm.toml, even a malformed one. Flags and defaults apply. `crates/microvms-cli/src/cli.rs:663`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## doctor

```
microvm doctor [OPTIONS]
```

Check every prerequisite and say which one is wrong.

`crates/microvms-cli/src/commands/doctor.rs:33`

Flags:

- `--binary`: The agentd binary to check the architecture of. `crates/microvms-cli/src/cli.rs:2352`.
- `--infra-dir`: The Terraform stack directory. Defaults to ./conformance/infra. `crates/microvms-cli/src/cli.rs:2356`.
- `--config`: Read this project config file instead of ./microvm.toml. `crates/microvms-cli/src/cli.rs:659`.
- `--no-config`: Ignore any microvm.toml, even a malformed one. Flags and defaults apply. `crates/microvms-cli/src/cli.rs:663`.
- `--region`: AWS region, any region name. Defaults to $AWS_REGION, then $AWS_DEFAULT_REGION, then us-east-1. `crates/microvms-cli/src/cli.rs:551`.
- `--unlisted-region`: Deprecated since 1.3.0: `--region` takes any region name. `crates/microvms-cli/src/cli.rs:559`.
- `--bucket`: S3 bucket for the build artifact. Defaults to $MICROVM_BUCKET. `crates/microvms-cli/src/cli.rs:706`.
- `--build-role-arn`: Build role ARN. Defaults to $MICROVM_BUILD_ROLE_ARN. `crates/microvms-cli/src/cli.rs:710`.
- `--execution-role-arn`: Execution role ARN. Defaults to $MICROVM_EXECUTION_ROLE_ARN. `crates/microvms-cli/src/cli.rs:714`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## manifest

```
microvm manifest [OPTIONS]
```

Emit the whole command surface, its exit codes, and its envelope schema.

`crates/microvms-cli/src/commands/local.rs:933`

Flags:

- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## constants

```
microvm constants [OPTIONS]
```

Emit every service constraint this client believes, for the drift gate.

`crates/microvms-cli/src/commands/local.rs:956`

Flags:

- `--emit-json`: Emit the raw constants object, unwrapped by an envelope. `crates/microvms-cli/src/cli.rs:2377`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## dockerfile

```
microvm dockerfile [OPTIONS]
```

Print the Dockerfile stanza that wraps any base image with agentd.

`crates/microvms-cli/src/commands/local.rs:994`

Flags:

- `--from`: The image ref for the FROM line. Defaults to the managed al2023 base's pair. `crates/microvms-cli/src/cli.rs:2389`.
- `--port`: The port agentd listens on inside the guest. `crates/microvms-cli/src/cli.rs:2393`.
- `--workdir`: A working directory to create and set. Strongly recommended. `crates/microvms-cli/src/cli.rs:2401`.
- `--wrap`: Append the agentd stanza to this task Dockerfile rather than print the default one. `crates/microvms-cli/src/cli.rs:2409`.
- `--preserve-user` keeps the final `USER` the builder resolves as the default identity for workload execs, with `--wrap`. The daemon still starts as root, and an `AGENTD_DEFAULT_USER` the image sets or a request's `user` takes precedence. `crates/microvms-cli/src/cli.rs:2414`, `crates/microvms-app/src/control/artifact.rs:631-645`.
- `--inherit-workdir`: With --wrap: the exec working directory is the image's own `WORKDIR`, so one must be declared, by the task or by --workdir. `crates/microvms-cli/src/cli.rs:2419`.
- `--agent`: Print the Dockerfile of an agent VM image carrying this agent, the one `agent-up` builds. Repeatable. `crates/microvms-cli/src/cli.rs:2424`.
- `--claude-version`: With --agent: pin the Claude Code npm package to this version. `crates/microvms-cli/src/cli.rs:2428`.
- `--codex-version`: With --agent: pin the Codex npm package to this version. `crates/microvms-cli/src/cli.rs:2432`.
- `--json`: Emit the typed JSON envelope on stdout instead of human output. `crates/microvms-cli/src/cli.rs:74`.
- `--dense`: Token-lean output, for a consumer paying per token: tab-separated alone, compact one-line JSON with --json. `crates/microvms-cli/src/cli.rs:78`.
- `--quiet`: Suppress progress on stderr. Warnings still print. `crates/microvms-cli/src/cli.rs:82`.
- `-h, --help`: Print help. `crates/microvms-cli/src/cli.rs:50`.
- `-V, --version`: Print version. `crates/microvms-cli/src/cli.rs:53-53`.

## See also

These pages cite many of the same source files as this one.

- [processes](../behavior/processes.md)
- [contract map](../insights/contract-map.md)
- [module map](../architecture/module-map.md)
- [impact analysis](../insights/impact-analysis.md)
- [risk hotspots](../analysis/risk-hotspots.md)
