// SPDX-License-Identifier: Apache-2.0
/**
 * The "Architecture drift" page: the count `verify/ratchet/drift.json` holds, at each commit that changed it.
 *
 * `tools/ratchet-history.py` reads the series out of git and prints it as JSON; this file lays it
 * out as tables and Mermaid `xychart-beta` lines, which `beautiful-mermaid` renders at build time
 * like every other diagram here. The split mirrors the Python SDK's: the history script owns the part
 * that needs git and the file's schema, and the layout stays in the tier's own Markdown helpers.
 *
 * Every number on the page is read off the history at build time and carries the date of the commit
 * it came from, which is the one form of count `AGENTS.md` allows in docs.
 */

import { execFileSync } from "node:child_process"
import { join } from "node:path"

import { cell, code, fence, inlineText, sections, table } from "./markdown.mjs"
import { routeOf, TIER } from "./pages.mjs"

/** @typedef {import("./pages.mjs").ReferencePage} ReferencePage */

/** The file the page counts, and the script that reads its history. */
export const DRIFT_SOURCE = "verify/ratchet/drift.json"
export const HISTORY_SCRIPT = "tools/ratchet-history.py"
const THIS_FILE = "site/scripts/reference/drift.mjs"

/**
 * What an entry in each category records, and the kind of drift that makes it. The file mixes kinds
 * since #271 and #295, so the page can't say one thing about every entry. A category the ratchet
 * collects and this table doesn't name fails `validateHistory`: the page would describe it wrongly.
 *
 * @type {Record<string, { kind: string, meaning: string }>}
 */
export const CATEGORIES = {
  placement: {
    kind: "layering",
    meaning: `a dependency a crate has outside its allowed set in ${code("verify/arch/placement.toml")}`
  },
  subprocess: {
    kind: "layering",
    meaning: `a ${code("Command::new")} in the source of a crate that ships`
  },
  "port-impl": {
    kind: "layering",
    meaning: `a port implemented above ${code("microvms-edges")}, where the production implementations belong`
  },
  "adapter-logic": {
    kind: "layering",
    meaning:
      "a control-plane operation name spelled out, or a default retyped as a number, in a driving adapter"
  },
  "parity-gap": {
    kind: "parity",
    meaning: `a capability one surface lacks until an issue closes it: an exemption in ${code("verify/parity/capabilities.toml")} that names the issue`
  },
  untraced: {
    kind: "traceability",
    meaning: `a requirement in ${code("verify/spec/")} that no group file in ${code("verify/spec/traced/")} lists, so ${code("trace:check")} holds no layer to it`
  }
}

/**
 * The charts, each over the categories of its kinds. Untraced requirements get their own because
 * their backlog arrived all at once with #295 and is larger than the rest of the file: on one line
 * it would flatten every layering fix into noise.
 *
 * @type {ReadonlyArray<{ title: string, axis: string, kinds: ReadonlyArray<string>, decisions: boolean }>}
 */
export const CHARTS = [
  {
    title: "Layering and parity drift",
    axis: "Drift entries",
    kinds: ["layering", "parity"],
    decisions: true
  },
  {
    title: "Untraced requirements",
    axis: "Untraced requirements",
    kinds: ["traceability"],
    // The ratchet refuses an untraced decision, so the history's file-wide decision count is
    // the first chart's alone.
    decisions: false
  }
]

/**
 * @typedef {object} DriftPoint
 * @property {string | null} sha the commit, or null for an uncommitted working-tree change
 * @property {string} date ISO 8601
 * @property {Record<string, number | null>} counts entries per category, null where that commit's
 *   ratchet didn't collect the category yet
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
  for (const category of history.categories) {
    if (!Object.hasOwn(CATEGORIES, category)) {
      throw new Error(
        `${HISTORY_SCRIPT} printed ${category}, and the page doesn't describe ${category}: add it to CATEGORIES in ${THIS_FILE}`
      )
    }
  }
  if (!Array.isArray(history.points) || history.points.length === 0) {
    // The working tree always yields a point while the file exists, so none means it's gone.
    throw new Error(`${HISTORY_SCRIPT} found no history for ${DRIFT_SOURCE}; is the file missing?`)
  }
  for (const point of history.points) {
    for (const category of history.categories) {
      const count = point.counts?.[category]
      // null is a count nobody took: the commit's ratchet didn't collect the category yet.
      if (count !== null && !Number.isInteger(count)) {
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
 * Each category first counted after the first point, with the point it was first counted at. The
 * total rises there by that category's first count, which is a new measurement, not new drift.
 *
 * @param {string[]} categories
 * @param {DriftPoint[]} points
 * @returns {[string, DriftPoint][]}
 */
const laterCategories = (categories, points) =>
  categories.flatMap((category) => {
    const first = points.findIndex((point) => point.counts[category] !== null)
    return first > 0 ? [[category, points[first]]] : []
  })

/**
 * The sum of a point's counts over some categories, a null count adding nothing.
 *
 * @param {DriftPoint} point
 * @param {string[]} categories
 */
const totalOf = (point, categories) =>
  categories.reduce((sum, category) => sum + (point.counts[category] ?? 0), 0)

/**
 * One line, the chart's total, because `xychart-beta` draws no legend and a second unlabeled line
 * would be a guess. The table below it carries each category.
 *
 * @param {DriftPoint[]} points
 * @param {number[]} totals
 * @param {string} axis
 */
const chart = (points, totals, axis) => {
  const labels = points.map((point) => `"${day(point)} ${commitOf(point)}"`)
  const ceiling = Math.max(1, Math.ceil(Math.max(...totals) * 1.25))
  return fence(
    "mermaid",
    [
      "xychart-beta",
      `  x-axis [${labels.join(", ")}]`,
      `  y-axis "${axis}" 0 --> ${ceiling}`,
      `  line [${totals.join(", ")}]`
    ].join("\n")
  )
}

