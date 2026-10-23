# Optional formal verifier

From the repository root, run:

```sh
mise run spec:setup
mise run spec
mise run spec:core
```

Setup uses Node 24.22.0 only for the formal tasks. The published Node binding
continues to support its documented Node 22 floor. `npm ci` installs the exact
symspec 2.0.0 dependency graph in `package-lock.json`; the package is private and
is never published. `effect`, which symspec depends on, asks for `toml`
`^4.1.2`, and the `overrides` entry in `package.json` replaces it with `toml`
5.0.1, the first release that fixes GHSA-4qx8-w7c3-pj3p, GHSA-9j65-hcg3-gc64 and
GHSA-9qmh-m7c6-f92p. Remove the entry once a symspec release depends on a fixed
`toml`. The wrapper refuses a different verifier version and returns
the verifier's exit status without changing it.

Setup downloads the verifier's semantic model into `model/`, which is ignored.
The symspec distribution pins the model revision and verifies each asset's SHA-256
before using it. Checks use these local assets; they do not silently download a
missing model or skip semantic analysis. Remove `node_modules/` and `model/` to
repeat setup from a clean environment. Do not commit either directory.

The verifier is Apache-2.0 licensed. Its semantic model is MIT licensed:
[model card at the pinned revision](https://huggingface.co/Xenova/bge-base-en-v1.5/blob/4d6cd88e18e51a5e020c2c305726d76ada9c03cf/README.md).
Dependencies retain the licenses in their installed package metadata. The
verifier's pinned npm tarball contains its license and model downloader; no source
or model weights are vendored here.

`spec` and `spec:core` report existing specification findings. Installing this
environment does not resolve those findings or establish strict verification.
The offline `spec:check` task guards the wrapper's version and verdict handling
without installing the optional environment.
