// SPDX-License-Identifier: Apache-2.0
//! The checks over a daemon binary's bytes: is it an aarch64 ELF (#234).
//!
//! Fetching, caching and digest verification are `microvms_core::provision`, which
//! re-exports these under the same names. The checks take the bytes, so the caller reads the
//! file.

/// The `e_machine` of an aarch64 ELF (`EM_AARCH64`).
pub const REQUIRED_ELF_MACHINE: u16 = 0xB7;

/// The `e_machine` field of an ELF header, or `None` if `bytes` does not start with one.
///
/// Twenty bytes: the four-byte magic, `EI_DATA` at offset 5 deciding the byte order, then
/// the two-byte `e_machine` at 18. Reading the byte order rather than assuming little is
/// not pedantry: a big-endian binary would otherwise report machine `0xB700` and be
/// rejected with a number nobody can look up.
pub fn elf_machine(bytes: &[u8]) -> Option<u16> {
    let header = bytes.get(..20)?;
    if &header[..4] != b"\x7fELF" {
        return None;
    }
    let field = [header[18], header[19]];
    Some(if header[5] == 1 {
        u16::from_le_bytes(field)
    } else {
        u16::from_be_bytes(field)
    })
}

/// Why `bytes` is not an aarch64 ELF, or `None` when it is.
pub fn not_aarch64(bytes: &[u8]) -> Option<String> {
    match elf_machine(bytes) {
        Some(REQUIRED_ELF_MACHINE) => None,
        Some(machine) => Some(format!(
            "ELF machine 0x{machine:x}, not aarch64 (0x{REQUIRED_ELF_MACHINE:x})"
        )),
        None => Some("not an ELF binary at all".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn elf_header(machine: u16) -> Vec<u8> {
        let mut header = vec![0u8; 20];
        header[..4].copy_from_slice(b"\x7fELF");
        header[5] = 1;
        header[18..20].copy_from_slice(&machine.to_le_bytes());
        header
    }

    /// The ELF parser, both byte orders, and everything shorter than a header.
    #[test]
    fn the_elf_header_is_read_in_its_own_byte_order() {
        assert_eq!(elf_machine(&elf_header(0xB7)), Some(0xB7));
        let mut big = elf_header(0);
        big[5] = 2;
        big[18..20].copy_from_slice(&0xB7u16.to_be_bytes());
        assert_eq!(elf_machine(&big), Some(0xB7));
        assert_eq!(elf_machine(b"#!/bin/sh"), None);
        assert_eq!(elf_machine(&elf_header(0xB7)[..19]), None);
        assert_eq!(not_aarch64(&elf_header(0xB7)), None);
    }

    #[test]
    fn a_non_aarch64_binary_is_refused_with_its_machine() {
        assert!(not_aarch64(&elf_header(0x3E)).is_some_and(|why| why.contains("0x3e")));
        assert!(not_aarch64(b"#!/bin/sh").is_some_and(|why| why.contains("not an ELF")));
    }
}
