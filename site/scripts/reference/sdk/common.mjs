// SPDX-License-Identifier: Apache-2.0
/**
 * What the two SDK references share: routes, docstring hygiene, the README quickstart, and the
 * overview page's index table.
 *
 * `typescript.mjs` renders `microvms-js/index.d.ts` through TypeDoc and `python.mjs` renders
 * `microvms-py/microvms.pyi` through Griffe. Each returns `ReferencePage` records in the shape
 * `../pages.mjs` defines, and `../../gen-reference.mjs` writes them under the same ownership
 * manifest as the CLI reference, so a page here cannot collide with a page the sync writes and
 * cannot outlive the declaration it describes.
 */

import { readFileSync } from "node:fs"
import { join } from "node:path"

import { cell, code, fence, inlineText, link, table } from "../markdown.mjs"
import { routeOf, TIER } from "../pages.mjs"

/** @typedef {import("../pages.mjs").ReferencePage} ReferencePage */

/** The published origin plus base, as the package READMEs spell absolute links into this site. */
const PUBLISHED_SITE = "https://laithalsaadoon.github.io/microvms-agentd/"

/** How long a derived description may be before it is cut at a word boundary. */
const DESCRIPTION_LIMIT = 180

/**
 * The two languages, each with where its pages live and which files it reads.
 *
 * `directory` is under the tier, so `reference/python/` and `reference/typescript/` are the
 * overview routes. `source` is the repo-relative file every page of the language is derived
 * from, and it is what `gen-reference.mjs` stamps `lastUpdated` from.
 */
export const LANGUAGES = Object.freeze({
  python: Object.freeze({
    directory: `${TIER}/python`,
    label: "Python",
    source: "microvms-py/microvms.pyi",
    readme: "microvms-py/README.md",
    regenerate: "mise run stubs",
    check: "mise run stubs:check"
  }),
  typescript: Object.freeze({
    directory: `${TIER}/typescript`,
    label: "TypeScript",
    source: "microvms-js/index.d.ts",
    readme: "microvms-js/README.md",
    regenerate: "mise run dts",
    check: "mise run dts:check"
  })
})

/**
 * A page's slug from a declaration name: lowercased, because Starlight lowercases every content
 * id, so `AgentVm.md` is served at `agentvm/` whatever the file is called. Two names that differ
 * only in case would land on one route, and `gen-reference.mjs` refuses two pages on one path.
 *
 * @param {string} name
 */
export const pageSlug = (name) => name.toLowerCase().replace(/[^a-z0-9_-]/g, "-")

/**
 * A page record, with the route derived from the id so the two cannot disagree.
 *
 * @param {object} fields
 * @param {string} fields.id content id, no leading or trailing slash
 * @param {string} fields.title
 * @param {string} fields.description plain text
 * @param {string} fields.body Markdown
 * @param {string} fields.sidebarLabel
 * @param {number} fields.sidebarOrder
 * @param {string} fields.source repo-relative path the page is derived from
 * @param {boolean} [fields.index] write `<id>/index.md` rather than `<id>.md`: a language's
 *   overview, which shares its id with the directory its other pages live in
 * @returns {ReferencePage}
 */
export const page = ({
  id,
  title,
  description,
  body,
  sidebarLabel,
  sidebarOrder,
  source,
  index
}) => ({
  id,
  path: index === true ? `${id}/index.md` : `${id}.md`,
  route: routeOf(id),
  title,
  description,
  body,
  sidebarLabel,
  sidebarOrder,
  source
})

/**
 * The first paragraph of a docstring, on one line: what an index row and a meta description say.
 *
 * @param {string} docstring
 */
export const summaryOf = (docstring) =>
  (docstring.trim().split(/\n\s*\n/)[0] ?? "").replace(/\s*\n\s*/g, " ").trim()

/**
 * Plain text for a frontmatter description: no code markers or link syntax, one line, cut at a
 * word boundary.
 *
 * @param {string} text
 * @param {string} fallback used when the text is empty, because Starlight wants a description
 */
export const plainDescription = (text, fallback) => {
  const flat =
    text
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replaceAll("`", "")
      .replaceAll("**", "")
      .replace(/\s+/g, " ")
      .trim() || fallback
  if (flat.length <= DESCRIPTION_LIMIT) return flat
  const cut = flat.slice(0, DESCRIPTION_LIMIT)
  const boundary = cut.lastIndexOf(" ")
  return `${(boundary === -1 ? cut : cut.slice(0, boundary)).replace(/[,;:.-]$/, "")}...`
}

/** A fenced block: an opener, anything, the closer of the same run. Unterminated runs to the end. */
const FENCED = /^( {0,3})(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^\1?\2[^\n]*$|(?![\s\S]))/gm

