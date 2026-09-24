// SPDX-License-Identifier: Apache-2.0
// The name registry the CLI shares, and records kept outside it — all without AWS.
//
// A missing name and a foreign region are refused before any control-plane call; the live
// half, a VM adopted by name in another process, is `conformance/run_rs.py`'s
// `drive_find_by_name`.

import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { AgentVm, NameRecord, NameRegistry, Region, Sandbox } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

const CANARY = 'names-canary-token-7c1f';
const ENDPOINT = 'https://mvm-1.example.invalid';
const record = (name = 'ci', id = 'microvm-a') =>
  new NameRecord(name, id, ENDPOINT, CANARY, Region.usEast1());

function withDir(body) {
  const dir = mkdtempSync(join(tmpdir(), 'microvms-names-'));
  const cleanup = () => rmSync(dir, { recursive: true, force: true });
  let result;
  try {
    result = body(dir);
  } catch (error) {
    cleanup();
    throw error;
  }
  if (result && typeof result.then === 'function') return result.finally(cleanup);
  cleanup();
  return result;
}

test('put, get, list, delete, and release round trip', () =>
  withDir((dir) => {
    const registry = new NameRegistry(dir);
    assert.equal(registry.get('ci'), null);
    registry.put(record('ci'));
    registry.put(record('alias'));
    registry.put(record('other', 'microvm-b'));
    assert.equal(registry.get('ci').microvmId, 'microvm-a');
    assert.equal(registry.get('ci').agentToken(), CANARY);
    assert.deepEqual(
      registry.list().map((r) => r.name),
      ['alias', 'ci', 'other'],
    );
    assert.deepEqual(registry.releaseByVm('microvm-a'), ['alias', 'ci']);
    assert.equal(registry.delete('other'), true);
    assert.equal(registry.delete('other'), false);
    assert.deepEqual(registry.list(), []);
    assert.equal(registry.directory, join(dir, 'names'));
  }));

test('a record the CLI wrote resolves', () =>
  withDir((dir) => {
    mkdirSync(join(dir, 'names'));
    writeFileSync(
      join(dir, 'names', 'old.json'),
      JSON.stringify(
        {
          name: 'old',
          microvmId: 'microvm-1',
          endpoint: ENDPOINT,
          agentToken: CANARY,
          region: 'us-east-1',
          at: 1789000000,
          egressPosture: 'managed',
        },
        null,
        2,
      ),
    );
    const found = new NameRegistry(dir).get('old');
    assert.equal(found.microvmId, 'microvm-1');
    assert.equal(found.at, 1789000000);
    assert.equal(found.egressPosture, 'managed');
  }));

test('the token stays out of JSON and strings; toObject is the one way out', () => {
  const original = record();
  assert.equal(JSON.stringify(original), '{}');
  assert.doesNotMatch(String(original), new RegExp(CANARY));
  const object = original.toObject();
  assert.equal(object.agentToken, CANARY);
  const back = NameRecord.fromObject(object);
  assert.deepEqual(back.toObject(), object);
});

test('bad records are refused without printing the token', () => {
  const refused = (body, pattern) =>
    assert.throws(body, (error) => {
      assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
      if (pattern) assert.match(error.message, pattern);
      assert.doesNotMatch(error.message, new RegExp(CANARY));
      return true;
    });
  refused(() => new NameRecord('microvm-x', 'id', ENDPOINT, CANARY, Region.usEast1()), /prefix/);
  refused(() => new NameRecord('ci', 'id', ENDPOINT, '', Region.usEast1()), /agentToken/);
  refused(() => NameRecord.fromObject({ ...record().toObject(), at: -1 }));
});

test('a torn file keeps its name taken', () =>
  withDir((dir) => {
    const registry = new NameRegistry(dir);
    registry.put(record('good'));
    writeFileSync(join(dir, 'names', 'torn.json'), '{"name": "to');
    assert.throws(
      () => registry.get('torn'),
      (error) => codeOf(error) === 'ERR_PRECONDITION' && /torn\.json/.test(error.message),
    );
    assert.deepEqual(
      registry.list().map((r) => r.name),
      ['good'],
    );
  }));

test('a record file is owner-only', { skip: process.platform === 'win32' }, () =>
  withDir((dir) => {
    new NameRegistry(dir).put(record());
    assert.equal(statSync(join(dir, 'names', 'ci.json')).mode & 0o777, 0o600);
  }));

test('fromName refuses missing and foreign names before any call', async () =>
  withDir(async (dir) => {
    const registry = new NameRegistry(dir);
    for (const fromName of [Sandbox.fromName, AgentVm.fromName]) {
      await assert.rejects(fromName(Region.usEast1(), 'ci', registry), (error) => {
        assert.equal(codeOf(error), 'ERR_PRECONDITION', error.message);
        return /no VM is named/.test(error.message);
      });
    }
    registry.put(record());
    for (const fromName of [Sandbox.fromName, AgentVm.fromName]) {
      await assert.rejects(fromName(Region.usWest2(), 'ci', registry), (error) => {
        assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
        assert.doesNotMatch(error.message, new RegExp(CANARY));
        return /registered in us-east-1/.test(error.message);
      });
    }
  }));

test('an unlaunched sandbox has nothing to name', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  await assert.rejects(NameRecord.forSandbox('ci', sandbox), (error) => {
    assert.equal(codeOf(error), 'ERR_PRECONDITION', error.message);
    return /no VM to name/.test(error.message);
  });
});
