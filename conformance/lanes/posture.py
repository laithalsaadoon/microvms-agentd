# SPDX-License-Identifier: Apache-2.0
"""What "no egress" means on this platform: what a connector-less VM reaches, the MMDS
and the execution role it serves, and the egress posture the CLI envelope and the core session
behind the bindings each report."""

from __future__ import annotations

import os
import secrets
import subprocess
from typing import Any

from harness.cli import Cli, attach_args
from harness.constants import BASELINE_MEMORY_MIB, REPO
from harness.envelope import Envelope
from harness.redact import command_for_log
from harness.results import Results

MMDS = "http://169.254.169.254"
#: `curl -w '%{http_code}'` prints this when no connection was made at all. A guest that
#: the platform had sealed would answer it, or a `curl=<exit>` marker, for every host.
NO_CONNECTION = "000"
#: The credential document measured 2026-09-11 and 2026-09-12 was 1164 bytes: `Code`,
#: `LastUpdated`, `Type`, `AccessKeyId`, `SecretAccessKey`, a ~950-byte `Token`,
#: `Expiration`. A body under this floor is not a credential document, whatever its status.
CREDENTIAL_DOCUMENT_FLOOR_BYTES = 500
#: A package registry, probed because it is the *harm* class rather than a reachability
#: curiosity: an external review of a VM launched without `--egress` had DuckDB fetch a
#: 242 MB extension from `extensions.duckdb.org` at query time, and read the download as a
#: client defect because the run envelope said `egress: false` and nothing said what that
#: meant. `pypi.org` stands in for the class — a registry, TLS, a 200 — and it is a
#: different network path from `example.com`, so a platform that started sealing only the
#: plainest host would not pass both.
PACKAGE_REGISTRY = "https://pypi.org"
#: What `microvm run` must report for a VM launched with no egress connector. `unsealed`,
#: not `sealed`: the omission is the strongest request `RunMicrovm` accepts and the platform
#: gives such a VM outbound network anyway. Flipping
#: `microvms_core::control::PLATFORM_HONOURS_OMITTED_EGRESS` is what changes this, and the
#: reach check above is what earns the flip.
CONNECTORLESS_POSTURE = "unsealed"
#: The advisory deny's black hole, as `--deny-egress` sets it
#: (`crates/microvms-app/src/sandbox.rs`, `DENY_EGRESS_PROXY_URL`). Loopback port 1, privileged,
#: nothing serving it.
DENY_EGRESS_PROXY_URL = "http://127.0.0.1:1"
#: The API family the conformance execution role may grant. Resources are checked
#: separately against the service log namespace in the same region and account.
EXECUTION_ROLE_ACTION_PREFIX = "logs:"


def _guest_curl(cli: Cli, attach: list[str], script: str, *exec_flags: str) -> str:
    """One shell line in the guest built around `curl`, returning its stdout stripped.

    Every caller writes `-o /dev/null` and `-w` with a status or a size, never a body:
    the credential path serves a live secret, and this suite's report is printed.
    """
    got = cli.call("exec", script, *attach, "--timeout", "60", *exec_flags)
    return (got.data.get("stdout") or "").strip()


def _status(url: str, *curl_flags: str) -> str:
    flags = " ".join(curl_flags)
    return (
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 {flags} {url} "
        f"|| echo curl=$?"
    )


#: The IMDSv2 handshake, as one shell line: the PUT's status and the token's LENGTH,
#: then the token is used for `$1` and discarded. The token itself is never printed.
MMDS_HANDSHAKE = (
    "code=$(curl -s -o /tmp/.mmds -w '%{http_code}' --max-time 5 -X PUT "
    f"{MMDS}/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "
    "|| echo curl=$?); T=$(cat /tmp/.mmds 2>/dev/null); rm -f /tmp/.mmds; "
    'echo "$code ${#T}"; '
)


