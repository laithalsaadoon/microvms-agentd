# microvms-agentd · Sequences

## Build or reuse an image

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:941`
    participant Sandbox
    %% `crates/microvms-app/src/control/ensure.rs:729`
    participant Ensure as ensure
    %% `crates/microvms-app/src/control/ensure.rs:606`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/image.rs:243`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/control/services.rs:17`
    participant Build as BuildServices

    %% `crates/microvms-app/src/sandbox.rs:972`
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
    %% `crates/microvms-app/src/sandbox.rs:1105`
    participant Sandbox
    %% `crates/microvms-app/src/control/microvm.rs:433`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/microvm.rs:466`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/session/mod.rs:221`
    participant Session
    %% `crates/agentd/src/routes.rs:373`
    participant Agent as agentd

    %% `crates/microvms-app/src/sandbox.rs:1205`
    Sandbox->>Plane: run_microvm
    %% `crates/microvms-app/src/control/microvm.rs:466-467`
    Plane->>Lambda: RunMicrovm
    %% `crates/microvms-app/src/control/microvm.rs:468`
    Lambda-->>Plane: accepted VM
    %% `crates/microvms-app/src/control/microvm.rs:469`
    Plane-->>Sandbox: accepted VM
    %% `crates/microvms-app/src/sandbox.rs:1227-1228`
    opt fresh launch, wait
        %% `crates/microvms-app/src/sandbox.rs:1290`
        Sandbox->>Plane: wait running
        loop until RUNNING
            %% `crates/microvms-app/src/control/microvm.rs:724-727`
            Plane->>Lambda: GetMicrovm
        end
        %% `crates/microvms-app/src/control/microvm.rs:677-678`
        Plane-->>Sandbox: running VM
        %% `crates/microvms-app/src/sandbox.rs:1250-1258`
        Sandbox->>Session: builder()
        %% `crates/microvms-app/src/sandbox.rs:1302-1305`
        Sandbox->>Session: wait ready
        loop until ready
            %% `crates/microvms-app/src/session/mod.rs:383-386`
            Session->>Agent: GET /v1/health
            %% `crates/agentd/src/routes.rs:377-411`
            Agent-->>Session: health
        end
        %% `crates/microvms-app/src/session/mod.rs:417-418`
        Session-->>Sandbox: ready
    end
```

## Suspend and resume

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:1584`
    participant Sandbox
    %% `crates/microvms-app/src/control/microvm.rs:794`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/microvm.rs:796`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/session/mod.rs:368`
    participant Session

    %% `crates/microvms-app/src/sandbox.rs:1603`
    Sandbox->>Plane: suspend
    %% `crates/microvms-app/src/control/microvm.rs:796-797`
    Plane->>Lambda: SuspendMicrovm
    %% `crates/microvms-app/src/control/microvm.rs:798`
    Plane-->>Sandbox: accepted
    %% `crates/microvms-app/src/sandbox.rs:1610-1618`
    Sandbox->>Plane: wait_for_state
    loop until settled
        %% `crates/microvms-app/src/control/microvm.rs:675`
        Plane->>Lambda: GetMicrovm
    end
    %% `crates/microvms-app/src/sandbox.rs:1624-1641`
    Plane-->>Sandbox: settled VM
    opt settled SUSPENDED
        %% `crates/microvms-app/src/sandbox.rs:1691`
        Sandbox->>Plane: resume
        %% `crates/microvms-app/src/control/microvm.rs:804-805`
        Plane->>Lambda: ResumeMicrovm
        %% `crates/microvms-app/src/control/microvm.rs:806`
        Plane-->>Sandbox: accepted
        %% `crates/microvms-app/src/sandbox.rs:1697-1705`
        Sandbox->>Plane: wait_for_state
        loop until RUNNING
            %% `crates/microvms-app/src/control/microvm.rs:675`
            Plane->>Lambda: GetMicrovm
        end
        %% `crates/microvms-app/src/control/microvm.rs:677-678`
        Plane-->>Sandbox: running VM
        %% `crates/microvms-app/src/sandbox.rs:1711-1714`
        Sandbox->>Session: rebind
    end
```

## See also

- [processes](../../behavior/processes.md) — 6 shared source citations
- [contract map](../../insights/contract-map.md) — 6 shared source citations
- [module map](../../architecture/module-map.md) — 5 shared source citations
- [components](../architecture/components.md) — 5 shared source citations
- [data flow](../../architecture/data-flow.md) — 4 shared source citations
