// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest"

import { robotsPolicy } from "../src/lib/robots.js"

/**
 * The crawler policy as text, split into the groups a crawler reads.
 *
 * This site never emits the file at its current base (see `src/lib/robots.ts`), so nothing in `dist/`
 * exercises it; the policy is checked here, against the string the integration would write. A group is
 * a run of `User-agent:` lines followed by its rules, ended by a blank line.
 */
const SITEMAP = "https://example.test/docs/sitemap-index.xml"

const groups = (): { agents: string[]; rules: string[] }[] => {
  const lines = robotsPolicy(SITEMAP)
    .split("\n")
    .filter((line) => !line.startsWith("#"))
  const out: { agents: string[]; rules: string[] }[] = []
  let current: { agents: string[]; rules: string[] } | undefined
  for (const line of lines) {
    if (line.trim() === "") {
      current = undefined
      continue
    }
    const field = line.slice(0, line.indexOf(":")).toLowerCase()
    if (field === "user-agent") {
      if (!current || current.rules.length > 0) {
        current = { agents: [], rules: [] }
        out.push(current)
      }
      current.agents.push(line.slice(line.indexOf(":") + 1).trim())
    } else if (current) {
      current.rules.push(line)
    }
  }
  return out
}

describe("robots policy", () => {
  it("has the named groups and the fallback group", () => {
    const all = groups().flatMap((g) => g.agents)
    expect(all).toContain("*")
    expect(all).toContain("GPTBot")
    expect(all).toContain("Google-Extended")
  })

  it("uses only RFC 9309 product-token characters", () => {
    // s2.2.1 allows a-zA-Z_- in a token. A look-alike hyphen parses as a token no crawler answers to.
    for (const agent of groups().flatMap((g) => g.agents)) {
      expect(agent).toMatch(/^(\*|[A-Za-z_-]+)$/)
    }
  })

  it("carries one Content-Signal line with all three categories in every group", () => {
    // A crawler obeys only its own group, so a signal stated in `*` alone never reaches a named token.
    for (const group of groups()) {
      const signals = group.rules.filter((rule) => rule.startsWith("Content-Signal:"))
      expect(signals, group.agents.join(", ")).toHaveLength(1)
      const labels = (signals[0] ?? "")
        .slice("Content-Signal:".length)
        .split(",")
        .map((pair) => pair.trim().split("=")[0])
        .sort()
      expect(labels, group.agents.join(", ")).toEqual(["ai-input", "ai-train", "search"])
    }
  })

  it("names the sitemap once, as an absolute URL outside every group", () => {
    const text = robotsPolicy(SITEMAP)
    expect(text.match(/^Sitemap: .*$/gm)).toEqual([`Sitemap: ${SITEMAP}`])
    expect(groups().every((g) => g.rules.every((rule) => !rule.startsWith("Sitemap:")))).toBe(true)
  })
})
