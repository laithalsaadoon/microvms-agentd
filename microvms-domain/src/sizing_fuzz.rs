// SPDX-License-Identifier: Apache-2.0
//! The fuzz harness for BIND-14: [`SizeClass::from_request`] over arbitrary requests.
//!
//! `bolero::check!` runs this as an ordinary `#[test]` under stable `cargo test`, and as a
//! coverage-guided target under
//! `cargo +nightly bolero test sizing_fuzz::from_request_is_minimal_and_covering -p microvms-domain -T 120s`.
//!
//! # What it checks
//!
//! * Coverage: an answered class's baseline covers both named axes.
//! * Minimality: no smaller class covers them, so the answer is the smallest.
//! * Refusal: a refusal means no class covers the request (it names the largest), or the CPU
//!   figure is not a finite non-negative number. Never a refusal for a coverable request.
//! * Nothing named, or zeros, is the default class.

use crate::error::ErrorKind;
use crate::sizing::SizeClass;

/// Whether `class`'s baseline covers the request; `None` and zero are no requirement.
fn covers(class: SizeClass, cpus: Option<f64>, memory_mib: Option<u32>) -> bool {
    cpus.is_none_or(|cpus| class.baseline_vcpu() >= cpus)
        && memory_mib.is_none_or(|mib| class.baseline_mib() >= mib)
}

#[test]
fn from_request_is_minimal_and_covering() {
    bolero::check!()
        .with_type::<(Option<f64>, Option<u32>, bool)>()
        .for_each(|(cpus, memory_mib, scale)| {
            // Raw f64s are mostly huge or tiny; scaling a share of them into the table's
            // range (0..8 vCPU) makes the boundaries reachable.
            let cpus = cpus.map(|cpus| if *scale { cpus.abs() % 8.0 } else { cpus });
            let memory_mib = memory_mib.map(|mib| if *scale { mib % 10_000 } else { mib });
            let quantity = cpus.is_none_or(|cpus| cpus.is_finite() && cpus >= 0.0);
            let named = cpus.is_some_and(|cpus| cpus != 0.0) || memory_mib.is_some_and(|m| m != 0);
            match SizeClass::from_request(cpus, memory_mib) {
                Ok(class) => {
                    assert!(quantity, "{cpus:?} is not a quantity but was answered");
                    if !named {
                        assert_eq!(class, SizeClass::DEFAULT, "nothing named is the default");
                        return;
                    }
                    assert!(covers(class, cpus, memory_mib), "{class:?} does not cover");
                    for smaller in SizeClass::ALL.into_iter().take_while(|c| *c != class) {
                        assert!(
                            !covers(smaller, cpus, memory_mib),
                            "{smaller:?} is smaller and covers {cpus:?} / {memory_mib:?}"
                        );
                    }
                }
                Err(error) => {
                    assert_eq!(error.kind(), ErrorKind::InvalidArg, "{error}");
                    if quantity {
                        assert!(
                            !covers(SizeClass::Mib8192, cpus, memory_mib),
                            "a coverable request was refused: {cpus:?} / {memory_mib:?}"
                        );
                        assert!(error.to_string().contains("largest size class"), "{error}");
                    }
                }
            }
        });
}
