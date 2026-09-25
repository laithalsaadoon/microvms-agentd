// SPDX-License-Identifier: Apache-2.0
import { existsSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import { rawMarkdownUrl } from "../src/lib/agent-surface.js"

/**
 * The SDK references in `dist/`, checked against the two declaration files they are generated from.
 *
 * The expected set is read off the source files with a regex rather than taken from the generator, so
 * a generator that drops a class, or a TypeDoc option that folds interfaces into one page, fails here
 * by name instead of agreeing with itself. The declaration files are regular enough for that: napi-rs
 * writes one `export declare class X` per class at column 0, and the stub generator writes one
 * `class X:` per class at column 0.
 */

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const repoRoot = dirname(root)
const dist = join(root, "dist")

const CONFIG = {
  origin: (process.env.DOCS_SITE ?? "https://laithalsaadoon.github.io").replace(/\/+$/, ""),
  base: process.env.DOCS_BASE ?? "/microvms-agentd/"
}
const segment = CONFIG.base.endsWith("/") ? CONFIG.base : `${CONFIG.base}/`
const context = { site: new URL(CONFIG.origin), base: CONFIG.base }
const served = (path: string): string =>
  path.startsWith(segment) ? path.slice(segment.length) : path.replace(/^\/+/, "")

const dts = readFileSync(join(repoRoot, "microvms-js", "index.d.ts"), "utf8")
const pyi = readFileSync(join(repoRoot, "microvms-py", "microvms.pyi"), "utf8")

const names = (source: string, pattern: RegExp): ReadonlyArray<string> =>
  [...source.matchAll(pattern)].map((match) => match[1] ?? "").sort()

const TS_CLASSES = names(dts, /^export declare class (\w+)/gm)
const TS_INTERFACES = names(dts, /^export interface (\w+)/gm)
const TS_FUNCTIONS = names(dts, /^export declare function (\w+)/gm)

/** `class X:` or `class X(Base):` at column 0; exceptions are the ones with a base, all of them. */
const PY_ALL = [...pyi.matchAll(/^class (\w+)(?:\(([\w, ]+)\))?:/gm)]
const PY_CLASSES = PY_ALL.filter((match) => match[2] === undefined)
  .map((match) => match[1] ?? "")
  .sort()
const PY_EXCEPTIONS = PY_ALL.filter((match) => match[2] !== undefined)
  .map((match) => match[1] ?? "")
  .sort()
const PY_FUNCTIONS = names(pyi, /^def (\w+)\(/gm)

const read = (path: string): string => {
  if (!existsSync(dist))
    throw new Error("`dist/` is absent; run `pnpm run build` before this suite")
  if (!existsSync(path)) throw new Error(`${path} was not built`)
  return readFileSync(path, "utf8")
}

/** The rendered page and its raw Markdown twin, for a content id. */
const built = (id: string) => ({
  html: read(join(dist, id, "index.html")),
  twin: read(join(dist, served(rawMarkdownUrl(id, context).pathname)))
})

/**
 * The first sentence of a declaration's doc comment, read out of the source file: what a reader
 * should find on the page if the docstring survived parsing, rendering and the site build.
 */
const tsSummary = (name: string): string => {
  const at = dts.indexOf(`\nexport declare class ${name} `)
  const opener = dts.lastIndexOf("/**", at)
  const comment = opener === -1 ? "" : dts.slice(opener + 3, dts.indexOf("*/", opener))
  const line = comment
    .split("\n")
    .map((text) => text.replace(/^\s*\*?\s?/, "").trim())
    .find((text) => text !== "")
  if (at === -1 || line === undefined) {
    throw new Error(`no doc comment above \`export declare class ${name}\``)
  }
  return line
}

/** Each class body in the declarations, by name. napi-rs closes a class with `}` at column 0. */
const TS_BODIES = new Map(
  [...dts.matchAll(/^export declare class (\w+)[^\n]*\{\n([\s\S]*?)^\}/gm)].map((match) => [
    match[1] ?? "",
    match[2] ?? ""
  ])
)

const pySummary = (name: string): string => {
  const match = new RegExp(String.raw`^class ${name}:\n    """\n    (.+)\n`, "m").exec(pyi)
  if (match?.[1] === undefined) throw new Error(`no docstring on \`class ${name}\``)
  return match[1]
}

describe("the declaration files carry what this suite counts", () => {
  it("finds classes, interfaces, functions and exceptions, so nothing below is vacuous", () => {
    expect(TS_CLASSES.length).toBeGreaterThan(10)
    expect(TS_INTERFACES.length).toBeGreaterThan(10)
    expect(TS_FUNCTIONS.length).toBeGreaterThan(5)
    expect(PY_CLASSES.length).toBeGreaterThan(10)
    expect(PY_EXCEPTIONS.length).toBeGreaterThan(5)
    expect(PY_FUNCTIONS.length).toBeGreaterThan(5)
    expect(TS_CLASSES).toContain("Sandbox")
    expect(PY_CLASSES).toContain("Sandbox")
  })
})

describe("the Python SDK reference", () => {
  it("builds the overview, with a twin, linking every class", () => {
    const { html, twin } = built("reference/python")
    expect(html).toContain("<h1")
    expect(twin).toContain("## Install")
    for (const name of PY_CLASSES) {
      expect(twin, name).toContain(`](${segment}reference/python/classes/${name.toLowerCase()}/)`)
    }
  })

  it("gives every public class in microvms.pyi a page and a twin", () => {
    for (const name of PY_CLASSES) {
      const { html } = built(`reference/python/classes/${name.toLowerCase()}`)
      expect(html, name).toContain(`class ${name}`)
    }
  })

  it("puts every module-level function and every exception on the module page", () => {
    const { twin } = built("reference/python/module")
    for (const name of [...PY_FUNCTIONS, ...PY_EXCEPTIONS]) {
      expect(twin, name).toContain(`### \`${name}\``)
    }
  })

  it("carries a class docstring from the stub through to the rendered page", () => {
    const summary = pySummary("Region")
    const { html, twin } = built("reference/python/classes/region")
    expect(twin).toContain(summary)
    expect(html).toContain(summary)
  })
})

describe("the TypeScript SDK reference", () => {
  it("builds the overview, with a twin, linking every class and interface", () => {
    const { html, twin } = built("reference/typescript")
    expect(html).toContain("<h1")
    expect(twin).toContain("## Install")
    for (const name of TS_CLASSES) {
      expect(twin, name).toContain(
        `](${segment}reference/typescript/classes/${name.toLowerCase()}/)`
      )
    }
    for (const name of TS_INTERFACES) {
      expect(twin, name).toContain(
        `](${segment}reference/typescript/interfaces/${name.toLowerCase()}/)`
      )
    }
  })

  it("gives every exported class and interface in index.d.ts a page and a twin", () => {
    for (const name of TS_CLASSES) built(`reference/typescript/classes/${name.toLowerCase()}`)
    for (const name of TS_INTERFACES) built(`reference/typescript/interfaces/${name.toLowerCase()}`)
  })

  it("puts every exported function on the module page", () => {
    const { twin } = built("reference/typescript/module")
    for (const name of TS_FUNCTIONS) expect(twin, name).toContain(`### ${name}()`)
  })

  it("carries a class doc comment from the declarations through to the rendered page", () => {
    const summary = tsSummary("Region")
    const { html, twin } = built("reference/typescript/classes/region")
    expect(twin).toContain(summary)
    expect(html).toContain(summary)
  })

  it("documents no constructor napi-rs did not declare", () => {
    // `Region` has only static factories, so `new Region()` throws; the page must not offer it.
    expect(TS_BODIES.get("Region")).toBeDefined()
    expect(TS_BODIES.get("Region")).not.toMatch(/^ {2}constructor\(/m)
    const { twin } = built("reference/typescript/classes/region")
    expect(twin).not.toContain("new Region(")
    // A class that does declare `constructor(...)` keeps it.
    const declared = [...TS_BODIES]
      .filter(([, body]) => /^ {2}constructor\(/m.test(body))
      .map(([name]) => name)
    expect(declared.length).toBeGreaterThan(0)
    for (const name of declared) {
      expect(built(`reference/typescript/classes/${name.toLowerCase()}`).twin, name).toContain(
        `new ${name}(`
      )
    }
  })
})
