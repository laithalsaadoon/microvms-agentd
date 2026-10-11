# Use code-server in a MicroVM

Run [code-server](https://github.com/coder/code-server) in an AWS Lambda
MicroVM and open it locally through `microvm port-forward`. The example builds
an image, starts a named VM, starts the IDE as uid 1000, and forwards port 8080.
The commands create billable AWS resources.

## Start the example

Complete the repository's [AWS setup](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/).
The script expects `microvm` on PATH and a locally built ARM64 daemon at
`target/aarch64-unknown-linux-musl/release/agentd`, or a path in `AGENTD`.
From the repository root:

```bash
mise run build
# Put your IDE password in a local file with mode 0600 first.
CODE_SERVER_PASSWORD_FILE="$HOME/.config/code-server-password" \
  bash examples/code-server-remote-dev/run.sh
# Open http://127.0.0.1:8080
```

The first image build takes minutes. On later runs, the script reuses the VM
if its health request succeeds. Ctrl-C stops the local forward. The VM
keeps running until you call `terminate` or its launch policy ends it.

## Image and launch settings

[Dockerfile](Dockerfile) installs a pinned code-server RPM and the guest
runtime tools. It uses the managed base image, installs agentd, and creates a
workspace for uid 1000. A changed Dockerfile changes the reusable image name.

[microvm.toml](microvm.toml) configures a 2048 MiB memory baseline, outbound
networking, shell access, auto-resume, and an idle window of 900 seconds.
The launch environment supplies `HOME=/workspace` and an explicit `PATH`.

The script starts code-server detached as uid 1000 with password authentication
and a guest loopback listener. The local forward also defaults to loopback and
supplies the platform's port-scoped proxy credentials.

## Password authentication

The IDE requires its own password. On first launch, `run.sh` reads
`CODE_SERVER_PASSWORD_FILE`, requires mode 0600 or stricter, and uploads a
mode-0600 configuration with `microvm cp`. The password is never included in
an exec command or printed. Keep the password available when reconnecting;
the configuration stays in the VM for that session.

Any local user who can reach the forward can send a request, so the guest app
must authenticate its users. The forward requires a `Host` header matching its
bound address and port. Loopback bindings accept `localhost` and loopback IP
literals. Existing VMs created without the password configuration must be
terminated and recreated with this script.

## Work, suspend, and reconnect

Use the registered name for later commands:

```bash
microvm shell --name code-server-dev
microvm suspend code-server-dev
microvm resume code-server-dev
```

Suspend preserves guest memory and disk. It can still interrupt network
connections, so reload the browser and restart the local forward when needed.
Inbound endpoint traffic resets the idle timer. An open browser tab alone is
not a measured guarantee that traffic continues; save work and
check VM state when returning to the session.

The example uses the default lifetime and suspended window. Its lifetime
limit is one hour, and its suspended window is ten minutes, despite its
fifteen-minute running idle window. Auto-resume
cannot revive a VM after termination. Set the launch policy deliberately if a longer
session is needed; the service supports a lifetime of up to eight hours.
See [Suspend and resume](../../docs/SUSPEND-RESUME.md) for measured behavior.

## Save results and clean up

Copy the files you need before the VM shuts down:

```bash
microvm cp --tar vm:/workspace ./workspace-backup.tar --name code-server-dev
microvm terminate code-server-dev --wait
```

The backup is a tar file. Review its contents before restoring it on the host.
The reusable image remains after VM termination; delete it separately when
retiring the environment. Snapshot storage is charged for at least one week. Check `microvm ls --remote` and cleanup reports for resources that
remain.

This documentation refresh checked the script and launch defaults locally.
It did not repeat a live AWS or browser session.
