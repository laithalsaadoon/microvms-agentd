// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  BuildHookTimeout, RunHookTimeout, SizeClass, Session,
  compareResidency, estimateRun, runReport,
} from '../index.js';

const invalidArg = (error) => error.cause?.message === 'ERR_INVALID_ARG';
const badIntegers = [-1, 0.5, 2 ** 32, NaN, Infinity, -Infinity];

test('hook timeouts and memory sizes reject truncation and 32-bit wrapping', () => {
  for (const Constructor of [RunHookTimeout, BuildHookTimeout]) {
    for (const value of [...badIntegers, 30.9, 2 ** 32 + 30, 30 - 2 ** 32]) {
      assert.throws(() => new Constructor(value), invalidArg, `${Constructor.name}(${value})`);
    }
    assert.equal(new Constructor(30).seconds, 30);
  }
  for (const value of [...badIntegers, 512.9, 2 ** 32 + 512, 512 - 2 ** 32]) {
    assert.throws(() => SizeClass.fromBaselineMib(value), invalidArg);
  }
  assert.equal(SizeClass.fromBaselineMib(512).baselineMib, 512);
});

test('cost cycle counts reject wrapping and fractional counts', () => {
  const size = SizeClass.defaultClass();
  for (const value of badIntegers) {
    assert.throws(() => runReport(size, { suspendResumeCycles: value }), invalidArg);
    assert.throws(() => estimateRun(size, { suspendResumeCycles: value }), invalidArg);
    assert.throws(() => compareResidency(size, 60, value), invalidArg);
  }
});

test('invalid user and group IDs are rejected before starting a process', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  // In particular, 2**32 must never become uid 0.
  for (const field of ['user', 'group']) {
    for (const value of badIntegers) {
      const options = { [field]: value };
      await assert.rejects(() => session.run(['true'], options), invalidArg);
      await assert.rejects(() => session.runSync(['true'], options), invalidArg);
      await assert.rejects(() => session.spawn(['true'], { exec: options }), invalidArg);
    }
  }
});

test('stream offsets and reconnect counts reject silent clamping and wrapping', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  const handle = await session.exec('numeric-validation');
  for (const value of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => handle.stream({ offset: value }), invalidArg);
    await assert.rejects(() => session.spawn(['true'], { offset: value }), invalidArg);
  }
  for (const value of badIntegers) {
    assert.throws(() => handle.stream({ maxReconnects: value }), invalidArg);
    await assert.rejects(() => session.spawn(['true'], { maxReconnects: value }), invalidArg);
  }
});

test('proxy port inputs cannot be truncated or wrapped to another port', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  for (const value of [...badIntegers, 65536, 9000.5, 2 ** 32 + 9000]) {
    await assert.rejects(() => session.connectHeaders(value), invalidArg);
    await assert.rejects(() => session.connectSubprotocols(value), invalidArg);
  }
  assert.deepEqual(await session.connectHeaders(9000), {});
  assert.equal(await session.connectSubprotocols(9000), null);
});
