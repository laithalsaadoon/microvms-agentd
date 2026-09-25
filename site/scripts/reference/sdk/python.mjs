// SPDX-License-Identifier: Apache-2.0
/**
 * The Python SDK reference: `microvms-py/microvms.pyi`, parsed by Griffe, rendered to pages.
 *
 * `griffe_dump.py` does the parsing and prints the surface as JSON; this file only lays it out.
 * The split is deliberate. Griffe owns the hard part, reading a stub with `ast` and rendering
 * every annotation and default back to source text, and the layout stays in the same language
 * and the same Markdown helpers as the rest of the Reference tier.
 *
 * The pages:
 *
 * - `reference/python/`: install and quickstart from `microvms-py/README.md`, then one index row
 *   per class, function, constant, and exception.
 * - `reference/python/classes/<name>/`: one per class, every member with its full signature.
 * - `reference/python/module/`: the module-level functions, constants, and exceptions.
 */

import { execFileSync } from "node:child_process"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

import {
  code,
  indexTable,
  LANGUAGES,
  memberAnchor,
  memberHeading,
  page,
  pageSlug,
  plainDescription,
  prose,
  provenance,
  readmeSections,
  signatureBlock,
  summaryOf
} from "./common.mjs"

const HERE = dirname(fileURLToPath(import.meta.url))

/** The Griffe entry point, a PEP 723 script whose inline block pins Griffe exactly. */
export const GRIFFE_DUMP = join(HERE, "..", "griffe_dump.py")

const LANGUAGE = LANGUAGES.python

/** Past this many characters a signature is written one parameter per line, as ruff formats. */
const LINE_LIMIT = 88

/** The two special methods that are the constructor rather than a protocol hook. */
const CONSTRUCTORS = new Set(["__new__", "__init__"])

/**
 * @typedef {object} PyParameter
 * @property {string} name
 * @property {string | null} kind Griffe's `ParameterKind` value
 * @property {string | null} annotation
 * @property {string | null} default
 */

/**
 * @typedef {object} PyFunction
 * @property {"function"} kind
 * @property {string} name
 * @property {string} docstring
 * @property {string[]} decorators
 * @property {string[]} labels
 * @property {PyParameter[]} parameters
 * @property {string | null} returns
 * @property {boolean} special
 */

/**
 * @typedef {object} PyAttribute
 * @property {"attribute"} kind
 * @property {string} name
 * @property {string} docstring
 * @property {string[]} labels
 * @property {string | null} annotation
 * @property {string | null} value
 */

/**
 * @typedef {object} PyClass
 * @property {"class"} kind
 * @property {string} name
 * @property {string} docstring
 * @property {string[]} bases
 * @property {string[]} decorators
 * @property {Array<PyFunction | PyAttribute | PyClass>} members
 */

/**
 * @typedef {object} PyModule
 * @property {string} griffe the Griffe version that parsed the stub
 * @property {string} module
 * @property {string} docstring
 * @property {Array<PyFunction | PyAttribute | PyClass>} members
 */

/**
 * Run Griffe over the stub and return what it read.
 *
 * `uv run --script` resolves the script's pinned dependency against its committed lockfile, so
 * the parser is the same one on every machine. `VIRTUAL_ENV` is dropped from the environment
 * because uv otherwise reports, and in some subcommands uses, whatever venv the caller's shell
 * has active.
 *
 * @param {string} repoRoot
 * @returns {PyModule}
 */
export const loadPythonSurface = (repoRoot) => {
  const env = { ...process.env }
  delete env.VIRTUAL_ENV
  let out
  try {
    out = execFileSync(
      "uv",
      ["run", "--quiet", "--locked", "--script", GRIFFE_DUMP, join(repoRoot, LANGUAGE.source)],
      { cwd: repoRoot, encoding: "utf8", env, maxBuffer: 64 * 1024 * 1024 }
    )
  } catch (cause) {
    throw new Error(
      `Griffe could not read ${LANGUAGE.source}. The Python reference needs uv on PATH ` +
        "(`mise install` provides it); the error above is uv's own.",
      { cause }
    )
  }
  const parsed = JSON.parse(out)
  if (!Array.isArray(parsed?.members) || typeof parsed?.griffe !== "string") {
    throw new Error(`griffe_dump.py printed no members for ${LANGUAGE.source}`)
  }
  return parsed
}

