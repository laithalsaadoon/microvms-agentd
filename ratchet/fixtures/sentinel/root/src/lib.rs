// SPDX-License-Identifier: Apache-2.0
//! The sentinel's composition root: it wires the kernel's ports and should implement none.

// An extension trait on a kernel type, the prelude's shape: the root's own public trait, so not
// a port, and its impl isn't a finding.
pub trait KernelExt {}

impl KernelExt for kernel::Local {}

// A port implemented in the root, which ARCH-8 says belongs below it.
pub struct Wired;

impl kernel::Fetch for Wired {}
