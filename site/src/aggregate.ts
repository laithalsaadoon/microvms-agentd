// SPDX-License-Identifier: Apache-2.0
import type { Aggregation } from "./gates.js"

/**
 * One number out of several runs, the way lhci folded them.
 *
 * `better` says which direction is good for the value being folded: for a score, higher; for a byte
 * count, lower. `optimistic` returns the best run under that direction, `pessimistic` the worst, and
 * `median` the middle value (the mean of the two middle values for an even count, as lhci did).
 */
export const fold = (
  values: ReadonlyArray<number>,
  over: Aggregation,
  better: "higher" | "lower"
): number => {
  if (values.length === 0) throw new Error("nothing to fold")
  const sorted = [...values].sort((a, b) => a - b)
  if (over === "median") {
    const middle = Math.floor((sorted.length - 1) / 2)
    const upper = sorted[middle + 1]
    const lower = sorted[middle]
    if (lower === undefined) throw new Error("nothing to fold")
    return sorted.length % 2 === 1 || upper === undefined ? lower : (lower + upper) / 2
  }
  const best = better === "higher" ? sorted[sorted.length - 1] : sorted[0]
  const worst = better === "higher" ? sorted[0] : sorted[sorted.length - 1]
  const picked = over === "optimistic" ? best : worst
  if (picked === undefined) throw new Error("nothing to fold")
  return picked
}
