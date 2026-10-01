// SPDX-License-Identifier: Apache-2.0
// `baseImageVersion` and `projectDir` on the image calls (#264).
//
// `buildImage`, `preflight`, `buildArtifact` and `ensureImage` take both, and each value reaches
// core's request: an illegal pin, and a directory without exactly one manifest+lockfile pair, are
// core's refusals before any call, and a pair the Dockerfile never installs from is refused by
// core's install check, which only sees the files the directory gave. HTTPS goes to a proxy on a
// loopback port nothing listens on, so a refusal that regresses fails on a connection error
// instead of reaching AWS.

import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { inflateRawSync } from 'node:zlib';

import { Region, Sandbox, defaultBaseImage, wrapDockerfile } from '../index.js';
import { codeOf } from './support/sse.mjs';

process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
process.env.AWS_SECRET_ACCESS_KEY = 'secret';
delete process.env.AWS_PROFILE;
process.env.HTTPS_PROXY = 'http://127.0.0.1:9';

const PYPROJECT = '[project]\nname = "probe"\nversion = "0.1.0"\n';
const UV_LOCK = 'version = 1\n';

// An aarch64 ELF header: the daemon a build takes.
const daemon = () => {
  const header = new Uint8Array(20);
  header.set([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01], 0);
  header[18] = 0xb7;
  return header;
};

const emptyDir = () => mkdtempSync(join(tmpdir(), 'build-project-'));

const pairDir = () => {
  const dir = emptyDir();
  writeFileSync(join(dir, 'pyproject.toml'), PYPROJECT);
  writeFileSync(join(dir, 'uv.lock'), UV_LOCK);
  writeFileSync(join(dir, '.env'), 'SECRET=1\n');
  return dir;
};

const options = (overrides = {}) => ({
  name: 'task',
  binary: daemon(),
  codeArtifactUri: 's3://agentd-conformance-bucket/task.zip',
  buildRoleArn: 'arn:aws:iam::123456789012:role/build',
  ...overrides,
});

const rejectsWith = async (promise, code, cause) =>
  assert.rejects(promise, (error) => {
    assert.equal(codeOf(error), code, error.message);
    assert.match(error.message, cause);
    return true;
  });

// The stored or deflated entries of a zip, by name, read from the central directory.
const zipEntries = (bytes) => {
  const buffer = Buffer.from(bytes);
  const end = buffer.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  const count = buffer.readUInt16LE(end + 10);
  let at = buffer.readUInt32LE(end + 16);
  const entries = new Map();
  for (let index = 0; index < count; index += 1) {
    const method = buffer.readUInt16LE(at + 10);
    const size = buffer.readUInt32LE(at + 20);
    const nameLength = buffer.readUInt16LE(at + 28);
    const extraLength = buffer.readUInt16LE(at + 30);
    const commentLength = buffer.readUInt16LE(at + 32);
    const local = buffer.readUInt32LE(at + 42);
    const name = buffer.toString('utf8', at + 46, at + 46 + nameLength);
    const dataAt = local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28);
    const data = buffer.subarray(dataAt, dataAt + size);
    entries.set(name, method === 8 ? inflateRawSync(data) : data);
    at += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
};

test('a pin and a project pair pass preflight', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  assert.equal(
    await sandbox.preflight(options({ baseImageVersion: '1', projectDir: pairDir() })),
    undefined,
  );
});

test('a bad pin is refused before any call', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  await rejectsWith(
    sandbox.preflight(options({ baseImageVersion: '1 0' })),
    'ERR_INVALID_ARG',
    /baseImageVersion/,
  );
});

test('a directory without a pair is refused before any call', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  const dir = emptyDir();
  await rejectsWith(
    sandbox.preflight(options({ projectDir: dir })),
    'ERR_PRECONDITION',
    /pyproject\.toml\+uv\.lock/,
  );
  writeFileSync(join(dir, 'pyproject.toml'), PYPROJECT);
  await rejectsWith(sandbox.preflight(options({ projectDir: dir })), 'ERR_PRECONDITION', /uv lock/);
});

test('a pair the Dockerfile never installs from is refused', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  const dockerfile = wrapDockerfile(`FROM ${defaultBaseImage().dockerRef}\n`);
  await rejectsWith(
    sandbox.preflight(options({ projectDir: pairDir(), dockerfile })),
    'ERR_INVALID_ARG',
    /never mentions uv\.lock/,
  );
});

test('the artifact carries the pair and nothing else from the directory', async () => {
  const sandbox = await Sandbox.create(Region.usEast1());
  const entries = zipEntries(await sandbox.buildArtifact(options({ projectDir: pairDir() })));
  assert.equal(entries.get('pyproject.toml')?.toString('utf8'), PYPROJECT);
  assert.equal(entries.get('uv.lock')?.toString('utf8'), UV_LOCK);
  assert.equal(entries.has('.env'), false, [...entries.keys()].join(', '));
});

for (const [label, overrides, code, cause] of [
  ['the pin', { baseImageVersion: '1 0' }, 'ERR_INVALID_ARG', /baseImageVersion/],
  ['the project', { projectDir: 'EMPTY' }, 'ERR_PRECONDITION', /pyproject\.toml\+uv\.lock/],
]) {
  test(`ensureImage refuses ${label} before any call`, async () => {
    const sandbox = await Sandbox.create(Region.usEast1());
    const dockerfile = wrapDockerfile(`FROM ${defaultBaseImage().dockerRef}\n`);
    const given = overrides.projectDir === 'EMPTY' ? { projectDir: emptyDir() } : overrides;
    await rejectsWith(
      sandbox.ensureImage({
        namePrefix: 'task',
        binary: daemon(),
        dockerfile,
        s3Bucket: 'agentd-conformance-bucket',
        buildRoleArn: 'arn:aws:iam::123456789012:role/build',
        ...given,
      }),
      code,
      cause,
    );
  });
}
