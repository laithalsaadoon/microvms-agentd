# microvms-agentd · Sequences

## Build or reuse an image

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:943`
    participant Sandbox
    %% `crates/microvms-app/src/control/ensure.rs:729`
    participant Ensure as ensure
    %% `crates/microvms-app/src/control/ensure.rs:606`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/image.rs:243`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/control/services.rs:17`
    participant Build as BuildServices

    %% `crates/microvms-app/src/sandbox.rs:974`
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
    %% `crates/microvms-app/src/sandbox.rs:1108`
    participant Sandbox
    %% `crates/microvms-app/src/control/microvm.rs:433`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/microvm.rs:466`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/session/mod.rs:221`
    participant Session
    %% `crates/agentd/src/routes.rs:414`
    participant Agent as agentd

    %% `crates/microvms-app/src/sandbox.rs:1208`
    Sandbox->>Plane: run_microvm
    %% `crates/microvms-app/src/control/microvm.rs:466-467`
    Plane->>Lambda: RunMicrovm
    %% `crates/microvms-app/src/control/microvm.rs:468`
    Lambda-->>Plane: accepted VM
    %% `crates/microvms-app/src/control/microvm.rs:469`
    Plane-->>Sandbox: accepted VM
    %% `crates/microvms-app/src/sandbox.rs:1230-1231`
    opt fresh launch, wait
        %% `crates/microvms-app/src/sandbox.rs:1293`
        Sandbox->>Plane: wait running
        loop until RUNNING
            %% `crates/microvms-app/src/control/microvm.rs:724-727`
            Plane->>Lambda: GetMicrovm
        end
        %% `crates/microvms-app/src/control/microvm.rs:677-678`
        Plane-->>Sandbox: running VM
        %% `crates/microvms-app/src/sandbox.rs:1253-1261`
        Sandbox->>Session: builder()
        %% `crates/microvms-app/src/sandbox.rs:1305-1308`
        Sandbox->>Session: wait ready
        loop until ready
            %% `crates/microvms-app/src/session/mod.rs:383-386`
            Session->>Agent: GET /v1/health
            %% `crates/agentd/src/routes.rs:418-449`
            Agent-->>Session: health
        end
        %% `crates/microvms-app/src/session/mod.rs:417-418`
        Session-->>Sandbox: ready
    end
```

## Suspend and resume

```mermaid
sequenceDiagram
    %% `crates/microvms-app/src/sandbox.rs:1587`
    participant Sandbox
    %% `crates/microvms-app/src/control/microvm.rs:794`
    participant Plane as ControlPlane
    %% `crates/microvms-app/src/control/microvm.rs:796`
    participant Lambda as Lambda MicroVMs
    %% `crates/microvms-app/src/session/mod.rs:368`
    participant Session

    %% `crates/microvms-app/src/sandbox.rs:1606`
    Sandbox->>Plane: suspend
    %% `crates/microvms-app/src/control/microvm.rs:796-797`
    Plane->>Lambda: SuspendMicrovm
    %% `crates/microvms-app/src/control/microvm.rs:798`
    Plane-->>Sandbox: accepted
    %% `crates/microvms-app/src/sandbox.rs:1613-1621`
    Sandbox->>Plane: wait_for_state
    loop until settled
        %% `crates/microvms-app/src/control/microvm.rs:675`
        Plane->>Lambda: GetMicrovm
    end
    %% `crates/microvms-app/src/sandbox.rs:1627-1644`
    Plane-->>Sandbox: settled VM
    opt settled SUSPENDED
        %% `crates/microvms-app/src/sandbox.rs:1694`
        Sandbox->>Plane: resume
        %% `crates/microvms-app/src/control/microvm.rs:804-805`
        Plane->>Lambda: ResumeMicrovm
        %% `crates/microvms-app/src/control/microvm.rs:806`
        Plane-->>Sandbox: accepted
        %% `crates/microvms-app/src/sandbox.rs:1700-1708`
        Sandbox->>Plane: wait_for_state
        loop until RUNNING
            %% `crates/microvms-app/src/control/microvm.rs:675`
            Plane->>Lambda: GetMicrovm
        end
        %% `crates/microvms-app/src/control/microvm.rs:677-678`
        Plane-->>Sandbox: running VM
        %% `crates/microvms-app/src/sandbox.rs:1714-1717`
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
