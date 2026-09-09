// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest"

import { fold } from "../src/aggregate.js"

/**
 * The arithmetic behind every floor in `src/gates.ts`. Small enough to read, and pinned here because
 * a wrong fold is the silent kind of defect: the Lighthouse tier would still pass, just against the
 * wrong run. The cases are lhci's semantics (`packages/utils/src/assertions.js`), which is what the
 * floors were measured against.
 */
describe("fold", () => {
  it("takes the best run under optimistic, in the direction that is good for the value", () => {
    expect(fold([0.7, 0.95, 0.9], "optimistic", "higher")).toBe(0.95)
    expect(fold([300, 100, 200], "optimistic", "lower")).toBe(100)
  })

  it("takes the worst run under pessimistic", () => {
    expect(fold([0.7, 0.95, 0.9], "pessimistic", "higher")).toBe(0.7)
    expect(fold([300, 100, 200], "pessimistic", "lower")).toBe(300)
  })

  it("takes the middle run at the median, and the mean of the two middle runs for an even count", () => {
    expect(fold([0.7, 0.95, 0.9], "median", "higher")).toBe(0.9)
    expect(fold([4, 1, 3, 2], "median", "lower")).toBe(2.5)
    expect(fold([1], "median", "higher")).toBe(1)
  })

  it("refuses to fold nothing rather than answering 0 or NaN", () => {
    expect(() => fold([], "optimistic", "higher")).toThrow("nothing to fold")
    expect(() => fold([], "median", "higher")).toThrow("nothing to fold")
  })
})