def execution_role_actions(
    iam: Any, role_arn: str, region: str
) -> tuple[list[str], list[str], list[str]]:
    """Read every inline grant, attached policy, and resource outside the log namespace.

    IAM returns either strings or lists for Action and Resource. NotAction is kept
    distinct from logs actions, and NotResource never establishes the required scope.
    Read all pages so the verdict covers the whole role, not just the first page.
    """
    role_name = role_arn.rsplit("/", 1)[-1]
    arn = role_arn.split(":", 5)
    log_prefix = f"arn:{arn[1]}:logs:{region}:{arn[4]}:log-group:/aws/lambda-microvms/"
    actions: list[str] = []
    outside_namespace: list[str] = []
    for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
        for policy_name in page["PolicyNames"]:
            document = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)[
                "PolicyDocument"
            ]
            statements = document.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]
            for statement in statements:
                if statement.get("Effect") != "Allow":
                    continue
                action = statement.get("Action", [])
                actions.extend([action] if isinstance(action, str) else list(action))
                not_action = statement.get("NotAction", [])
                actions.extend(
                    f"NotAction:{name}"
                    for name in (
                        [not_action] if isinstance(not_action, str) else not_action
                    )
                )
                resources = statement.get("Resource", [])
                resources = [resources] if isinstance(resources, str) else resources
                if not resources or "NotResource" in statement:
                    outside_namespace.append(
                        f"{policy_name}: requires explicit Resource"
                    )
                for resource in resources:
                    if not isinstance(resource, str) or not resource.startswith(
                        log_prefix
                    ):
                        outside_namespace.append(f"{policy_name}: {resource!r}")
    attached = [
        policy["PolicyArn"]
        for page in iam.get_paginator("list_attached_role_policies").paginate(
            RoleName=role_name
        )
        for policy in page["AttachedPolicies"]
    ]
    return actions, attached, outside_namespace


def check_execution_role(
    iam: Any, role_arn: str, region: str, results: Results
) -> None:
    """Require inline logs grants scoped to this run's service log namespace."""
    actions, attached, outside_namespace = execution_role_actions(iam, role_arn, region)
    off_prefix = [a for a in actions if not a.startswith(EXECUTION_ROLE_ACTION_PREFIX)]
    results.check(
        "the conformance execution role grants only logs actions",
        bool(actions) and not off_prefix and not attached,
        f"{role_arn.rsplit('/', 1)[-1]}: {len(actions)} allowed actions, "
        f"off-prefix {off_prefix!r}, attached managed policies {attached!r}",
    )
    results.check(
        "the conformance execution role scopes logs to the service namespace",
        bool(actions) and not outside_namespace and not attached,
        f"resources outside /aws/lambda-microvms/: {outside_namespace!r}, "
        f"attached managed policies {attached!r}",
    )


