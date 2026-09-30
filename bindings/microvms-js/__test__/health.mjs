// SPDX-License-Identifier: Apache-2.0
//
// `Health` carries the daemon's hook log, each workload handler's outcome, and the identity
// repair steps. A local HTTP server stands in for the daemon; the shapes are the daemon's own.

import assert from 'node:assert/strict';
import http from 'node:http';
import { test } from 'node:test';

import { Session } from '../index.js';

const BASE = {
  version: 'test',
  bootstrapped: true,
  disk: null,
  identity_degraded: true,
  identity_repaired: true,
};

const CURRENT = {
  ...BASE,
  hooks: [
    { hook: 'run', fired_at: 10 },
    {
      hook: 'suspend',
      fired_at: 20,
      handler: { exit_code: null, signal: 9, timed_out: true, duration_ms: 20000 },
    },
    { hook: 'resume', fired_at: 30, handler: { exit_code: 0, duration_ms: 12 } },
  ],
  hooks_dropped: 2,
  identity_steps: [
    { name: 'machine-id', outcome: 'repaired' },
    { name: 'boot-id', outcome: 'failed', error: 'EPERM' },
  ],
};

async function healthFrom(body) {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(body));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const { port } = server.address();
    return await Session.direct(`http://127.0.0.1:${port}`, 'token').health();
  } finally {
    server.close();
  }
}

test('hooks carry each handler outcome', async () => {
  const health = await healthFrom(CURRENT);
  const [run, suspend, resume] = health.hooks;
  assert.equal(run.hook, 'run');
  assert.equal(run.firedAt, 10);
  assert.equal(run.handler ?? null, null);
  assert.equal(suspend.handler.timedOut, true);
  assert.equal(suspend.handler.signal, 9);
  assert.equal(suspend.handler.succeeded, false);
  assert.equal(resume.handler.succeeded, true);
  assert.equal(resume.handler.durationMs, 12);
  assert.equal(health.hooksDropped, 2);
});

test('identity steps say which step failed', async () => {
  const health = await healthFrom(CURRENT);
  assert.deepEqual(
    health.identitySteps.map((step) => [step.name, step.outcome, step.error ?? null]),
    [
      ['machine-id', 'repaired', null],
      ['boot-id', 'failed', 'EPERM'],
    ],
  );
});

test('an older daemon reports empty lists', async () => {
  const health = await healthFrom(BASE);
  assert.deepEqual(health.hooks, []);
  assert.deepEqual(health.identitySteps, []);
  assert.equal(health.hooksDropped, 0);
  assert.equal(health.imageEnvKeys ?? null, null);
});