/* =================================================================================================
 * Signatures.
 * ================================================================================================= */

/** @param {PyParameter} parameter */
const parameterText = (parameter) => {
  const prefix =
    parameter.kind === "variadic positional"
      ? "*"
      : parameter.kind === "variadic keyword"
        ? "**"
        : ""
  const annotated =
    parameter.annotation === null
      ? `${prefix}${parameter.name}`
      : `${prefix}${parameter.name}: ${parameter.annotation}`
  if (parameter.default === null) return annotated
  return parameter.annotation === null
    ? `${annotated}=${parameter.default}`
    : `${annotated} = ${parameter.default}`
}

/**
 * The parameter list as a caller writes it: the bound `self` or `cls` dropped, `/` after the last
 * positional-only parameter, and a bare `*` before the first keyword-only one when no `*args`
 * already separates them.
 *
 * @param {PyFunction} fn
 * @param {boolean} bound whether the first parameter is the receiver
 */
const parameterTokens = (fn, bound) => {
  const parameters = bound ? fn.parameters.slice(1) : fn.parameters
  const tokens = []
  const lastPositionalOnly = parameters.findLastIndex((p) => p.kind === "positional-only")
  const hasStar = parameters.some((p) => p.kind === "variadic positional")
  let starred = false
  parameters.forEach((parameter, at) => {
    if (parameter.kind === "keyword-only" && !hasStar && !starred) {
      tokens.push("*")
      starred = true
    }
    tokens.push(parameterText(parameter))
    if (at === lastPositionalOnly) tokens.push("/")
  })
  return tokens
}

/**
 * `head(tokens) -> returns`, on one line when it fits and one parameter per line when not.
 *
 * @param {string} head
 * @param {string[]} tokens
 * @param {string | null} returns
 */
const callSignature = (head, tokens, returns) => {
  const tail = returns === null ? "" : ` -> ${returns}`
  const oneLine = `${head}(${tokens.join(", ")})${tail}`
  if (oneLine.length <= LINE_LIMIT || tokens.length === 0) return oneLine
  return [`${head}(`, ...tokens.map((token) => `    ${token},`), `)${tail}`].join("\n")
}

/** @param {PyFunction} fn */
const isStatic = (fn) =>
  fn.labels.includes("staticmethod") || fn.decorators.includes("staticmethod")

/** @param {PyFunction} fn */
const isClassMethod = (fn) =>
  fn.labels.includes("classmethod") || fn.decorators.includes("classmethod")

/**
 * A method's signature, decorated the way the stub declares it.
 *
 * @param {PyFunction} fn
 */
const methodSignature = (fn) => {
  const decorator = isStatic(fn) ? "@staticmethod\n" : isClassMethod(fn) ? "@classmethod\n" : ""
  return `${decorator}${callSignature(`def ${fn.name}`, parameterTokens(fn, !isStatic(fn)), fn.returns)}`
}

/**
 * The constructor as a caller invokes it: `Sandbox(region: Region)`, not `__new__(cls, ...)`.
 *
 * @param {PyClass} cls
 * @param {PyFunction} fn
 */
const constructorSignature = (cls, fn) => callSignature(cls.name, parameterTokens(fn, true), null)

/** @param {PyClass} cls */
const classSignature = (cls) => {
  const decorators = cls.decorators.map((decorator) => `@${decorator}\n`).join("")
  const bases = cls.bases.length === 0 ? "" : `(${cls.bases.join(", ")})`
  return `${decorators}class ${cls.name}${bases}`
}

/** @param {PyAttribute} attribute */
const attributeSignature = (attribute) => {
  const annotated =
    attribute.annotation === null ? attribute.name : `${attribute.name}: ${attribute.annotation}`
  return attribute.value === null ? annotated : `${annotated} = ${attribute.value}`
}

/* =================================================================================================
 * Pages.
 * ================================================================================================= */

