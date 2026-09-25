// SPDX-License-Identifier: Apache-2.0
/**
 * The "Architecture drift" page: the count `ratchet/drift.json` holds, at each commit that changed it.
 *
 * `scripts/ratchet-history.py` reads the series out of git and prints it as JSON; this file lays it
 * out as a table and a Mermaid `xychart-beta` line, which `beautiful-mermaid` renders at build time
 * like every other diagram here. The split mirrors the Python SDK's: the history script owns the part
 * that needs git and the file's schema, and the layout stays in the tier's own Markdown helpers.
 *
 * Every number on the page is read off the history at build time and carries the date of the commit
 * it came from, which is the one form of count `AGENTS.md` allows in docs.
 */

import { execFileSync } from "node:child_process"
import { join } from "node:path"

import { code, fence, inlineText, sections, table } from "./markdown.mjs"
import { routeOf, TIER } from "./pages.mjs"

/** @typedef {import("./pages.mjs").ReferencePage} ReferencePage */

/** The file the page counts, and the script that reads its history. */
export const DRIFT_SOURCE = "ratchet/drift.json"
export const HISTORY_SCRIPT = "scripts/ratchet-history.py"

/**
 * @typedef {object} DriftPoint
 * @property {string | null} sha the commit, or null for an uncommitted working-tree change
 * @property {string} date ISO 8601
 * @property {Record<string, number>} counts entries per collected category
 * @property {number} total
 * @property {number} decisions
 */

/**
 * @typedef {object} DriftHistory
 * @property {string} source
 * @property {string[]} categories the collected categories, in the script's order
 * @property {Record<string, string>} notCollected category -> when it starts being collected
 * @property {DriftPoint[]} points oldest first
 */

/**
 * Run the history script and return what it printed.
 *
 * Through `uv run --script` for the reason `loadPythonSurface` gives, with `VIRTUAL_ENV` dropped the
 * same way. The script has no dependencies, so there's no lockfile to pass `--locked` against.
 *
 * @param {string} repoRoot
 * @returns {DriftHistory}
 */
export const loadDriftHistory = (repoRoot) => {
  const env = { ...process.env }
  delete env.VIRTUAL_ENV
  let out
  try {
    out = execFileSync(
      "uv",
      ["run", "--quiet", "--script", join(repoRoot, HISTORY_SCRIPT), "--root", repoRoot],
      { cwd: repoRoot, encoding: "utf8", env, maxBuffer: 16 * 1024 * 1024 }
    )
  } catch (cause) {
    throw new Error(
      `${HISTORY_SCRIPT} could not read the history of ${DRIFT_SOURCE}. It needs uv and git on PATH; ` +
        "the error above is its own.",
      { cause }
    )
  }
  return validateHistory(JSON.parse(out))
}

/**
 * @param {unknown} parsed
 * @returns {DriftHistory}
 */
export const validateHistory = (parsed) => {
  const history = /** @type {DriftHistory} */ (parsed)
  if (!Array.isArray(history?.categories) || history.categories.length === 0) {
    throw new Error(`${HISTORY_SCRIPT} printed no categories`)
  }
  if (!Array.isArray(history.points) || history.points.length === 0) {
    // The working tree always yields a point while the file exists, so none means it's gone.
    throw new Error(`${HISTORY_SCRIPT} found no history for ${DRIFT_SOURCE}; is the file missing?`)
  }
  for (const point of history.points) {
    for (const category of history.categories) {
      if (!Number.isInteger(point.counts?.[category])) {
        throw new Error(`a point in ${HISTORY_SCRIPT}'s output has no count for ${category}`)
      }
    }
  }
  return history
}

/** @param {DriftPoint} point */
const day = (point) => point.date.slice(0, 10)

/** @param {DriftPoint} point */
const commitOf = (point) => (point.sha === null ? "working tree" : point.sha.slice(0, 7))

/**
 * The line chart: one line, the total, because `xychart-beta` draws no legend and a second unlabeled
 * line would be a guess. The table below it carries each category.
 *
 * @param {DriftHistory} history
 */
const chart = (history) => {
  const labels = history.points.map((point) => `"${day(point)} ${commitOf(point)}"`)
  const totals = history.points.map((point) => point.total)
  const ceiling = Math.max(1, Math.ceil(Math.max(...totals) * 1.25))
  return fence(
    "mermaid",
    [
      "xychart-beta",
      `  x-axis [${labels.join(", ")}]`,
      `  y-axis "Drift entries" 0 --> ${ceiling}`,
      `  line [${totals.join(", ")}]`
    ].join("\n")
  )
}

/**
 * @param {DriftHistory} history
 * @returns {ReferencePage}
 */
export const driftPage = (history) => {
  const id = `${TIER}/architecture-drift`
  const latest = history.points[history.points.length - 1]
  const notCollected = Object.entries(history.notCollected ?? {})
  return {
    id,
    path: `${id}.md`,
    route: routeOf(id),
    title: "Architecture drift",
    description:
      "How much layering drift the repository carries: the entries in ratchet/drift.json per category, at each commit that changed the file.",
    // After the four contract pages and the wire schema; the sidebar lists it by hand beside them.
    sidebarOrder: 5,
    sidebarLabel: "Architecture drift",
    source: DRIFT_SOURCE,
    body: sections([
      {
        title: "What the count is",
        body: inlineText(
          `Each entry in ${code(DRIFT_SOURCE)} is a place where a driving adapter (the CLI or a binding) does work that belongs in a lower layer, and it names the issue that removes it. A decision is a permanent exception with its reason, and it isn't counted. ${code("mise run ratchet:check")} fails when the file and the tree disagree in either direction, and it refuses an entry the base branch doesn't have, so the count can only go down.`
        )
      },
      {
        title: "The trend",
        body: [
          inlineText(
            `The total at each commit, oldest first. At ${latest.sha === null ? "the working tree" : code(commitOf(latest))}, dated ${day(latest)}, it's ${latest.total}, with ${latest.decisions} decisions beside it.`
          ),
          chart(history)
        ].join("\n\n")
      },
      {
        title: "By commit",
        body: [
          table(
            ["Date", "Commit", ...history.categories.map(code), "Total", "Decisions"],
            history.points.map((point) => [
              day(point),
              point.sha === null ? "working tree" : code(commitOf(point)),
              ...history.categories.map((category) => String(point.counts[category])),
              String(point.total),
              String(point.decisions)
            ])
          ),
          ...(notCollected.length === 0
            ? []
            : [
                inlineText(
                  `Not collected yet, so absent from the table rather than shown as zero: ${notCollected
                    .map(([category, when]) => `${code(category)} (${when})`)
                    .join(", ")}.`
                )
              ])
        ].join("\n\n")
      },
      {
        title: "Provenance",
        body: inlineText(
          `This page is generated from the git history of ${code(DRIFT_SOURCE)}: ${code(HISTORY_SCRIPT)} reads the file at each first-parent commit that changed it, and ${code("site/scripts/gen-reference.mjs")} writes the page on every ${code("pnpm run sync")}. A working tree whose copy differs from HEAD's adds a last point marked "working tree". To change a number, change the code the entry names and run ${code("mise run ratchet:update")}.`
        )
      }
    ])
  }
}
