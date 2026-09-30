// SPDX-License-Identifier: Apache-2.0
// `Sandbox.preflight` and `Sandbox.managedBaseVersions` (#264): core's local refusals.
//
// `preflight` is `buildImage`'s local guards alone, with zero calls, so both its refusals and
// its pass are offline. `managedBaseVersions` refuses a bare base name before its call.
// Credentials come from the environment, which the default chain reads without a network call.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Region, Sandbox } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

// An aarch64 ELF header: the daemon a build takes.
const daemon = () => {
  const header = new Uint8Array(20);
  header.set([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01], 0);
  header[18] = 0xb7;
  return header;
};

const options = (overrides = {}) => ({
  name: 'task',
  binary: daemon(),
  codeArtifactUri: 's3://agentd-conformance-bucket/task.zip',
  buildRoleArn: 'arn:aws:iam::123456789012:role/build',
  ...overrides,
});

test('a request buildImage would send passes preflight', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  assert.equal(await sandbox.preflight(options()), undefined);
  assert.equal(
    await sandbox.preflight(options({ tags: { team: 'x' }, logGroup: '/g', logStream: 's' })),
    undefined,
  );
});

for (const [overrides, cause] of [
  [{ name: 'my.image' }, /name/],
  [{ buildRoleArn: 'not-an-arn' }, /buildRoleArn/],
  [{ codeArtifactUri: '' }, /codeArtifact\.uri/],
  [{ logStream: 's' }, /logGroup/],
  [{ dockerfile: 'FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal\n' }, /CMD/],
  [{ inheritWorkdir: true }, /WORKDIR/],
]) {
  test(`preflight rejects what buildImage would: ${Object.keys(overrides)[0]}`, async () => {
    const sandbox = await Sandbox.create(Region.usEast1());
    await assert.rejects(sandbox.preflight(options(overrides)), (error) => {
      assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
      assert.match(error.message, cause);
      return true;
    });
  });
}

test('managedBaseVersions needs the base full ARN', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  await assert.rejects(sandbox.managedBaseVersions('al2023-1'), (error) => {
    assert.equal(codeOf(error), 'ERR_PRECONDITION', error.message);
    assert.match(error.message, /full ARN/);
    return true;
  });
});