/**
 * One member: its heading, its signature as a code block, then its docstring.
 *
 * @param {string} name
 * @param {string} signature
 * @param {string} docstring
 * @param {number} depth
 */
const memberSection = (name, signature, docstring, depth = 3) =>
  [memberHeading(depth, name), signatureBlock("python", signature), prose(docstring)]
    .filter((part) => part !== "")
    .join("\n\n")

/**
 * @param {string} title
 * @param {string[]} entries
 */
const group = (title, entries) => (entries.length === 0 ? [] : [`## ${title}`, ...entries])

/** @param {PyClass} cls */
const classBody = (cls, griffe) => {
  const functions = /** @type {PyFunction[]} */ (cls.members.filter((m) => m.kind === "function"))
  const allAttributes = /** @type {PyAttribute[]} */ (
    cls.members.filter((m) => m.kind === "attribute")
  )
  // Griffe models a `@property` as an attribute labelled `property`, not as a function.
  const properties = allAttributes.filter((attribute) => attribute.labels.includes("property"))
  const attributes = allAttributes.filter((attribute) => !attribute.labels.includes("property"))
  const init = functions.find((fn) => CONSTRUCTORS.has(fn.name))
  const methods = functions.filter((fn) => !fn.special)
  const special = functions.filter((fn) => fn.special && !CONSTRUCTORS.has(fn.name))

  const lead = [signatureBlock("python", classSignature(cls)), prose(cls.docstring)]
  const construction =
    init === undefined
      ? [
          "## Constructor",
          `${code(cls.name)} declares no ${code("__new__")} or ${code("__init__")}, so it is not constructed directly: an instance comes back from another call in this package.`
        ]
      : [
          "## Constructor",
          [signatureBlock("python", constructorSignature(cls, init)), prose(init.docstring)]
            .filter((part) => part !== "")
            .join("\n\n")
        ]
  return [
    ...lead,
    ...construction,
    ...group(
      "Properties",
      properties.map((property) =>
        memberSection(property.name, attributeSignature(property), property.docstring)
      )
    ),
    ...group(
      "Attributes",
      attributes.map((attribute) =>
        memberSection(attribute.name, attributeSignature(attribute), attribute.docstring)
      )
    ),
    ...group(
      "Methods",
      methods.map((fn) => memberSection(fn.name, methodSignature(fn), fn.docstring))
    ),
    ...group(
      "Special methods",
      special.map((fn) => memberSection(fn.name, methodSignature(fn), fn.docstring))
    ),
    provenance(LANGUAGE, `Griffe ${griffe}`)
  ]
    .filter((part) => part !== "")
    .join("\n\n")
}

/**
 * Whether a class is an exception: it derives, directly or through another class in this
 * module, from a builtin exception.
 *
 * @param {PyClass} cls
 * @param {Map<string, PyClass>} byName
 */
const isException = (cls, byName) => {
  const seen = new Set()
  const walk = (current) =>
    current.bases.some((base) => {
      if (/^(?:Base)?Exception$|Error$/.test(base) && !byName.has(base)) return true
      const parent = byName.get(base)
      if (parent === undefined || seen.has(base)) return false
      seen.add(base)
      return walk(parent)
    })
  return walk(cls)
}

/**
 * @typedef {object} PythonSurface
 * @property {PyClass[]} classes
 * @property {PyClass[]} exceptions
 * @property {PyFunction[]} functions
 * @property {PyAttribute[]} constants
 */

/**
 * The module's public members, split the way the pages are.
 *
 * @param {PyModule} module
 * @returns {PythonSurface}
 */
export const splitSurface = (module) => {
  const all = module.members.filter(
    (member) => !member.name.startsWith("_") || member.name.startsWith("__")
  )
  const classList = /** @type {PyClass[]} */ (all.filter((m) => m.kind === "class"))
  const byName = new Map(classList.map((cls) => [cls.name, cls]))
  return {
    classes: classList.filter((cls) => !isException(cls, byName)),
    exceptions: classList.filter((cls) => isException(cls, byName)),
    functions: /** @type {PyFunction[]} */ (all.filter((m) => m.kind === "function")),
    constants: /** @type {PyAttribute[]} */ (all.filter((m) => m.kind === "attribute"))
  }
}

