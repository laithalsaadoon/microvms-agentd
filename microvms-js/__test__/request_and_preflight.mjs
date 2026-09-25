// SPDX-License-Identifier: Apache-2.0
//
// `SizeClass.fromRequest` and `preflight` (#223; BIND-14, BIND-15, BIND-16).
//
// `fromRequest` is a pure selection over the documented table, so every boundary is testable
// here. `preflight` needs AWS for its passing path, which the live suite runs; a unit run asserts
// the report's shape and the path that makes no call at all: an environment region the client
// refuses stops the preflight at the region check.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SizeClass, preflight } from '../index.js';

test('BIND-14: the smallest class whose baseline covers both axes', () => {
  const rows = [
    [0.25, 512, 512],
    [1, 2048, 2048],
    [4, 8192, 8192],
    [undefined, 1024, 1024],
    [0.5, undefined, 1024],
    [2, 1024, 4096],
    [0.5, 3072, 4096],
    [0.25, 513, 1024],
  ];
  for (const [cpus, memoryMib, baseline] of rows) {
    assert.equal(SizeClass.fromRequest(cpus, memoryMib).baselineMib, baseline, `${cpus}/${memoryMib}`);
  }
});

test('BIND-14: a request that names nothing is the default class', () => {
  assert.equal(SizeClass.fromRequest().baselineMib, SizeClass.defaultClass().baselineMib);
  assert.equal(SizeClass.fromRequest(0, 0).baselineMib, SizeClass.defaultClass().baselineMib);
});

test('BIND-14: a request over the largest class names it', () => {
  const largest = SizeClass.all().at(-1);
  for (const [cpus, memoryMib] of [
    [4.5, undefined],
    [undefined, 8193],
  ]) {
    assert.throws(
      () => SizeClass.fromRequest(cpus, memoryMib),
      (error) => {
        assert.equal(error.code, 'ERR_INVALID_ARG', error.message);
        assert.match(error.message, /largest size class/);
        assert.ok(error.message.includes(largest.describe()), error.message);
        return true;
      },
    );
  }
});

test('BIND-15 and BIND-16: a refused environment region stops the preflight before any call', async () => {
  const saved = { region: process.env.AWS_REGION, fallback: process.env.AWS_DEFAULT_REGION };
  process.env.AWS_REGION = 'eu-central-1';
  delete process.env.AWS_DEFAULT_REGION;
  try {
    const report = await preflight();
    assert.equal(report.ok, false);
    assert.equal(report.region, undefined);
    assert.deepEqual(
      report.checks.map((check) => check.name),
      ['region', 'credentials', 'service'],
    );
    const [region, credentials, service] = report.checks;
    assert.equal(region.ok, false);
    assert.equal(region.ran, true);
    assert.match(region.detail, /eu-central-1/);
    for (const check of [credentials, service]) {
      assert.deepEqual([check.ok, check.fatal, check.ran], [false, true, false]);
    }
  } finally {
    for (const [key, value] of [
      ['AWS_REGION', saved.region],
      ['AWS_DEFAULT_REGION', saved.fallback],
    ]) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
