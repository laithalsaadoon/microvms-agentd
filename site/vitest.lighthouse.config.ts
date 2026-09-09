// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "vitest/config"

/**
 * The budget tier: Lighthouse over the audited pages. Its own config rather than a third entry in
 * `vitest.a11y.config.ts`, because `mise run docs:gate` and `docs.yml` run the two browser tiers as
 * separate, sequential steps: each drives a Chromium, and the layout probe in the other tier measures
 * WHEN things move, so a Lighthouse run sharing its cores would be a different answer, not noise.
 *
 * The timeouts cover fifteen Lighthouse runs in one `beforeAll` on a cold CI runner. Reached through
 * `mise run docs:budget`, and through `mise run docs:gate` after the a11y tier.
 */
export default defineConfig({
  test: {
    include: ["tests/lighthouse.test.ts"],
    fileParallelism: false,
    testTimeout: 900_000,
    hookTimeout: 900_000,
    environment: "node"
  }
})
