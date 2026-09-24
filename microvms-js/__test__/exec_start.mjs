// SPDX-License-Identifier: Apache-2.0
//
// `run` passes `user`, `group`, `shell` and `inheritImageEnv` through unchanged (AGENTD-7,
// AGENTD-14, AGENTD-11 on the client side). A local HTTP server stands in for the daemon and
// records the start body; it answers `400 unknown_user` for one name, as the daemon does
// (AGENTD-8).

import assert from 'node:assert/strict';
import http from 'node:http';
import { test } from 'node:test';

import { Session } from '../index.js';

async function withDaemon(fn) {
  const bodies = [];
  const server = http.createServer((request, response) => {
    let raw = '';
    request.on('data', (chunk) => {
      raw += chunk;
    });
    request.on('end', () => {
      if (request.method === 'GET') {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(
          JSON.stringify({
            version: 'test',
            bootstrapped: true,
            disk: null,
            identity_degraded: false,
            identity_repaired: true,
            image_env_keys: 4,
          }),
        );
        return;
      }
      const body = JSON.parse(raw);
      bodies.push(body);
      if (body.user === 'ghost') {
        response.writeHead(400, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: 'unknown_user', detail: 'user "ghost"' }));
        return;
      }
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ exec_id: body.exec_id, phase: 'running' }));
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const { port } = server.address();
    await fn(Session.direct(`http://127.0.0.1:${port}`, 'token'), bodies);
  } finally {
    server.close();
  }
}

test('AGENTD-7 AGENTD-14 AGENTD-11: names and a named shell reach the wire as strings', async () => {
  await withDaemon(async (session, bodies) => {
    await session.run('set -o pipefail; false | true', {
      shell: 'bash',
      user: 'agent',
      group: 'staff',
      inheritImageEnv: true,
    });
    const [body] = bodies;
    assert.equal(body.shell, 'bash');
    assert.equal(body.user, 'agent');
    assert.equal(body.group, 'staff');
    assert.equal(body.inherit_image_env, true);
  });
});

test('AGENTD-16: numbers and booleans reach the wire as they always did', async () => {
  await withDaemon(async (session, bodies) => {
    await session.run('id -u', { shell: true, user: 1000, group: 1000 });
    await session.run(['/usr/bin/env']);
    const [first, second] = bodies;
    assert.deepEqual([first.shell, first.user, first.group], [true, 1000, 1000]);
    assert.equal(first.inherit_image_env, false);
    assert.equal(second.shell, false);
    assert.equal(second.user, null);
  });
});

test('AGENTD-8: an unknown user surfaces as an error naming the slug', async () => {
  await withDaemon(async (session) => {
    await assert.rejects(
      session.run('true', { shell: true, user: 'ghost' }),
      (error) => JSON.stringify([error.message, error.cause?.message]).includes('unknown_user'),
    );
  });
});

test('a fractional or negative uid is refused before anything is sent', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  await assert.rejects(session.run('true', { user: 1.5 }));
  await assert.rejects(session.run('true', { user: -1 }));
});

test('AGENTD-13: health reports the image env count', async () => {
  await withDaemon(async (session) => {
    const health = await session.health();
    assert.equal(health.imageEnvKeys, 4);
  });
});
