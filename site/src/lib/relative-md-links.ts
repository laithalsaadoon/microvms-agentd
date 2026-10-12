// SPDX-License-Identifier: Apache-2.0
import { existsSync } from "node:fs"
import { posix, relative, sep } from "node:path"
import { fileURLToPath } from "node:url"

import { defineMdastPlugin, type MdastPluginDefinition } from "satteri"

/**
 * Rewrites a relative link to another page's Markdown source into the route that page is served at.
 *
 * The tree cross-links with relative `.md` targets such as `rpc-tools.md` and
 * `../internals/insights/contract-map.md`. That form resolves on GitHub and on the raw `.md` twins,
 * because there each target sits beside its source. The rendered page sits one directory deeper than
 * its source: `reference/public-api.md` is served at `/reference/public-api/`. Sätteri emits a link
 * target as written, so the browser resolved `rpc-tools.md` on that page to
 * `/reference/public-api/rpc-tools.md`, a 404. Every relative `.md` link in the rendered tree broke
 * this way, 148 of them on 29 pages, and the llms bundles inherited each one, because
 * `starlight-llms-txt` flattens the rendered page back to Markdown.
 *
 * This resolves the target against the source document's path under the content root and emits the
 * target's route root-relative, `/reference/rpc-tools/`, with any fragment kept. `starlightBasePath()`
 * then prefixes the base onto it, exactly as it does for an authored `/agents/`; its Sätteri pass is
 * pushed after the plugins configured in `astro.config.ts`, and Sätteri applies each plugin's edits
 * before the next one runs, so it reads the rewritten URL.
 *
 * The raw twins never pass through this processor and keep the relative form, which resolves there
 * for every page except an `index.md`: its twin is served one level up (`reference/index.md` at
 * `reference.md`), so a relative link written in an index page resolves one level too high on the
 * twin. The index pages link root-relative, and `tests/built-links.test.ts` fails on the first
 * relative link that breaks a twin.
 *
 * A relative `.md` target that names no file under the content root fails the build with the document
 * and line. `scripts/sync-docs.mjs` already turns a link that leaves the published corpus into a
 * permalink, so a target missing here is a defect, and emitting it would ship a 404.
 *
 * The route is the content path lowercased, which is the route Astro gives a file whose path
 * segments use only `[a-z0-9_-]`: the glob loader runs each segment through github-slugger, which
 * leaves those characters alone and drops or replaces most others (`v1.2-notes` becomes `v12-notes`).
 * Every page in the tree meets that rule, so a target outside it fails the build here rather than
 * through a guessed route. A frontmatter `slug:` override is not read; the link validator fails the
 * build on the route it would leave unserved.
 *
 * Only `link` nodes are rewritten. A reference definition (`[x]: page.md`) would need the base added
 * here as well, because `starlightBasePath()` rewrites `link` nodes only; the tree uses none, and
 * `tests/built-links.test.ts` fails on the first one that ships unresolved.
 */

type LinkVisitor = NonNullable<MdastPluginDefinition["link"]>
type LinkNode = Parameters<LinkVisitor>[0]

/** A target this plugin owns: no scheme, not root-relative, not an in-page anchor, ending `.md`. */
const RELATIVE_MD = /^(?![a-z][a-z0-9+.-]*:)(?![/#])(?<path>[^?#]*\.md)(?<suffix>[?#].*)?$/i

/** A path segment whose github-slugger slug is itself once lowercased. */
const SLUG_SAFE = /^[a-z0-9][a-z0-9_-]*$/

/** A content path as the route it is served at: `reference/cli.md` is `/reference/cli/`. */
const routeOfContentPath = (contentPath: string): string =>
  `/${contentPath.slice(0, -".md".length).toLowerCase()}/`.replace(/(^|\/)index\/$/, "$1")

/**
 * @param contentRoot the absolute path of Starlight's content directory, `src/content/docs`. A
 *   document outside it is left alone: its route is not derived from its path.
 */
export const relativeMdLinks = (contentRoot: string): MdastPluginDefinition => {
  const root = contentRoot.endsWith(sep) ? contentRoot : `${contentRoot}${sep}`

  return defineMdastPlugin({
    name: "docs-relative-md-links",
    // Positions are read for the error message, which names the line the link sits on.
    options: { position: true },

    link(node: LinkNode, ctx) {
      const groups = RELATIVE_MD.exec(node.url)?.groups
      if (groups?.path === undefined || ctx.fileURL === undefined) return
      const document = fileURLToPath(ctx.fileURL)
      if (!document.startsWith(root)) return

      const line = node.position?.start.line
      const refuse = (why: string): never => {
        throw new Error(
          `docs-relative-md-links: ${line === undefined ? document : `${document}:${line}`}: ` +
            `\`${node.url}\` ${why}`
        )
      }
      let path = groups.path
      try {
        path = decodeURI(groups.path)
      } catch {
        refuse("is not valid percent-encoding.")
      }
      const from = posix.dirname(relative(root, document).split(sep).join("/"))
      const target = posix.normalize(posix.join(from, path))
      if (target.startsWith("../") || !existsSync(`${root}${target}`)) {
        refuse(
          `resolves to \`${target}\`, which is not a page under ${root}. Link to a published ` +
            "page, or let `scripts/sync-docs.mjs` turn the link into a permalink."
        )
      }
      const segments = target.slice(0, -".md".length).toLowerCase().split("/")
      if (!segments.every((segment) => SLUG_SAFE.test(segment))) {
        refuse(
          `resolves to \`${target}\`, whose route this rewrite cannot derive: Astro slugs a path ` +
            "segment with github-slugger, which changes characters outside `[a-z0-9_-]`. Rename " +
            "the page, or link to its route."
        )
      }
      ctx.setProperty(node, "url", `${routeOfContentPath(target)}${groups.suffix ?? ""}`)
    }
  })
}