def drive_platform_posture(
    cli: Cli, launched: Envelope, aws: Any, results: Results
) -> None:
    """Issues #154 and #155: what the guest can reach, pinned to the MEASURED posture.

    The suite's VM is launched **without** `--egress` (`drive_lifecycle`'s argv names no
    connector), so it is the right VM for both questions, and every pin here is what
    2026-09-11 (microvm 0.5.0) and 2026-09-12 (this tree's binaries) measured rather
    than what `docs/PLATFORM.md` used to claim: the platform gives a connector-less VM
    outbound network, Firecracker's MMDS answers the IMDSv2 handshake, and the
    execution role's credential document is served to root and non-root alike. `ip`,
    `iptables` and `nft` are absent from `al2023-minimal`. On a guest from an image built
    without `--repair-identity` the exec child's bounding set (`CapBnd a80425fb`) carries
    no `CAP_NET_ADMIN`, so a blackhole route is `EPERM` as root. On a guest from an image
    built with it, root can blackhole MMDS for processes that aren't root, and any root
    process can remove the route (docs/PLATFORM.md, 2026-10-12). Either way the role
    check is about the role and not about the guest.

    Three checks were added on 2026-09-13, after an external review of an unrelated tool
    reported "the VM reached `extensions.duckdb.org` without `--egress`" as a defect in this
    client: a package registry is reached (the harm class, not a reachability curiosity), the
    launch envelope's own `egressPosture` label agrees with what the guest can reach, and
    `--deny-egress`'s advisory proxy stops a well-behaved client while the platform keeps
    routing. The label check is the one that could not exist before: a client that reports
    nothing about its network cannot be caught claiming the wrong thing.

    **Four of these checks are meant to go red when the platform improves.** A VM that stops
    reaching `example.com` or the registry without a connector, an MMDS that stops answering,
    or a posture label that no longer matches the reach, is the platform starting to honour
    the omission; the response is to re-measure, append to `docs/PLATFORM.md`, and flip
    `PLATFORM_HONOURS_OMITTED_EGRESS` in `microvms-core` — never to loosen a pin into
    "either".
    """
    print(
        "\n-- platform posture (egress without a connector, MMDS, the execution role) --"
    )
    attach = attach_args(cli, launched)

    egress = _guest_curl(cli, attach, _status("https://example.com"))
    results.check(
        "the platform gives egress without a connector (goes red when it starts "
        "honouring the omission)",
        egress == "200",
        f"curl https://example.com from a VM launched without --egress: {egress!r}; "
        f"platform gives egress without a connector — when this fails the platform "
        f"started honouring the omission: re-measure and update docs/PLATFORM.md",
    )

    # The harm class, and a second network path: a registry download is what a caller who
    # read `egress: false` as a seal actually got, so the suite names it rather than leaving
    # it as an inference from `example.com`.
    registry = _guest_curl(cli, attach, _status(PACKAGE_REGISTRY))
    results.check(
        "a connector-less VM reaches a package registry (the 242 MB-extension class of "
        "surprise; goes red when the platform seals it)",
        registry == "200",
        f"curl {PACKAGE_REGISTRY} from a VM launched without --egress: {registry!r}; "
        f"a workload's package manager, model download or DuckDB INSTALL leaves this VM",
    )

    # The client's own claim about the VM it just launched, against the two measurements
    # above. This is the check the review's finding asked for: an envelope that says
    # `sealed` while the guest reaches a registry is the defect, and so is an envelope that
    # says nothing at all — `egress: false` did.
    posture = launched.data.get("egressPosture")
    results.check(
        "the run envelope labels its own connector-less launch (never `sealed` while the "
        "guest reaches the internet)",
        posture == CONNECTORLESS_POSTURE and registry == "200" and egress == "200",
        f"egressPosture {posture!r} with example.com {egress!r} and "
        f"{PACKAGE_REGISTRY} {registry!r}; expected {CONNECTORLESS_POSTURE!r} while the "
        f"guest reaches both — when the platform seals the VM this goes red and the flip is "
        f"`PLATFORM_HONOURS_OMITTED_EGRESS` in microvms-core",
    )

    # `--deny-egress`'s mechanism, measured on this VM rather than on a second billable one:
    # the flag's whole effect is those variables in the launch env, and `exec --env` puts the
    # same pair in front of the same client. What is asserted is that a well-behaved client
    # fails closed AND that the platform still routes — the second half is why the posture is
    # `best-effort` and not `sealed`.
    denied = _guest_curl(
        cli,
        attach,
        _status(PACKAGE_REGISTRY),
        "--env",
        f"https_proxy={DENY_EGRESS_PROXY_URL}",
        "--env",
        f"http_proxy={DENY_EGRESS_PROXY_URL}",
    )
    results.check(
        "--deny-egress's advisory proxy stops a well-behaved client, and the platform still "
        "routes (best-effort, never sealed)",
        denied != "200" and (NO_CONNECTION in denied or "curl=" in denied),
        f"curl {PACKAGE_REGISTRY} with the advisory proxy set: {denied!r} (refused), while "
        f"the same VM without it answered {registry!r} — the deny is in the client, not in "
        f"the network, which is what `egressPosture: best-effort` reports",
    )

    handshake = _guest_curl(cli, attach, MMDS_HANDSHAKE)
    put_status, _, token_len = handshake.partition(" ")
    results.check(
        "MMDS answers the IMDSv2 token handshake (goes red when the platform seals it)",
        put_status == "200" and token_len.isdigit() and int(token_len) > 0,
        f"PUT /latest/api/token: status {put_status!r}, token length {token_len}",
    )

    listing = _guest_curl(
        cli,
        attach,
        MMDS_HANDSHAKE + f'curl -s --max-time 5 -H "X-aws-ec2-metadata-token: $T" '
        f"{MMDS}/latest/meta-data/iam/security-credentials/",
    )
    role_listing = listing.split("\n")[-1] if listing else ""
    results.eq(
        "MMDS lists the execution role under iam/security-credentials/",
        role_listing,
        "execution_role",
    )

    document = _guest_curl(
        cli,
        attach,
        MMDS_HANDSHAKE
        + 'curl -s -o /dev/null -w "%{http_code} %{size_download}" --max-time 5 '
        f'-H "X-aws-ec2-metadata-token: $T" '
        f"{MMDS}/latest/meta-data/iam/security-credentials/execution_role",
    )
    doc_status, _, doc_size = (document.split("\n")[-1] if document else "").partition(
        " "
    )
    results.check(
        "the credential document is served (status and size only; the body is a secret)",
        doc_status == "200"
        and doc_size.isdigit()
        and int(doc_size) > CREDENTIAL_DOCUMENT_FLOOR_BYTES,
        f"GET .../security-credentials/execution_role: status {doc_status!r}, "
        f"{doc_size} bytes (floor {CREDENTIAL_DOCUMENT_FLOOR_BYTES})",
    )

    # A non-root workload holds the role too. `agent-prompt` runs as uid 1000 on purpose
    # (docs/AGENT-VMS.md), and this is the check that says the uid buys nothing here.
    non_root = _guest_curl(cli, attach, MMDS_HANDSHAKE, "--user", "1000")
    non_root_status = non_root.partition(" ")[0]
    results.eq(
        "a non-root exec reaches MMDS too (uid 1000, pinned to the measured posture)",
        non_root_status,
        "200",
    )

    # Since the guest holds the role, the role's grant is the sandbox's real boundary.
    # Asserted against IAM rather than against `main.tf`'s text: the text is what someone
    # intended, the role is what the guest gets.
    role_arn = os.environ["MICROVM_EXECUTION_ROLE_ARN"]
    check_execution_role(aws.client("iam"), role_arn, cli.region, results)


