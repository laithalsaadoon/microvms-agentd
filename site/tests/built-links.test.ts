// SPDX-License-Identifier: Apache-2.0
import { readdirSync, readFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, it } from "vitest"

import { BASE, DIST_DIR } from "../src/gates.js"

/**
 * Every link on every built surface resolves to a file the build emitted, and every fragment into a
 * rendered page names an id on that page.
 *
 * Each link is resolved the way a browser resolves it: against the URL its file is served at, so
 * `reference/public-api/index.html` resolves its links against `/microvms-agentd/reference/public-api/`.
 * No other gate does that for the rendered pages or the llms bundles. `starlight-links-validator` reads
 * the links the Markdown pipeline produced and skips `.md` targets, and before
 * `src/lib/relative-md-links.ts` existed a relative `.md` link passed every gate on 29 pages while the
 * browser resolved it one directory too deep: `rpc-tools.md` on `/reference/public-api/` fetched
 * `/reference/public-api/rpc-tools.md`, a 404. The llms bundles carried the same links from the site
 * root, where `../` climbed above the base.
 *
 * `tests/raw-route-links.test.ts` asserts the twins' own link forms (the base exactly once, twins
 * reachable from twins). This file holds every surface to one rule with one resolver, twins included.
 * External links are lychee's: `mise run docs:links` and the weekly `links.yml` check them.
 */

/* =================================================================================================
 * CONFIG: the whole site-specific surface of this file.
 * ================================================================================================= */

const CONFIG = {
  /** The origin the site is published at, read from the same variable `astro.config.ts` reads. */
  origin: new URL(process.env.DOCS_SITE ?? "https://laithalsaadoon.github.io").origin,
  /** Output directories that hold assets, not documents. Their files are link targets, not sources. */
  ignore: ["_astro/", "pagefind/"],
  /**
   * The page GitHub Pages serves, with status 404, for every path that has no file. A relative link on
   * it resolves against whichever missing URL was requested, so it may carry none.
   */
  notFoundPage: "404.html",
  /**
   * Links that name a missing file on purpose, each with the reason. Starlight gives the 404 page a
   * canonical URL and an `og:url` of its own route, and that route has no file because the page is
   * served at every missing path instead.
   */
  allowed: [{ from: "404.html", target: `${BASE}404/` }]
} as const

/* =================================================================================================
 * The built files and the URLs they are served at.
 * ================================================================================================= */

const distDir = join(dirname(dirname(fileURLToPath(import.meta.url))), DIST_DIR)

const files = (directory: string): ReadonlyArray<string> =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? files(path) : [path.slice(distDir.length + 1)]
  })

const built = files(distDir)
const documents = built.filter((path) => !CONFIG.ignore.some((prefix) => path.startsWith(prefix)))
const pages = documents.filter((path) => path.endsWith(".html"))
const twins = documents.filter((path) => path.endsWith(".md"))
const bundles = documents.filter((path) => /^llms[^/]*\.txt$/.test(path))
const sitemaps = documents.filter((path) => /^sitemap[^/]*\.xml$/.test(path))

/** `reference/cli/index.html` is served at `/microvms-agentd/reference/cli/`; anything else as named. */
const servedAt = (path: string): URL =>
  new URL(`${BASE}${path.replace(/(^|\/)index\.html$/, "$1")}`, CONFIG.origin)

const builtFiles: ReadonlySet<string> = new Set(built)
const isFile = (path: string): boolean => builtFiles.has(path)

/**
 * The built file a decoded same-origin path is served from, or `undefined` when GitHub Pages would
 * answer 404. Pages serves `x/index.html` at `x/`, redirects `x` to `x/` when that directory has an
 * index (the bare base included), and serves `x.html` at `x`. A doubled slash inside the path is
 * reported even though Pages collapses it, because it is how a base joined on twice shows up.
 */
const fileAt = (pathname: string): string | undefined => {
  if (`${pathname}/` === BASE) return "index.html"
  if (!pathname.startsWith(BASE)) return undefined
  const path = pathname.slice(BASE.length)
  const candidates =
    path === "" || path.endsWith("/")
      ? [`${path}index.html`]
      : [path, `${path}/index.html`, `${path}.html`]
  return candidates.find(isFile)
}

/* =================================================================================================
 * Reading links out of each kind of file.
 * ================================================================================================= */

const decodeEntities = (value: string): string =>
  value.replace(/&(#x[0-9a-f]+|#\d+|amp|quot|apos|lt|gt);/gi, (_, entity: string) => {
    const named: Record<string, string> = { amp: "&", quot: '"', apos: "'", lt: "<", gt: ">" }
    if (entity[0] !== "#") return named[entity.toLowerCase()] ?? ""
    return String.fromCodePoint(
      entity[1] === "x" || entity[1] === "X"
        ? Number.parseInt(entity.slice(2), 16)
        : Number.parseInt(entity.slice(1), 10)
    )
  })

