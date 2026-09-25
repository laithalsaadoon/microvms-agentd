// SPDX-License-Identifier: Apache-2.0
/**
 * The TypeScript SDK reference: `microvms-js/index.d.ts`, read by TypeDoc, rendered to Markdown by
 * `typedoc-plugin-markdown`, and laid out as pages here.
 *
 * TypeDoc owns the parsing and `typedoc-plugin-markdown` owns the member layout (signatures as
 * code blocks, parameter tables, return types); this file only decides where each file lands,
 * rewrites the plugin's file-relative links into site routes, and writes the overview.
 *
 * `starlight-typedoc` was the first candidate and was declined for where it runs, not for what
 * it renders: it generates inside Astro's `config:setup` hook, which is after `pnpm run braces`
 * has scanned the content directory and outside `.reference-manifest.json`, so its pages would
 * reach the raw-twin MDX parser unscanned and be owned by nothing that refuses a collision.
 * Running TypeDoc here keeps both guarantees for these pages that the CLI pages already have.
 *
 * The pages:
 *
 * - `reference/typescript/`: install and quickstart from `microvms-js/README.md`, then one index
 *   row per class, interface, function, constant, and enum.
 * - `reference/typescript/classes/<name>/` and `reference/typescript/interfaces/<name>/`.
 * - `reference/typescript/module/`: the functions, constants, and enums.
 */

import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, posix, relative } from "node:path"
import { fileURLToPath } from "node:url"

import { Application, Comment, Converter, ReflectionKind } from "typedoc"

import {
  code,
  indexTable,
  LANGUAGES,
  page,
  pageSlug,
  plainDescription,
  provenance,
  readmeSections,
  summaryOf
} from "./common.mjs"

const HERE = dirname(fileURLToPath(import.meta.url))
const LANGUAGE = LANGUAGES.typescript

/**
 * A tsconfig naming exactly the declaration file. TypeDoc refuses an entry point the project's
 * tsconfig does not include, and the site's own `tsconfig.json` covers the site, not the binding.
 * `types: ["node"]` because the declarations name `Buffer`, which resolves from this package's
 * own `@types/node`.
 */
const TSCONFIG = join(HERE, "typedoc.tsconfig.json")

/** The npm package name, which is also how a caller imports the module. */
const readPackageName = (repoRoot) =>
  JSON.parse(readFileSync(join(repoRoot, "microvms-js", "package.json"), "utf8")).name

/**
 * The TypeDoc and `typedoc-plugin-markdown` options, in one place so the reasoning sits beside them.
 *
 * - `membersWithOwnFile`: a page per class and per interface, and everything else (functions, the
 *   loader constant, the one enum) on the module page, which is the shape the Python reference has.
 * - `useCodeBlocks` and `expandParameters`: every signature is a fenced `ts` block carrying its
 *   parameter types, rather than prose with the types in a table below.
 * - `*Format: "list"`: a property gets a heading and a signature block, as a method does, so every
 *   member is addressable by anchor and reads the same way.
 * - `sanitizeComments`: the plugin escapes `<`, `>`, `{` and `}` in doc comments, because every
 *   page reaches an MDX parser on its way to the raw Markdown twin.
 * - `hidePageTitle`, `hidePageHeader`, `hideBreadcrumbs`: the title is Starlight's, from the
 *   frontmatter written by `gen-reference.mjs`, and the sidebar is the breadcrumb.
 * - `disableSources`: a "Defined in index.d.ts:142" line on every member, pointing at a file most
 *   readers never open, is noise on a page generated from that file.
 * - `treatWarningsAsErrors`: a broken `{@link}` or an unresolved type is a build failure, not a
 *   warning scrolled past in CI.
 *
 * @param {string} repoRoot
 * @param {string} out
 */
