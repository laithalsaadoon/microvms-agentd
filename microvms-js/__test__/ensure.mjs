// SPDX-License-Identifier: Apache-2.0
// `sandbox.ensureImage` (IMAGE-12): thin, with core's local refusals before any AWS call.
//
// Credentials come from the environment, which the default chain reads without a network
// call. Every case here is refused by core's local half, so nothing reaches STS, S3, or the
// control plane. The decisions, the race, and the upload are core's (IMAGE-6 through
// IMAGE-11) and run live in `conformance/run_rs.py`'s ensure-image section.

import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { Region, Sandbox, wrapDockerfile } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

const options = (overrides = {}) => ({
  namePrefix: 'task',
  binary: new Uint8Array([0x7f, 0x45, 0x4c, 0x46]),
  dockerfile: wrapDockerfile('FROM python:3.12-slim\n'),
  s3Bucket: 'agentd-conformance-bucket',
  buildRoleArn: 'arn:aws:iam::123456789012:role/build',
  ...overrides,
});

async function refused(overrides, cause) {
  const sandbox = await Sandbox.create(Region.usEast1());
  await assert.rejects(sandbox.ensureImage(options(overrides)), (error) => {
    assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
    assert.match(error.message, cause);
    return true;
  });
}

for (const [overrides, cause] of [
  [{ s3Bucket: 'Not_A_Bucket' }, /bucket/],
  [{ namePrefix: '///' }, /prefix/],
  [{ buildRoleArn: 'not-an-arn' }, /buildRoleArn/],
  [{ s3KeyPrefix: 'a\nb' }, /key prefix/],
  [{ dockerfile: 'FROM x\nENTRYPOINT ["/bin/sh"]\nCMD ["/agentd"]\n' }, /ENTRYPOINT/],
]) {
  test(`core refuses ${JSON.stringify(overrides)} before any call`, async () => {
    await refused(overrides, cause);
  });
}

test('a context directory is read by core', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'microvms-ensure-'));
  await refused({ contextDir: join(dir, 'missing') }, /not a directory/);
  writeFileSync(join(dir, 'agentd'), 'not the daemon');
  await refused({ contextDir: dir }, /agentd/);
});
