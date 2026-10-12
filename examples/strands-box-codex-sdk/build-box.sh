#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build Strands Box for the MicroVM guest: aarch64 Linux, glibc 2.34 (Amazon Linux 2023).
#
# Box publishes a macOS release only (v0.1.0, 2026-10-07), but its main branch carries a Linux
# backend (user, mount, PID, IPC, UTS, and network namespaces plus seccomp) that it enables on
# aarch64 alone. Lambda MicroVMs run on Graviton, so the guest is that target. This script
# builds the three binaries `box run` needs side by side, from a pinned commit, into .box-bin/.
#
# On an x86_64 host it cross-compiles with cargo-zigbuild and zig, which also pins the glibc
# floor: a binary linked on Ubuntu 22.04 needs GLIBC_2.35, which AL2023 lacks. On an aarch64
# AL2023 host plain cargo builds the same binaries.
#
# Needs: git, rustup. On x86_64 also: zig and cargo-zigbuild (`cargo install cargo-zigbuild`).
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
rustup toolchain install "$TOOLCHAIN" --profile minimal >/dev/null
rustup target add "$TARGET" --toolchain "$TOOLCHAIN" >/dev/null

PACKAGES=(-p strands-box -p strands-box-containment)
if [ "$(uname -m)" = "aarch64" ]; then
  cargo build --release --target "$TARGET" "${PACKAGES[@]}"
else
  # The `.2.34` suffix is cargo-zigbuild's glibc pin: link against AL2023's glibc.
  cargo zigbuild --release --target "$TARGET.2.34" "${PACKAGES[@]}"
fi

mkdir -p "$OUT"
for bin in strands-box strands-box-sock-alias strands-box-contain-trampoline; do
  install -m 0755 "target/$TARGET/release/$bin" "$OUT/$bin"
done
echo "$BOX_COMMIT" > "$OUT/COMMIT"
echo "built Strands Box $BOX_COMMIT for $TARGET into $OUT" >&2
