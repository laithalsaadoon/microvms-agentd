# microvms-agentd · Sequences

Processes that cross the client/daemon HTTP boundary. Each participant is one module or
one external actor; each edge is one call site.

## Exec start, stream, and ack

```mermaid
sequenceDiagram
    participant CLI as microvm CLI
    participant Core as core session
    participant Auth as ProxyAuth
    participant Routes as agentd exec routes
    participant Ring as Shared ring
    participant Child as child pgroup

    CLI ->> Core: run(argv)
    Core ->> Auth: headers()
    Core ->> Routes: POST exec/start
    Routes ->> Child: spawn pgid
    Routes ->> Ring: register entry
    Routes -->> Core: 200 running
    Child ->> Ring: publish bytes
    Core ->> Routes: GET ?offset=N
    Routes ->> Ring: attach(offset)
    Routes -->> Core: output events
    Core -->> CLI: bytes + cursor
    Routes -->> Core: exit event
    Core ->> Routes: POST exec/ack
    Routes -->> Core: released output
```

Participants:

- `microvm CLI` — the `exec`, `exec --stream`, and `ack` subcommands
  (`crates/microvms-cli/src/commands/attached.rs:136`, `crates/microvms-cli/src/commands/attached.rs:253`,
  `crates/microvms-cli/src/commands/attached.rs:622`).
- `core session` — `Session` plus `ExecHandle`, banded because they share one module
  (`crates/microvms-app/src/session/mod.rs:322`, `crates/microvms-app/src/session/exec.rs:218`).
- `ProxyAuth` — the proxy-token cache whose mint sits inside the request path
  (`crates/microvms-app/src/session/proxy.rs:405`, `crates/microvms-app/src/session/mod.rs:79`).
- `agentd exec routes` — `start`, `stream`, `ack` (`crates/agentd/src/exec.rs:331`,
  `crates/agentd/src/exec.rs:455`, `crates/agentd/src/exec.rs:831`).
- `Shared ring` — the replay ring plus the broadcast channel, keyed by exec id
  (`crates/agentd/src/exec.rs:220`).
- `child pgroup` — the spawned process group (`crates/agentd/src/exec.rs:1113`).

Edges in order:

1. `run(argv)` — `crates/microvms-cli/src/commands/attached.rs:165`.
2. `headers()` — the mint runs inside `Transport::headers`, so every request re-checks freshness
   (`crates/microvms-app/src/session/mod.rs:83`, `crates/microvms-app/src/session/mod.rs:106`).
3. `POST exec/start` — `crates/microvms-app/src/session/mod.rs:324`; the handle is built from the id the
   daemon confirmed (`crates/microvms-app/src/session/mod.rs:336`).
4. `spawn pgid` — the pgid is captured while `Child::id()` still answers
   (`crates/agentd/src/exec.rs:1113`, `crates/agentd/src/exec.rs:1119`).
5. `register entry` — the registry insert makes the id addressable (`crates/agentd/src/exec.rs:1141`).
6. `200 running` — `crates/agentd/src/exec.rs:380`; a retried start returns the same 200 without a second
   child (`crates/agentd/src/exec.rs:366`).
7. `publish bytes` — `Capped::pump` into `Shared::publish`, which appends to the ring and fans out
   live under one lock (`crates/agentd/src/exec.rs:1366`, `crates/agentd/src/exec.rs:255`).
8. `GET ?offset=N` — `ExecHandle::attach` builds `/v1/exec/{id}/stream?offset=`, mints its own
   headers because the streaming path bypasses `Transport::request`, and is re-entered per
   reconnect (`crates/microvms-app/src/session/exec.rs:600`,
   `crates/microvms-app/src/session/exec.rs:608`, `crates/microvms-app/src/session/exec.rs:499`).
9. `attach(offset)` — subscribe-before-snapshot, enforced by one lock so the unsafe order is not
   expressible from the handler (`crates/agentd/src/exec.rs:474`, `crates/agentd/src/exec.rs:293`).
10. `output events` — base64 `output` frames carrying the offset of their first byte
    (`crates/agentd/src/exec.rs:642`); a lagged or evicted range comes through as a typed `gap`
    (`crates/agentd/src/exec.rs:656`).
11. `bytes + cursor` — the cursor advances only past bytes handed over, and past a gap's `to`
    (`crates/microvms-app/src/session/exec.rs:534`, `crates/microvms-app/src/session/exec.rs:551`);
    the CLI writes an NDJSON line plus the raw bytes
    (`crates/microvms-cli/src/commands/attached.rs:281`).
12. `exit event` — the terminal marker is written before the result slot, so a stream that sees
    `Finished` always finds an exit event (`crates/agentd/src/exec.rs:535`,
    `crates/agentd/src/exec.rs:1182`).
