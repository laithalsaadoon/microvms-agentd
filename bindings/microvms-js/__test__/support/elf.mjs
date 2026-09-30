// SPDX-License-Identifier: Apache-2.0
// Daemon bytes for the build tests: an ELF header and nothing else.

/** A little-endian ELF header naming `machine`: 0xb7 is aarch64, 0x3e is x86_64. */
export function elfHeader(machine) {
  const header = new Uint8Array(20);
  header.set([0x7f, 0x45, 0x4c, 0x46], 0);
  header[5] = 1;
  header[18] = machine & 0xff;
  header[19] = machine >> 8;
  return header;
}
