# SPDX-License-Identifier: Apache-2.0
"""The suite's own image: the Dockerfile the shared VM is built from, and its base.

The image bakes a WORKDIR, a named user and one image `ENV` line, so the exec sections have a
working directory to inherit, a passwd row to resolve and a variable to inherit or not.
"""

from __future__ import annotations

from harness.constants import AGENT_PORT

# Every public ARM64 base we measured (al2023-minimal, python:3.12-slim,
# node:20-slim, 2026-08-05) leaves WorkingDir empty, so a baked WORKDIR is the only
# way to test cwd inheritance at all. The deleted oracle used the same value.
BAKED_WORKDIR = "/opt/baked-workdir"
# A named user and one image ENV line, baked so `drive_exec_start_protocol` (#224, #225,
# #226) has a passwd row to resolve and an image variable to inherit. Written into
# /etc/passwd and /etc/group directly: al2023-minimal ships no `useradd`.
CONFORMANCE_USER = "conformance"
CONFORMANCE_UID = 4242
CONFORMANCE_HOME = "/home/conformance"
IMAGE_ENV_KEY = "MICROVMS_CONFORMANCE_IMAGE_ENV"
IMAGE_ENV_VALUE = "from-image"


#: The managed base image the Rust client defaults to.
DEFAULT_BASE_IMAGE = "al2023-1"

#: Managed base image name -> the Dockerfile `FROM` that pairs with it. A map rather
#: than one loose literal because the two must agree and used to be able to disagree:
#: the *name* goes into `baseImageArn`, the *ref* goes into the Dockerfile `FROM`, and
#: `microvms-core` refuses a Dockerfile whose `FROM` disagrees with the create call's
#: `baseImageArn` (`control/artifact.rs:233`). Pairing them means selecting one thing
#: and having both follow, which is the shape the deleted `sandbox.BASE_IMAGES` had.
#:
#: `al2023-1` is the managed base every measurement in `docs/PLATFORM.md` from
#: 2026-08-06 onward used, paired with the `amazonlinux:2023-minimal` registry ref the
#: same builds used as `FROM`. Literals here rather than a read of
#: `microvm constants --emit-json`, because that dump carries API constraints and not
#: base images — see `resolve_base_ref` for what that costs.
BASE_IMAGE_REFS = {
    "al2023-1": "public.ecr.aws/amazonlinux/amazonlinux:2023-minimal",
}


def conformance_dockerfile(base_ref: str) -> str:
    """The image recipe, with a WORKDIR baked in so cwd inheritance is testable.

    The `FROM` is taken from the CLI's own manifest rather than written here, because
    `microvms-core` refuses a Dockerfile whose `FROM` disagrees with the `baseImageArn`
    the create call sends (`control/artifact.rs:233`) — and that refusal is correct, so
    hardcoding a ref here would make this suite fail on a base-image change with a
    message about a Dockerfile rather than about a base image.
    """
    return "\n".join(
        [
            f"FROM {base_ref}",
            "COPY agentd /agentd",
            "RUN chmod 0755 /agentd",
            f"RUN mkdir -p {BAKED_WORKDIR}",
            f"WORKDIR {BAKED_WORKDIR}",
            (
                f"RUN echo '{CONFORMANCE_USER}:x:{CONFORMANCE_UID}:{CONFORMANCE_UID}:"
                f"Conformance:{CONFORMANCE_HOME}:/bin/sh' >> /etc/passwd"
                f" && echo '{CONFORMANCE_USER}:x:{CONFORMANCE_UID}:' >> /etc/group"
                f" && mkdir -p {CONFORMANCE_HOME}"
                f" && chown {CONFORMANCE_UID}:{CONFORMANCE_UID} {CONFORMANCE_HOME}"
            ),
            f"ENV {IMAGE_ENV_KEY}={IMAGE_ENV_VALUE}",
            f"ENV AGENTD_PORT={AGENT_PORT}",
            "ENV AGENTD_LOG=info",
            f"EXPOSE {AGENT_PORT}",
            "ENTRYPOINT []",
            'CMD ["/agentd"]',
            "",
        ]
    )


def resolve_base_ref() -> str:
    """The Dockerfile `FROM` the Rust client pairs with its default base image.

    A module table (`BASE_IMAGE_REFS`) rather than a value read out of the client under
    test, which is a real limitation and worth naming. It used to be read from the
    Python client's `BASE_IMAGES`, held equal to the Rust one by `check-model-drift`'s
    cross-comparison; that table went with the client. Reading it from
    `microvm constants --emit-json` would be better and is not possible — that dump
    carries API constraints, not base images.

    The failure mode is bounded and loud. If `microvms-core`'s default base image
    changes and this table does not, the build fails on `control/artifact.rs:233`'s
    refusal — the `FROM` disagreeing with `baseImageArn` — which names both values.
    That is a suite that fails to run rather than one that passes wrongly, and it is
    the same shape the check already had when the two tables could disagree. A base
    image not in the table fails here instead, before anything is launched.
    """
    try:
        return BASE_IMAGE_REFS[DEFAULT_BASE_IMAGE]
    except KeyError:
        raise SystemExit(
            f"no Dockerfile FROM paired with base image {DEFAULT_BASE_IMAGE!r}. The name "
            "alone does not say what `FROM` goes with it, and guessing is how the two fell "
            f"out of step before — add the pair to BASE_IMAGE_REFS (have: "
            f"{', '.join(sorted(BASE_IMAGE_REFS))})."
        ) from None
