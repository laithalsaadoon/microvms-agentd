#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# The SDLC variant of the Strands Box example: a coding agent (Codex, or Claude Code with
# SDLC_AGENT=claude) fixes a bug and pushes the fix, and a Dogwood policy (box/policy.dw) holds
# the order red check -> fix -> commit -> check -> push. Nothing outside the agent sequences
# those steps.
#
# What this does, end to end:
#   1. builds a MicroVM image from ./Dockerfile (the parent example's image plus git-core and
#      Claude Code),
#   2. launches a VM with outbound network, blocks guest metadata access, and copies Box, the
#      parent example's metadata probe, and this directory into the VM,
#   3. mints a short-lived Bedrock API key for Box,
#   4. runs the parent example's metadata probes outside and inside a box,
#   5. runs vm/driver.py: the model-free probe, one goal for the agent, and the agent
#      prompted to cheat, each in a fresh box against a freshly seeded repo and bare origin,
#   6. copies the results and every phase's decision log back to $BOX_OUT_DIR, and terminates.
#
# It exits 0 only when both metadata probes pass and every phase passes. SDLC_PHASES picks the
# phases (default probe,goal,cheats; impossible runs the goal on shop-conflict); SDLC_PROJECT
# picks the goal's project (calc or shop); SDLC_NO_HINTS=1 runs the goal without the hints.
# Prerequisites are the parent example's (../run.sh). The policy names us-east-1.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PARENT="$(cd "$HERE/.." && pwd)"
BOXBIN="${BOX_BIN_DIR:-$PARENT/.box-bin}"
OUT="${BOX_OUT_DIR:-$HERE/out}"
MICROVM="${MICROVM:-microvm}"
NAME="strands-box-sdlc-$$"
PHASES="${SDLC_PHASES:-probe,goal,cheats}"
DRIVER_ARGS="--phases $PHASES --project ${SDLC_PROJECT:-calc} --agent ${SDLC_AGENT:-codex}"
[ "${SDLC_NO_HINTS:-0}" = "1" ] && DRIVER_ARGS="$DRIVER_ARGS --no-hints"

[ "$REGION" = "us-east-1" ] || { echo "the SDLC variant's policy names us-east-1" >&2; exit 2; }
for bin in box strands-box-sock-alias strands-box-contain-trampoline; do
  [ -x "$BOXBIN/$bin" ] || { echo "missing $BOXBIN/$bin: run ../download-box.sh first" >&2; exit 2; }
done
python3 "$PARENT/stage_box.py" "$REGION"

jqr() { python3 -c "import json,sys; print(json.load(sys.stdin)['data']$1)"; }
vm() { "$MICROVM" "$@" --name "$NAME" --region "$REGION"; }
vmroot() { vm exec "$1" --user 0 --timeout "${2:-120}"; }

ID="" KEYFILE="" STAGE=""
trap 'rm -f "$KEYFILE"; [ -z "$STAGE" ] || rm -rf "$STAGE"
  [ -z "$ID" ] || "$MICROVM" terminate "$ID" --region "$REGION" >/dev/null || true' EXIT

# ── 1. the image ────────────────────────────────────────────────────────────
echo "resolving image (builds server-side on first run, several minutes)..." >&2
mkdir -p "$OUT"
rm -rf "$OUT/sdlc-out" "$OUT/sdlc-out.tar" "$OUT/metadata-outside.ok" "$OUT/metadata-inside.ok"
if [ -n "${BOX_IMAGE:-}" ]; then
  IMAGE_ARN="$BOX_IMAGE"
else
  BUILT=$("$MICROVM" build --json --reuse --repair-identity \
    --name strands-box-codex-sdlc \
    --dockerfile "$HERE/Dockerfile" \
    --project "$PARENT/guest" \
    --region "$REGION")
  IMAGE_ARN=$(echo "$BUILT" | jqr "['imageIdentifier']")
fi
echo "image: $IMAGE_ARN" >&2

# ── 2. launch, block metadata, upload ───────────────────────────────────────
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
vmroot "/sbin/ip -4 route replace blackhole 169.254.169.254/32" >/dev/null

STAGE=$(mktemp -d)
mkdir -p "$STAGE/opt" "$STAGE/home/project" "$STAGE/home/codex-tmp" "$STAGE/home/sdlc/box" \
  "$STAGE/home/sdlc/seed" "$STAGE/home/agent-state/tmp"
