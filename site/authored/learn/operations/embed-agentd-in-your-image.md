---
title: Embed agentd in your own image
description: Append the daemon to an arbitrary task image so your own harness can drive it over the published wire protocol, and the pointers a harness client needs.
editUrl: false
sidebar:
  order: 2
---

Use agentd to run commands and move files through a VM's HTTPS endpoint.
Build the image first. Launch it, wait for readiness, then start work through
a session. Use the shared SDKs when possible; [Embedding](/internals/embedding/)
covers their lifecycle and cleanup rules.

## Build the image

```bash
microvm dockerfile --workdir /workspace > Dockerfile
# Add your package-install steps before the generated ENV and CMD lines.
microvm build --dockerfile Dockerfile --name my-task-image
```

The generated file is the same one the default build uses. Add the tools your
work needs and keep its daemon lines. Use the managed base image, an explicit
`WORKDIR`, `ENTRYPOINT []`, and `CMD ["/agentd"]`. The client checks the base
and working directory before it calls AWS. Keep task commands out of image
startup: they must wait for per-VM setup.

Do not put secrets in the image. A shared image supplies the same files and
saved memory to each VM. Send per-VM credentials after launch.
[Write a guest Dockerfile](/learn/operations/write-a-guest-dockerfile/) explains
the build checks and how to diagnose failures.

## Connect a client

The SDKs handle launch, bootstrap, proxy tokens, and the daemon connection.
For a custom client, read [Protocol](/internals/protocol/) and the
[wire schema](/reference/wire-schema/). `GET /v1/schema` serves the schema
without a daemon token. Access through the endpoint still needs proxy auth.

The platform sends `runHookPayload` to `/run` at launch. It sets the daemon's
agent token. Protected calls then need both that bearer token and the proxy
headers. Refresh proxy tokens before they expire.

Keep the ID of each command you start. Reuse it to reconcile an uncertain
start, and use a byte cursor to resume a stream. Check for output gaps and
terminal outcomes. Ack the result after reading it. The daemon limits how
much output it retains and how long it retains it.

If the VM must remain ready between tasks, poll its health endpoint from
outside the guest. Inbound traffic resets the idle timer. A guest-side poll
does not. The launch policy still sets the VM's maximum lifetime.

## Obtain the daemon from a library

A client that builds its own archive can obtain the daemon bytes:

```python
import microvms

agentd = microvms.provision_agentd()
```

```js
import { provisionAgentd } from 'microvms';

const agentd = await provisionAgentd();
```

The call checks a supplied `binary`, then `MICROVM_AGENTD`, then the local
cache, before fetching the matching release. It checks that the binary is
ARM64 ELF. The release path verifies the asset in the client.
`provision_agentd_report()` in Python and `provisionAgentdReport()` in Node
also return its source, verification result, path, and SHA-256.

Pass the file to `build` or `run`, or set `MICROVM_AGENTD`.
`microvm doctor --binary ./agentd` checks its format before a build.
[Harness capabilities](/internals/harness-capabilities/) maps the current APIs
to integration needs.