/** @param {DriftPoint} point */
const where = (point) => (point.sha === null ? "the working tree" : code(commitOf(point)))

/**
 * A chart's section: the latest count, the line, and the table by commit. It starts at the first
 * point that counts one of its categories, so a chart whose categories came late doesn't open on a
 * run of "not collected" rows.
 *
 * @param {DriftHistory} history
 * @param {(typeof CHARTS)[number]} spec
 * @returns {import("./markdown.mjs").Section}
 */
const chartSection = (history, spec) => {
  const categories = history.categories.filter((category) =>
    spec.kinds.includes(CATEGORIES[category].kind)
  )
  const start = history.points.findIndex((point) =>
    categories.some((category) => point.counts[category] !== null)
  )
  if (start === -1) {
    return {
      title: spec.title,
      body: inlineText(
        `No commit that changed the file counted it yet: the chart starts at the first commit whose ratchet collects ${categories.map(code).join(", ")}.`
      )
    }
  }
  const points = history.points.slice(start)
  const totals = points.map((point) => totalOf(point, categories))
  const latest = points[points.length - 1]
  const later = laterCategories(categories, points)
  const several = categories.length > 1
  const lead =
    start === 0
      ? `The ${several ? "total" : "count"} at each commit, oldest first.`
      : `The ${several ? "total" : "count"} at each commit, oldest first, from ${where(points[0])}, dated ${day(points[0])}, where the ratchet started collecting it.`
  return {
    title: spec.title,
    body: [
      inlineText(
        `${lead} At ${where(latest)}, dated ${day(latest)}, it's ${totals[totals.length - 1]}${spec.decisions ? `, with ${latest.decisions} decisions beside it` : ""}.`
      ),
      chart(points, totals, spec.axis),
      ...(later.length === 0
        ? []
        : [
            inlineText(
              `A category joins the count at the commit whose ratchet started collecting it, and the cells before that read "not collected": ${later
                .map(
                  ([category, point]) =>
                    `${code(category)} from ${where(point)}, dated ${day(point)}, where its first count joins the total`
                )
                .join("; ")}.`
            )
          ]),
      table(
        [
          "Date",
          "Commit",
          ...categories.map(code),
          ...(several ? ["Total"] : []),
          ...(spec.decisions ? ["Decisions"] : [])
        ],
        points.map((point, at) => [
          day(point),
          point.sha === null ? "working tree" : code(commitOf(point)),
          ...categories.map((category) =>
            point.counts[category] === null ? "not collected" : String(point.counts[category])
          ),
          ...(several ? [String(totals[at])] : []),
          ...(spec.decisions ? [String(point.decisions)] : [])
        ])
      )
    ].join("\n\n")
  }
}

/**
 * @param {DriftHistory} history
 * @returns {ReferencePage}
 */
export const driftPage = (history) => {
  const id = `${TIER}/architecture-drift`
  const notCollected = Object.entries(history.notCollected ?? {})
  return {
    id,
    path: `${id}.md`,
    route: routeOf(id),
    title: "Architecture drift",
    description:
      "The drift verify/ratchet/drift.json records at each commit that changed the file: layering and parity drift, and the requirements no layer traces yet.",
    // After the four contract pages and the wire schema; the sidebar lists it by hand beside them.
    sidebarOrder: 5,
    sidebarLabel: "Architecture drift",
    source: DRIFT_SOURCE,
    body: sections([
      {
        title: "What the count is",
        body: [
          inlineText(
            `Each entry in ${code(DRIFT_SOURCE)} is one piece of drift, and what it records depends on its category. Layering drift is work a driving adapter (the CLI or a binding) does that belongs in a lower layer; a parity gap is a capability one surface has and another lacks; an untraced requirement is one no test, model or live check is held to yet.`
          ),
          table(
            ["Category", "Kind", "An entry is"],
            history.categories.map((category) => [
              code(category),
              CATEGORIES[category].kind,
              cell(CATEGORIES[category].meaning)
            ])
          ),
          inlineText(
            `A decision in ${code("verify/ratchet/decisions.toml")} is a permanent exception with its reason, and it isn't counted. ${code("mise run ratchet:check")} collects the drift from a change's tree and from its merge base's, and fails on drift the base doesn't have, so the count can only go down.`
          ),
          ...(notCollected.length === 0
            ? []
            : [
                inlineText(
                  `Not collected yet, so absent from the tables rather than shown as zero: ${notCollected
                    .map(([category, when]) => `${code(category)} (${when})`)
                    .join(", ")}.`
                )
              ])
        ].join("\n\n")
      },
      ...CHARTS.map((spec) => chartSection(history, spec)),
      {
        title: "Provenance",
        body: inlineText(
          `This page is generated from the git history of ${code(DRIFT_SOURCE)}: ${code(HISTORY_SCRIPT)} reads the file at each first-parent commit that changed it, and ${code("site/scripts/gen-reference.mjs")} writes the page on every ${code("pnpm run sync")}. Until the merge-base rule the file was the hand-kept count, so each merge that moved it is a point; since then it's a snapshot that ${code("mise run ratchet:snapshot")} rewrites in a change of its own, so each rewrite is one. A working tree whose copy differs from HEAD's adds a last point marked "working tree". To change a number, fix what the entry names (move the code, give the surface the capability, or trace the requirement in its group's file in ${code("verify/spec/traced/")}); the next snapshot counts it.`
        )
      }
    ])
  }
}
