// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "vitest/config"

/**
 * The default tier: everything that needs no browser.
 *
 * The suites read `dist/`, so they are worthless without a build and must say so rather than skip.
 * `tests/agent-surface.test.ts` and `tests/built-site.test.ts` both throw with the command to run when
 * a file they need is absent; wiring the build into the task runner is what keeps an ordered run
 * honest. See `mise run docs:check`.
 *
 * The three browser suites are excluded rather than left to be discovered. They drive Chromium, so
 * folding them in here would make `mise run docs:check` require a 150 MB browser download to run a
 * string assertion, and it would run them a second time in a tier with no `fileParallelism: false`,
 * putting two browsers on one runner while one of them measures WHEN the layout settles. They have
 * their own tasks (`docs:a11y`, `docs:budget`) and their own configs (`vitest.a11y.config.ts`,
 * `vitest.lighthouse.config.ts`), and `tests/built-site.test.ts` asserts that every suite importing
 * `playwright` is excluded here and included in exactly one of those.
 */
export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts", "tests/**/*.test.mjs"],
    exclude: [
      "tests/a11y.test.ts",
      "tests/layout-stability.test.ts",
      "tests/lighthouse.test.ts",
      "**/node_modules/**",
      "**/dist/**"
    ],
    environment: "node"
  }
})
