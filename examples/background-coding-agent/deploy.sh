#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
cd "$(dirname "$0")"

# The binding is built from this repository, so package on Linux x86_64 to match
# the Lambda runtime. The guest image is ARM64 and is built separately.
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
  echo 'Run deployment packaging on Linux x86_64.' >&2; exit 1;
}
: "${TF_VAR_image_arn:?Set TF_VAR_image_arn to a coding-agent image ARN}"
mkdir -p .agent/wheels
rm -f .agent/wheels/microvms-*.whl
uvx maturin@1.14.1 build --release --locked --manifest-path ../../microvms-py/Cargo.toml \
  --compatibility manylinux_2_34 --out .agent/wheels
uv export --frozen --no-dev --no-emit-package microvms --format requirements-txt \
  --output-file .agent/requirements.txt
bundle=$(mktemp -d)
trap 'rm -rf "$bundle"' EXIT
uv pip install --target "$bundle" --python-version 3.13 \
  --python-platform x86_64-manylinux_2_34 --only-binary :all: \
  -r .agent/requirements.txt .agent/wheels/microvms-*.whl
cp handler.py jobs.py github_ops.py "$bundle/"
uv run --frozen python -I -S -c \
  'import sys; sys.path.insert(0, sys.argv[1]); import handler' "$bundle"
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
