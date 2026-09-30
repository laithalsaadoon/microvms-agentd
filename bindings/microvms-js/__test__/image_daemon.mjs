// SPDX-License-Identifier: Apache-2.0
// A build refuses a daemon that isn't an aarch64 ELF before any AWS call (#257, BIND-20).
//
// Credentials come from the environment, which the default chain reads without a network
// call, so a request core let through would reach AWS with them and fail as something other
// than a precondition. `buildArtifact` refuses where the bytes enter an artifact, which is the
// upload a caller makes before `buildImage`; `buildImage` and `ensureImage` refuse in core's
// preflight.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Region, Sandbox, wrapDockerfile } from '../index.js';
import { elfHeader } from './support/elf.mjs';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;

const ROLE = 'arn:aws:iam::123456789012:role/build';
const URI = 's3://agentd-conformance-bucket/task.zip';
const WRONG = [
  ['an x86_64 ELF', elfHeader(0x3e), /ELF machine 0x3e, not aarch64/],
  ['a script', new TextEncoder().encode('#!/bin/sh\nexec agentd\n'), /not an ELF binary at all/],
];
const EMPTY = ['no bytes', new Uint8Array(0), /not an ELF binary at all/];

async function precondition(promise, why) {
  await assert.rejects(promise, (error) => {
    assert.equal(codeOf(error), 'ERR_PRECONDITION', error.message);
    assert.match(error.message, why);
    return true;
  });
}

const build = (binary) => ({ name: 'task', binary, codeArtifactUri: URI, buildRoleArn: ROLE });

for (const [label, binary, why] of [...WRONG, EMPTY]) {
  test(`BIND-20: buildArtifact refuses ${label} as the daemon`, async () => {
    const sandbox = await Sandbox.create(Region.usEast1());
    await precondition(sandbox.buildArtifact(build(binary)), why);
  });

  test(`BIND-20: ensureImage refuses ${label} as the daemon before any call`, async () => {
    const sandbox = await Sandbox.create(Region.usEast1());
    await precondition(
      sandbox.ensureImage({
        namePrefix: 'task',
        binary,
        dockerfile: wrapDockerfile('FROM python:3.12-slim\n'),
        s3Bucket: 'agentd-conformance-bucket',
        buildRoleArn: ROLE,
      }),
      why,
    );
  });
}

for (const [label, binary, why] of WRONG) {
  test(`BIND-20: buildImage refuses ${label} as the daemon before any call`, async () => {
    const sandbox = await Sandbox.create(Region.usEast1());
    await precondition(sandbox.buildImage(build(binary)), why);
  });
}

test('an aarch64 daemon builds an artifact', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  const artifact = await sandbox.buildArtifact(build(elfHeader(0xb7)));
  assert.equal(artifact.subarray(0, 2).toString('latin1'), 'PK');
});
