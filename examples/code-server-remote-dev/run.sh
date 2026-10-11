#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# code-server in a Lambda MicroVM, reached through `microvm port-forward`.
#
# What this does, end to end:
#   1. builds a MicroVM image carrying agentd + code-server (first run only),
#   2. launches a named, kept VM from it — shell-capable, auto-resuming,
#   3. starts code-server inside it, detached, as a non-root user,
#   4. forwards local port 8080 to it and prints the URL.
#
# Re-running the script reattaches: if the name still answers, it skips
# straight to the forward. Ctrl-C stops the forward, never the VM — the VM
# idle-suspends on its own and auto-resumes when you come back.
#
# Prerequisites (see the repo README's Getting started):
#   - `microvm` on PATH and the aarch64 agentd binary built,
#   - MICROVM_BUCKET / MICROVM_BUILD_ROLE_ARN / MICROVM_EXECUTION_ROLE_ARN set,
#   - AWS credentials that can call lambda-microvms.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
AGENTD="${AGENTD:-target/aarch64-unknown-linux-musl/release/agentd}"
VM_NAME="${VM_NAME:-code-server-dev}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

jqr() { python3 -c "import json,sys; print(json.load(sys.stdin)['data']$1)"; }

if microvm health --name "$VM_NAME" --region "$REGION" >/dev/null 2>&1; then
  # The name answers: either the VM is running, or it was suspended and the
  # health request itself resumed it (auto-resume = true in microvm.toml).
  # Only reattach to a VM initialized with this authenticated configuration.
  microvm exec "test -f /run/code-server-authenticated" \
    --name "$VM_NAME" --region "$REGION" --json >/dev/null || {
    echo "Recreate $VM_NAME with this script to initialize password authentication." >&2
    exit 1
  }
  echo "reusing $VM_NAME" >&2
else
  # Read the password from a protected local file, before creating resources.
  : "${CODE_SERVER_PASSWORD_FILE:?Set CODE_SERVER_PASSWORD_FILE to a protected password file}"
  CONFIG_DIR=$(mktemp -d)
  trap 'rm -rf "$CONFIG_DIR"' EXIT
  python3 - "$CODE_SERVER_PASSWORD_FILE" "$CONFIG_DIR/config.yaml" <<'PYCONFIG'
import json
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
if source.stat().st_mode & 0o077:
    raise SystemExit("The password file must have mode 0600 or stricter.")
password = source.read_text().rstrip("\r\n")
if not password or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in password):
    raise SystemExit("The password must be nonempty and contain no control characters.")
fd = os.open(sys.argv[2], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, "w") as config:
    config.write("bind-addr: 127.0.0.1:8080\nauth: password\npassword: " + json.dumps(password) + "\n")
PYCONFIG

  # ── 1. the image: build once per content hash, reuse otherwise ──────────
  echo "resolving image (builds server-side on first run, several minutes)..." >&2
  BUILT=$(microvm build "$AGENTD" --json --reuse \
    --name code-server \
    --dockerfile "$HERE/Dockerfile" \
    --region "$REGION")
  IMAGE_ARN=$(echo "$BUILT" | jqr "['imageIdentifier']")

  # ── 2. launch, kept and named ────────────────────────────────────────────
  # --vm-name registers the name locally, so every later command — exec,
  # port-forward, shell, suspend, terminate — addresses the VM without
  # pasting the endpoint/token/id triple. The sizing, egress, shell
  # capability, and the idle/auto-resume loop all come from microvm.toml.
  echo "launching $VM_NAME..." >&2
  microvm run --json --keep --vm-name "$VM_NAME" --image "$IMAGE_ARN" \
    --config "$HERE/microvm.toml" --region "$REGION" >/dev/null

  # ── 3. code-server, detached, demoted ────────────────────────────────────
  # The IDE verifies its own password; the forward authenticates only the upstream hop.
  microvm cp "$CONFIG_DIR/config.yaml" vm:/run/code-server.yaml --mode 0600 \
    --name "$VM_NAME" --region "$REGION" --json >/dev/null
  microvm exec "chown 1000:1000 /run/code-server.yaml" \
    --name "$VM_NAME" --region "$REGION" --json >/dev/null
  rm -rf "$CONFIG_DIR"
  trap - EXIT
  microvm exec "code-server --config /run/code-server.yaml /workspace" \
    --detach --user 1000 --group 1000 --name "$VM_NAME" --region "$REGION" \
    --json >/dev/null

  # Wait for the listener with bash's /dev/tcp — nothing to install.
  microvm exec "timeout 60 bash -c 'until echo > /dev/tcp/127.0.0.1/8080; do sleep 1; done' 2>/dev/null" \
    --name "$VM_NAME" --region "$REGION" --json >/dev/null
  microvm exec "touch /run/code-server-authenticated" \
    --name "$VM_NAME" --region "$REGION" --json >/dev/null
fi

# ── 4. the forward ──────────────────────────────────────────────────────────
echo "code-server: http://127.0.0.1:$LOCAL_PORT"
echo "  a shell beside it:  microvm shell --name $VM_NAME --region $REGION"
echo "  pause it now:       microvm suspend $VM_NAME --region $REGION"
echo "  tear it down:       microvm terminate $VM_NAME --region $REGION"
echo "(Ctrl-C stops this forward, not the VM)"
exec microvm port-forward "$LOCAL_PORT:8080" --name "$VM_NAME" --region "$REGION"
