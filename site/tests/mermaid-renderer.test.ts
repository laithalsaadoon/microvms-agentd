// SPDX-License-Identifier: Apache-2.0
import { readdirSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import { beautifulMermaid, figureName, MERMAID_LANG, mermaidPlugin } from "../src/lib/mermaid.js"

/**
 * The renderer gate: does `beautiful-mermaid` cover every diagram in this corpus, and does the plugin
 * refuse the ones it cannot draw?
 *
 * This runs with NO BUILD, in milliseconds, over the fences as authored. It is the cheap half of the
 * pair — `agent-surface.test.ts` asserts that each fence became a figure in the HTML and stayed a fence
 * in the twin, which needs `dist/`. This one answers the question that decides the renderer, and it
 * answers it before a build is paid for.
 *
 * The renderer is a subset of Mermaid, and a diagram type it does not implement is a BUILD FAILURE by
 * design: a fence that silently survives as a code block ships a page that looks plausible and an
 * artifact that is wrong, with nothing reported anywhere. So the throw is asserted here too.
 */

const tree = join(dirname(dirname(dirname(fileURLToPath(import.meta.url)))), "docs")

/** Every mermaid fence in the source tree, with the file and the diagram's own header line. */
const fences = (): ReadonlyArray<{ file: string; header: string; source: string }> => {
  const walk = (directory: string): ReadonlyArray<string> =>
    readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) return entry.name.startsWith(".") ? [] : walk(path)
      return entry.name.endsWith(".md") ? [path] : []
    })

  return walk(tree).flatMap((file) =>
    [...readFileSync(file, "utf8").matchAll(/^```mermaid[^\n]*\n([\s\S]*?)^```/gm)].map((match) => {
      const source = match[1] ?? ""
      return {
        file: file.slice(tree.length + 1),
        header: source.trim().split("\n")[0]?.trim() ?? "",
        source
      }
    })
  )
}

describe("the mermaid renderer covers this corpus", () => {
  const corpus = fences()

  it("finds the fences it is meant to check, so nothing below is vacuous", () => {
    expect(corpus.length).toBeGreaterThan(0)
  })

  it.each(
    fences().map((fence, index) => [`${fence.file}#${index} ${fence.header}`, fence] as const)
  )("renders %s to an SVG", (_label, fence) => {
    const svg = beautifulMermaid({ bg: "var(--sl-color-bg)", fg: "var(--sl-color-text)" })({
      source: fence.source,
      meta: undefined,
      index: 0,
      label: fence.file
    })
    expect(svg.trimStart().startsWith("<svg")).toBe(true)
    expect(svg).toContain("</svg>")
  })

  it("strips the webfont import the renderer writes unconditionally", () => {
    /*
     * The SVG carries `@import url('https://fonts.googleapis.com/…')` with no option to suppress it, so
     * a build-time render would ship a third-party font request on every page carrying a diagram — a
     * privacy fact and a render-block that is invisible in the integration's configuration.
     */
    const diagram = { source: "graph TD\n  A --> B", meta: undefined, index: 0, label: "probe" }
    expect(beautifulMermaid()(diagram)).not.toContain("fonts.googleapis.com")
    expect(beautifulMermaid({ webfontImport: true })(diagram)).toContain("fonts.googleapis.com")
  })

  it("refuses a diagram type it cannot draw, rather than passing the fence through", () => {
    /*
     * The negative control. Without it "the corpus renders" and "the renderer accepts anything" are the
     * same green — and the failure mode being guarded is precisely a fence that survives as a code block
     * on a build that succeeded.
     */
    const render = beautifulMermaid()
    expect(() =>
      render({
        source: "gantt\n  title A\n  section S\n  T :a1, 2020-01-01, 30d",
        meta: undefined,
        index: 0,
        label: "probe"
      })
    ).toThrow()
    expect(() => render({ source: "", meta: undefined, index: 0, label: "probe" })).toThrow()
  })
})

