#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build Strands Box for the MicroVM guest: aarch64 Linux, glibc 2.34 (Amazon Linux 2023).
#
# Box publishes a macOS release only (v0.1.0, 2026-10-07), but its main branch carries a Linux
# backend (user, mount, PID, IPC, UTS, and network namespaces plus seccomp) that it enables on
# aarch64 alone. Lambda MicroVMs run on Graviton, so the guest is that target. This script
# builds the three binaries `box run` needs side by side, from a pinned commit, into .box-bin/.
#
# It builds with cargo-zigbuild and zig on every host, x86_64 or aarch64, because zig pins the
# glibc the binaries link against: a binary linked against the host's own glibc on Ubuntu 22.04
# needs GLIBC_2.35, which AL2023 lacks, and fails to load in the guest.
#
# Needs: git, rustup, zig, and cargo-zigbuild (`cargo install cargo-zigbuild`).
set -euo pipefail

BOX_REPO="${BOX_REPO:-https://github.com/strands-agents/box.git}"
# main on 2026-10-12. Box pins its own toolchain in rust-toolchain.toml (1.98.1 here).
BOX_COMMIT="${BOX_COMMIT:-93c4749c377a64fb7a04f4d5a07afd880a290b38}"
TARGET=aarch64-unknown-linux-gnu
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${BOX_SRC:-$HERE/.box-src}"
OUT="$HERE/.box-bin"

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet "$BOX_REPO" "$SRC"
fi
git -C "$SRC" fetch --quiet origin "$BOX_COMMIT"
git -C "$SRC" checkout --quiet --detach "$BOX_COMMIT"

cd "$SRC"
TOOLCHAIN=$(sed -n 's/^channel = "\(.*\)"/\1/p' rust-toolchain.toml)
# Box's own toolchain, whatever the caller's environment says: inside this repo mise exports
# RUSTUP_TOOLCHAIN for the repo's Rust, which would win over Box's rust-toolchain.toml and lack
# the aarch64 target installed below.
export RUSTUP_TOOLCHAIN="$TOOLCHAIN"
rustup toolchain install "$TOOLCHAIN" --profile minimal >/dev/null
rustup target add "$TARGET" --toolchain "$TOOLCHAIN" >/dev/null

# The `.2.34` suffix is cargo-zigbuild's glibc pin: link against AL2023's glibc.
cargo zigbuild --locked --release --target "$TARGET.2.34" -p strands-box -p strands-box-containment

mkdir -p "$OUT"
for bin in strands-box strands-box-sock-alias strands-box-contain-trampoline; do
  install -m 0755 "target/$TARGET/release/$bin" "$OUT/$bin"
done
echo "$BOX_COMMIT" > "$OUT/COMMIT"
# Record the native host separately from the target: a cross build is not evidence
# for the native aarch64 path. Hashes bind the receipt to the binaries later run.
python3 - "$OUT" <<'PY_RECEIPT'
import hashlib
import json
import pathlib
import platform
import subprocess
import sys

out = pathlib.Path(sys.argv[1])
def version(*args):
    return subprocess.check_output(args, text=True).strip()

receipt = {
    "sourceRevision": version("git", "rev-parse", "HEAD"),
    "hostSystem": platform.system(),
    "hostArchitecture": platform.machine(),
    "target": "aarch64-unknown-linux-gnu.2.34",
    "rustc": version("rustc", "--version"),
    "cargoZigbuild": version("cargo", "zigbuild", "--version"),
    "zig": version("zig", "version"),
    "sha256": {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in ("strands-box", "strands-box-sock-alias", "strands-box-contain-trampoline")
    },
}
(out / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
PY_RECEIPT
echo "built Strands Box $BOX_COMMIT for $TARGET into $OUT" >&2
