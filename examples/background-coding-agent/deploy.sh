#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
cd "$(dirname "$0")"

# Every dependency, the microvms binding included, installs from published wheels, so
# this packages the arm64 Lambda from any OS. The guest image is built separately.
: "${TF_VAR_image_arn:?Set TF_VAR_image_arn to a coding-agent image ARN}"
mkdir -p .agent
uv export --frozen --no-dev --format requirements-txt --output-file .agent/requirements.txt
bundle=$(mktemp -d)
trap 'rm -rf "$bundle"' EXIT
uv pip install --target "$bundle" --python-version 3.13 \
  --python-platform aarch64-manylinux_2_17 --only-binary :all: \
  -r .agent/requirements.txt
cp handler.py jobs.py github_ops.py "$bundle/"
# The bundle's native modules are arm64, so import the handler with the host's own
# environment and check that the binding in the bundle is the arm64 build.
uv run --frozen python -c 'import handler'
uv run --frozen python -c '
import glob, struct, sys
[so] = glob.glob(sys.argv[1] + "/microvms/*.so")
with open(so, "rb") as f:
    head = f.read(20)
sys.exit(0 if head[:4] == b"\x7fELF" and struct.unpack("<H", head[18:20])[0] == 183
         else "the bundled microvms extension is not an aarch64 ELF")' "$bundle"
uv run --frozen python -c \
  'import shutil,sys; shutil.make_archive(".agent/function", "zip", sys.argv[1])' "$bundle"

terraform -chdir=infra init -input=false
terraform -chdir=infra apply "$@"
terraform -chdir=infra output -json config > .agent/config.json
read -r region secret < <(uv run --frozen python -c \
  'import json; c=json.load(open(".agent/config.json")); print(c["region"], c["github_secret"])')
echo "Wrote .agent/config.json. Store a GitHub token once (see README for scopes):"
echo "  gh auth token | aws secretsmanager put-secret-value --region $region \\"
echo "    --secret-id $secret --secret-string file:///dev/stdin"
