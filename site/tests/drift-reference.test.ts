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

// `parity-gap` is null where that commit's ratchet didn't collect it yet, as the history script
// prints it for every commit before #271.
const point = (
  sha: string | null,
  date: string,
  placement: number,
  subprocess: number,
  gaps: number | null = null
) => ({
  sha,
  date,
  counts: { placement, subprocess, "port-impl": 1, "parity-gap": gaps },
  total: placement + subprocess + 1 + (gaps ?? 0),
  decisions: 4
})

const history = validateHistory({
  source: "ratchet/drift.json",
  categories: ["placement", "subprocess", "port-impl", "parity-gap"],
  notCollected: { "later-gap": "until its collector lands" },
  points: [
    point("4119cd1ab7b3951ff80d075b8cf82aff185398d7", "2026-09-25T12:00:00Z", 5, 2),
    point("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "2026-10-01T09:30:00-05:00", 2, 2, 3),
    point(null, "2026-10-02T00:00:00+00:00", 2, 1, 3)
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
    expect(source).toContain("line [8, 8, 7]")
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
      "| Date | Commit | `placement` | `subprocess` | `port-impl` | `parity-gap` | Total | Decisions |"
    )
    expect(page.body).toContain("| 2026-10-02 | working tree | 2 | 1 | 1 | 3 | 7 | 4 |")
  })

  it("writes not collected where a commit's ratchet didn't count the category, not zero", () => {
    expect(page.body).toContain("| 2026-09-25 | `4119cd1` | 5 | 2 | 1 | not collected | 8 | 4 |")
  })

  it("names the commit a category started being counted at, where the total can jump", () => {
    expect(page.body).toContain(
      "`parity-gap` from `a1b2c3d`, dated 2026-10-01, where its first count joins the total"
    )
  })

  it("dates the latest count", () => {
    expect(page.body).toContain(
      "At the working tree, dated 2026-10-02, it's 7, with 4 decisions beside it."
    )
  })

  it("names a category that isn't collected rather than counting it", () => {
    expect(page.body).toContain("`later-gap` (until its collector lands)")
    expect(page.body).not.toContain("`later-gap` |")
  })

  it("refuses a history with no points, which means the file is gone", () => {
    expect(() => validateHistory({ ...history, points: [] })).toThrow("found no history")
  })

  it("refuses a point with a category missing, rather than charting a gap as zero", () => {
    const broken = {
      ...point(null, "2026-10-02T00:00:00Z", 1, 1),
      counts: { placement: 1, "port-impl": 1, "parity-gap": null }
    }
    expect(() => validateHistory({ ...history, points: [broken] })).toThrow(
      "no count for subprocess"
    )
  })
})
