// SPDX-License-Identifier: Apache-2.0
// Adopting a VM another process launched, refused before any AWS call when the record is bad.
//
// Credentials come from the environment, which the default chain reads without a network
// call. The lifecycle mapping and every guard are covered in the core; the live half is
// `conformance/run_rs.py`'s `drive_adopt_by_id`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { AgentVm, Region, Sandbox } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

const CANARY = 'adopt-canary-token-5e2d';
const ENDPOINT = 'https://mvm-1.example.invalid';
const region = () => Region.usEast1();

async function refused(promise, pattern) {
  await assert.rejects(promise, (error) => {
    assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
    if (pattern) assert.match(error.message, pattern);
    assert.doesNotMatch(error.message, new RegExp(CANARY));
    return true;
  });
}

test('adopt needs the launch token before any call', async () => {
  await refused(Sandbox.adopt(region(), 'mvm-1', ENDPOINT, ''), /agent token/);
  await refused(AgentVm.adopt(region(), 'mvm-1', ENDPOINT, ''), /agent token/);
});

test('a bad identifier is refused without printing the token', async () => {
  await refused(Sandbox.adopt(region(), '', ENDPOINT, CANARY));
  await refused(AgentVm.adopt(region(), '', ENDPOINT, CANARY));
});

test('an agent VM checks its specs before any call', async () => {
  await refused(AgentVm.adopt(region(), 'mvm-1', ENDPOINT, CANARY, []));
  await refused(
    AgentVm.adopt(region(), 'mvm-1', ENDPOINT, CANARY, [{ agent: 'codex' }, { agent: 'codex' }]),
  );
});

test('a sandbox this process made is not adopted', async () => {
  const sandbox = await Sandbox.create(region());
  assert.equal(await sandbox.adopted(), false);
});
