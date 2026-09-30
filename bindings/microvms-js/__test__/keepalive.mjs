// SPDX-License-Identifier: Apache-2.0
//
// `session.keepAwake(...)` against a local fake of the daemon's health route. The poll policy is
// the core's and is tested there; these check the handle's contract as JavaScript sees it.

import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { setTimeout as sleep } from 'node:timers/promises';

import { Session } from '../index.js';
import { codeOf } from './support/sse.mjs';

/**
 * A health route answering from a `busy` script; the last answer repeats. Any `status` but 200
 * answers every poll with it instead, the way a daemon that hasn't bootstrapped answers 503.
 */
async function daemon(busy, status = 200) {
  const seen = [];
  const server = createServer((req, res) => {
    seen.push([req.url, req.headers.authorization ?? null]);
    if (status !== 200) {
      res.writeHead(status, { 'content-length': '0' });
      res.end();
      return;
    }
    const body = JSON.stringify({
      version: '0.1.0',
      bootstrapped: true,
      disk: null,
      identity_degraded: false,
      identity_repaired: true,
      busy: busy[Math.min(seen.length, busy.length) - 1],
    });
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(body);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    seen,
    session: Session.direct(`http://127.0.0.1:${port}`, 'agent-token'),
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

test('whileBusy ends on the first idle answer, polling only unauthenticated health', async () => {
  const fake = await daemon([true, true, false]);
  try {
    const keepalive = await fake.session.keepAwake({ intervalSec: 1, whileBusy: true });
    const report = await keepalive.done();
    assert.deepEqual(
      [report.end, report.polls, report.lastBusy],
      ['idle', 3, false],
    );
    assert.equal(keepalive.running, false);
    assert.deepEqual(fake.seen, Array(3).fill(['/v1/health', null]));
  } finally {
    await fake.close();
  }
});

test('stop ends it and no poll follows', async () => {
  const fake = await daemon([true]);
  try {
    const keepalive = await fake.session.keepAwake({ intervalSec: 1 });
    assert.equal(keepalive.running, true);
    await sleep(1500);
    const report = await keepalive.stop();
    assert.equal(report.end, 'stopped');
    assert.ok(report.polls >= 2);
    const polls = fake.seen.length;
    await sleep(1500);
    assert.equal(fake.seen.length, polls, 'a stopped keepalive kept polling');
    assert.equal((await keepalive.done()).end, 'stopped', 'done() resolves again after stop');
  } finally {
    await fake.close();
  }
});

test('maxDurationSec ends it even while busy', async () => {
  const fake = await daemon([true]);
  try {
    const keepalive = await fake.session.keepAwake({ intervalSec: 1, maxDurationSec: 2.5 });
    const report = await keepalive.done();
    assert.equal(report.end, 'elapsed');
    assert.ok(report.elapsedSec >= 2.4 && report.elapsedSec < 4, `${report.elapsedSec}`);
  } finally {
    await fake.close();
  }
});

test('an interval over half the idle window is refused before any poll', async () => {
  const fake = await daemon([true]);
  try {
    await assert.rejects(fake.session.keepAwake({ intervalSec: 31 }), (error) => {
      assert.equal(codeOf(error), 'ERR_INVALID_ARG');
      assert.match(error.message, /half the 60s idle window/);
      return true;
    });
    await assert.rejects(fake.session.keepAwake({ idleWindowSec: 59 }), /platform minimum/);
    assert.deepEqual(fake.seen, []);
    const wide = await fake.session.keepAwake({ intervalSec: 31, idleWindowSec: 600 });
    assert.equal((await wide.stop()).end, 'stopped');
  } finally {
    await fake.close();
  }
});

test('an unreachable daemon is retried, then rejects as retryable', async () => {
  const keepalive = await Session.direct('http://127.0.0.1:9', 'agent-token').keepAwake({
    intervalSec: 1,
  });
  await assert.rejects(keepalive.done(), (error) => {
    assert.equal(codeOf(error), 'ERR_RETRYABLE');
    return true;
  });
  assert.equal(keepalive.running, false);
});

/** Every poll answers 503; the keepalive gives up once it has retried `tolerated` times. */
async function pollsBeforeGivingUp(tolerated) {
  const fake = await daemon([true], 503);
  try {
    const keepalive = await fake.session.keepAwake({ intervalSec: 1, toleratedErrors: tolerated });
    await assert.rejects(keepalive.done(), (error) => {
      assert.equal(codeOf(error), 'ERR_RETRYABLE');
      return true;
    });
    return fake.seen.length;
  } finally {
    await fake.close();
  }
}

test('one tolerated error is one retry before the keepalive rejects', async () => {
  // **Falsification**: drop `toleratedErrors` on its way to the core's `KeepAwake` in
  // `KeepAwakeOptions::policy`, and the core's default retries three times: four polls.
  assert.equal(await pollsBeforeGivingUp(1), 2);
});

test('no tolerated errors rejects on the first failed poll', async () => {
  assert.equal(await pollsBeforeGivingUp(0), 1);
});
