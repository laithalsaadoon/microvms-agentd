// SPDX-License-Identifier: Apache-2.0
// Lifecycle by ID, launch options, and per-VM logging, refused before any AWS call.
//
// Credentials come from the environment, which the default chain reads without a network
// call, so every object here is the real one. The live half is `conformance/run_rs.py`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { AgentVm, ControlPlane, Region, Sandbox } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

const region = () => Region.usEast1();

async function refused(promise, pattern) {
  await assert.rejects(promise, (error) => {
    assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
    if (pattern) assert.match(error.message, pattern);
    return true;
  });
}

test('the control plane checks identifiers before the wire', async () => {
  const plane = await ControlPlane.create(region());
  assert.equal(plane.region, 'us-east-1');
  for (const call of ['get', 'suspend', 'resume', 'terminate']) {
    await refused(plane[call](''));
  }
  await refused(plane.list({ imageIdentifier: '' }));
  await refused(plane.waitForState('', ['RUNNING']));
  await refused(plane.waitForState('mvm-1', ['RUNNING'], { timeout: -1 }));
});

// Image administration (#264). Titles carry no `#`, which TAP escapes.
test('image administration checks its arguments before the wire', async () => {
  const plane = await ControlPlane.create(region());
  await refused(plane.listImageVersions(''), /imageIdentifier/);
  await refused(plane.listImageBuilds('', '1.0'), /imageIdentifier/);
  await refused(plane.listImageBuilds('arn:image', ''), /imageVersion/);
  await refused(plane.getImageBuild('arn:image', '1.0', ''), /buildId/);
  await refused(plane.setImageVersionStatus('arn:image', '2.0\n', 'INACTIVE'), /imageVersion/);
});

test('a version status is the model spelling', async () => {
  // Parsed by core before the call: a near miss is refused naming both values, and a legal one
  // gets past the parse to the identifier check behind it.
  const plane = await ControlPlane.create(region());
  for (const status of ['INACTIVATE', 'inactive', '']) {
    await refused(plane.setImageVersionStatus('arn:image', '2.0', status), /ACTIVE or INACTIVE/);
  }
  for (const status of ['ACTIVE', 'INACTIVE']) {
    await refused(plane.setImageVersionStatus('', '2.0', status), /imageIdentifier/);
  }
});

test('deleting an image the service would refuse resolves false', async () => {
  const plane = await ControlPlane.create(region());
  assert.equal(await plane.deleteImage(''), false);
  await refused(plane.deleteImage('arn:image', { backoff: -1 }));
  await refused(plane.deleteImage('arn:image', { attempts: 1.5 }), /attempts/);
});

test('per-VM logging is refused locally', async () => {
  const cases = [
    [{ logStream: 's' }, /pass log_group/],
    [{ logGroup: '/g', disableLogging: true }, /cannot be combined/],
    [{ logGroup: 'bad group!' }, /log/],
  ];
  for (const [options, pattern] of cases) {
    const sandbox = await Sandbox.create(region());
    await refused(sandbox.run({ imageIdentifier: 'arn:image', ...options }), pattern);
  }
});

test('waitUntilRunning needs a launch', async () => {
  const sandbox = await Sandbox.create(region());
  await assert.rejects(sandbox.waitUntilRunning(), (error) => {
    assert.equal(codeOf(error), 'ERR_PRECONDITION', error.message);
    return true;
  });
});

test('an agent VM takes VPC connectors and checks them locally', async () => {
  const vm = await AgentVm.create(region(), [{ agent: 'claude-code' }]);
  await refused(
    vm.launch({
      imageIdentifier: 'arn:image',
      imageVersion: '1.0',
      egressNetworkConnectors: ['not-a-connector-arn'],
    }),
  );
  await refused(vm.launch({ imageIdentifier: 'arn:image', logStream: 's' }), /pass log_group/);
});
