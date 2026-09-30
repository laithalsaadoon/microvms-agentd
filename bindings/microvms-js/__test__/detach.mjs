// SPDX-License-Identifier: Apache-2.0
// Handing a VM off with `detach()`, refused before any AWS call when there is nothing to hand
// off.
//
// Credentials come from the environment, which the default chain reads without a network
// call. The hand-off itself (fields, silence on drop, refused transitions) is covered in the
// core; the detach → adopt round trip against a real VM is recorded in the PR.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { AgentVm, Detached, Region, Sandbox } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

const region = () => Region.usEast1();

async function precondition(promise) {
  await assert.rejects(promise, (error) => {
    assert.equal(codeOf(error), 'ERR_PRECONDITION', error.message);
    assert.match(error.message, /detach/);
    return true;
  });
}

test('a sandbox with nothing launched has nothing to detach', async () => {
  const sandbox = await Sandbox.create(region());
  await precondition(sandbox.detach());
  assert.equal(await sandbox.isDetached(), false);
});

test('an agent VM with nothing launched has nothing to detach', async () => {
  const vm = await AgentVm.create(region(), [{ agent: 'claude-code' }]);
  await precondition(vm.detach());
});

test('the hand-off record is a class the binding builds, not a constructor callers use', () => {
  assert.equal(typeof Detached, 'function');
  assert.equal(typeof Detached.prototype.agentToken, 'function');
  assert.equal(typeof Detached.prototype.toObject, 'function');
});