const options = (repoRoot, out) => ({
  entryPoints: [join(repoRoot, LANGUAGE.source)],
  tsconfig: TSCONFIG,
  plugin: ["typedoc-plugin-markdown"],
  out,
  readme: "none",
  router: "member",
  membersWithOwnFile: ["Class", "Interface"],
  useCodeBlocks: true,
  expandParameters: true,
  parametersFormat: "table",
  classPropertiesFormat: "list",
  interfacePropertiesFormat: "list",
  enumMembersFormat: "list",
  typeDeclarationFormat: "list",
  sanitizeComments: true,
  hidePageTitle: true,
  hidePageHeader: true,
  hideBreadcrumbs: true,
  disableSources: true,
  excludeExternals: true,
  excludePrivate: true,
  treatWarningsAsErrors: true,
  logLevel: "Warn"
})

/**
 * rustdoc headings (`# What the core refuses`) inside a doc comment, as bold lead-ins.
 *
 * The comments are written in Rust and carried into the declarations by napi-rs, so they follow
 * rustdoc's convention of `#` section headings. Rendered as they are, each becomes an `h1` in the
 * middle of a member, below that member's `h3`, which breaks the heading outline the table of
 * contents and every screen reader navigate by.
 *
 * @param {import("typedoc").Comment | undefined} comment
 */
