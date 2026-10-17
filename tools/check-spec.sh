#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Run the repository's locked optional verifier, never an unrelated global CLI.
set -euo pipefail

doc=${1:?usage: check-spec.sh <doc> [symspec check flags...]}
shift
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
verifier="$root/verify/formal/node_modules/.bin/symspec"
if [[ ! -x $verifier ]]; then
  echo "The optional formal verifier is missing; run mise run spec:setup" >&2
  exit 1
fi
version=$("$verifier" --version)
if [[ $version != "symspec v2.0.0" ]]; then
  echo "Expected symspec 2.0.0; run mise run spec:setup to restore the locked environment" >&2
  exit 1
fi
export SYMSPEC_MODEL_DIR=${SYMSPEC_MODEL_DIR:-"$root/verify/formal/model"}
exec "$verifier" check "$doc" "$@"
