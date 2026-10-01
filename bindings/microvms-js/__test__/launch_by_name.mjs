// SPDX-License-Identifier: Apache-2.0
// `sandbox.run` by bare image name reaches the core's resolver (#253).
//
// The core resolves a name to its ARN inside its one `run`, before `RunMicrovm`. A unit run has
// no control plane to answer the listing, so what is asserted here is which call a launch makes
// first: with a credential chain that finds nothing, the core refuses the first signed call and
// names it. The listing and the ARN it answers are asserted in Rust
// (`crates/microvms-app/src/sandbox.rs`), and the live suite launches by name against AWS.

import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { Region, Sandbox } from '../index.js';
import { codeOf } from './support/sse.mjs';

// A default chain that resolves nothing, and makes no network call finding that out: the
// shared config files point at paths that don't exist and the instance metadata lookup is off.
// The proxy on a port nothing listens on is the parity runner's: had a chain resolved anyway,
// the call would fail to connect rather than reach AWS. `node --test` runs each file in its own
// process, so this environment is this file's alone.
for (const name of [
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'AWS_PROFILE',
  'AWS_DEFAULT_PROFILE',
  'AWS_WEB_IDENTITY_TOKEN_FILE',
  'AWS_ROLE_ARN',
  'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
  'AWS_CONTAINER_CREDENTIALS_FULL_URI',
  'https_proxy',
  'no_proxy',
  'HTTP_PROXY',
  'http_proxy',
  'ALL_PROXY',
  'all_proxy',
]) {
  delete process.env[name];
}
const empty = mkdtempSync(join(tmpdir(), 'microvms-no-credentials-'));
process.env.AWS_CONFIG_FILE = join(empty, 'config');
process.env.AWS_SHARED_CREDENTIALS_FILE = join(empty, 'credentials');
process.env.AWS_EC2_METADATA_DISABLED = 'true';
process.env.HTTPS_PROXY = 'http://127.0.0.1:9';
process.env.NO_PROXY = '127.0.0.1,localhost';

/** The refusal the launch's first signed call meets. */
async function firstSignedCall(imageIdentifier) {
  const sandbox = await Sandbox.create(Region.usEast1());
  let refusal;
  await assert.rejects(sandbox.run({ imageIdentifier }), (error) => {
    assert.equal(codeOf(error), 'ERR_CREDENTIALS', error.message);
    refusal = error.message;
    return true;
  });
  return refusal;
}

test('a bare image name is listed before any launch', async () => {
  const message = await firstSignedCall('wanted-image');
  assert.match(message, /for ListMicrovmImages/);
});

test('an image ARN goes straight to the launch', async () => {
  const message = await firstSignedCall(
    'arn:aws:lambda:us-east-1:123456789012:microvm-image:wanted-image',
  );
  assert.match(message, /for RunMicrovm/);
});

test('identity on run reaches the launch request', async () => {
  // #263: the core refuses a stable client token with an identity, locally, and the same launch
  // without one reaches its first call.
  const stable = {
    imageIdentifier: 'arn:aws:lambda:us-east-1:123456789012:microvm-image:wanted-image',
    clientToken: 'ct-1',
    agentToken: 'tok',
  };
  const sandbox = await Sandbox.create(Region.usEast1());
  await assert.rejects(sandbox.run({ ...stable, identity: true }), (error) => {
    assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
    assert.match(error.message, /identity=false/);
    return true;
  });
  const plain = await Sandbox.create(Region.usEast1());
  await assert.rejects(plain.run({ ...stable, identity: false }), (error) => {
    assert.equal(codeOf(error), 'ERR_CREDENTIALS', error.message);
    assert.match(error.message, /for RunMicrovm/);
    return true;
  });
  assert.equal(await plain.tunnelIdentity(), null);
});
