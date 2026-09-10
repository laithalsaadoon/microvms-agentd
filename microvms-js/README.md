# @theagenticguy/microvms

Node bindings over `microvms-core`, the client library for AWS Lambda MicroVMs.

```bash
npm install @theagenticguy/microvms
```

A prebuilt native addon per platform, selected through `optionalDependencies`. No compiler
and no install-time download script runs on a consumer's machine.

Requires Node >= 22.13.

## Async-native

The core's async surface maps straight through: an exported `async` function runs on napi's
managed runtime and returns a real `Promise`, with an error rejecting it. Exec output and
stdin are handed across as `ReadableStream<Uint8Array>` rather than as async iterators a
consumer would have to adapt.

## The traps are in the types

The constraints the platform enforces at runtime are shapes this package refuses to
construct: a raw token cannot be passed where a session is expected, and a dollar amount is
never a bare float. Each planted bypass has a test that goes red if the door reopens.

## Coding agents in a VM

`AgentVm` is the L3 layer over the sandbox: an image with Claude Code and/or Codex CLI in
it, a launch with egress, a Bedrock bearer token minted in process and installed as a file
the agent sources, and one method that hands the agent a task as uid 1000 in `/workspace`.

```ts
const vm = await AgentVm.create(Region.usEast1(), [{ agent: 'codex' }]);
await vm.launch({ imageIdentifier: imageArn, executionRoleArn: role });
await vm.installAccess();
console.log((await vm.promptSync('codex', 'Create hello.py that prints hello, run it.')).stdout);
await vm.terminate();
```

`findImage`, `imageName`, `buildArtifact`, and `buildImage` cover the image, with the S3
upload left to you. `installedAgents`, `installAgentAccess`, and `promptAgent` do the same
over a bare `Session` for a process that holds only the identifier triple.

## Reading

- [Documentation](https://laithalsaadoon.github.io/microvms-agentd/)
- [`docs/EMBEDDING.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/EMBEDDING.md)
- [`docs/TRUST.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/TRUST.md)

## License

Apache-2.0