describe("the plugin", () => {
  /** The visitor's context, reduced to what the `code` visitor reads: a fence with no ancestors. */
  const context = {
    fileURL: undefined,
    sourceFormat: "markdown",
    parent: () => undefined,
    indexOf: () => undefined,
    textContent: () => "",
    data: {}
  } as never

  it("claims only the mermaid fence and leaves every other language alone", () => {
    const plugin = mermaidPlugin({ renderer: () => "<svg></svg>" })
    const code = plugin.code
    if (typeof code !== "function") throw new Error("the plugin declares no `code` visitor")

    const claimed = code(
      { type: "code", lang: MERMAID_LANG, value: "graph TD\n A-->B" } as never,
      context
    )
    expect(claimed).toMatchObject({ type: "html" })
    // The figure is one decided image: a role and a name the a11y tier's `inline-svg-undecided`
    // probe accepts through the ancestor. With no heading and no info string, the name is positional.
    expect((claimed as { value: string }).value).toContain(
      '<figure class="docs-mermaid" role="img" aria-label="Diagram 1" tabindex="0"><svg>'
    )

    // A `rust` fence is the overwhelming majority of this corpus; claiming it would replace every code
    // block on the site with an SVG.
    expect(
      code({ type: "code", lang: "rust", value: "fn main() {}" } as never, context)
    ).toBeUndefined()
  })

  it("throws rather than returning the node when the renderer fails", () => {
    const plugin = mermaidPlugin({
      renderer: () => {
        throw new Error("unrenderable")
      }
    })
    const code = plugin.code
    if (typeof code !== "function") throw new Error("the plugin declares no `code` visitor")
    expect(() =>
      code({ type: "code", lang: MERMAID_LANG, value: "gantt" } as never, context)
    ).toThrow(/did not render/)
  })

  it("throws when the renderer returns nothing, which no build step would otherwise report", () => {
    const plugin = mermaidPlugin({ renderer: () => "   " })
    const code = plugin.code
    if (typeof code !== "function") throw new Error("the plugin declares no `code` visitor")
    expect(() =>
      code({ type: "code", lang: MERMAID_LANG, value: "graph TD\n A-->B" } as never, context)
    ).toThrow(/empty string/)
  })

  it("returns a synchronous result, because an async visitor breaks the link validator", () => {
    /*
     * Measured on this corpus: an async `code` visitor switches Sätteri's whole walk to the async path,
     * on which `starlight-links-validator` records no per-file entry — so every link INTO each of the six
     * diagram-carrying pages reported `InvalidLink` while those pages built and served correctly. The
     * type forbids a promise; this asserts the implementation does too.
     */
    const plugin = mermaidPlugin({ renderer: () => "<svg></svg>" })
    const code = plugin.code
    if (typeof code !== "function") throw new Error("the plugin declares no `code` visitor")
    const result = code(
      { type: "code", lang: MERMAID_LANG, value: "graph TD\n A-->B" } as never,
      context
    )
    expect(result).not.toBeInstanceOf(Promise)
  })
})

describe("the figure's accessible name", () => {
  /**
   * A tree of one root whose children are given in order; `parent`/`indexOf`/`textContent` are the
   * three visitor-context accessors `figureName` reads, reduced to a flat sibling list.
   */
  const treeOf = (children: ReadonlyArray<{ type: string; text?: string }>, title?: string) => {
    const root = { type: "root", children }
    return {
      data: title === undefined ? {} : { astro: { frontmatter: { title } } },
      parent: (node: unknown) => (node === root ? undefined : root),
      indexOf: (node: unknown) => {
        const at = children.indexOf(node as never)
        return at === -1 ? undefined : at
      },
      textContent: (node: unknown) => (node as { text?: string }).text ?? ""
    } as never
  }

  it("takes the nearest heading before the fence, which is how every fence in this corpus is named", () => {
    const fence = { type: "code", lang: MERMAID_LANG, value: "graph TD" }
    const ctx = treeOf([
      { type: "heading", text: "Earlier section" },
      { type: "heading", text: "Module map" },
      { type: "paragraph", text: "prose between" },
      fence as never
    ])
    expect(figureName(fence as never, ctx, 0)).toBe("Module map")
  })

  it("prefers the fence's own info string, with a `title=` prefix and quotes removed", () => {
    const ctx = treeOf([{ type: "heading", text: "Module map" }])
    expect(figureName({ type: "code", meta: "The run lifecycle" } as never, ctx, 0)).toBe(
      "The run lifecycle"
    )
    expect(figureName({ type: "code", meta: 'title="The run lifecycle"' } as never, ctx, 0)).toBe(
      "The run lifecycle"
    )
  })

  it("names a fence with no heading before it for the page, whose H1 docs:sync moved into frontmatter", () => {
    const fence = { type: "code", value: "graph TD" }
    expect(figureName(fence as never, treeOf([fence as never], "Components"), 0)).toBe("Components")
    // A heading on the page still wins over the page title: it is the nearer name.
    expect(
      figureName(
        fence as never,
        treeOf([{ type: "heading", text: "Module map" }, fence as never], "System overview"),
        0
      )
    ).toBe("Module map")
  })

  it("falls back to a positional name when there is no heading and no page title", () => {
    const fence = { type: "code", value: "graph TD" }
    expect(figureName(fence as never, treeOf([fence as never]), 2)).toBe("Diagram 3")
    expect(figureName(fence as never, treeOf([]), 0)).toBe("Diagram 1")
  })

  it("escapes the name it writes into the attribute", () => {
    const plugin = mermaidPlugin({ renderer: () => "<svg></svg>" })
    const code = plugin.code
    if (typeof code !== "function") throw new Error("the plugin declares no `code` visitor")
    const fence = {
      type: "code",
      lang: MERMAID_LANG,
      value: "graph TD",
      meta: 'A "quoted" <name> & more'
    }
    const out = code(fence as never, treeOf([fence as never]) as never) as { value: string }
    expect(out.value).toContain('aria-label="A &quot;quoted&quot; &lt;name&gt; &amp; more"')
  })
})
