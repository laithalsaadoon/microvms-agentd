#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Checks one symspec document with the `symspec` on PATH: `check-spec.sh <doc> [flags...]`.
#
# Both documents are `docVersion: 3`, which symspec reads from 1.0 on; 0.x cannot parse the
# state model or run the reachability tier, and would report on less rather than fail. So an
# absent or pre-1.0 CLI is refused by name here instead of producing a thinner report.
# Exits with symspec's own status: 0 means no error-severity finding.
set -euo pipefail

doc=${1:?usage: check-spec.sh <doc> [symspec check flags...]}
shift

if ! command -v symspec >/dev/null 2>&1; then
  echo "symspec is not on PATH; install a >= 1.0 CLI (npm install -g symspec)" >&2
  exit 1
fi
version=$(symspec --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [[ -z $version || ${version%%.*} -lt 1 ]]; then
  echo "symspec ${version:-(unknown)} cannot read docVersion 3; install a >= 1.0 CLI" >&2
  exit 1
fi
exec symspec check "$doc" "$@"
