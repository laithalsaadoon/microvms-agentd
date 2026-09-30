// SPDX-License-Identifier: Apache-2.0
//
// The claims `src/hooks.rs` and `src/lib.rs` make about `tsc` (#337): the two hook-timeout
// classes are different types, and an object literal is neither one nor an `EstimatedUsd`.
// Each `@ts-expect-error` is one claim. When the declarations let the mistake through, the
// line compiles and the directive goes unused (TS2578), so each is also this probe's control.
import { BuildHookTimeout, EstimatedUsd, RunHookTimeout } from '@theagenticguy/microvms'

export function runCeiling(timeout: RunHookTimeout): number {
  return timeout.maxSecs
}

export function buildCeiling(timeout: BuildHookTimeout): number {
  return timeout.maxSecs
}

export function matched(): number {
  return runCeiling(new RunHookTimeout(30)) + buildCeiling(new BuildHookTimeout(1800))
}

export function transposed(): number {
  // @ts-expect-error: a build-family timeout is not a run-family one, though every member shares a name
  const run = runCeiling(new BuildHookTimeout(1800))
  // @ts-expect-error: nor is a run-family timeout a build-family one
  const build = buildCeiling(new RunHookTimeout(30))
  return run + build
}

export function literals(): void {
  // @ts-expect-error: an object literal is not a RunHookTimeout, the coercion a #[napi(object)] allows
  runCeiling({ seconds: 3600 })
  // @ts-expect-error: nor is one an EstimatedUsd
  const usd: EstimatedUsd = { amount: 1.5 }
  void usd
}
