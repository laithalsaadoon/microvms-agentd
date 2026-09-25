// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest"

import { driftPage, validateHistory } from "../scripts/reference/drift.mjs"
import { beautifulMermaid } from "../src/lib/mermaid.js"

/**
 * The Architecture drift page, from a synthetic history, with no build and no git.
 *
 * The real history is one point until the file has been through a few merges, so the shapes a longer
 * series takes (a falling total, a working-tree point after commits) are asserted here instead of
 * waited for. `agent-surface.test.ts` covers the built page's figure along with every other diagram.
 */

const point = (sha: string | null, date: string, placement: number, subprocess: number) => ({
  sha,
  date,
  counts: { placement, subprocess, "port-impl": 1 },
  total: placement + subprocess + 1,
  decisions: 4
})

const history = validateHistory({
  source: "ratchet/drift.json",
  categories: ["placement", "subprocess", "port-impl"],
  notCollected: { "parity-gap": "until #271's `parity:check --json` lands" },
  points: [
    point("4119cd1ab7b3951ff80d075b8cf82aff185398d7", "2026-09-25T12:00:00Z", 5, 2),
    point("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "2026-10-01T09:30:00-05:00", 2, 2),
    point(null, "2026-10-02T00:00:00+00:00", 2, 1)
  ]
})

const mermaidSource = (body: string): string => {
  const match = /^```mermaid\n([\s\S]*?)^```/m.exec(body)
  if (match?.[1] === undefined) throw new Error("the page carries no mermaid fence")
  return match[1]
}

describe("the Architecture drift page", () => {
  const page = driftPage(history)

  it("charts the total at every point, oldest first", () => {
    const source = mermaidSource(page.body)
    expect(source).toContain(
      'x-axis ["2026-09-25 4119cd1", "2026-10-01 a1b2c3d", "2026-10-02 working tree"]'
    )
    expect(source).toContain("line [8, 5, 4]")
  })

  it("renders the chart with the site's own renderer", () => {
    const svg = beautifulMermaid()({
      source: mermaidSource(page.body),
      meta: undefined,
      index: 0,
      label: "drift"
    })
    expect(svg.trimStart().startsWith("<svg")).toBe(true)
  })

  it("has one table row per point, each category in its own column", () => {
    expect(page.body).toContain(
      "| Date | Commit | `placement` | `subprocess` | `port-impl` | Total | Decisions |"
    )
    expect(page.body).toContain("| 2026-09-25 | `4119cd1` | 5 | 2 | 1 | 8 | 4 |")
    expect(page.body).toContain("| 2026-10-02 | working tree | 2 | 1 | 1 | 4 | 4 |")
  })

  it("dates the latest count", () => {
    expect(page.body).toContain(
      "At the working tree, dated 2026-10-02, it's 4, with 4 decisions beside it."
    )
  })

  it("names a category that isn't collected rather than counting it", () => {
    expect(page.body).toContain("`parity-gap` (until #271's `parity:check --json` lands)")
    expect(page.body).not.toContain("`parity-gap` |")
  })

  it("refuses a history with no points, which means the file is gone", () => {
    expect(() => validateHistory({ ...history, points: [] })).toThrow("found no history")
  })

  it("refuses a point with a category missing, rather than charting a gap as zero", () => {
    const broken = { ...point(null, "2026-10-02T00:00:00Z", 1, 1), counts: { placement: 1 } }
    expect(() => validateHistory({ ...history, points: [broken] })).toThrow(
      "no count for subprocess"
    )
  })
})
