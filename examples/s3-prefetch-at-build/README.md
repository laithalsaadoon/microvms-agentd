# Prefetch S3 data into an image

This example attempts to copy an S3 prefix into `/opt/prefetch` before the
image snapshot is captured. A successful snapshot can then supply those files
to later VMs without another startup download. The workflow is experimental:
network access and credentials in the snapshot-build process have not been
verified here against live AWS.

## Run the example

Complete the repository's [AWS setup](https://laithalsaadoon.github.io/microvms-agentd/learn/tutorial/first-run/).
The script expects `microvm` and a locally built ARM64 daemon, or its path in
`AGENTD`. From the repository root:

```bash
mise run build
PREFETCH_URI=s3://my-bucket/models/ bash examples/s3-prefetch-at-build/run.sh
# For a public bucket, skip request signing:
PREFETCH_URI=s3://a-public-bucket/prefix/ PREFETCH_NO_SIGN=1 \
  bash examples/s3-prefetch-at-build/run.sh
```

These commands create billable AWS resources. A public bucket avoids the need
for credentials on the download, but still needs working network access in
the build process.

## Startup wrapper

[Dockerfile](Dockerfile) installs AWS CLI v2 and creates `/start.sh`. That
wrapper runs `aws s3 sync`, changes the downloaded files' ownership to uid
1000, and then uses `exec /agentd`. Agentd cannot answer the build's readiness
hook until the copy completes. The snapshot process follows readiness; the
ordering is the reason to put this work before starting the daemon.

The image's `CMD` is the wrapper, which must finish initialization and hand
control to agentd. It must not start task workloads before launch bootstrap.
The repository's ordinary images use agentd directly as `CMD`.

The wrapper runs with `set -e`, so a failed copy stops it instead of producing
a supposedly ready daemon. Read the build record and logs to determine the
actual failure. A timeout alone does not establish a credential failure.
`microvm logs <image-name>` prints the log-reading command.

## Build environment and time limits

The repository has observed outbound package downloads during the container
build phase. That does not establish the same network or credential behavior
in the later snapshot phase. For a private bucket, the AWS CLI uses its
credential chain, and the build role needs the intended read permission if
its credentials are supplied there. Do not bake credentials into the image.

[Platform](../../docs/PLATFORM.md) records an observed readiness timeout of
five minutes, despite the larger hook limit present in the service model.
Measure your transfer within the real build budget. The snapshot pass can
run for multiple chipset variants, so do not assume one download per image.

A container `RUN` step is another place to prepare data when its build-time
access is established. Its files also enter the image. Choose that placement
when initialization does not require the snapshot process.

## Verify the restored data

After a successful build, the script launches the image and inspects
`/opt/prefetch` as uid 1000. Confirm the expected files and contents, rather
than only checking that the directory exists.

[microvm.toml](microvm.toml) leaves `egress` false. That omits the managed
internet connector but does not enforce internet isolation. The example
shows how to avoid a required startup S3 call; it does not establish that the
VM cannot make one. See [Networking](../../docs/NETWORKING.md).

## Reuse, changes, and cleanup

The script writes the URI and signing mode into the Dockerfile it builds.
Those values affect the reusable image name. Content changing at the same S3
URI does not change that name, so an existing image can retain old data. Use
versioned input locations or deliberately rebuild when the data changes.

Shared snapshots must contain only data appropriate for every VM launched
from them. Deliver per-VM credentials after launch. The downloaded bytes also
increase snapshot storage and read costs; select only what the workload
needs.

Inspect the script's cleanup result and check remaining resources. Images are
retained for reuse and can be deleted when retired; their snapshot storage has
a one-week minimum retention charge. This refresh was checked locally;
AWS execution remains unverified.