/**
 * A docstring as Markdown that is safe to place under a member heading.
 *
 * The docstrings are written as Markdown, and two things in them are wrong for a reference page:
 *
 * - A `# Heading` line. rustdoc convention puts section headings in doc comments, and on a page
 *   whose member headings are `###`, an `h1` inside a member breaks the outline every screen
 *   reader and the table of contents navigate by. It becomes a bold lead-in paragraph instead.
 * - A bare `{` or `<` outside code. Every page reaches an MDX parser on its way to the raw
 *   Markdown twin, where `{` opens an expression and `<` opens an element; `inlineText` escapes
 *   both, and leaves code spans alone.
 *
 * Fenced blocks pass through untouched: inside a fence, both characters are leaf text already.
 *
 * @param {string} docstring
 */
export const prose = (docstring) => {
  const text = docstring.trim()
  if (text === "") return ""
  const parts = []
  let last = 0
  for (const match of text.matchAll(FENCED)) {
    parts.push(escapeProse(text.slice(last, match.index)))
    parts.push(match[0])
    last = (match.index ?? 0) + match[0].length
  }
  parts.push(escapeProse(text.slice(last)))
  return parts.join("")
}

/** @param {string} text */
const escapeProse = (text) =>
  inlineText(
    text.replace(/^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$/gm, (_, heading) => `**${heading}**`)
  )

/**
 * A heading for one declaration: the name as code, so an identifier with underscores does not
 * read as emphasis, and so the slug Starlight derives is the name itself.
 *
 * @param {number} depth
 * @param {string} name
 */
export const memberHeading = (depth, name) => `${"#".repeat(depth)} ${code(name)}`

/**
 * The in-page anchor Starlight gives a heading written by `memberHeading`: github-slugger over
 * the heading's text, which for an identifier is the identifier lowercased.
 *
 * @param {string} name
 */
export const memberAnchor = (name) => name.toLowerCase().replace(/[^\p{L}\p{N}_-]/gu, "")

/**
 * The install and quickstart sections of a package README, with links into this site made
 * root-relative so the links validator checks them and a fork builds them against its own base.
 *
 * Read at generation time, so the overview carries whatever the README says today.
 *
 * @param {string} repoRoot
 * @param {string} readmePath repo-relative
 * @param {ReadonlyArray<string>} headings the `##` headings to keep, in README order
 */
export const readmeSections = (repoRoot, readmePath, headings) => {
  const text = readFileSync(join(repoRoot, readmePath), "utf8")
  const chunks = text.split(/^(?=## )/m)
  const kept = headings.map((heading) => {
    const chunk = chunks.find((candidate) => candidate.startsWith(`## ${heading}\n`))
    if (chunk === undefined) {
      throw new Error(
        `${readmePath} has no "## ${heading}" section, which the SDK overview quotes. Rename the ` +
          "heading in site/scripts/reference/sdk/ to match the README."
      )
    }
    return chunk.trim()
  })
  return kept.join("\n\n").replaceAll(PUBLISHED_SITE, "/")
}

/**
 * @typedef {object} IndexRow
 * @property {string} name
 * @property {string} kind
 * @property {string} href root-relative, optionally with an anchor
 * @property {string} summary a docstring's first paragraph, Markdown
 */

/**
 * The overview's index: one row per declaration, linked to its page or anchor.
 *
 * @param {ReadonlyArray<IndexRow>} rows
 */
export const indexTable = (rows) =>
  table(
    ["Name", "Kind", "Summary"],
    rows.map((row) => [
      link(code(row.name), row.href),
      row.kind,
      row.summary === "" ? "-" : cell(row.summary)
    ])
  )

/**
 * The closing section every SDK page carries: which file it came from, what rendered it, and how
 * to regenerate the file.
 *
 * @param {{ source: string, regenerate: string, check: string }} language
 * @param {string} renderer the tool and version that parsed the source
 */
export const provenance = (language, renderer) =>
  [
    "## Provenance",
    `This page is generated from ${code(language.source)} by ${renderer}, run from ${code("site/scripts/gen-reference.mjs")} on every ${code("pnpm run sync")}, so an edit made here is overwritten by the next run.`,
    `To change the page, change the doc comment in the binding's Rust source, then regenerate the declarations with ${code(language.regenerate)}. ${code(language.check)} fails when the committed file no longer matches the source.`
  ].join("\n\n")

/**
 * The code block a signature renders as.
 *
 * @param {string} language
 * @param {string} signature
 */
export const signatureBlock = (language, signature) => fence(language, signature)

export { code, link }
