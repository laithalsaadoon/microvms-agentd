# microvms-agentd · Sequences

## Build or reuse an image

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:948`
    participant Sandbox
    %% `crates/microvms-app/src/control/ensure.rs:729`
    participant Ensure as ensure
    %% `crates/microvms-app/src/control/ensure.rs:606`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/image.rs:243`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/control/services.rs:17`
    participant Build as BuildServices

    %% `crates/microvms-app/src/sandbox.rs:979`
    Sandbox->>Ensure: ensure()
    %% `crates/microvms-app/src/control/ensure.rs:755`
    Ensure->>Plane: describe_image
    %% `crates/microvms-app/src/control/ensure.rs:611`
    Plane->>Lambda: GetMicrovmImage
    %% `crates/microvms-app/src/control/ensure.rs:612-616`
    Lambda-->>Plane: image or absent
    %% `crates/microvms-app/src/control/ensure.rs:755-760`
    Plane-->>Ensure: image or absent
    alt ready, no force
        %% `crates/microvms-app/src/control/ensure.rs:761-763`
        Ensure-->>Sandbox: reused
    else absent image
        %% `crates/microvms-app/src/control/ensure.rs:803-807`
        Ensure->>Build: put_object
        %% `crates/microvms-app/src/control/services.rs:22-27`
        Build-->>Ensure: uploaded
        %% `crates/microvms-app/src/control/ensure.rs:809`
        Ensure->>Plane: create_image
        %% `crates/microvms-app/src/control/image.rs:243-245`
        Plane->>Lambda: POST image
        %% `crates/microvms-app/src/control/image.rs:247-257`
        Plane-->>Ensure: created
        %% `crates/microvms-app/src/control/ensure.rs:810-812`
        Ensure->>Plane: wait_for_image
        loop until ready
            %% `crates/microvms-app/src/control/image.rs:405-408`
            Plane->>Lambda: GetMicrovmImage
        end
        %% `crates/microvms-app/src/control/image.rs:410-423`
        Plane-->>Ensure: ready
        %% `crates/microvms-app/src/control/ensure.rs:814-819`
        Ensure-->>Sandbox: built
    end
```

## Launch and wait for readiness

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:1113`
    participant Sandbox
    %% `crates/microvms-app/src/control/microvm.rs:433`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/microvm.rs:476`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/session/mod.rs:244`
    participant Session
    %% `crates/agentd/src/routes.rs:438`
    participant Agent as agentd

    %% `crates/microvms-app/src/sandbox.rs:1244-1247`
    Sandbox->>Plane: run_microvm_reported
    %% `crates/microvms-app/src/control/microvm.rs:476-485`
    Plane->>Lambda: RunMicrovm
    %% `crates/microvms-app/src/control/microvm.rs:487`
    Lambda-->>Plane: accepted VM
    %% `crates/microvms-app/src/control/microvm.rs:488`
    Plane-->>Sandbox: accepted VM
    %% `crates/microvms-app/src/sandbox.rs:1276-1277`
    opt fresh launch, wait
        %% `crates/microvms-app/src/sandbox.rs:1340`
        Sandbox->>Plane: wait running
        loop until RUNNING
            %% `crates/microvms-app/src/control/microvm.rs:778-781`
            Plane->>Lambda: GetMicrovm
        end
        %% `crates/microvms-app/src/control/microvm.rs:674`
        Plane-->>Sandbox: running VM
        %% `crates/microvms-app/src/sandbox.rs:1299-1309`
        Sandbox->>Session: builder()
        %% `crates/microvms-app/src/sandbox.rs:1359-1362`
        Sandbox->>Session: wait ready
        loop until ready
            %% `crates/microvms-app/src/session/mod.rs:417-420`
            Session->>Agent: GET /v1/health
            %% `crates/agentd/src/routes.rs:442-473`
            Agent-->>Session: health
        end
        %% `crates/microvms-app/src/session/mod.rs:476-486`
        Session-->>Sandbox: ready
    end
```

## Suspend and resume

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:1635`
    participant Sandbox
    %% `crates/microvms-app/src/control/microvm.rs:862`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/microvm.rs:864`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/session/mod.rs:402`
    participant Session

    %% `crates/microvms-app/src/sandbox.rs:1654`
    Sandbox->>Plane: suspend
    %% `crates/microvms-app/src/control/microvm.rs:864-865`
    Plane->>Lambda: SuspendMicrovm
    %% `crates/microvms-app/src/control/microvm.rs:866`
    Plane-->>Sandbox: accepted
    %% `crates/microvms-app/src/sandbox.rs:1657-1665`
    Sandbox->>Plane: wait_for_state
    loop until settled
        %% `crates/microvms-app/src/control/microvm.rs:729`
        Plane->>Lambda: GetMicrovm
    end
    %% `crates/microvms-app/src/sandbox.rs:1671-1688`
    Plane-->>Sandbox: settled VM
    opt settled SUSPENDED
        %% `crates/microvms-app/src/sandbox.rs:1779`
        Sandbox->>Plane: resume
        %% `crates/microvms-app/src/control/microvm.rs:889-890`
        Plane->>Lambda: ResumeMicrovm
        %% `crates/microvms-app/src/control/microvm.rs:891`
        Plane-->>Sandbox: accepted
        %% `crates/microvms-app/src/sandbox.rs:1791-1799`
        Sandbox->>Plane: wait_for_state
        loop until RUNNING
            %% `crates/microvms-app/src/control/microvm.rs:729`
            Plane->>Lambda: GetMicrovm
        end
        %% `crates/microvms-app/src/control/microvm.rs:731-732`
        Plane-->>Sandbox: running VM
        %% `crates/microvms-app/src/sandbox.rs:1805-1808`
        Sandbox->>Session: rebind
    end
```

## See also

These pages cite many of the same source files as this one.

- [processes](../../behavior/processes.md)
- [contract map](../../insights/contract-map.md)
- [module map](../../architecture/module-map.md)
- [components](../architecture/components.md)
- [data flow](../../architecture/data-flow.md)