def drive_posture_parity(cli: Cli, launched: Envelope, results: Results) -> None:
    """BIND-11 and BIND-12 (#227): the bindings' session posture against the CLI envelope's.

    The bindings' `Sandbox.run` wraps core `Sandbox::run`, and their `Session.egress_posture` /
    `session.egressPosture()` read the core session's value, so the core live test
    `live_posture` launches through that call, once with no network options and once with
    managed egress, and prints each session's posture. The CLI side is the suite's own
    connector-less launch and one `run --egress` launched and torn down here. An adopter of the
    egress VM, holding no launch options, must report `unsealed`. Three VMs, each bounded at
    600 seconds; the Rust test observes TERMINATED for both of its own.

    The posture is computed from the options, not read from AWS, so this proves the launch path
    carries it end to end; what the guest can reach is `drive_platform_posture`'s question.
    """
    print("\n-- egress posture parity (core session behind the bindings vs the CLI) --")
    image = str(launched.data["imageIdentifier"])
    egress = cli.call(
        "run",
        "--image",
        image,
        "--name",
        f"microvm-cli-conformance-posture-{secrets.token_hex(4)}",
        "--memory",
        str(BASELINE_MEMORY_MIB),
        "--egress",
        "--region",
        cli.region,
        "--max-duration-sec",
        "600",
    )
    results.check(
        "BIND-12 an --egress run's envelope reports open and tears down clean",
        egress.data.get("egressPosture") == "open" and not egress.data.get("leaked"),
        f"egressPosture={egress.data.get('egressPosture')!r} "
        f"leaked={egress.data.get('leaked')!r}",
    )

    env = os.environ.copy()
    env["MICROVM_BACKGROUND_TEST_IMAGE"] = image
    env["AWS_REGION"] = cli.region
    command = [
        "cargo",
        "test",
        "-p",
        "microvms-core",
        "--test",
        "live_posture",
        "a_launched_sessions_posture_is_its_requests",
        "--",
        "--ignored",
        "--exact",
        "--nocapture",
    ]
    cli.log.append(command_for_log(command))
    try:
        run = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            timeout=20 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        results.check(
            "BIND-12 the core posture launches ran",
            False,
            "exceeded 20 minutes; VM lifetime 600s",
        )
        return
    sessions = posture_lines(run.stderr)
    results.check(
        "BIND-12 the core posture launches ran and tore down to TERMINATED",
        run.returncode == 0 and run.stderr.count("state=TERMINATED") == 2,
        f"exit={run.returncode} postures={sessions!r} "
        f"terminated={run.stderr.count('state=TERMINATED')}",
    )
    results.eq(
        "BIND-12 a connector-less session reports what its CLI envelope reports",
        sessions.get("default"),
        launched.data.get("egressPosture"),
    )
    results.eq(
        "BIND-12 an --egress session reports what its CLI envelope reports",
        sessions.get("egress"),
        egress.data.get("egressPosture"),
    )
    results.eq(
        "BIND-12 a VM adopted without its launch options reports unsealed",
        sessions.get("egress-adopted"),
        "unsealed",
    )
    results.check(
        "BIND-11 no launch in this section reported sealed",
        "sealed" not in {*sessions.values(), egress.data.get("egressPosture")},
        f"postures={sessions!r} envelope={egress.data.get('egressPosture')!r}",
    )


def posture_lines(stderr: str) -> dict[str, str]:
    """`POSTURE <launch>=<label>` lines from `live_posture`, as {launch: label}."""
    postures: dict[str, str] = {}
    for line in stderr.splitlines():
        if line.startswith("POSTURE "):
            launch, _, rest = line.removeprefix("POSTURE ").partition("=")
            postures[launch] = rest.split(" ", 1)[0]
    return postures