const demoteHeadings = (comment) => {
  if (comment === undefined) return
  const parts = [comment.summary, ...comment.blockTags.map((tag) => tag.content)]
  for (const list of parts) {
    for (const part of list) {
      if (part.kind === "text") {
        part.text = part.text.replace(/^#{1,6}[ \t]+(.+?)[ \t#]*$/gm, "**$1**")
      }
    }
  }
}

/**
 * Convert the declarations and tidy the model before it is rendered.
 *
 * napi-rs emits `export declare class X` with no constructor for a class that has no
 * `#[napi(constructor)]`, and TypeScript reads a class with no constructor declaration as having
 * a public no-argument one, so TypeDoc documents `new X()` for twenty-odd classes whose only real
 * entry points are static factories. Calling it throws at run time. The converter reports the
 * TypeScript declaration behind each constructor signature, and an implied one has none, so those
 * are removed here and the four real constructors stay.
 *
 * @param {string} repoRoot
 * @param {string} out
 */
const convert = async (repoRoot, out) => {
  const app = await Application.bootstrapWithPlugins(options(repoRoot, out))
  const implied = new Set()
  app.converter.on(Converter.EVENT_CREATE_SIGNATURE, (_context, signature, declaration) => {
    if (signature.kindOf(ReflectionKind.ConstructorSignature) && declaration === undefined) {
      implied.add(signature.parent)
    }
  })
  // Removed at the start of resolution rather than after it, because TypeDoc groups members into
  // "Constructors", "Methods" and the rest while resolving, and a reflection removed afterwards is
  // still listed in its group and still rendered.
  app.converter.on(Converter.EVENT_RESOLVE_BEGIN, (context) => {
    for (const reflection of implied) context.project.removeReflection(reflection)
  })
  const project = await app.convert()
  if (project === undefined || app.logger.hasErrors() || app.logger.hasWarnings()) {
    throw new Error(`TypeDoc could not convert ${LANGUAGE.source}; its messages are above.`)
  }
  for (const reflection of Object.values(project.reflections)) demoteHeadings(reflection.comment)
  return { app, project }
}

/** @param {import("typedoc").Reflection} reflection */
const summaryText = (reflection) => {
  const comment =
    reflection.comment ??
    /** @type {any} */ (reflection).signatures?.[0]?.comment ??
    /** @type {any} */ (reflection).getSignature?.comment
  return comment === undefined ? "" : summaryOf(Comment.combineDisplayParts(comment.summary))
}

/**
 * Every file the plugin wrote, as paths relative to the output directory, POSIX-separated.
 *
 * @param {string} root
 */
const writtenFiles = (root) =>
  readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
    .map((entry) => relative(root, join(entry.parentPath, entry.name)).split("\\").join("/"))
    .sort()

/** The plugin's link targets: a file-relative `.md` path, optionally with an anchor. */
const MD_LINK = /\]\(([^()\s]+?\.md)(#[^()\s]*)?\)/g

/**
 * Rewrite the plugin's file-relative links into site routes.
 *
 * The plugin links `classes/Region.md` to `../interfaces/ExecOptions.md`. On this site that
 * shape would resolve to the raw Markdown twin rather than the rendered page, and the file names
 * are mixed-case where Starlight's routes are not, so each target is looked up in the map of what
 * was written and replaced with the route. A target the map lacks fails the generation: it is a
 * link to a page nothing wrote.
 *
 * @param {string} body
 * @param {string} file the plugin's path for the page being rewritten
 * @param {Map<string, string>} routes plugin path to site route
 */
const rewriteLinks = (body, file, routes) =>
  body.replace(MD_LINK, (_, target, anchor = "") => {
    const resolved = posix.normalize(posix.join(posix.dirname(file), target))
    const route = routes.get(resolved)
    if (route === undefined) {
      throw new Error(`typedoc-plugin-markdown linked ${file} to ${target}, which it did not write`)
    }
    return `](${route}${anchor})`
  })

/**
 * The module page keeps what has no page of its own. The plugin also lists every class and
 * interface on it; the overview's index table says the same with summaries, so those two lists
 * are dropped here rather than published twice.
 *
 * @param {string} body
 */
const withoutPageLists = (body) =>
  body
    .split(/^(?=## )/m)
    .filter((chunk) => !/^## (?:Classes|Interfaces)\n/.test(chunk))
    .join("")

/**
 * The heading slug Starlight derives for a heading the plugin writes: github-slugger over the
 * heading text, which for `agentConstants()` is `agentconstants`.
 *
 * @param {string} heading
 */
const anchorOf = (heading) =>
  heading
    .replace(/\\(.)/g, "$1")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-")

/**
 * Every TypeScript reference page.
 *
 * @param {{ repoRoot: string }} options
 * @returns {Promise<import("../pages.mjs").ReferencePage[]>}
 */
export const typescriptPages = async ({ repoRoot }) => {
  const out = mkdtempSync(join(tmpdir(), "microvms-typedoc-"))
  try {
    const { app, project } = await convert(repoRoot, out)
    await app.generateOutputs(project)
    if (app.logger.hasErrors() || app.logger.hasWarnings()) {
      throw new Error(`typedoc-plugin-markdown could not render ${LANGUAGE.source}.`)
    }
    return await layout(repoRoot, project, out)
  } finally {
    rmSync(out, { recursive: true, force: true })
  }
}

const MODULE_ID = `${LANGUAGE.directory}/module`

/** The versions that rendered the pages, read from the packages themselves. */
const rendererName = async () => {
  const { createRequire } = await import("node:module")
  const require = createRequire(import.meta.url)
  const version = (name) => {
    const entry = require.resolve(name)
    let directory = dirname(entry)
    while (!readdirSync(directory).includes("package.json")) directory = dirname(directory)
    return JSON.parse(readFileSync(join(directory, "package.json"), "utf8")).version
  }
  return `TypeDoc ${version("typedoc")} and typedoc-plugin-markdown ${version("typedoc-plugin-markdown")}`
}

/**
 * @param {string} repoRoot
 * @param {import("typedoc").ProjectReflection} project
 * @param {string} out
 */
const layout = async (repoRoot, project, out) => {
  const renderer = await rendererName()
  const packageName = readPackageName(repoRoot)
  const files = writtenFiles(out)

  const byKind = (kind) =>
    project
      .getReflectionsByKind(kind)
      .filter((reflection) => reflection.parent === project)
      .sort((a, b) => a.name.localeCompare(b.name))

  const kinds = [
    { kind: ReflectionKind.Class, directory: "classes", label: "class" },
    { kind: ReflectionKind.Interface, directory: "interfaces", label: "interface" }
  ]

  /** plugin path to site id, for every page that has one */
  const ids = new Map([["README.md", MODULE_ID]])
  const owned = kinds.flatMap(({ kind, directory, label }) =>
    byKind(kind).map((reflection) => {
      const file = `${directory}/${reflection.name}.md`
      if (!files.includes(file)) {
        throw new Error(
          `typedoc-plugin-markdown wrote no ${file} for the ${label} ${reflection.name}`
        )
      }
      const id = `${LANGUAGE.directory}/${directory}/${pageSlug(reflection.name)}`
      ids.set(file, id)
      return { reflection, file, id, label }
    })
  )
  const unclaimed = files.filter((file) => !ids.has(file))
  if (unclaimed.length > 0) {
    throw new Error(
      `typedoc-plugin-markdown wrote pages this layout does not place: ${unclaimed.join(", ")}`
    )
  }
  const routes = new Map([...ids].map(([file, id]) => [file, `/${id}/`]))
  const body = (file) => rewriteLinks(readFileSync(join(out, file), "utf8"), file, routes).trim()

  const memberPages = owned.map(({ reflection, file, id, label }, at) =>
    page({
      id,
      title: reflection.name,
      description: plainDescription(
        summaryText(reflection),
        `The ${reflection.name} ${label} in ${packageName}.`
      ),
      body: [body(file), provenance(LANGUAGE, renderer)].join("\n\n"),
      sidebarLabel: reflection.name,
      sidebarOrder: at,
      source: LANGUAGE.source
    })
  )

  const moduleBody = withoutPageLists(body("README.md")).trim()
  const modulePage = page({
    id: MODULE_ID,
    title: `${packageName}: functions, constants, and enums`,
    description: `The module-level functions, constants, and enums of the ${packageName} npm package.`,
    body: [
      `Everything ${code(packageName)} exports that is not a class or an interface of its own. Import any of them by name from the package.`,
      moduleBody,
      provenance(LANGUAGE, renderer)
    ].join("\n\n"),
    sidebarLabel: "Functions and enums",
    sidebarOrder: 1,
    source: LANGUAGE.source
  })

  const headings = new Set(
    [...moduleBody.matchAll(/^### (.+)$/gm)].map((match) => anchorOf(match[1] ?? ""))
  )
  const moduleRow = (reflection, label, heading) => {
    const anchor = anchorOf(heading)
    if (!headings.has(anchor)) {
      throw new Error(`the module page has no heading for the ${label} ${reflection.name}`)
    }
    return {
      name: reflection.name,
      kind: label,
      href: `/${MODULE_ID}/#${anchor}`,
      summary: summaryText(reflection)
    }
  }
  const rows = [
    ...owned.map(({ reflection, id, label }) => ({
      name: reflection.name,
      kind: label,
      href: `/${id}/`,
      summary: summaryText(reflection)
    })),
    ...byKind(ReflectionKind.Function).map((r) => moduleRow(r, "function", `${r.name}()`)),
    ...byKind(ReflectionKind.Variable).map((r) => moduleRow(r, "constant", r.name)),
    ...byKind(ReflectionKind.Enum).map((r) => moduleRow(r, "enum", r.name))
  ]

  const overview = page({
    id: LANGUAGE.directory,
    title: "TypeScript SDK",
    description: `Install the ${packageName} npm package, run a first command, and find every class, interface, function, and enum it exports.`,
    body: [
      `The ${code(packageName)} package on npm, for Node.js from JavaScript or TypeScript. This reference is generated from ${code(LANGUAGE.source)}, the declaration file the package ships, so a signature here is the one your editor and ${code("tsc")} see.`,
      readmeSections(repoRoot, LANGUAGE.readme, ["Install", "Run your first command"]),
      "## Everything the package exports",
      `Each class and interface has its own page. Functions, constants, and enums share [one page](/${MODULE_ID}/).`,
      indexTable(rows),
      provenance(LANGUAGE, renderer)
    ].join("\n\n"),
    sidebarLabel: "Overview",
    sidebarOrder: 0,
    source: LANGUAGE.source,
    index: true
  })

  return [overview, modulePage, ...memberPages]
}
