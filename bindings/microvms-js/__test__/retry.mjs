// SPDX-License-Identifier: Apache-2.0
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ControlPlane, Region, Sandbox, retryDetails } from '../index.js';

for (const kind of [ControlPlane, Sandbox]) {
  test(`${kind.name} validates its retry budget before credentials`, async () => {
    for (const retry of [
      { maxAttempts: 0, maxElapsedSeconds: 1 },
      { maxAttempts: 1, maxElapsedSeconds: 0 },
      { maxAttempts: 1.5, maxElapsedSeconds: 1 },
    ]) {
      await assert.rejects(kind.create(Region.usEast1(), retry), error => {
        assert.equal(error.cause.message, 'ERR_INVALID_ARG');
        return true;
      });
    }
  });
}

test('retryDetails refuses unrelated values and reads telemetry without parsing text', () => {
  for (const error of [null, undefined, 1, 'throttled', new Error('throttled')]) {
    assert.equal(retryDetails(error), null);
  }
  assert.deepEqual(retryDetails(Object.assign(new Error('anything'), {
    attemptCount: 3, backoffSeconds: 0.5, throttled: true,
  })), { attemptCount: 3, backoffSeconds: 0.5, throttled: true });
});

test('Sandbox control-plane failures retain retry telemetry', async () => {
  const sandbox = Sandbox.parityFromScript(JSON.stringify(Array.from({length: 6}, () => ({
    operation: 'ListManagedMicrovmImageVersions', status: 429, body: '{}',
  }))));
  await assert.rejects(sandbox.managedBaseVersions('arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1'), error => {
    const details = retryDetails(error);
    assert.equal(error.cause.message, 'ERR_RETRYABLE');
    assert.equal(details.attemptCount, 6);
    assert.equal(details.throttled, true);
    assert.ok(details.backoffSeconds > 0);
    return true;
  });
});

test('ControlPlane sweep failures retain retry telemetry', async () => {
  const plane = ControlPlane.parityFromScript(JSON.stringify([
    { operation: 'ListMicrovmImages', status: 429, body: '{}' },
    { operation: 'ListMicrovmImages', status: 200, body: '{"items":[]}' },
    { operation: 'ListMicrovms', status: 200, body: '{"items":[]}' },
  ]), { maxAttempts: 1, maxElapsedSeconds: 30 });
  await assert.rejects(plane.sweep('job-'), error => {
    assert.equal(error.cause.message, 'ERR_RETRYABLE');
    assert.deepEqual(retryDetails(error), {
      attemptCount: 1, backoffSeconds: 0, throttled: true,
    });
    return true;
  });
  assert.deepEqual(plane.parityCalls(), ['ListMicrovmImages']);
});
