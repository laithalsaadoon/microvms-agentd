// SPDX-License-Identifier: Apache-2.0
//
// TypeScript's answers to the shared case corpus (`verify/parity/cases/`, #272).
//
// Each case is one test, titled by its file. A case the capability table exempts TypeScript
// from, or one whose `skip` names it, is skipped with its reason. The rules for reading and
// judging a case are `verify/parity/cases/README.md`'s, restated here the way the Rust runners' shared
// `crates/microvms-core/tests/parity_corpus/mod.rs` states them, so no runner relies on another.
//
// Everything is offline. The image, launch and posture cases are refused or answered by core
// before any AWS call (credentials come from the environment, which the default chain reads
// without a network call), and the error cases talk to `startSseServer` on loopback. HTTPS goes
// to a proxy on a loopback port nothing listens on, so a refusal that regresses fails on a
// connection error instead of sending a signed request to AWS.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

import {
  AgentVm,
  NameRegistry,
  Region,
  Sandbox,
  Session,
  SizeClass,
  defaultBaseImage,
  egressPostureFor,
  estimateRun,
  isRetryable,
  wrapDockerfile,
} from '../index.js';
import { codeOf, startSseServer, wireKindOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;
for (const name of ['https_proxy', 'no_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy']) {
  delete process.env[name];
}
process.env.HTTPS_PROXY = 'http://127.0.0.1:9';
process.env.NO_PROXY = '127.0.0.1,localhost';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const CASES = join(ROOT, 'verify', 'parity', 'cases');
const TABLE = join(ROOT, 'verify', 'parity', 'capabilities.toml');
const SURFACE = 'ts';
const SURFACES = ['core', 'cli', 'py', 'ts'];
const SENTINEL = 'wrap-dockerfile/sentinel';
const CASE_KEYS = new Set(['capability', 'input', 'expect', 'ignore', 'skip']);
// A skip ends by naming the issue or trace id that holds the gap, such as `(IMAGE-12)`.
const SKIP_REFERENCE = /\((#[1-9][0-9]*|[A-Z]+-[1-9][0-9]*)\)$/;
const BUILD_ROLE = 'arn:aws:iam::123456789012:role/build';
const IMAGE_ARN = 'arn:aws:lambda:us-east-1:123456789012:microvm-image:img';

/** Each row's cells by surface: a name (string or array) or `{exempt, issue}`.
 *
 * Node has no TOML reader, so Python's standard-library one parses the table, run through uv
 * the way `tools/check-parity.py` runs: `tomllib` needs Python 3.11, which a bare `python3`
 * isn't on every machine, and uv finds one (`--offline`, so a missing interpreter fails here
 * rather than downloading). A hand-written TOML reader here would be a second parser to keep
 * right.
 */
function readTable(path) {
  const script = 'import json, sys, tomllib; json.dump(tomllib.load(open(sys.argv[1], "rb")), sys.stdout)';
  const table = JSON.parse(
    execFileSync(
      'uv',
      ['run', '--offline', '--no-project', '--python', '>=3.11', 'python', '-c', script, path],
      { encoding: 'utf8' },
    ),
  );
  return new Map(
    table.capability.map((row) => [
      row.id,
      Object.fromEntries(SURFACES.map((surface) => [surface, row[surface]])),
    ]),
  );
}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const isPath = (value) => typeof value === 'string' && value.length > 0;

function readCase(path, area, name) {
  const id = `${area}/${name}`;
  const raw = JSON.parse(readFileSync(path, 'utf8'));
  if (!isObject(raw)) throw new Error(`${id}: a case is a JSON object`);
  if ('known_drift' in raw) {
    // The ratchet enforces parity-drift, so no case marks a surface as disagreeing (#258).
    throw new Error(
      `${id}: a known_drift marker is refused, because parity-drift is enforced ` +
        `(verify/ratchet/decisions.toml): make the surface give the case's answer, or skip it ` +
        'with the issue or trace id that holds the gap',
    );
  }
  const unknown = Object.keys(raw).filter((key) => !CASE_KEYS.has(key));
  if (unknown.length) throw new Error(`${id}: unknown keys ${JSON.stringify(unknown)}`);
  if (typeof raw.capability !== 'string') throw new Error(`${id}: capability must be a string`);
  for (const key of ['input', 'expect']) {
    if (!isObject(raw[key])) throw new Error(`${id}: ${key} must be an object`);
  }
  const ignore = raw.ignore ?? [];
  if (!Array.isArray(ignore) || !ignore.every(isPath)) {
    throw new Error(`${id}: ignore must be an array of paths`);
  }
  const skips = raw.skip ?? {};
  if (!isObject(skips)) throw new Error(`${id}: skip must be an object`);
  for (const [surface, reason] of Object.entries(skips)) {
    if (!SURFACES.includes(surface)) throw new Error(`${id}: skip names no surface ${surface}`);
    if (typeof reason !== 'string' || !reason.trim()) {
      throw new Error(`${id}: skip.${surface} needs a reason`);
    }
    if (!SKIP_REFERENCE.test(reason)) {
      throw new Error(
        `${id}: skip.${surface} ends by naming its issue or trace id, as \`(#N)\` or \`(IMAGE-12)\``,
      );
    }
  }
  return {
    id,
    area,
    capability: raw.capability,
    input: raw.input,
    expect: raw.expect,
    ignore,
    skip: skips,
  };
}

/** Every `<area>/<case>.json` under `directory`, sorted, and a problem per stray file. */
function readCases(directory) {
  const cases = [];
  const problems = [];
  if (!existsSync(directory) || !statSync(directory).isDirectory()) {
    return { cases, problems: [`the corpus directory ${directory} can't be read`] };
  }
  for (const area of readdirSync(directory).sort()) {
    const areaPath = join(directory, area);
    if (statSync(areaPath).isFile()) {
      if (area !== 'README.md') {
        problems.push(`${areaPath}: only README.md may sit beside the area directories`);
      }
      continue;
    }
    for (const file of readdirSync(areaPath).sort()) {
      const path = join(areaPath, file);
      const name = file.endsWith('.json') ? file.slice(0, -'.json'.length) : '';
      if (!name || !statSync(path).isFile()) {
        problems.push(`${path}: a case is a .json file directly under its area`);
        continue;
      }
      try {
        cases.push(readCase(path, area, name));
      } catch (error) {
        problems.push(error.message);
      }
    }
  }
  return { cases, problems };
}

/** `null` to run the case, or the reason to skip it. Throws on an inconsistent case. */
function decide(testCase, row) {
  const cell = row[SURFACE];
  if (isObject(cell)) {
    if (SURFACE in testCase.skip) {
      throw new Error(`${testCase.id}: skip.${SURFACE} repeats what the table already says`);
    }
    return `the table exempts ${SURFACE} from ${testCase.capability}: ${cell.exempt}`;
  }
  return testCase.skip[SURFACE] ?? null;
}

// ── judging ──────────────────────────────────────────────────────────────────

function remove(value, path) {
  const parts = path.split('.');
  const last = parts.pop();
  for (const key of parts) {
    if (!isObject(value) || !(key in value)) return;
    value = value[key];
  }
  if (isObject(value)) delete value[last];
}

// The ids `judge` has seen, so the last test can fail on a planned case nothing compared.
const JUDGED = new Set();

function judge(testCase, answer) {
  JUDGED.add(testCase.id);
  const expect = structuredClone(testCase.expect);
  const actual = structuredClone(answer);
  if (isObject(expect.error) && isObject(actual.error)) {
    actual.error = Object.fromEntries(
      Object.entries(actual.error).filter(([key]) => key in expect.error),
    );
  }
  for (const path of testCase.ignore) {
    remove(expect, path);
    remove(actual, path);
  }
  const problems = [];
  if (!isDeepStrictEqual(actual, expect)) {
    problems.push(
      `${testCase.id}: ${SURFACE} answered\n  ${JSON.stringify(actual)}\nexpected\n  ` +
        JSON.stringify(expect),
    );
  }
  if (problems.length) assert.fail(problems.join('\n'));
}

// ── TypeScript's entry points, one handler per area ──────────────────────────

/** The facets every runner reports. TypeScript's `retryable` is `isRetryable(error)`. */
function refusal(error) {
  const code = codeOf(error);
  if (code === undefined) throw error;
  return {
    error: { code, wire_kind: wireKindOf(error) ?? null, retryable: isRetryable(error) },
  };
}

async function answered(call) {
  try {
    return await call();
  } catch (error) {
    return refusal(error);
  }
}

const binaryOf = (testCase) => new Uint8Array(Buffer.from(testCase.input.binary_hex, 'hex'));
const sizeOf = (testCase) => SizeClass.fromBaselineMib(testCase.input.size_mib);

async function imageName(testCase) {
  if (testCase.capability === 'ensure-image') {
    const dockerfile = wrapDockerfile(`FROM ${defaultBaseImage().dockerRef}\n`);
    return answered(async () => {
      const sandbox = await Sandbox.create(Region.usEast1());
      const image = await sandbox.ensureImage(
        {
          namePrefix: testCase.input.name_prefix,
          binary: binaryOf(testCase),
          dockerfile,
          s3Bucket: 'parity-cases-bucket',
          buildRoleArn: BUILD_ROLE,
        },
        sizeOf(testCase),
      );
      return { built: String(image) };
    });
  }
  if (testCase.capability === 'agent-image-name') {
    return answered(async () => {
      const vm = await AgentVm.create(
        Region.usEast1(),
        testCase.input.agents.map((agent) => ({ agent })),
      );
      const name = await vm.imageName(
        { binary: binaryOf(testCase), buildRoleArn: BUILD_ROLE },
        sizeOf(testCase),
      );
      return { name };
    });
  }
  throw new Error(`${testCase.id}: no TypeScript handler for ${testCase.capability} in image-name`);
}

async function cost(testCase) {
  assert.equal(testCase.capability, 'estimate', testCase.id);
  // `input.defaults` stays out: the binding's own defaults for `launched` and the label are what
  // the case asks about.
  return answered(() =>
    JSON.parse(
      estimateRun(sizeOf(testCase), {
        runningSeconds: testCase.input.running_seconds,
        suspendedSeconds: testCase.input.suspended_seconds,
        suspendResumeCycles: testCase.input.suspend_resume_cycles,
      }).toJson(),
    ),
  );
}

async function daemonStatus(testCase) {
  const server = await startSseServer([[testCase.input.body]], { status: testCase.input.status });
  try {
    const session = Session.direct(server.endpoint, 'agent-token');
    return await answered(async () => {
      if (testCase.capability === 'health') {
        await session.health();
      } else if (testCase.capability === 'upload-file') {
        await session.uploadFile(testCase.input.path, new Uint8Array(Buffer.from('parity')));
      } else {
        throw new Error(`${testCase.id}: no TypeScript handler for ${testCase.capability} in error`);
      }
      return { ok: true };
    });
  } finally {
    await server.close();
  }
}

async function egress(testCase) {
  const options = testCase.input;
  if (testCase.capability === 'launch') {
    return answered(async () => {
      const sandbox = await Sandbox.create(Region.usEast1());
      const session = await sandbox.run({
        imageIdentifier: IMAGE_ARN,
        egress: options.egress,
        egressNetworkConnectors: options.egress_network_connectors,
        denyEgress: options.deny_egress,
      });
      return { launched: String(session) };
    });
  }
  if (testCase.capability === 'egress-posture-for') {
    return answered(() => ({
      posture: egressPostureFor(
        options.egress,
        options.egress_network_connectors,
        options.deny_egress,
      ),
    }));
  }
  throw new Error(`${testCase.id}: no TypeScript handler for ${testCase.capability} in egress`);
}

/** The case's record written where the CLI's registry keeps it, then adopted by name. */
async function names(testCase) {
  assert.equal(testCase.capability, 'from-name', testCase.id);
  const state = mkdtempSync(join(tmpdir(), 'parity-names-'));
  try {
    const registry = new NameRegistry(state);
    mkdirSync(registry.directory, { recursive: true });
    writeFileSync(join(registry.directory, `${testCase.input.name}.json`), testCase.input.record_text);
    try {
      const sandbox = await Sandbox.fromName(
        Region.parse(testCase.input.region),
        testCase.input.name,
        registry,
      );
      return { adopted: String(sandbox) };
    } catch (error) {
      const answer = refusal(error);
      answer.message_mentions = Object.fromEntries(
        testCase.input.message_mentions.map((mention) => [mention, error.message.includes(mention)]),
      );
      return answer;
    }
  } finally {
    rmSync(state, { recursive: true, force: true });
  }
}

async function sizeClass(testCase) {
  return answered(() => ({
    baseline_mib: SizeClass.fromRequest(testCase.input.cpus, testCase.input.memory_mib).baselineMib,
  }));
}

async function wrap(testCase) {
  return answered(() => ({ dockerfile: wrapDockerfile(testCase.input.task) }));
}

const HANDLERS = new Map([
  ['image-name', imageName],
  ['cost', cost],
  ['error', daemonStatus],
  ['egress', egress],
  ['names', names],
  ['size-class', sizeClass],
  ['wrap-dockerfile', wrap],
]);

// ── the plan, then one test per case ─────────────────────────────────────────

function plan(directory = CASES, tablePath = TABLE) {
  const table = readTable(tablePath);
  const { cases, problems } = readCases(directory);
  const result = { run: [], skipped: [], problems, loaded: new Set() };
  for (const testCase of cases) {
    result.loaded.add(testCase.id);
    const row = table.get(testCase.capability);
    if (!row) {
      result.problems.push(
        `${testCase.id}: capability ${testCase.capability} names no row in verify/parity/capabilities.toml`,
      );
      continue;
    }
    let reason;
    try {
      reason = decide(testCase, row);
    } catch (error) {
      result.problems.push(error.message);
      continue;
    }
    if (reason !== null) {
      result.skipped.push([testCase, reason]);
    } else if (HANDLERS.has(testCase.area)) {
      result.run.push(testCase);
    } else {
      result.problems.push(
        `${testCase.id}: the ${SURFACE} runner has no handler for area ${testCase.area}, and ` +
          `the table doesn't exempt ${SURFACE} from ${testCase.capability}`,
      );
    }
  }
  return result;
}

/** An owned area with nothing to run, or a sentinel that isn't there or won't run. */
function floorProblems(result) {
  const problems = [...HANDLERS.keys()]
    .filter((area) => !result.run.some((testCase) => testCase.area === area))
    .map((area) => `no case runs in area ${area} on ${SURFACE}: the corpus is empty or unread there`);
  if (!result.loaded.has(SENTINEL)) {
    problems.push(`the sentinel ${SENTINEL}.json wasn't loaded`);
  } else if (!result.run.some((testCase) => testCase.id === SENTINEL)) {
    problems.push(`the sentinel ${SENTINEL}.json doesn't run on ${SURFACE}`);
  }
  return problems;
}

const PLAN = plan();

test('the corpus plans cleanly', () => {
  assert.deepEqual(PLAN.problems, []);
});

test('every area and the sentinel run', () => {
  assert.deepEqual(floorProblems(PLAN), []);
});

for (const testCase of PLAN.run) {
  test(testCase.id, async () => {
    judge(testCase, await HANDLERS.get(testCase.area)(testCase));
  });
}

for (const [testCase, reason] of PLAN.skipped) {
  test(testCase.id, { skip: reason }, () => {});
}

// The floors above count what the plan holds; this counts what was compared. A skip option or a
// handler that returns before `judge` would otherwise leave every case skipped and the file
// green. Top-level tests run in order, so this one runs after every case; a run filtered by
// name leaves it out along with the cases it would hold.
test('every planned case was judged', () => {
  const unjudged = PLAN.run
    .filter((testCase) => !JUDGED.has(testCase.id))
    .map((testCase) => `${testCase.id}: planned for ${SURFACE} but never judged`);
  assert.deepEqual(unjudged, []);
});
