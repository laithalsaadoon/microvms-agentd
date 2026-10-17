// SPDX-License-Identifier: Apache-2.0
// A state-only postcondition cannot distinguish a legal suspend from a repeated one.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const root = fileURLToPath(new URL("../../", import.meta.url));
const source = join(root, "verify/spec/core.symspec.json");

function check(path) {
  // Skipping semantic suggestions is explicit: this test asserts only the actual
  // reachability verdict, and makes no claim about verified or strict coverage.
  const directory = mkdtempSync(join(tmpdir(), "state5-report-"));
  const output = join(directory, "report.json");
  const descriptor = openSync(output, "w");
  try {
    // A file descriptor avoids losing the CLI's buffered JSON when it exits with
    // a nonzero verdict; the violating report can exceed a pipe's capacity.
    const run = spawnSync("bash", [
      join(root, "tools/check-spec.sh"), path,
      "--semantic=false", "--reachability-timeout-ms", "5000",
    ], { encoding: "utf8", timeout: 30_000, stdio: ["ignore", descriptor, "pipe"] });
    assert.ifError(run.error);
    assert.equal(run.signal, null, "the verifier must finish within the test budget");
    const report = JSON.parse(readFileSync(output, "utf8"));
    assert.equal(report.type, "check", "the verifier must return a check report");
    assert.ok(report.data, "the check report must carry its verdict");
    return { status: run.status, data: report.data };
  } finally {
    closeSync(descriptor);
    rmSync(directory, { recursive: true, force: true });
  }
}

test("STATE-5 proves legal calls and rejects repeated suspend calls", () => {
  const document = JSON.parse(readFileSync(source, "utf8"));
  const requirements = Object.values(document.requirements);
  const suspend = requirements.find((row) => row.key === "STATE-4");
  const invariant = requirements.find((row) => row.key === "STATE-5");
  assert.ok(suspend && invariant, "the original STATE-4 and STATE-5 must exist");
  const finding = (data) => data.findings.find((row) =>
    row.code.startsWith("FND_REACHABILITY_") && row.requirementIds.includes(invariant.id));

  const legal = check(source);
  assert.equal(legal.status, 0, "the original model must have no error findings");
  assert.equal(finding(legal.data)?.code, "FND_REACHABILITY_PROVED");
  assert.equal(finding(legal.data)?.evidence?.certificateVerified, true);
  assert.equal(finding(legal.data)?.evidence?.frameClosed, true);

  // Admit the same call from non-RUNNING states without changing its effects.
  // Both legal and illegal calls land in SUSPENDING with the same token; only
  // an audit of the pre-state can expose the violation.
  const originalGuard = "when vm_state = RUNNING:";
  assert.ok(suspend.stateEffect.startsWith(originalGuard));
  suspend.stateEffect = suspend.stateEffect.replace(originalGuard,
    "when vm_state = RUNNING or vm_state = SUSPENDING or vm_state = SUSPENDED:");
  const directory = mkdtempSync(join(tmpdir(), "state5-regression-"));
  try {
    const path = join(directory, "illegal-suspend.json");
    writeFileSync(path, JSON.stringify(document));
    const illegal = check(path);
    const violation = finding(illegal.data);
    assert.equal(violation?.code, "FND_REACHABILITY_VIOLATED",
      "STATE-5 must reject the illegal suspend guard");
    assert.equal(illegal.status, 1, "the illegal-call model must fail verification");
    assert.ok(violation.evidence.states.some((state) =>
      state.suspend_outside_running === "true"), "the trace must record the bad pre-state");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
