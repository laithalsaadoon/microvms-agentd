// SPDX-License-Identifier: Apache-2.0
import { spawnSync } from "node:child_process"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

import { afterEach, describe, expect, it } from "vitest"

import { braceOffenders, maskedBody } from "../scripts/brace-gate.mjs"

const GATE = join(dirname(fileURLToPath(import.meta.url)), "..", "scripts", "brace-gate.mjs")

/**
 * The negative controls for the brace gate.
 *
 * The gate asserts over a found set — "no bare brace anywhere" — and every assertion over a found set is
 * green on an empty corpus and green on a broken scanner. Without a poison case, "the tree is clean" and
 * "the check does not work" are the same result.
 *
 * The poison is a synthetic input to a pure function, so nothing on disk moves and the control cannot rot
 * away from the gate it verifies: both read the same two exports.
 */

describe("the brace gate", () => {
  it("finds the REST path in a heading, which is how this corpus breaks the build", () => {
    const offenders = braceOffenders("# Title\n\n## GET /v1/exec/{id}/stream\n\nProse.\n")
    expect(offenders).toHaveLength(1)
    expect(offenders[0]?.line).toBe(3)
    expect(offenders[0]?.text).toContain("/v1/exec/{id}/stream")
  })

  it("accepts the same path inside backticks, which is the only legal repair", () => {
    /*
     * The other half of the pair. Without it the gate is satisfied by a scanner that refuses every brace,
     * including the ones the build accepts — and the fix the message recommends would not clear it.
     */
    expect(braceOffenders("## GET `/v1/exec/{id}/stream`\n")).toEqual([])
    expect(braceOffenders("A fence:\n\n```mermaid\ngraph TD\n  A{Decision} --> B\n```\n")).toEqual(
      []
    )
  })

  it("reports the line the brace is on, not the line it would be on with code deleted", () => {
    /*
     * The mask blanks fenced and inline code while KEEPING every newline. Deleting the regions instead is
     * enough to answer "is there a brace", and it moves every later line so the report points at the
     * wrong one — which sends a reader to a line that looks fine.
     */
    const body = ["# Title", "", "```json", '{ "a": 1 }', "```", "", "## GET /v1/x/{id}", ""].join(
      "\n"
    )
    expect(maskedBody(body).split("\n")).toHaveLength(body.split("\n").length)
    expect(braceOffenders(body).map((offender) => offender.line)).toEqual([7])
  })

  it("skips a brace the MDX parser reads as literal text", () => {
    expect(braceOffenders("Escaped: \\{not an expression}\n")).toEqual([])
  })

  it("ignores frontmatter, which reaches a different parser entirely", () => {
    expect(braceOffenders('---\ntitle: "a {brace} in a title"\n---\n\nProse.\n')).toEqual([])
  })
})

/**
 * The same controls one level up: the pure function can be right while the walk hands it nothing. These
 * run the gate as a process over a throwaway directory, because the floor and the sentinel live in
 * `main`, where the file set is.
 */
describe("the brace gate's file set", () => {
  const made = []
  afterEach(() => {
    for (const directory of made.splice(0)) rmSync(directory, { recursive: true, force: true })
  })

  const corpus = (pages) => {
    const root = mkdtempSync(join(tmpdir(), "brace-gate-"))
    made.push(root)
    for (const [path, body] of Object.entries(pages)) {
      mkdirSync(dirname(join(root, path)), { recursive: true })
      writeFileSync(join(root, path), body)
    }
    return root
  }

  const gate = (root) => spawnSync(process.execPath, [GATE, root], { encoding: "utf8" })

  it("fails on an empty directory instead of reporting a clean scan of nothing", () => {
    const run = gate(corpus({}))
    expect(run.status).toBe(1)
    expect(run.stderr).toContain("found no .md or .mdx files")
  })

  it("fails on pages that don't include the index, which every docs tree has", () => {
    const run = gate(corpus({ "learn/page.md": "# Page\n" }))
    expect(run.status).toBe(1)
    expect(run.stderr).toContain("index.md")
  })

  it("reads pages two directories down, where most of the real tree lives", () => {
    // The floor and the index sentinel both hold for a walk that stops at the first level, and a
    // walk like that passed the real corpus while skipping most of it (#277 review). A bare brace
    // this deep must still be found.
    const run = gate(
      corpus({ "index.md": "# Home\n", "learn/operations/deep.md": "GET /v1/{id}\n" })
    )
    expect(run.status).toBe(1)
    expect(run.stdout).toContain("deep.md:1:")
  })

  it("passes a clean tree that includes the index", () => {
    const run = gate(corpus({ "index.mdx": "# Home\n", "learn/page.md": "# Page\n" }))
    expect(run.stderr).toBe("")
    expect(run.status).toBe(0)
  })
})
