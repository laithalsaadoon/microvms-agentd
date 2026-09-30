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

test('terminate waits for a boolean or a number of seconds', async () => {
  // A sandbox that launched nothing has no VM to wait for, so each report comes back at once:
  // what's asserted is that both shapes reach the core's teardown options, and that a number no
  // duration can be is refused before the teardown runs.
  //
  // **Falsification**: map a number to the core's default wait in `TeardownOptions::into_opts`
  // (drop the seconds on their way to `TeardownOpts`) and the negative wait is no longer refused.
  for (const waitForTerminated of [false, true, 0.5, 120]) {
    const sandbox = await Sandbox.create(region());
    const report = await sandbox.terminate({ waitForTerminated });
    assert.equal(report.terminateAccepted, false);
    assert.equal(report.leaked, false);
  }
  const sandbox = await Sandbox.create(region());
  await refused(sandbox.terminate({ waitForTerminated: -1 }));
});
