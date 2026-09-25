// SPDX-License-Identifier: Apache-2.0
//
// The egress posture a harness reads before and after a launch (#227; BIND-11, BIND-12, BIND-13).
//
// `egressPostureFor` is the request-side answer: what `sandbox.run` with the same options will
// report, or the `ERR_INVALID_ARG` it will throw, with no AWS call. `session.egressPosture()` is
// the launched session's value, the same string the CLI envelope's `egressPosture` carries. The
// launch half is asserted in Rust (`microvms-cli/src/guards.rs`) and against AWS in the live
// suite, because a unit run here has no control plane to launch against.
//
// `sealed` needs a VPC egress connector and separately verified VPC routing without an internet
// gateway or NAT gateway. No launch option carries that audit, so nothing here answers `sealed`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Region, Session, egressPostureFor } from '../index.js';

const VPC = 'arn:aws:lambda:us-east-1:123456789012:network-connector:isolated-vpc';

test('BIND-11: the request-side answer is the decision table', () => {
  const rows = [
    [false, [], false, 'unsealed'],
    [false, [], true, 'best-effort'],
    [false, [VPC], false, 'unsealed'],
    [false, [VPC], true, 'best-effort'],
    [true, [], false, 'open'],
  ];
  for (const [egress, connectors, denyEgress, posture] of rows) {
    assert.equal(egressPostureFor(egress, connectors, denyEgress), posture);
    assert.equal(egressPostureFor(egress, connectors, denyEgress, Region.usEast1()), posture);
  }
});

test('BIND-11: the defaults are a default launch, and it is not sealed', () => {
  assert.equal(egressPostureFor(), 'unsealed');
  assert.notEqual(egressPostureFor(false, [], false), 'sealed');
});

test('BIND-13: options the launch refuses throw its refusal', () => {
  const rows = [
    [true, [], true, /opposite things/],
    [true, [VPC], false, /INTERNET_EGRESS cannot be combined/],
    [false, ['not-an-arn'], false, /network connector ARN/],
    [false, Array(11).fill(VPC), false, /NetworkConnectorList ceiling/],
  ];
  for (const [egress, connectors, denyEgress, reason] of rows) {
    assert.throws(
      () => egressPostureFor(egress, connectors, denyEgress),
      (error) => {
        assert.equal(error.code, 'ERR_INVALID_ARG', error.message);
        assert.match(error.message, reason);
        return true;
      },
    );
  }
});

test('BIND-13: a connector is checked against the launch region when one is given', () => {
  const elsewhere = 'arn:aws:lambda:eu-west-1:123456789012:network-connector:vpc';
  assert.equal(egressPostureFor(false, [elsewhere], false), 'unsealed');
  assert.throws(
    () => egressPostureFor(false, [elsewhere], false, Region.usEast1()),
    /in us-east-1/,
  );
});

test('BIND-12: a session without its launch options reports unsealed', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'agent-token');
  assert.equal(await session.egressPosture(), 'unsealed');
});