13. `POST exec/ack` — `crates/microvms-app/src/session/exec.rs:662`; `wait_and_ack` returns the ack's
    result rather than a post-ack poll (`crates/microvms-app/src/session/exec.rs:695`).
14. `released output` — the result slot is taken once and `acked_at` is set while the slot lock is
    still held (`crates/agentd/src/exec.rs:863`, `crates/agentd/src/exec.rs:867`).

Stdin is a separate request, never multiplexed onto this connection
(`crates/microvms-app/src/session/exec.rs:632`, `crates/agentd/src/exec.rs:682`).

## Tar upload and extraction

```mermaid
sequenceDiagram
    participant CLI as microvm cp --tar
    participant Tx as Transport
    participant Routes as agentd fs routes
    participant Guard as disk Guard
    participant Conf as Confined
    participant FS as VM filesystem

    CLI ->> Tx: upload_tar()
    Tx ->> Routes: PUT /v1/fs/tar
    Routes ->> Guard: preflight(root)
    Guard -->> Routes: disk reading
    Routes ->> Guard: spool body
    Guard -->> Routes: spool file
    Routes ->> Conf: extract_into
    Conf ->> FS: openat root
    Conf ->> FS: create member
    Conf ->> Guard: pace bytes
    Conf ->> FS: deferred modes
    Conf -->> Routes: members count
    Routes -->> Tx: 204 No Content
    Tx -->> CLI: bytes uploaded
```

Participants:

- `microvm cp --tar` — resolves direction from the `vm:` prefix and inspects no archive
  (`crates/microvms-cli/src/commands/attached.rs:821`, `crates/microvms-cli/src/commands/attached.rs:825`).
- `Transport` — `files::upload_tar` plus the shared send path
  (`crates/microvms-app/src/session/files.rs:98`, `crates/microvms-app/src/session/mod.rs:97`).
- `agentd fs routes` — `write_tar` (`crates/agentd/src/fs.rs:1433`).
- `disk Guard` — the reserve-aware probe, the body spool, and the pacer
  (`crates/agentd/src/fs.rs:1454`, `crates/agentd/src/fs.rs:872`, `crates/agentd/src/disk.rs:170`).
- `Confined` — the `openat2`-based extractor, the one confined write path
  (`crates/agentd/src/fs.rs:297`, `crates/agentd/src/fs.rs:621`).
- `VM filesystem` — the extraction root inside the guest.

Edges in order:

1. `upload_tar()` — `crates/microvms-cli/src/commands/attached.rs:845`.
2. `PUT /v1/fs/tar` — content type `application/x-tar`; the client does not inspect the archive,
   so the daemon's extractor stays the only implementation of the member rules
   (`crates/microvms-app/src/session/files.rs:103`, `crates/microvms-app/src/session/files.rs:94`).
3. `preflight(root)` — run against the extraction root before the body is spooled, so an upload
   aimed at a full filesystem is refused without spending the wire time
   (`crates/agentd/src/fs.rs:1459`).
4. `disk reading` — a reading below the reserve becomes 507 naming the path
   (`crates/agentd/src/fs.rs:1460`, `crates/agentd/src/fs.rs:106`).
5. `spool body` — the archive lands in full before a single member is extracted
   (`crates/agentd/src/fs.rs:1463`, `crates/agentd/src/fs.rs:872`).
6. `spool file` — spool pressure and a truncated body are distinct outcomes, 507 and 400
   (`crates/agentd/src/fs.rs:1469`, `crates/agentd/src/fs.rs:1475`).
7. `extract_into` — inside `spawn_blocking`, because `tar`'s reader is blocking
   (`crates/agentd/src/fs.rs:1479`, `crates/agentd/src/fs.rs:621`).
8. `openat root` — one confined root held for the whole extraction, so a component that turns out
   to be a symlink stops the write instead of redirecting it (`crates/agentd/src/fs.rs:631`,
   `crates/agentd/src/fs.rs:350`).
9. `create member` — `resolve_member` refuses an escaping path and a non-directory naming the root;
   device and fifo members are refused; an absolute link target is refused
   (`crates/agentd/src/fs.rs:679`, `crates/agentd/src/fs.rs:704`, `crates/agentd/src/fs.rs:726`,
   `crates/agentd/src/fs.rs:783`).
10. `pace bytes` — checked after each member lands, and extraction is not transactional by design
    (`crates/agentd/src/fs.rs:803`).
11. `deferred modes` — replayed deepest-first after all content has landed, so a directory packed
    `0o500` does not block the writes into it (`crates/agentd/src/fs.rs:810`,
    `crates/agentd/src/fs.rs:825`).
12. `members count` — `crates/agentd/src/fs.rs:1485`.
13. `204 No Content` — `crates/agentd/src/fs.rs:1487`.
14. `bytes uploaded` — `crates/microvms-cli/src/commands/attached.rs:851`.

## Daemon bootstrap through the run hook

