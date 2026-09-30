// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest"

import { CATEGORIES, CHARTS, driftPage, validateHistory } from "../scripts/reference/drift.mjs"
import { beautifulMermaid } from "../src/lib/mermaid.js"

/**
 * The Architecture drift page, from a synthetic history, with no build and no git.
 *
 * The real history is one point until the file has been through a few merges, so the shapes a longer
 * series takes (a falling total, a working-tree point after commits) are asserted here instead of
 * waited for. `agent-surface.test.ts` covers the built page's figure along with every other diagram.
 */

// `parity-gap` and `untraced` are null where that commit's ratchet didn't collect them yet, as the
// history script prints them for every commit before #271 and #295.
const point = (
  sha: string | null,
  date: string,
  placement: number,
  subprocess: number,
  gaps: number | null = null,
  untraced: number | null = null
) => ({
  sha,
  date,
  counts: { placement, subprocess, "port-impl": 1, "parity-gap": gaps, untraced },
  total: placement + subprocess + 1 + (gaps ?? 0) + (untraced ?? 0),
  decisions: 4
})

const categories = ["placement", "subprocess", "port-impl", "parity-gap", "untraced"]

const history = validateHistory({
  source: "verify/ratchet/drift.json",
  categories,
  notCollected: { "later-gap": "until its collector lands" },
  points: [
    point("4119cd1ab7b3951ff80d075b8cf82aff185398d7", "2026-09-25T12:00:00Z", 5, 2),
    point("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "2026-10-01T09:30:00-05:00", 2, 2, 3),
    point("b2c3d4e5f60718293a4b5c6d7e8f901234567890", "2026-10-01T18:00:00Z", 2, 2, 3, 57),
    point(null, "2026-10-02T00:00:00+00:00", 2, 1, 3, 55)
  ]
})

const mermaidSources = (body: string): string[] =>
  [...body.matchAll(/^```mermaid\n([\s\S]*?)^```/gm)].map((match) => match[1] ?? "")

/** The values the site's renderer plotted for one chart, in order. */
const plotted = (source: string): string[] => {
  const svg = beautifulMermaid()({ source, meta: undefined, index: 0, label: "drift" })
  expect(svg.trimStart().startsWith("<svg")).toBe(true)
  return [...svg.matchAll(/data-value="([^"]*)"/g)].map((match) => match[1] ?? "")
}

/** The body of the numbered section whose heading carries `title`. */
const section = (body: string, title: string): string => {
  const parts = body.split(/^## /m)
  const found = parts.find((part) => part.split("\n", 1)[0]?.endsWith(title))
  if (found === undefined) throw new Error(`the page has no section titled ${title}`)
  return found
}

describe("the Architecture drift page", () => {
  const page = driftPage(history)
  // Looked up per case, so a page without a section fails the cases that read it, not the suite.
  const layering = () => section(page.body, "Layering and parity drift")
  const untraced = () => section(page.body, "Untraced requirements")

  it("charts the layering and parity total at every point, oldest first", () => {
    const [source] = mermaidSources(layering())
    expect(source).toContain(
      'x-axis ["2026-09-25 4119cd1", "2026-10-01 a1b2c3d", "2026-10-01 b2c3d4e", "2026-10-02 working tree"]'
    )
    expect(source).toContain("line [8, 8, 8, 7]")
  })

  it("charts untraced on its own, so its backlog doesn't swamp the layering line", () => {
    const [source] = mermaidSources(untraced())
    expect(source).toContain('x-axis ["2026-10-01 b2c3d4e", "2026-10-02 working tree"]')
    expect(source).toContain("line [57, 55]")
    expect(mermaidSources(page.body)).toHaveLength(2)
  })

  it("renders both charts with the site's own renderer, a mark for every total", () => {
    // The renderer returns an <svg> for a chart it drew nothing in, so the marks are counted.
    const sources = mermaidSources(page.body)
    expect(sources.map(plotted)).toEqual([
      ["8", "8", "8", "7"],
      ["57", "55"]
    ])
  })

  it("renders a chart whose categories one point counted, as the real untraced chart is", () => {
    const first = validateHistory({
      ...history,
      points: [...history.points.slice(0, 2), point(null, "2026-10-02T00:00:00Z", 2, 1, 3, 57)]
    })
    const [source] = mermaidSources(section(driftPage(first).body, "Untraced requirements"))
    expect(source).toBeDefined()
    expect(plotted(source ?? "")).toEqual(["57"])
  })

  it("has one table row per point, each category of the chart in its own column", () => {
    expect(layering()).toContain(
      "| Date | Commit | `placement` | `subprocess` | `port-impl` | `parity-gap` | Total | Decisions |"
    )
    expect(layering()).toContain("| 2026-10-02 | working tree | 2 | 1 | 1 | 3 | 7 | 4 |")
    expect(layering()).not.toContain("`untraced` |")
  })

  it("starts the untraced table at the first commit that counted it, not with rows of nothing", () => {
    expect(untraced()).toContain("| Date | Commit | `untraced` |")
    expect(untraced()).toContain("| 2026-10-01 | `b2c3d4e` | 57 |")
    expect(untraced()).toContain("| 2026-10-02 | working tree | 55 |")
    expect(untraced()).not.toContain("not collected")
    expect(untraced()).not.toContain("`4119cd1`")
    expect(untraced()).toContain(
      "The count at each commit, oldest first, from `b2c3d4e`, dated 2026-10-01, where the ratchet started collecting it."
    )
  })

  it("writes not collected where a commit's ratchet didn't count the category, not zero", () => {
    expect(layering()).toContain("| 2026-09-25 | `4119cd1` | 5 | 2 | 1 | not collected | 8 | 4 |")
  })

  it("names the commit a category started being counted at, where the total can jump", () => {
    expect(layering()).toContain(
      "`parity-gap` from `a1b2c3d`, dated 2026-10-01, where its first count joins the total"
    )
  })

  it("dates the latest count of each chart", () => {
    expect(layering()).toContain(
      "At the working tree, dated 2026-10-02, it's 7, with 4 decisions beside it."
    )
    expect(untraced()).toContain("At the working tree, dated 2026-10-02, it's 55.")
  })

  it("says what an entry is per category, rather than calling every entry layering drift", () => {
    const meaning = section(page.body, "What the count is")
    expect(meaning).not.toContain("is a place where a driving adapter")
    expect(meaning).toMatch(/^\| `placement` \| layering \| /m)
    expect(meaning).toMatch(/^\| `parity-gap` \| parity \| /m)
    expect(meaning).toMatch(/^\| `untraced` \| traceability \| .*`verify\/spec\/traced\/`/m)
    expect(page.description).not.toContain("How much layering drift")
    expect(section(page.body, "Provenance")).toContain(
      "trace the requirement in its group's file in `verify/spec/traced/`"
    )
  })

  it("says so when no commit counted a chart's categories yet, and draws nothing for it", () => {
    const before = validateHistory({ ...history, points: history.points.slice(0, 2) })
    const body = section(driftPage(before).body, "Untraced requirements")
    expect(body).toContain("No commit that changed the file counted it yet")
    expect(mermaidSources(body)).toHaveLength(0)
  })

  it("names a category that isn't collected rather than counting it", () => {
    expect(page.body).toContain("`later-gap` (until its collector lands)")
    expect(page.body).not.toContain("`later-gap` |")
  })

  it("refuses a category the page can't say anything about", () => {
    const mystery = {
      ...history,
      categories: [...categories, "mystery"],
      points: history.points.map((p) => ({ ...p, counts: { ...p.counts, mystery: 0 } }))
    }
    expect(() => validateHistory(mystery)).toThrow("doesn't describe mystery")
  })

  it("charts every kind of drift exactly once, so no category drops off the page", () => {
    for (const { kind } of Object.values(CATEGORIES)) {
      expect(
        CHARTS.filter((chart) => chart.kinds.includes(kind)),
        kind
      ).toHaveLength(1)
    }
  })

  it("refuses a history with no points, which means the file is gone", () => {
    expect(() => validateHistory({ ...history, points: [] })).toThrow("found no history")
  })

  it("refuses a point with a category missing, rather than charting a gap as zero", () => {
    const broken = {
      ...point(null, "2026-10-02T00:00:00Z", 1, 1),
      counts: { placement: 1, "port-impl": 1, "parity-gap": null, untraced: null }
    }
    expect(() => validateHistory({ ...history, points: [broken] })).toThrow(
      "no count for subprocess"
    )
  })
})
