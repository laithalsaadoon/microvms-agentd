# SPDX-License-Identifier: Apache-2.0
"""The live run: every section in order against one account, then the summary.

The sections that launch nothing run first, the shared VM's sections run before the one that
suspends it, the sections with their own builds sit together, and the sections that wait out
idle windows run last, so a cheaper failure is reported before they spend their time. The kept
VM's teardown runs in the `finally`, whatever happened above it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

import boto3
from harness.cli import Cli
from harness.constants import REPO, SERVICE
from harness.daemon import Daemon
from harness.envelope import Envelope
from harness.image import conformance_dockerfile, resolve_base_ref
from harness.results import Results, run_section

from lanes.agents import drive_agent_vm
from lanes.bootstrap import drive_daemon_lane
from lanes.caller_artifact import drive_caller_artifact
from lanes.closed_output import drive_closed_output, drive_closed_output_bdd
from lanes.ensure_image import drive_ensure_image
from lanes.exec import (
    drive_exec,
    drive_exec_identity,
    drive_exec_start_protocol,
    drive_kill_and_procs,
    drive_output_cap,
    drive_stdin,
    drive_streaming,
    drive_token_rotation,
)
from lanes.files import drive_config_and_sync, drive_file_transfer
from lanes.image_versions import drive_image_versions
from lanes.keepalive import drive_idle_keepalive, drive_keepalive_helper
from lanes.lifecycle import (
    drive_build_logging,
    drive_health,
    drive_identity_per_vm,
    drive_launch_by_name,
    drive_launch_without_waiting,
    drive_lifecycle,
)
from lanes.local import drive_doctor_region, drive_local_commands, drive_preflight
from lanes.names import drive_named_vm
from lanes.posture import drive_platform_posture, drive_posture_parity
from lanes.project_build import drive_project_build
from lanes.quickstart import drive_provisioned_quickstart
from lanes.sessions import (
    drive_adopt_by_id,
    drive_find_by_name,
    drive_lifecycle_by_id,
    drive_run_to_completion,
    drive_serve,
    drive_stable_launch,
)
from lanes.skew import drive_version_skew
from lanes.suspend import drive_auto_resume, drive_suspend_resume
from lanes.teardown import drive_teardown, read_daemon_logs
from lanes.tunnel import drive_tunnel_identity


def sh(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def run_suite(args: argparse.Namespace) -> int:
    """The live run: the stack's outputs, the suite's image, every section, the summary."""
    if args.binary is None:
        print("--binary is required for a live run (or pass --self-test)")
        return 2

    repo = REPO
    infra = args.infra_dir or repo / "conformance" / "infra"
    binary = (
        (repo / args.binary).resolve() if not args.binary.is_absolute() else args.binary
    )
    microvm = (
        (repo / args.microvm_binary).resolve()
        if not args.microvm_binary.is_absolute()
        else args.microvm_binary
    )
    for label, path in (("agentd binary", binary), ("microvm CLI", microvm)):
        if not path.exists():
            print(f"{label} not found: {path}")
            return 2

    # The same three Terraform outputs the oracle read, handed to the CLI through the
    # environment rather than as flags: `MICROVM_BUCKET` and the two role ARNs are the
    # names `seam.rs:324` resolves, so this is how a human runs it too.
    outputs = json.loads(sh(["terraform", "output", "-json"], cwd=infra))
    os.environ["MICROVM_BUCKET"] = outputs["s3_bucket"]["value"]
    os.environ["MICROVM_BUILD_ROLE_ARN"] = outputs["build_role_arn"]["value"]
    os.environ["MICROVM_EXECUTION_ROLE_ARN"] = outputs["execution_role_arn"]["value"]
    print(f"infra: bucket={os.environ['MICROVM_BUCKET']}")

    cli = Cli(binary=microvm)
    results = Results()
    if args.only == "ensure_image":
        # A targeted run of the one section that owns everything it creates: no suite VM,
        # no suite build, the same named checks and the same independent cleanup.
        run_section(
            results,
            "ensure_image",
            drive_ensure_image,
            binary,
            boto3.Session(region_name=cli.region),
            results,
        )
        print(f"\n  passed: {len(results.passed)}")
        print(f"  failed: {len(results.failed)}")
        for failed, detail in results.failed:
            print(f"  FAIL {failed}: {detail}")
        return 1 if results.failed else 0
    launched: Envelope | None = None
    daemon: Daemon | None = None
    # Built here rather than inside the `try`, because the teardown in the `finally` needs a
    # CloudWatch client and a name bound inside the block it is cleaning up after is a
    # `NameError` waiting for the one run that fails early — which would replace a real
    # failure with this file's own. Creating a boto3 session costs no API call.
    aws = boto3.Session(region_name=cli.region)
    # The configured build-logging pair (issue #98), bound before the `try` for `aws`'s
    # reason: the teardown in the `finally` deletes this group, and a name bound inside
    # the block is a NameError on the one run that fails early. Under
    # /aws/lambda-microvms/ because that is the only prefix the build role can write
    # (infra/main.tf, WriteBuildLogs) — outside it the build writes nothing and the
    # stream checks would measure IAM rather than the client's suffixing. The random
    # component keeps concurrent suite runs out of each other's groups.
    build_log_group = (
        f"/aws/lambda-microvms/conformance-configured-{secrets.token_hex(4)}"
    )
    build_log_stream_prefix = "suite-build"

    with tempfile.TemporaryDirectory() as tmp:
        dockerfile = Path(tmp) / "Dockerfile"
        dockerfile.write_text(conformance_dockerfile(resolve_base_ref()))

        try:
            # `record_unsupported(results)` was here, printing 34 SKIP lines before
            # anything was launched so a reader knew what the run would not tell them
            # *while* it spent money. Every one of those became a real check when the CLI
            # grew the five surfaces `docs/CLI-COVERAGE-PLAN.md` names, so there is nothing
            # left to announce. The summary still prints a skip count, which should read
            # zero — see `Results.skip`.
            run_section(results, "local_commands", drive_local_commands, cli, results)
            # Preflight (#223) before anything is launched: it launches nothing itself.
            run_section(results, "preflight", drive_preflight, cli, results)
            # `doctor --region` (#250) launches nothing either.
            run_section(results, "doctor_region", drive_doctor_region, cli, results)
            launched = drive_lifecycle(
                cli,
                binary,
                dockerfile,
                results,
                build_log_group,
                build_log_stream_prefix,
            )
            run_section(
                results,
                "build_logging",
                drive_build_logging,
                aws.client("logs"),
                build_log_group,
                build_log_stream_prefix,
                results,
            )

            # The daemon lane pokes the VM the CLI launched, over raw HTTP. Composed
            # rather than duplicated: the Rust client launched it and this reaches
            # around every client, which is the honest division of labour for six
            # checks that are about neither one.
            daemon = Daemon(
                endpoint=str(launched.data["endpoint"]),
                agent_token=str(launched.data["agentToken"]),
                microvm_id=str(launched.data["microvmId"]),
                microvm_client=aws.client(SERVICE),
            )
            run_section(results, "daemon_lane", drive_daemon_lane, daemon, results)

            run_section(results, "exec", drive_exec, cli, launched, results)
            run_section(results, "health", drive_health, cli, launched, results)
            # Platform posture (#154, #155) on the suite's own connector-less VM: the pins
            # are the measured facts, and two of them are designed to go red the day the
            # platform starts honouring an omitted egress connector. Needs `iam:Get/List`
            # on the execution role, which the conformance caller already has.
            run_section(
                results,
                "platform_posture",
                drive_platform_posture,
                cli,
                launched,
                aws,
                results,
            )
            # Egress posture parity (#227): the core session the bindings wrap against the
            # CLI envelope, on two bounded VMs from the suite's image plus one CLI launch.
            run_section(
                results, "posture_parity", drive_posture_parity, cli, launched, results
            )
            run_section(
                results, "exec_identity", drive_exec_identity, cli, launched, results
            )
            # Named users, groups and shells, and the image env (#224, #225, #226), on the
            # suite's own VM: its image carries the passwd row and the ENV line.
            run_section(
                results,
                "exec_start_protocol",
                drive_exec_start_protocol,
                cli,
                launched,
                results,
            )
            # Machine-id per VM (#205): a second VM from the suite's image, compared
            # against the suite's own.
            run_section(
                results,
                "identity_per_vm",
                drive_identity_per_vm,
                cli,
                launched,
                results,
            )
            # A launch by the suite image's bare name (#253), on a bounded VM of its own, so a
            # resolution that broke fails these checks and not another section's.
            run_section(
                results,
                "launch_by_name",
                drive_launch_by_name,
                cli,
                launched,
                results,
            )
            # `run --keep --no-wait` and `wait` (#269), on a bounded VM of its own from the
            # suite's image.
            run_section(
                results,
                "launch_without_waiting",
                drive_launch_without_waiting,
                cli,
                launched,
                results,
            )
            # After the identity section because it leans on the same detach/poll surface
            # that section just proved: every process fact here is read through `ps` and
            # every stop through `kill`, against the same shared VM.
            run_section(
                results, "kill_and_procs", drive_kill_and_procs, cli, launched, results
            )
            run_section(
                results, "stable_launch", drive_stable_launch, cli, launched, results
            )
            # Lifecycle by id (#195, #197, #201, #203) on its own bounded VMs, from the
            # suite's image.
            run_section(
                results,
                "lifecycle_by_id",
                drive_lifecycle_by_id,
                cli,
                launched,
                aws,
                results,
            )
            # The suite image's versions and builds through the CLI (#264), and a retire
            # round trip that restores the version before the sections after it launch.
            run_section(
                results,
                "image_versions",
                drive_image_versions,
                cli,
                str(launched.data["imageIdentifier"]),
                results,
            )
            # Adoption (#196) on its own bounded VM, from the suite's image.
            run_section(
                results, "adopt_by_id", drive_adopt_by_id, cli, launched, results
            )
            # Names (#202): a CLI-registered VM found and released through core.
            run_section(
                results, "find_by_name", drive_find_by_name, cli, launched, results
            )
            # After the identity section because it leans on the same detach/poll/ack
            # surface that section just proved, so a rotation failure here points at the
            # rotation rather than at a broken poll.
            run_section(
                results, "token_rotation", drive_token_rotation, cli, launched, results
            )
            run_section(results, "streaming", drive_streaming, cli, launched, results)
            run_section(
                results, "closed_output", drive_closed_output, cli, launched, results
            )
            run_section(
                results,
                "closed_output_bdd",
                drive_closed_output_bdd,
                cli,
                launched,
                results,
            )
            run_section(results, "stdin", drive_stdin, cli, launched, results)
            run_section(
                results,
                "run_to_completion",
                drive_run_to_completion,
                cli,
                launched,
                results,
            )
            run_section(results, "serve", drive_serve, cli, launched, results)
            run_section(
                results,
                "file_transfer",
                drive_file_transfer,
                cli,
                launched,
                results,
                Path(tmp),
            )
            # The cap trio *after* the file and stream sections, deliberately: it pushes
            # 32 MiB through the guest and asserts the daemon survived, so anything that
            # ran before it is evidence the survival claim is about a daemon that was
            # already doing real work — and anything after it would be confounded by it.
            run_section(results, "output_cap", drive_output_cap, cli, launched, results)
            # Suspend/resume last among the shared-VM sections, because it is the only one
            # that changes the VM's state for forty seconds and every section above wants a
            # running one.
            run_section(
                results, "suspend_resume", drive_suspend_resume, cli, launched, results
            )
            # Named VMs on their own VM (from the suite's image, no second build):
            # registration only happens at launch, so the suite's VM cannot carry it.
            run_section(
                results,
                "named_vm",
                drive_named_vm,
                cli,
                launched,
                Path(tmp) / "named-state",
                results,
            )
            # Tunnel identity on its own VM as well (launched `--identity`, from the
            # suite's image): the seed is delivered only at launch, so the suite's VM
            # cannot carry one.
            run_section(
                results,
                "tunnel_identity",
                drive_tunnel_identity,
                cli,
                launched,
                Path(tmp) / "ident-state",
                results,
            )
            # microvm.toml + run <DIR> on their own VM too (launched *through the config
            # file*, from the suite's image): the config merge and the sync round trip
            # both happen at launch, so the suite's VM cannot carry them either.
            run_section(
                results,
                "config_and_sync",
                drive_config_and_sync,
                cli,
                launched,
                Path(tmp) / "sync-project",
                results,
            )
            # The self-provisioned quickstart is the one section with its own build:
            # provisioning fires only when building, so no launch from the suite's
            # image can exercise it. See its docstring for the version-coupled caveat.
            run_section(
                results,
                "provisioned_quickstart",
                drive_provisioned_quickstart,
                cli,
                Path(tmp) / "quickstart-state",
                aws.client("logs"),
                results,
            )
            # `build --project` on its own build too (#74): the environment layer is
            # baked at build time, so no launch from the suite's image can carry one.
            # Beside the other own-build section so the two ~3-minute builds sit together.
            run_section(
                results,
                "project_build",
                drive_project_build,
                cli,
                binary,
                Path(tmp) / "project",
                aws.client("logs"),
                aws.client("s3"),
                aws.client(SERVICE),
                results,
            )
            # Version skew (#298) beside them: this tree's CLI builds an image around the
            # previous release's daemon, and the previous release's CLI launches the suite's
            # image, so an upgrade of either side alone is run end to end.
            run_section(
                results,
                "version_skew",
                drive_version_skew,
                cli,
                launched,
                binary,
                dockerfile,
                Path(tmp) / "skew",
                aws.client("logs"),
                results,
            )
            # `build --artifact-uri` with the suite's bucket set (#249), on its own build
            # from a copy of the suite's artifact: the property is what S3 holds afterward,
            # so it needs an object of its own to read back.
            run_section(
                results,
                "caller_artifact",
                drive_caller_artifact,
                cli,
                launched,
                aws,
                results,
            )
            # `Sandbox::ensure_image` (#221) on its own two builds and its own VM, through
            # the ignored Rust live test: the content-addressed image, the create race, the
            # reuse, and the forced rebuild all need an image nothing else built.
            run_section(
                results,
                "ensure_image",
                drive_ensure_image,
                binary,
                aws,
                results,
            )
            # Agent VMs on their own build and their own VM (`docs/AGENT-VMS.md`): the
            # image is derived from the profile set and the daemon bytes, so no launch
            # from the suite's image can carry a coding agent. The only section that
            # needs Bedrock; it prints the model ids it used to stderr. Beside the
            # other own-build sections for the same reason `drive_project_build` is.
            run_section(
                results,
                "agent_vm",
                drive_agent_vm,
                cli,
                binary,
                Path(tmp) / "agent-state",
                aws.client("logs"),
                aws.client("s3"),
                results,
            )
            # Auto-resume on its own VM (launched `--auto-resume` from the suite's image):
            # the policy is set only at launch, so the suite's VM cannot carry it. NEW in
            # 0.6.0 (#68) and not yet run live — first execution is the next sweep. Slow
            # (~4 minutes of deliberate waiting), so it sits with the other slow section.
            run_section(
                results, "auto_resume", drive_auto_resume, cli, launched, aws, results
            )
            # The idle-keepalive section runs on its own VM (launched from the image this
            # suite already built, so no second build) and is the slowest section here —
            # its own output says how long. Last, so its four minutes of deliberate
            # waiting delay nothing, and so a failure in any cheaper section is reported
            # before this one spends its time.
            run_section(
                results,
                "idle_keepalive",
                drive_idle_keepalive,
                cli,
                launched,
                aws,
                results,
            )
            # The busy-VM case of the same meter, through the supported helper (#199).
            run_section(
                results,
                "keepalive_helper",
                drive_keepalive_helper,
                cli,
                launched,
                aws,
                results,
            )

            print("\n== daemon logs ==")
            lines = read_daemon_logs(
                aws.client("logs"),
                str(launched.data["imageName"]),
                extra_groups=(build_log_group,),
            )
            results.check(
                "daemon logs reached CloudWatch under /aws/lambda-microvms/",
                bool(lines),
                f"{len(lines)} lines",
            )
        finally:
            if daemon is not None:
                daemon.close()
            if args.keep:
                print("\n== teardown SKIPPED (--keep) ==")
            elif launched is None:
                print("\n== teardown: nothing was launched ==")
            else:
                # Never raises out of here: an exception in teardown would replace the
                # real failure with a teardown failure. The log group is handled LAST
                # because the service can recreate a group deleted before its image —
                # which is how six of them leaked.
                #
                # `aws` is bound before the `try` for this call site's sake: a session
                # created inside the block would be a `NameError` here on the one run that
                # failed early, which would replace a real failure with this file's own.
                try:
                    drive_teardown(
                        cli,
                        launched,
                        results,
                        aws.client("logs"),
                        extra_log_groups=(build_log_group,),
                    )
                except Exception as exc:  # noqa: BLE001 - a teardown failure is a finding
                    results.check("teardown completed", False, repr(exc))

    print("\n== summary ==")
    print(f"  passed:  {len(results.passed)}")
    print(f"  failed:  {len(results.failed)}")
    print(f"  skipped: {len(results.skipped)}")
    for name, detail in results.failed:
        print(f"    FAIL {name}: {detail}")
    for name, reason in results.skipped:
        print(f"    SKIP {name}: {reason}")

    # `expressed` counts every check that ran either way, so the denominator is what this
    # suite *attempted* rather than what it managed. A failing check is still an expressed
    # one — the coverage claim and the pass/fail verdict are different facts, and folding
    # them would make a red run look like a narrower suite.
    expressed = len(results.passed) + len(results.failed)
    total = expressed + len(results.skipped)
    print(
        f"\n  {expressed} of {total} named checks are expressible through this client."
    )
    if results.skipped:
        # Never reached today, and the branch stays: the moment a surface goes away or a
        # check becomes inexpressible again, this is the line that says so instead of the
        # count quietly shrinking.
        print(
            f"  {len(results.skipped)} are not, and each is named above with the surface "
            "that would have to grow."
        )
    else:
        print(
            "  The 34 the deleted Python oracle alone could reach — file transfer, tar "
            "round trips,\n  the four hostile archives, SSE ordering, the stdin lifecycle, "
            "double-ack, the 8 MiB cap\n  trio, the identity-repair flags — are live checks "
            "now, under the names run.py gave them.\n  This report diffs line for line "
            "against the last oracle run in git history."
        )
    print("\n  every invocation (secret arguments and agent tasks redacted):")
    for line in cli.log:
        print(f"    {line}")
    return 0 if not results.failed else 1