```mermaid
sequenceDiagram
    participant Sandbox
    participant Plane as ControlPlane
    participant AWS as AWS lambda-microvms
    participant Hook as agentd open router
    participant State as AppState
    participant Session
    participant Guard as agentd auth guard

    Sandbox ->> Sandbox: mint 32 bytes
    Sandbox ->> Plane: run_microvm()
    Plane ->> AWS: RunMicrovm
    AWS ->> Hook: POST run hook
    Hook ->> State: bootstrap(tok)
    Hook -->> AWS: 200 installed
    Sandbox ->> Plane: wait RUNNING
    Plane ->> AWS: GetMicrovm
    AWS -->> Sandbox: RUNNING + url
    Sandbox ->> Session: builder(token)
    Session ->> Guard: Bearer request
    Guard ->> State: token_matches()
    State -->> Guard: 503/401/pass
```

Participants:

- `Sandbox` — the client lifecycle object outside the VM
  (`crates/microvms-app/src/sandbox.rs:1074`).
- `ControlPlane` — the signed AWS client (`crates/microvms-app/src/control/microvm.rs:356`).
- `AWS lambda-microvms` — the service, which calls the hook over loopback inside the VM
  (`crates/agentd/src/routes.rs:168`).
- `agentd open router` — the unauthenticated half of the router, holding the lifecycle hooks
  (`crates/agentd/src/routes.rs:48`, `crates/agentd/src/routes.rs:178`).
- `AppState` — the one-shot token slot and the launch-environment map
  (`crates/agentd/src/state.rs:202`).
- `Session` — the client bound to the reported endpoint with the same token
  (`crates/microvms-app/src/sandbox.rs:1162`).
- `agentd auth guard` — `require_token`, applied as a `route_layer` over every control route
  (`crates/agentd/src/auth.rs:62`, `crates/agentd/src/routes.rs:66`).

Edges in order:

1. `mint 32 bytes` — 32 bytes of `/dev/urandom` rendered as 64 hex characters, unless the caller
   supplied a token (`crates/microvms-app/src/sandbox.rs:1074`,
   `crates/microvms-app/src/sandbox.rs:1805`).
2. `run_microvm()` — the payload is validated before the launch, so an over-ceiling one fails with
   a byte count rather than as a service `ValidationException`
   (`crates/microvms-app/src/sandbox.rs:1092`, `crates/microvms-app/src/sandbox.rs:1116`).
3. `RunMicrovm` — `crates/microvms-app/src/control/microvm.rs:423`.
4. `POST run hook` — unauthenticated by necessity: the platform has no credential to present, and
   its request arrives over loopback indistinguishably from an in-VM process
   (`crates/agentd/src/routes.rs:168`, `crates/agentd/src/routes.rs:178`). A body that is not JSON is 400,
   never 404 (`crates/agentd/src/routes.rs:187`).
5. `bootstrap(tok)` — the token and the launch environment arrive in one payload and are taken as
   two arguments, so no path can move a byte from the first into the second
   (`crates/agentd/src/routes.rs:213`, `crates/agentd/src/state.rs:202`). The env is installed only for the
   first caller (`crates/agentd/src/state.rs:210`).
6. `200 installed` — an identical replay is also 200, because the platform may retry its own hook;
   a different token is 409 (`crates/agentd/src/routes.rs:224`, `crates/agentd/src/routes.rs:230`).
7. `wait RUNNING` — `crates/microvms-app/src/sandbox.rs:1192`.
8. `GetMicrovm` — polled until RUNNING, failing fast on a terminal state
   (`crates/microvms-app/src/control/microvm.rs:459`, `crates/microvms-app/src/control/microvm.rs:465`,
   `crates/microvms-app/src/control/microvm.rs:510`).
9. `RUNNING + url` — RUNNING is what reports the hook succeeded, so this is where
   `token_installed` and `bootstrap_count` move (`crates/microvms-app/src/sandbox.rs:1200`).
10. `builder(token)` — the same minted token becomes the session bearer
    (`crates/microvms-app/src/sandbox.rs:1162`).
11. `Bearer request` — the guard runs before the body is polled, and drains a bounded prefix on
    rejection (`crates/agentd/src/auth.rs:62`, `crates/agentd/src/auth.rs:87`).
12. `token_matches()` — constant-time comparison against the installed slot
    (`crates/agentd/src/auth.rs:75`, `crates/agentd/src/state.rs:214`).
13. `503/401/pass` — three-valued: not-yet-bootstrapped is 503, a wrong credential is 401, and a
    match falls through to the handler (`crates/agentd/src/auth.rs:73`, `crates/agentd/src/auth.rs:77`).

## See also

- [data flow](../../architecture/data-flow.md)
- [processes](../../behavior/processes.md)
- [business logic](../../insights/business-logic.md)
- [debugging guide](../../insights/debugging-guide.md)
- [components](../architecture/components.md)
