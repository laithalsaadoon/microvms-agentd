// SPDX-License-Identifier: Apache-2.0

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

const addonUrl = new URL('../index.js', import.meta.url).href;

// Each check has a separate runtime so its exit status is part of the assertion.
function inRuntime(body) {
  const directory = mkdtempSync(join(tmpdir(), 'microvms-classes-'));
  let child;
  try {
    child = spawnSync(process.execPath, ['--input-type=module', '--eval', `
    import assert from 'node:assert/strict';
    import * as addon from ${JSON.stringify(addonUrl)};
    const {
      Region, NameRecord, NameRegistry, Sandbox, Session, SizeClass,
      RunHookTimeout, BuildHookTimeout,
      estimateRun, runReport, checkBudget, installedAgents, installAgentAccess,
    } = addon;
    const region = Region.usEast1();
    const size = SizeClass.defaultClass();
    const record = new NameRecord('example', 'microvm-example',
      'https://example.invalid', 'example-token', region);
    const session = Session.direct('http://127.0.0.1:1', 'example-token');
    const directory = ${JSON.stringify(directory)};
    const registry = new NameRegistry(directory);
    function classError(name) {
      return (error) => {
        assert.ok(error instanceof Error);
        assert.equal(error.code, 'InvalidArg');
        assert.ok(error.message.includes('Value is not an instance of class '), error.message);
        assert.ok(error.message.includes(name), error.message);
        return true;
      };
    }
    ${body}
  `], { encoding: 'utf8', timeout: 20_000 });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
  assert.ifError(child.error);
  assert.equal(child.signal, null, `runtime signal: ${child.signal}`);
  assert.equal(child.status, 0, child.stderr);
}

test('synchronous class arguments require their declared instance type', () => {
  inRuntime(`
    assert.equal(region.equals(Region.usEast1()), true);
    assert.equal(estimateRun(size).size.baselineMib, size.baselineMib);
    for (const [invoke, expected] of [
      [() => region.equals(record), 'Region'],
      [() => new NameRecord('other', 'microvm-example', 'https://example.invalid',
        'example-token', record), 'Region'],
      [() => registry.put(region), 'NameRecord'],
      [() => estimateRun(region), 'SizeClass'],
      [() => estimateRun(size, undefined, region), 'RateTable'],
      [() => checkBudget(region, '1', 'warn'), 'CostReport'],
      [() => runReport(size, { running: region }), 'Duration'],
      [() => runReport(size, { suspended: region }), 'Duration'],
      [() => runReport(size, { imageBuild: region }), 'Duration'],
      [() => runReport(size, { imageRetained: region }), 'Duration'],
    ]) {
      assert.throws(invoke, classError(expected));
    }
    assert.deepEqual(registry.list(), []);
  `);
});

test('asynchronous class arguments require their declared instance type', () => {
  inRuntime(`
    process.env.AWS_ACCESS_KEY_ID = 'AKIDEXAMPLE';
    process.env.AWS_SECRET_ACCESS_KEY = 'example-secret';
    process.env.AWS_EC2_METADATA_DISABLED = 'true';
    delete process.env.AWS_PROFILE;
    const sandbox = await Sandbox.create(region);
    const options = { name: 'example', binary: Buffer.alloc(0),
      codeArtifactUri: 's3://example/artifact.zip', buildRoleArn: 'example-role' };
    for (const [invoke, expected] of [
      [() => installAgentAccess(session, [{ agent: 'claude-code' }], region), 'BearerToken'],
      [() => Sandbox.create(record), 'Region'],
      [() => installedAgents(region), 'Session'],
      [() => NameRecord.forSandbox('example', region), 'Sandbox'],
      [() => registry.register('example', region), 'Sandbox'],
      [() => registry.importRecord(region, session), 'NameRecord'],
      [() => registry.importRecord(record, region), 'Session'],
      [() => Sandbox.fromName(region, 'example', region), 'NameRegistry'],
      [() => session.tunnel(9000, undefined, region), 'TunnelIdentity'],
      [() => sandbox.buildImage(options, region), 'SizeClass'],
      [() => sandbox.buildImage(options, undefined, new BuildHookTimeout(30)), 'RunHookTimeout'],
      [() => sandbox.buildImage(options, undefined, undefined, new RunHookTimeout(30)), 'BuildHookTimeout'],
    ]) {
      // Argument conversion may throw before a Promise exists, so defer invocation.
      await assert.rejects(Promise.resolve().then(invoke), classError(expected));
    }
    assert.deepEqual(registry.list(), []);
    assert.equal(await sandbox.imageExists(), false);
  `);
});

test('native getters require a receiver of their declaring class', () => {
  inRuntime(`
    assert.equal(region.name, 'us-east-1');
    assert.equal(record.name, 'example');
    let checked = false;
    for (const [name, constructor] of Object.entries(addon)) {
      if (typeof constructor !== 'function' || !constructor.prototype) continue;
      for (const descriptor of Object.values(Object.getOwnPropertyDescriptors(constructor.prototype))) {
        if (!descriptor.get) continue;
        const receiver = name === 'Region' ? record : region;
        assert.throws(() => descriptor.get.call(receiver), classError(name));
        checked = true;
      }
    }
    assert.ok(checked, 'the addon exports native getters');
  `);
});
