// SPDX-License-Identifier: Apache-2.0
// Daemon provisioning through the binding (BIND-17 through BIND-20), without GitHub.
//
// Every case is answered before a download: a caller-supplied binary, the cache, a refusal,
// or a fetch that can't reach GitHub. The fake-release scenarios that drive the
// verification policy are `microvms-core/tests/features/provision.feature`.

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { coreVersion, provisionAgentd, provisionAgentdReport } from '../index.js';
import { codeOf } from './support/sse.mjs';

delete process.env.MICROVM_AGENTD;

/** A little-endian ELF header for `machine` (0xB7 is aarch64, 0x3E is x86_64). */
function elf(machine, tail = '') {
  const header = Buffer.alloc(20);
  header.write('\x7fELF', 0, 'latin1');
  header[5] = 1;
  header.writeUInt16LE(machine, 18);
  return Buffer.concat([header, Buffer.from(tail)]);
}

async function withDir(body) {
  const dir = mkdtempSync(join(tmpdir(), 'microvms-provision-'));
  try {
    return await body(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// Sends the in-process fetch through a proxy on a closed local port, so it can't reach
// GitHub. reqwest reads the proxy variables when the fetch builds its client.
const PROXY_VARIABLES = ['HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'];
const NO_PROXY_VARIABLES = ['NO_PROXY', 'no_proxy'];

async function withoutGitHub(body) {
  const saved = Object.fromEntries(
    [...PROXY_VARIABLES, ...NO_PROXY_VARIABLES].map((name) => [name, process.env[name]]),
  );
  for (const name of PROXY_VARIABLES) process.env[name] = 'http://127.0.0.1:9';
  for (const name of NO_PROXY_VARIABLES) delete process.env[name];
  try {
    return await body();
  } finally {
    for (const [name, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

test('BIND-17 a caller-supplied binary is returned without a fetch', () =>
  withDir(async (dir) => {
    const binary = join(dir, 'agentd');
    const data = elf(0xb7, 'caller');
    writeFileSync(binary, data);
    assert.deepEqual(await provisionAgentd({ stateDir: dir, binary }), data);

    const report = await provisionAgentdReport({ stateDir: dir, binary });
    assert.equal(report.source, 'caller-supplied');
    assert.equal(report.suppliedBy, 'argument');
    assert.equal(report.verification, undefined);
    assert.deepEqual(report.data, data);
    assert.equal(report.path, binary);
    assert.equal(report.version, coreVersion());
    assert.equal(report.sha256, createHash('sha256').update(data).digest('hex'));
  }));

test('BIND-17 MICROVM_AGENTD supplies the binary when the options do not', () =>
  withDir(async (dir) => {
    const binary = join(dir, 'env-agentd');
    writeFileSync(binary, elf(0xb7));
    process.env.MICROVM_AGENTD = binary;
    try {
      const report = await provisionAgentdReport({ version: 'v9.9.9', stateDir: dir });
      assert.equal(report.suppliedBy, 'env');
      assert.equal(report.version, '9.9.9');
    } finally {
      delete process.env.MICROVM_AGENTD;
    }
  }));

test('BIND-19 a recorded cache entry is served and a changed one is fetched again', () =>
  withDir(async (dir) => {
    const version = '9.9.9';
    const entry = join(dir, 'agentd', `v${version}`);
    mkdirSync(entry, { recursive: true });
    const data = elf(0xb7, 'cached');
    writeFileSync(join(entry, 'agentd'), data);
    writeFileSync(
      join(entry, 'agentd.verified.json'),
      JSON.stringify({
        version,
        sha256: createHash('sha256').update(data).digest('hex'),
        verification: 'attestation',
      }),
    );
    const report = await provisionAgentdReport({ version, stateDir: dir });
    assert.equal(report.source, 'cache');
    assert.equal(report.verification, 'attestation');

    writeFileSync(join(entry, 'agentd'), elf(0xb7, 'changed'));
    await withoutGitHub(() =>
      assert.rejects(provisionAgentd({ version, stateDir: dir }), (error) => {
        assert.equal(codeOf(error), 'ERR_PRECONDITION');
        assert.match(error.message, /could not download agentd/);
        return true;
      }),
    );
    assert.equal(existsSync(join(entry, 'agentd')), false);
  }));

test('BIND-18 a fetch that cannot run rejects naming the tag and every way out', () =>
  withDir((dir) =>
    withoutGitHub(() =>
      assert.rejects(provisionAgentd({ stateDir: dir }), (error) => {
        assert.equal(codeOf(error), 'ERR_PRECONDITION');
        assert.match(error.message, new RegExp(`v${coreVersion().replaceAll('.', '\\.')}`));
        assert.match(error.message, /gh release download/);
        assert.match(error.message, /MICROVM_AGENTD/);
        return true;
      }),
    ),
  ));

test('BIND-20 a caller binary that is not an aarch64 ELF is refused', () =>
  withDir(async (dir) => {
    const cases = [
      [elf(0x3e), /ELF machine 0x3e/],
      [Buffer.from('#!/bin/sh\nexec agentd\n'), /not an ELF/],
    ];
    for (const [content, detail] of cases) {
      const binary = join(dir, 'agentd');
      writeFileSync(binary, content);
      await assert.rejects(provisionAgentd({ stateDir: dir, binary }), (error) => {
        assert.equal(codeOf(error), 'ERR_PRECONDITION');
        assert.match(error.message, detail);
        return true;
      });
    }
    await assert.rejects(provisionAgentd({ stateDir: dir, binary: join(dir, 'gone') }), /does not exist/);
  }));

test('BIND-17 a version that is not a tag is an invalid argument', () =>
  withDir(async (dir) => {
    for (const version of ['../../etc', 'v', '1.0/../x']) {
      await assert.rejects(provisionAgentd({ version, stateDir: dir }), (error) => {
        assert.equal(codeOf(error), 'ERR_INVALID_ARG');
        return true;
      });
    }
  }));