cp "$BOXBIN"/box "$BOXBIN"/strands-box-sock-alias "$BOXBIN"/strands-box-contain-trampoline \
  "$PARENT/vm/launch_in_fresh_proc.py" "$STAGE/opt/"
# The parent example's box, for its metadata probe, and Codex's config.
python3 "$PARENT/stage_box.py" "$REGION" "$PARENT/box" "$STAGE/home/box"
mv "$STAGE/home/box/codex-home" "$STAGE/home/codex-home"
cp "$PARENT/vm/probe_mmds.py" "$STAGE/home/"
# This variant: its boxes, the seed for each phase, and the scripts.
cp "$HERE/box/box.toml" "$HERE/box/claude-box.toml" "$HERE/box/probe-box.toml" "$HERE/box/policy.dw" \
  "$STAGE/home/sdlc/box/"
cp "$HERE/gitconfig" "$HERE/vm/seed.sh" "$HERE/vm/probe.py" "$HERE/vm/driver.py" "$STAGE/home/sdlc/"
# One seed directory per project, each with the same frozen check.py: calc (the parent example's
# calculator), shop (a checkout library whose bug spans files), and shop-conflict (shop plus a
# test that contradicts one of its tests, so no change can pass).
mkdir -p "$STAGE/home/sdlc/seed/calc"
cp "$PARENT/project/calc.py" "$PARENT/project/test_calc.py" "$HERE/project/check.py" \
  "$STAGE/home/sdlc/seed/calc/"
cp -R "$HERE/projects/shop" "$STAGE/home/sdlc/seed/shop"
cp -R "$HERE/projects/shop" "$STAGE/home/sdlc/seed/shop-conflict"
cp -R "$HERE/projects/shop-conflict/." "$STAGE/home/sdlc/seed/shop-conflict/"
cp "$HERE/project/check.py" "$STAGE/home/sdlc/seed/shop/"
cp "$HERE/project/check.py" "$STAGE/home/sdlc/seed/shop-conflict/"
tar -C "$STAGE/opt" -cf "$STAGE/opt.tar" .
tar -C "$STAGE/home" -cf "$STAGE/home.tar" .
vm cp "$STAGE/opt.tar" vm:/home/agent/box-bin-upload --tar --json >/dev/null
vm cp "$STAGE/home.tar" vm:/home/agent --tar --json >/dev/null
rm -rf "$STAGE"
STAGE=""
vmroot "mv /home/agent/box-bin-upload /opt/box && chown -R 0:0 /opt/box && chmod 0755 /opt/box /opt/box/* \
  && chown -R 1000:1000 /home/agent" >/dev/null

# ── 3. the Bedrock API key, for Box ─────────────────────────────────────────
KEYFILE=$(mktemp)
uvx --from aws-bedrock-token-generator@1.1.0 --quiet python -c \
  "from aws_bedrock_token_generator import provide_token; print(provide_token(region='$REGION'))" > "$KEYFILE"
vm cp "$KEYFILE" vm:/home/agent/.bedrock-token --mode 0600 --json >/dev/null
rm -f "$KEYFILE"
KEYFILE=""
vmroot "chown 1000:1000 /home/agent/.bedrock-token" >/dev/null

# ── 4. the metadata service, outside and inside a box ───────────────────────
LAUNCHER="python3.12 /opt/box/launch_in_fresh_proc.py"
echo "--- metadata service probe ---"
vmroot "$LAUNCHER /usr/bin/python3.12 /home/agent/probe_mmds.py" | grep '^\['
printf 'passed\n' > "$OUT/metadata-outside.ok"
set +e
INSIDE=$(vmroot "$LAUNCHER /opt/box/box run --config /home/agent/box/probe-box.toml \
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

# ── 5. the phases ───────────────────────────────────────────────────────────
echo "--- sdlc phases: $PHASES ---"
set +e
vmroot "$LAUNCHER /opt/guest/.venv/bin/python /home/agent/sdlc/driver.py $DRIVER_ARGS" 2700
DRIVER_STATUS=$?
set -e

# ── 6. evidence back, then teardown in the trap ─────────────────────────────
vmroot "tar -C /home/agent -cf /home/agent/sdlc-out.tar sdlc-out" >/dev/null || true
vm cp vm:/home/agent/sdlc-out.tar "$OUT/sdlc-out.tar" --json >/dev/null || true
[ -f "$OUT/sdlc-out.tar" ] && tar -C "$OUT" -xf "$OUT/sdlc-out.tar"
echo "results in $OUT/sdlc-out; terminating $ID..." >&2
exit "$DRIVER_STATUS"
