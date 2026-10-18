#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Codex, driven by the Codex Python SDK, inside a Strands Box, inside a Lambda MicroVM, against
# GPT-6.1 Sol on Amazon Bedrock's bedrock-runtime endpoint.
#
# What this does, end to end:
#   1. builds a MicroVM image with agentd, Python 3.12, and the Codex SDK (first run only),
#   2. launches a VM from it with outbound network (--egress),
#   3. blocks guest metadata access, then copies Strands Box (from build-box.sh), the box's config, and the driver into the VM,
#   4. mints a short-lived Bedrock API key locally and copies it in for Box, not for Codex,
#   5. checks the VM's metadata service is refused outside and inside a box,
#   6. runs vm/driver.py, which runs six scenarios through the SDK and checks Box's decisions,
#   7. copies the results and Box's decision log back to out/, and terminates the VM.
#
# It exits 0 only when both metadata probes and every scenario pass.
#
# Prerequisites:
#   - `microvm` 1.1.1 or later on PATH, with MICROVM_BUCKET, MICROVM_BUILD_ROLE_ARN, and
#     MICROVM_EXECUTION_ROLE_ARN set (the repo README's Getting started),
#   - AWS credentials that can call lambda-microvms and bedrock:InvokeModel on GPT-6.1 Sol,
#   - uv on PATH (mints the Bedrock API key),
#   - Strands Box built with ./build-box.sh into .box-bin/.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BOXBIN="${BOX_BIN_DIR:-$HERE/.box-bin}"
OUT="${BOX_OUT_DIR:-$HERE/out}"
MICROVM="${MICROVM:-microvm}"
NAME="strands-box-codex-$$"

for bin in strands-box strands-box-sock-alias strands-box-contain-trampoline; do
  [ -x "$BOXBIN/$bin" ] || { echo "missing $BOXBIN/$bin: run ./build-box.sh first" >&2; exit 2; }
done
# Validate before creating AWS resources; stage_box.py later renders a private
# copy of the policy, gateway destination, and model region from the east template.
python3 "$HERE/stage_box.py" "$REGION"

jqr() { python3 -c "import json,sys; print(json.load(sys.stdin)['data']$1)"; }
vm() { "$MICROVM" "$@" --name "$NAME" --region "$REGION"; }
vmroot() { vm exec "$1" --user 0 --timeout "${2:-120}"; }

# Whatever happens below, the trap terminates the VM once it has an id, and removes the local
# copy of the Bedrock API key and the staging directory.
ID="" KEYFILE="" STAGE=""
trap 'rm -f "$KEYFILE"; [ -z "$STAGE" ] || rm -rf "$STAGE"
  [ -z "$ID" ] || "$MICROVM" terminate "$ID" --region "$REGION" >/dev/null || true' EXIT

# ── 1. the image ────────────────────────────────────────────────────────────
# --project guest bakes guest/uv.lock (the Codex SDK and its Codex binary) into the image.
# --repair-identity is what requests additionalOsCapabilities ["ALL"] for the VM: without it the
# platform masks parts of /proc and the guest holds no CAP_SYS_ADMIN, and Box cannot build its
# view (vm/launch_in_fresh_proc.py says why). --reuse keys the image name to the build inputs.
echo "resolving image (builds server-side on first run, several minutes)..." >&2
mkdir -p "$OUT"
rm -f "$OUT/metadata-outside.ok" "$OUT/metadata-inside.ok" "$OUT/results.json" "$OUT/records.jsonl"
if [ -n "${BOX_IMAGE:-}" ]; then
  IMAGE_ARN="$BOX_IMAGE"
else
  BUILT=$("$MICROVM" build --json --reuse --repair-identity \
    --name strands-box-codex \
    --dockerfile "$HERE/Dockerfile" \
    --project "$HERE/guest" \
    --region "$REGION")
  IMAGE_ARN=$(echo "$BUILT" | jqr "['imageIdentifier']")
fi

# ── 2. launch ───────────────────────────────────────────────────────────────
# --egress lets the guest reach bedrock-runtime. Inside the box, Codex reaches only what
# policy.dw permits, through Box's egress gateway.
# With --keep the CLI leaves a VM running when the launch fails after creating it, and its failure
# envelope still names the VM, so the id is read from whichever envelope came back.
echo "launching..." >&2
LAUNCH_STATUS=0
LAUNCH=$("$MICROVM" run --json --keep --egress --image "$IMAGE_ARN" --vm-name "$NAME" \
  --max-duration-sec 3600 --region "$REGION") || LAUNCH_STATUS=$?
ID=$(echo "$LAUNCH" | python3 -c \
  "import json,sys; print((json.load(sys.stdin).get('data') or {}).get('microvmId') or '')" \
  2>/dev/null || true)
if [ "$LAUNCH_STATUS" -ne 0 ]; then
  echo "launch failed (exit $LAUNCH_STATUS); terminating ${ID:-nothing}" >&2
  exit "$LAUNCH_STATUS"
fi
echo "microvm: $ID" >&2