/**
 * Every start tag in a document, with its attributes. Script and style bodies and comments are removed
 * first, and the opening tags of scripts and styles are kept, so a `<script src>` is read.
 *
 * Only real tags are read. Markup quoted in a code block is escaped (`&lt;a href=…`), so it never opens
 * with `<` and never reads as a link.
 */
const tags = (html: string): ReadonlyArray<{ name: string; attributes: Map<string, string> }> => {
  const markup = html
    .replace(/(<(script|style)\b[^>]*>)[\s\S]*?<\/\2>/gi, "$1")
    .replace(/<!--[\s\S]*?-->/g, "")
  const tag =
    /<([a-z][^\s/>]*)((?:\s+[^\s=/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*\/?>/gi
  const attribute = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
  return [...markup.matchAll(tag)].map(([, name, rest]) => ({
    name: (name ?? "").toLowerCase(),
    attributes: new Map(
      [...(rest ?? "").matchAll(attribute)].map(([, key, double, single, bare]) => [
        (key ?? "").toLowerCase(),
        decodeEntities(double ?? single ?? bare ?? "")
      ])
    )
  }))
}

type ParsedPage = {
  readonly links: ReadonlyArray<string>
  readonly ids: ReadonlySet<string>
  readonly graph: ReadonlyArray<string>
}

const parsed = new Map<string, ParsedPage>()

/**
 * Every `url` and `@id` on this site's origin in a page's JSON-LD graph. An `@id` names a node, so its
 * fragment (`#article`, `#website`) is an identifier rather than an anchor, and only the page it sits
 * on is checked.
 */
const graphUrls = (path: string, html: string): ReadonlyArray<string> =>
  [
    ...html.matchAll(/<script\b[^>]*type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/gi)
  ].flatMap(([, body]) => {
    const found: Array<string> = []
    const walk = (value: unknown, key?: string): void => {
      if (typeof value === "string") {
        if ((key === "url" || key === "@id") && value.startsWith(`${CONFIG.origin}/`)) {
          found.push(value)
        }
      } else if (Array.isArray(value)) {
        for (const item of value) walk(item, key)
      } else if (value !== null && typeof value === "object") {
        for (const [name, item] of Object.entries(value)) walk(item, name)
      }
    }
    try {
      walk(JSON.parse(body ?? ""))
    } catch (error) {
      throw new Error(`${path}: its JSON-LD does not parse: ${String(error)}`)
    }
    return found
  })

/**
 * A rendered page, read once for all of its roles: the links it carries (`href` and `src` on any
 * element, and a `<meta content>` that is a URL on this site's origin), the fragments it can be
 * scrolled to (element ids, and `name` on anchors), and the URLs its JSON-LD graph names.
 */
const page = (path: string): ParsedPage => {
  let found = parsed.get(path)
  if (found === undefined) {
    const links: Array<string> = []
    const ids = new Set<string>()
    const html = readFileSync(join(distDir, path), "utf8")
    for (const { name, attributes } of tags(html)) {
      for (const key of ["href", "src"]) {
        const value = attributes.get(key)
        if (value !== undefined) links.push(value)
      }
      const content = attributes.get("content")
      if (name === "meta" && content?.startsWith(`${CONFIG.origin}/`)) links.push(content)
      const id = attributes.get("id")
      if (id !== undefined) ids.add(id)
      const anchor = name === "a" ? attributes.get("name") : undefined
      if (anchor !== undefined) ids.add(anchor)
    }
    found = { links, ids, graph: graphUrls(path, html) }
    parsed.set(path, found)
  }
  return found
}

/**
 * Every link target in a Markdown body: inline links and images, with or without a title or angle
 * brackets, reference definitions (a footnote, `[^1]:`, is not one), autolinks, and `href` or `src`
 * in raw HTML. Fenced code and code spans are removed first, because a Markdown example quoted in a
 * fence is text, not a link.
 */
const markdownLinks = (body: string): ReadonlyArray<string> => {
  const prose = body
    .replace(/^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\1[^\n]*$/gm, "")
    .replace(/(`+)[\s\S]*?\1/g, "")
  return [
    ...[...prose.matchAll(/\]\(\s*(?:<([^>\n]*)>|([^)\s]+))(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g)].map(
      ([, angled, bare]) => angled ?? bare ?? ""
    ),
    ...[...prose.matchAll(/<(https?:\/\/[^>\s]+)>/g)].map(([, target]) => target ?? ""),
    ...[...prose.matchAll(/\s(?:href|src)="([^"]*)"/g)].map(([, target]) => target ?? ""),
    ...[...prose.matchAll(/^ {0,3}\[(?!\^)[^\]\n]+\]:\s*<?([^\s>]+)>?/gm)].map(
      ([, target]) => target ?? ""
    )
  ]
}

/* =================================================================================================
 * Resolution.
 * ================================================================================================= */

type Offender = string

/**
 * The defects in one link from one file: a protocol-relative link, a target with no file, or a
 * fragment with no id. External links and non-HTTP schemes (`mailto:`, `data:`) are out of scope.
 */
const check = (
  from: string,
  link: string,
  { fragments }: { fragments: boolean }
): ReadonlyArray<Offender> => {
  if (link === "") return []
  // A browser reads `//x/` (or `/\\x/`) as the host `x`, which is what joining an empty base onto a
  // leading slash produces; resolved, it would look external and be skipped.
  if (/^[/\\]{2}/.test(link.trim())) {
    return [
      `${from} -> ${link} is protocol-relative, so a browser reads its first segment as a host`
    ]
  }
  let url: URL
  try {
    url = new URL(link, servedAt(from))
  } catch {
    return [`${from} -> ${link} does not parse as a URL`]
  }
  if (!/^https?:$/.test(url.protocol) || url.origin !== CONFIG.origin) return []
  if (CONFIG.allowed.some((entry) => entry.from === from && entry.target === url.pathname))
    return []
  let pathname: string
  let fragment: string
  try {
    pathname = decodeURIComponent(url.pathname)
    fragment = decodeURIComponent(url.hash.slice(1))
  } catch {
    return [`${from} -> ${link} is not valid percent-encoding`]
  }
  const target = fileAt(pathname)
  if (target === undefined)
    return [`${from} -> ${link} resolves to ${pathname}, which was not built`]
  // HTML scrolls an empty fragment, and `top` in any case, to the top of the page without an element.
  if (
    !fragments ||
    fragment === "" ||
    fragment.toLowerCase() === "top" ||
    !target.endsWith(".html")
  ) {
    return []
  }
  return page(target).ids.has(fragment)
    ? []
    : [`${from} -> ${link} names #${fragment}, which ${target} has no id for`]
}

const offenders = (
  sources: ReadonlyArray<string>,
  linksOf: (path: string) => ReadonlyArray<string>,
  fragments: boolean
) => sources.flatMap((from) => linksOf(from).flatMap((link) => check(from, link, { fragments })))

const bodyLinks =
  (read: (body: string) => ReadonlyArray<string>) =>
  (path: string): ReadonlyArray<string> =>
    read(readFileSync(join(distDir, path), "utf8"))

/* =================================================================================================
 * ================================================================================================= */

describe("every link in the built site", () => {
  it("finds every surface, so the rest of this file is not vacuously true", () => {
    // Floors rather than counts: the counts belong to the build, and adding a page is not a defect.
    expect(pages.length).toBeGreaterThan(0)
    expect(twins.length).toBeGreaterThan(0)
    expect(bundles.length).toBeGreaterThan(0)
    expect(sitemaps.length).toBeGreaterThan(0)
    expect(pages.includes(CONFIG.notFoundPage)).toBe(true)
  })

  /*
   * This case reads every rendered page, about 30 MB, so it states its own timeout rather than
   * inherit vitest's 5 seconds, which a loaded machine can exceed.
   */
  it("resolves every link on every rendered page to a built file, and every fragment to an id", {
    timeout: 60_000
  }, () => {
    expect(offenders(pages, (path) => page(path).links, true)).toEqual([])
  })

  it("names only built pages in every page's JSON-LD graph", () => {
    expect(offenders(pages, (path) => page(path).graph, false)).toEqual([])
  })

  it("resolves every link in the llms bundles from the URL each bundle is served at", () => {
    // A bundle's own heading links (`[Section titled …](#…)`) name ids a plain-text file cannot carry,
    // so fragments into a non-HTML target are not checked; fragments into rendered pages are.
    expect(offenders(bundles, bodyLinks(markdownLinks), true)).toEqual([])
  })

  it("resolves every link in the raw Markdown twins", () => {
    expect(offenders(twins, bodyLinks(markdownLinks), true)).toEqual([])
  })

  it("lists only built pages in the sitemap", () => {
    const entries = (body: string) =>
      [...body.matchAll(/<loc>([^<]+)<\/loc>/g)].map(([, url]) => decodeEntities(url ?? ""))
    expect(offenders(sitemaps, bodyLinks(entries), false)).toEqual([])
  })

  it("carries no relative link on the 404 page, which is served at every missing path", () => {
    const relative = page(CONFIG.notFoundPage).links.filter(
      (link) => link !== "" && !link.startsWith("#") && !/^([a-z][a-z0-9+.-]*:|\/)/i.test(link)
    )
    expect(relative).toEqual([])
  })
})
