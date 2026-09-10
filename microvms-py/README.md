# microvms

Python bindings over `microvms-core`, the client library for AWS Lambda MicroVMs.

```bash
pip install microvms
```

One wheel per platform, built `abi3-py39`, so a single artifact loads on CPython 3.9 and
newer rather than one wheel per interpreter version.

## Typed

The wheel ships `py.typed` and a generated `__init__.pyi` beside the extension, so a checker
reads the surface by the ordinary PEP 561 rules. The stub is generated from the compiled
module and compared against it in CI — a stale stub is worse than none, because it leaves a
caller confidently wrong with an editor approving a call that raises `AttributeError`.

## The traps are in the types

The constraints the platform enforces at runtime are shapes this package refuses to
construct: a raw token cannot be passed where a session is expected, a capability list
cannot be widened after the fact, and a dollar amount is never a bare float. Nine planted
bypasses each have a test that goes red if the door reopens.

## Coding agents in a VM

`AgentVm` is the L3 layer over the sandbox: an image with Claude Code and/or Codex CLI in
it, a launch with egress, a Bedrock bearer token minted in process and installed as a file
the agent sources, and one method that hands the agent a task as uid 1000 in `/workspace`.

```python
vm = microvms.AgentVm(microvms.Region.us_east_1(), [microvms.AgentSpec.codex()])
vm.launch(image_identifier=image_arn, execution_role_arn=role)
vm.install_access()
print(vm.prompt_sync("codex", "Create hello.py that prints hello, run it.").stdout)
vm.terminate()
```

`find_image`, `image_name`, `build_artifact`, and `build_image` cover the image, with the
S3 upload left to you. `installed_agents`, `install_agent_access`, and `prompt_agent` do the
same over a bare `Session` for a process that holds only the identifier triple.

## Reading

- [Documentation](https://laithalsaadoon.github.io/microvms-agentd/)
- [`docs/EMBEDDING.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/EMBEDDING.md)
- [`docs/TRUST.md`](https://github.com/laithalsaadoon/microvms-agentd/blob/main/docs/TRUST.md)

## License

Apache-2.0