const MODULE_ID = `${LANGUAGE.directory}/module`
const classId = (name) => `${LANGUAGE.directory}/classes/${pageSlug(name)}`

/**
 * Every Python reference page.
 *
 * @param {PyModule} module
 * @param {{ repoRoot: string }} options
 * @returns {import("../pages.mjs").ReferencePage[]}
 */
export const pythonPages = (module, { repoRoot }) => {
  const surface = splitSurface(module)
  const classes = surface.classes
  const name = module.module

  const classPages = classes.map((cls, at) =>
    page({
      id: classId(cls.name),
      title: cls.name,
      description: plainDescription(summaryOf(cls.docstring), `The ${cls.name} class in ${name}.`),
      body: classBody(cls, module.griffe),
      sidebarLabel: cls.name,
      sidebarOrder: at,
      source: LANGUAGE.source
    })
  )

  const moduleBody = [
    `Everything ${code(name)} exports at module level that is not a class of its own: the functions, the constants, and the exception hierarchy. Import any of them with ${code(`from ${name} import ...`)}.`,
    ...group(
      "Functions",
      surface.functions.map((fn) =>
        memberSection(
          fn.name,
          callSignature(`def ${fn.name}`, parameterTokens(fn, false), fn.returns),
          fn.docstring
        )
      )
    ),
    ...group(
      "Constants",
      surface.constants.map((attribute) =>
        memberSection(attribute.name, attributeSignature(attribute), attribute.docstring)
      )
    ),
    ...group("Exceptions", [
      `Every exception derives from ${code(surface.exceptions[0]?.name ?? "Exception")}, so one ${code("except")} clause catches anything this package raises.`,
      ...surface.exceptions.map((cls) =>
        memberSection(cls.name, classSignature(cls), cls.docstring)
      )
    ]),
    provenance(LANGUAGE, `Griffe ${module.griffe}`)
  ].join("\n\n")

  const modulePage = page({
    id: MODULE_ID,
    title: `${name}: functions, constants, and exceptions`,
    description: `The module-level functions, constants, and exceptions of the ${name} Python package.`,
    body: moduleBody,
    sidebarLabel: "Functions and exceptions",
    sidebarOrder: 1,
    source: LANGUAGE.source
  })

  const rows = [
    ...classes.map((cls) => ({
      name: cls.name,
      kind: "class",
      href: `/${classId(cls.name)}/`,
      summary: summaryOf(cls.docstring)
    })),
    ...surface.functions.map((fn) => ({
      name: fn.name,
      kind: "function",
      href: `/${MODULE_ID}/#${memberAnchor(fn.name)}`,
      summary: summaryOf(fn.docstring)
    })),
    ...surface.constants.map((attribute) => ({
      name: attribute.name,
      kind: "constant",
      href: `/${MODULE_ID}/#${memberAnchor(attribute.name)}`,
      summary: summaryOf(attribute.docstring)
    })),
    ...surface.exceptions.map((cls) => ({
      name: cls.name,
      kind: "exception",
      href: `/${MODULE_ID}/#${memberAnchor(cls.name)}`,
      summary: summaryOf(cls.docstring)
    }))
  ]

  const overview = page({
    id: LANGUAGE.directory,
    title: "Python SDK",
    description: `Install the ${name} Python package, run a first command, and find every class, function, and exception it exports.`,
    body: [
      `The ${code(name)} package on PyPI. This reference is generated from ${code(LANGUAGE.source)}, the type stub the wheel ships, so a signature here is the one a type checker sees.`,
      readmeSections(repoRoot, LANGUAGE.readme, ["Install", "Run your first command"]),
      "## Everything the package exports",
      `Each class has its own page. Functions, constants, and exceptions share [one page](/${MODULE_ID}/).`,
      indexTable(rows),
      provenance(LANGUAGE, `Griffe ${module.griffe}`)
    ]
      .filter((part) => part !== "")
      .join("\n\n"),
    sidebarLabel: "Overview",
    sidebarOrder: 0,
    source: LANGUAGE.source,
    index: true
  })

  return [overview, modulePage, ...classPages]
}
