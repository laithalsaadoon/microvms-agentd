# agentd

The guest daemon: exec, file transfer, tunnels and lifecycle hooks inside a MicroVM. It
ships as a static `aarch64-unknown-linux-musl` binary (`mise run build`) and depends on
`microvms-protocol` and never on the client crates (ARCH-2). Every rule it enforces is in
`docs/PROTOCOL.md`, next to the defect that made it necessary; read the section before
changing a route.

- The trust boundary is written up in `src/lib.rs`. The platform's `/run` hook arrives from
  `127.0.0.1`, so source-address filtering on bootstrap would break every launch. Don't add
  it. Bootstrap is defended by being one-shot, by refusing a hijack at the hook and at the
  control API, and by keeping the agent token out of exec'd children.
- A change to bootstrap, auth or state transitions needs `cargo test -p agentd-model`, which
  checks those properties over every interleaving of platform, client and in-VM attacker.
- Routes and payloads are generated from the handlers' serde types. After a wire change, run
  `mise run schema` to regenerate `docs/schema.json`; `schema:check` fails on a stale one, and
  the wire types themselves live in `protocol/`.
- Hooks are the names in `hook_handlers::HOOK_NAMES`, run from `/etc/agentd/hooks.d`.
  The `Command::new` sites that run the user's commands and hooks are ratchet decisions;
  a new subprocess here needs the same reasoning recorded.
- The image bootstrap invariant is in the root `AGENTS.md`: `agentd` is the image's `CMD`, and
  workloads start only after readiness.
- `hook_handlers::tests::a_handler_runs_with_its_hook_name_and_its_exit_code_is_recorded` can
  fail with `NotFound` under full-suite load. Rerun it before chasing it.

Tests: `cargo test -p agentd --lib`, the integration tests in `tests/`, and the fuzz target
under `fuzz/`. A daemon change needs a live run before it's called verified.
