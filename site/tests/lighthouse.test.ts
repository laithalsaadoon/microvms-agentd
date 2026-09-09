// SPDX-License-Identifier: Apache-2.0
import { join } from "node:path"
import { fileURLToPath } from "node:url"

import { type LaunchedChrome, launch } from "chrome-launcher"
import lighthouse, { desktopConfig } from "lighthouse"
import { chromium } from "playwright"
import { afterAll, beforeAll, describe, expect, it } from "vitest"

import { fold } from "../src/aggregate.js"
import {
  AUDITED_PAGES,
  BASE,
  DIST_DIR,
  LIGHTHOUSE,
  TOTAL_BYTE_WEIGHT_BUDGET
} from "../src/gates.js"
import { type StaticSite, serveStatic } from "./static-server.js"

/**
 * The performance budget: Lighthouse over the five audited pages, three runs each, desktop preset,
 * against the same static server and the same Chromium the accessibility tier uses.
 *
 * Lighthouse is called as a library rather than through `@lhci/cli`. lhci's last release pins a
 * lighthouse whose puppeteer chain carries extract-zip 2.0.1, two High advisories with no fixed
 * release, and its `assert` step is the only part of it this repository used: a handful of floors
 * folded over three runs. That folding is `fold` in `src/aggregate.ts`, with lhci's vocabulary and
 * lhci's arithmetic (`packages/utils/src/assertions.js`), so the numbers in `src/gates.ts` mean what
 * they meant; `tests/aggregate.test.ts` pins the arithmetic in the node tier.
 *
 * Every run happens ONCE, in `beforeAll`, and each case reads the reports collected there, for the
 * same reason `tests/a11y.test.ts` visits each page once: fifteen Lighthouse runs are the cost of
 * this tier, and a case that re-ran them would double it.
 *
 * A page that is not in the build fails here by name, with its HTTP status, before Lighthouse ever
 * sees it. Lighthouse audits a 404 page as happily as any other, and a 404 is small and fast, so a
 * missing page would otherwise pass every floor.
 */

const dist = join(fileURLToPath(new URL("..", import.meta.url)), DIST_DIR)

type Report =
  Awaited<ReturnType<typeof lighthouse>> extends infer R
    ? R extends { lhr: infer L }
      ? L
      : never
    : never

let site: StaticSite
let chrome: LaunchedChrome
/** Every run's report, by audited page. */
const reports = new Map<string, ReadonlyArray<Report>>()

/**
 * An audit's score the way lhci read it: a `notApplicable` audit counts as passing and an
 * `informative` one as failing, so an audit that stopped being scored cannot pass by accident.
 */
const scoreOf = (report: Report, auditId: string): number => {
  const audit = report.audits[auditId]
  if (audit === undefined) throw new Error(`${report.finalDisplayedUrl}: no audit ${auditId}`)
  if (audit.scoreDisplayMode === "notApplicable") return 1
  if (audit.scoreDisplayMode === "informative") return 0
  if (audit.score === null) throw new Error(`${report.finalDisplayedUrl}: ${auditId} has no score`)
  return audit.score
}

const runsOf = (page: string): ReadonlyArray<Report> => {
  const runs = reports.get(page)
  if (runs === undefined) throw new Error(`${page} was never audited`)
  return runs
}

beforeAll(async () => {
  site = await serveStatic(dist, BASE)
  chrome = await launch({
    chromePath: chromium.executablePath(),
    chromeFlags: [...LIGHTHOUSE.chromeFlags]
  })
  for (const page of AUDITED_PAGES) {
    const url = `${site.origin}${page}`
    const status = (await fetch(url)).status
    if (status !== 200) {
      throw new Error(`${page} answered ${status}; it is not in the build (mise run docs:build)`)
    }
    const runs: Report[] = []
    for (let run = 0; run < LIGHTHOUSE.runs; run += 1) {
      const result = await lighthouse(
        url,
        {
          port: chrome.port,
          output: "json",
          logLevel: "error",
          skipAudits: [...LIGHTHOUSE.skipAudits]
        },
        desktopConfig
      )
      if (result === undefined) throw new Error(`${page}: Lighthouse returned no result`)
      if (result.lhr.runtimeError !== undefined) {
        throw new Error(
          `${page}: ${result.lhr.runtimeError.code} ${result.lhr.runtimeError.message}`
        )
      }
      runs.push(result.lhr)
    }
    reports.set(page, runs)
  }
})

afterAll(async () => {
  await chrome?.kill()
  await site?.close()
})

describe("Lighthouse over the audited pages", () => {
  it("ran every page the declared number of times, on the desktop preset", () => {
    expect([...reports.keys()]).toEqual([...AUDITED_PAGES])
    for (const page of AUDITED_PAGES) {
      const runs = runsOf(page)
      expect(runs.length, page).toBe(LIGHTHOUSE.runs)
      for (const run of runs) {
        expect(run.configSettings.formFactor, page).toBe("desktop")
        expect(run.configSettings.skipAudits, page).toEqual([...LIGHTHOUSE.skipAudits])
      }
    }
  })

  for (const [category, { floor, over }] of Object.entries(LIGHTHOUSE.categories)) {
    it(`holds the ${category} floor of ${floor}, ${over} over the runs`, () => {
      for (const page of AUDITED_PAGES) {
        const scores = runsOf(page).map((run) => {
          const score = run.categories[category]?.score
          if (score === undefined || score === null) {
            throw new Error(`${page}: no ${category} score in a run`)
          }
          return score
        })
        const folded = fold(scores, over, "higher")
        expect(
          folded,
          `${page}: ${category} ${folded} (${over} of ${scores.join(", ")}) is under ${floor}`
        ).toBeGreaterThanOrEqual(floor)
      }
    })
  }

  it(`transfers at most ${TOTAL_BYTE_WEIGHT_BUDGET} bytes on the heaviest run of every page`, () => {
    for (const page of AUDITED_PAGES) {
      const weights = runsOf(page).map((run) => {
        const value = run.audits["total-byte-weight"]?.numericValue
        if (value === undefined) throw new Error(`${page}: total-byte-weight has no numericValue`)
        return value
      })
      const folded = fold(weights, LIGHTHOUSE.byteWeightOver, "lower")
      expect(
        folded,
        `${page}: ${folded} bytes (${LIGHTHOUSE.byteWeightOver} of ${weights.join(", ")}) is over the budget`
      ).toBeLessThanOrEqual(TOTAL_BYTE_WEIGHT_BUDGET)
    }
  })

  for (const auditId of LIGHTHOUSE.passingAudits) {
    it(`passes ${auditId}`, () => {
      for (const page of AUDITED_PAGES) {
        const scores = runsOf(page).map((run) => scoreOf(run, auditId))
        expect(fold(scores, "optimistic", "higher"), `${page}: ${auditId}`).toBeGreaterThanOrEqual(
          0.9
        )
      }
    })
  }

  it("does not gate on Lighthouse's own CLS reading, which the layout probe replaces", () => {
    // The layout-stability probe holds the ceiling; a CLS floor here would be a second, flakier
    // reading of the same thing (`tests/layout-stability.test.ts` records the measurement).
    expect(Object.keys(LIGHTHOUSE.categories)).not.toContain("cumulative-layout-shift")
    expect(LIGHTHOUSE.passingAudits).not.toContain("cumulative-layout-shift")
  })
})
