# microvms-agentd · Components

```mermaid
classDiagram
    direction LR

    namespace microvms-cli {
        class CoreSeam {
            <<trait>>
            +control_plane(region)
            +open_sandbox(region, port)
            +attach_session(region, ..)
            +put_artifact(uri, bytes)
        }
    }

    namespace microvms-app {
        class Sandbox {
            +build_image(request)
            +run(request)
            +suspend()
            +resume()
            +terminate(opts)
        }
        class ControlPlane {
            +create_image(request)
            +run_microvm(request)
            +wait_for_running(id, opts)
            +terminate(id)
            +mint_auth_token(id)
        }
        class Session {
            +health()
            +wait_until_ready(timeout)
            +run(req)
            +run_sync(req, timeout)
            +upload_tar(remote, archive)
        }
        class ExecHandle {
            +poll()
            +wait(timeout)
            +stream()
            +write_stdin(data, eof)
            +ack()
        }
    }

    namespace agentd {
        class Routes {
            +app(state)
            +handler_for(endpoint)
            +surface_docs()
            +run_hook(state, body)
            +health(state)
        }
        class AppState {
            +bootstrap(presented, env)
            +token_matches(presented)
            +with_execs(f)
            +disk_guard()
            +identity_report()
        }
        class Confined {
            +open(root)
            +create_dir(parts)
            +create_file(parts)
            +create_symlink(parts, target)
            +set_mode(parts, mode)
        }
    }

    CoreSeam --> ControlPlane : builds
    CoreSeam --> Sandbox : opens
    CoreSeam --> Session : attaches
    Sandbox --> ControlPlane : invokes
    Sandbox --> Session : owns
    Session --> ExecHandle : creates
    Session ..> Routes : requests
    ExecHandle ..> Routes : streams
    Routes --> AppState : carries
    Routes --> Confined : dispatches
```

## Legend

| Node or edge | Citations |
| --- | --- |
| `CoreSeam` | trait `microvms-cli/src/seam.rs:136`; methods `microvms-cli/src/seam.rs:138`, `:141`, `:148`, `:172`; `AwsSeam` impl `:179`, `:183`, `:201`, `:225` |
| `Sandbox` | struct `microvms-app/src/sandbox.rs:602`; methods `:884`, `:1026`, `:1445`, `:1529`, `:1633` |
| `ControlPlane` | struct `microvms-app/src/control/mod.rs:130`; methods `microvms-app/src/control/image.rs:157`, `microvms-app/src/control/microvm.rs:356`, `:435`, `:563`, `:583` |
| `Session` | struct `microvms-app/src/session/mod.rs:174`; methods `:270`, `:283`, `:321`, `:349`, `:385` |
| `ExecHandle` | struct `microvms-app/src/session/exec.rs:213`; methods `:228`, `:248`, `:285`, `:627`, `:657` |
| `Routes` | module of free functions, not a type: `agentd/src/routes.rs:36`, `:110`, `:371`, `:178`, `:314` |
| `AppState` | struct `agentd/src/state.rs:110`; methods `:202`, `:245`, `:257`, `:176`, `:183` |
| `Confined` | struct `agentd/src/fs.rs:297`; methods `:350`, `:416`, `:428`, `:448`, `:535` |
| `CoreSeam --> ControlPlane` | `microvms-cli/src/seam.rs:138`, impl `:179` |
| `CoreSeam --> Sandbox` | `microvms-cli/src/seam.rs:141`, impl `:183` |
| `CoreSeam --> Session` | `microvms-cli/src/seam.rs:148`, impl `:201` |
| `Sandbox --> ControlPlane` | `microvms-app/src/sandbox.rs:65-67`, `:886`, `:1114`, `:1464`, `:1552`, `:1656` |
| `Sandbox --> Session` | `microvms-app/src/sandbox.rs:69`, `:820`, `:1026` |
| `Session --> ExecHandle` | `microvms-app/src/session/mod.rs:321`, `:344` |
| `Session ..> Routes` | `microvms-app/src/session/mod.rs:272`, `:323`; `microvms-app/src/session/files.rs:45`, `:52` |
| `ExecHandle ..> Routes` | `microvms-app/src/session/exec.rs:233`, `:595`, `:662` |
| `Routes --> AppState` | `agentd/src/routes.rs:36` |
| `Routes --> Confined` | `agentd/src/routes.rs:132-135`, `agentd/src/fs.rs:1433`, `:1480`, `:631` |
| `..>` dashed | the HTTP wire, not a crate dependency: the shared contract is the `protocol` crate, re-exported at `microvms-core/src/lib.rs:100` and `agentd/src/routes.rs:18-20`, and the permitted directions are asserted by `microvms-cli/tests/dependency_direction.rs` |
| `+` prefix | the class-diagram marker for a listed member, not a Rust visibility claim: `Confined` and its methods are crate-private (`agentd/src/fs.rs:297`, `:350`), as are `routes::run_hook` and `routes::health` (`agentd/src/routes.rs:178`, `:314`) |

## See also

- [impact analysis](../../insights/impact-analysis.md)
- [processes](../../behavior/processes.md)
- [business logic](../../insights/business-logic.md)
- [sequences](../behavioral/sequences.md)
- [data flow](../../architecture/data-flow.md)
