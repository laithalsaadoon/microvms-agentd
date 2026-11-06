#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Download Strands Box for the MicroVM guest: its published Linux ARM64 release, which needs
# glibc 2.34 or newer (Amazon Linux 2023 has 2.34) and runs on Graviton, as Lambda MicroVMs do.
#
# Box v0.2.1 (2026-11-06) is the first release with a Linux archive, box-v0.2.1-linux-arm64.tar.gz.
# Its release notes say "Package availability does not establish security acceptance", and Box's
# README calls Linux ARM64 a preview. This script downloads that archive, checks it against the
# digest pinned below, and unpacks the three binaries `box run` needs side by side into .box-bin/.
# It runs on any host, x86_64 or aarch64, macOS or Linux, because it only downloads: run.sh copies
# the binaries into each VM.
#
# Needs: curl, tar, and python3.
set -euo pipefail

BOX_VERSION="${BOX_VERSION:-v0.2.1}"
# The archive's digest from the release's SHA256SUMS.txt, read 2026-11-06. A different
# BOX_VERSION needs its own BOX_ARCHIVE_SHA256.
BOX_ARCHIVE_SHA256="${BOX_ARCHIVE_SHA256:-9060a4ccca1d6d5153da31e9214d3fd64c9bab0aa7762b6556d2790a8a0f06c0}"
BOX_RELEASES="${BOX_RELEASES:-https://github.com/strands-agents/box/releases/download}"
ARCHIVE="box-$BOX_VERSION-linux-arm64.tar.gz"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/.box-bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL --retry 3 -o "$TMP/$ARCHIVE" "$BOX_RELEASES/$BOX_VERSION/$ARCHIVE"
# python3 hashes on macOS and Linux alike, where sha256sum and shasum differ.
GOT=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$TMP/$ARCHIVE")
if [ "$GOT" != "$BOX_ARCHIVE_SHA256" ]; then
  echo "$ARCHIVE has sha256 $GOT, not the pinned $BOX_ARCHIVE_SHA256" >&2
  exit 1
fi

mkdir -p "$TMP/unpacked"
tar -C "$TMP/unpacked" -xzf "$TMP/$ARCHIVE"
rm -rf "$OUT"
mkdir -p "$OUT"
for bin in box strands-box-sock-alias strands-box-contain-trampoline; do
  install -m 0755 "$TMP/unpacked/$bin" "$OUT/$bin"
done
# The receipt binds the release and its archive to the exact binaries later run.
python3 - "$OUT" "$BOX_VERSION" "$ARCHIVE" "$GOT" <<'PY_RECEIPT'
import hashlib
import json
import pathlib
import sys

out, version, archive, archive_sha256 = pathlib.Path(sys.argv[1]), *sys.argv[2:]
receipt = {
    "version": version,
    "archive": archive,
    "archiveSha256": archive_sha256,
    "sha256": {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in ("box", "strands-box-sock-alias", "strands-box-contain-trampoline")
    },
}
(out / "release.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
PY_RECEIPT
echo "downloaded Strands Box $BOX_VERSION for aarch64 Linux into $OUT" >&2