# Block the driver as well as the boxed process. Root installs the route before
# any driver code is uploaded or started; an error exits through the cleanup trap.
# An exec's PATH has no /sbin, where iproute installs `ip`, so the route names it in full.
vmroot "/sbin/ip -4 route replace blackhole 169.254.169.254/32" >/dev/null

# ── 3. Box, its config, and the driver ──────────────────────────────────────
STAGE=$(mktemp -d)
mkdir -p "$STAGE/opt" "$STAGE/home/project" "$STAGE/home/codex-tmp"
cp "$BOXBIN"/strands-box "$BOXBIN"/strands-box-sock-alias "$BOXBIN"/strands-box-contain-trampoline \
  "$HERE/vm/launch_in_fresh_proc.py" "$STAGE/opt/"
python3 "$HERE/stage_box.py" "$REGION" "$HERE/box" "$STAGE/home/box"
mv "$STAGE/home/box/codex-home" "$STAGE/home/codex-home"
cp "$HERE/vm/driver.py" "$HERE/vm/probe_mmds.py" "$STAGE/home/"
# The project Codex works on, with a file the policy keeps unread.
printf '# calc\n\nA tiny calculator project.\n' > "$STAGE/home/project/README.md"
printf 'DB_PASSWORD=not-a-real-secret-0123\n' > "$STAGE/home/project/.env"
tar -C "$STAGE/opt" -cf "$STAGE/opt.tar" .
tar -C "$STAGE/home" -cf "$STAGE/home.tar" .
vm cp "$STAGE/opt.tar" vm:/home/agent/box-bin-upload --tar --json >/dev/null
vm cp "$STAGE/home.tar" vm:/home/agent --tar --json >/dev/null
rm -rf "$STAGE"
STAGE=""
# Upload into the default user's writable home, then install the launcher and Box
# under /opt as root. Box and the driver run as uid 1000.
vmroot "mv /home/agent/box-bin-upload /opt/box && chown -R 0:0 /opt/box && chmod 0755 /opt/box /opt/box/* \
  && chown -R 1000:1000 /home/agent && chmod 0600 /home/agent/project/.env" >/dev/null

# ── 4. the Bedrock API key, for Box ─────────────────────────────────────────
# aws-bedrock-token-generator turns the caller's AWS credentials into a key that lasts up to 12
# hours. The driver hands it to `strands-box run`, whose egress gateway adds it to each request to
# bedrock-runtime; Codex gets a placeholder. The key never appears on a command line or in a log.
KEYFILE=$(mktemp)
uvx --from aws-bedrock-token-generator@1.1.0 --quiet python -c \
  "from aws_bedrock_token_generator import provide_token; print(provide_token(region='$REGION'))" > "$KEYFILE"
vm cp "$KEYFILE" vm:/home/agent/.bedrock-token --mode 0600 --json >/dev/null
rm -f "$KEYFILE"
KEYFILE=""
vmroot "chown 1000:1000 /home/agent/.bedrock-token" >/dev/null

# ── 5. the metadata service, outside and inside a box ───────────────────────
# Outside, the host route must block uid 1000. Inside, keep proving Box's own
# metadata policy: the network namespace has no route, and the gateway must name
# a metadata rule. Either failed probe stops the run before the SDK driver starts.
LAUNCHER="python3.12 /opt/box/launch_in_fresh_proc.py"
echo "--- metadata service probe ---"
vmroot "$LAUNCHER /usr/bin/python3.12 /home/agent/probe_mmds.py" | grep '^\['
printf 'passed\n' > "$OUT/metadata-outside.ok"
set +e
INSIDE=$(vmroot "$LAUNCHER /opt/box/strands-box run --config /home/agent/box/probe-box.toml \
  2>/home/agent/probe-box.err")
PROBE_STATUS=$?
set -e
printf '%s\n' "$INSIDE" | grep '^\[' || true
if [ "$PROBE_STATUS" -ne 0 ]; then
  echo "the metadata probe inside the box failed (exit $PROBE_STATUS); Box's stderr:" >&2
  vmroot "tail -n 20 /home/agent/probe-box.err" >&2 || true
  exit "$PROBE_STATUS"
fi

printf 'passed\n' > "$OUT/metadata-inside.ok"

# ── 6. the scenarios ────────────────────────────────────────────────────────
echo "--- codex sdk scenarios ---"
set +e
vmroot "$LAUNCHER /opt/guest/.venv/bin/python /home/agent/driver.py" 900
DRIVER_STATUS=$?
set -e

# ── 7. evidence back, then teardown in the trap ─────────────────────────────
# A file missing from out/ means the driver stopped before it wrote one; an older run's copy
# never stands in for it.
mkdir -p "$OUT"
rm -f "$OUT/results.json" "$OUT/records.jsonl"
vm cp vm:/home/agent/results.json "$OUT/results.json" --json >/dev/null || true
vm cp vm:/home/agent/box/state/private/telemetry/records.jsonl "$OUT/records.jsonl" --json >/dev/null || true
echo "results in $OUT; terminating $ID..." >&2
[ "$PROBE_STATUS" -eq 0 ] || exit "$PROBE_STATUS"
exit "$DRIVER_STATUS"
