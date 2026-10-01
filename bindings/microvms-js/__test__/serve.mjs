// SPDX-License-Identifier: Apache-2.0
//
// `session.tunnel(...)` and `session.portForward(...)` over a direct session, and the tunnel
// identity. A direct session needs no endpoint proxy, so a forward reaches a local HTTP server at
// the guest port, and a tunnel to an endpoint nothing listens on fails each connection it
// accepts. That is enough to assert the handle's contract as JavaScript sees it: it listens where
// it says, serves until stopped, keeps serving after a failed connection, lists each one that
// didn't end clean, and cuts what's left open when `stop(timeout)` runs out. The relay through a
// real daemon and the endpoint proxy is `crates/microvms-edges/tests/serve.rs`'s, and the live
// suite drives the core loops against AWS (`drive_serve`).

import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { connect } from 'node:net';
import { test } from 'node:test';
import { setTimeout as sleep } from 'node:timers/promises';

import { NameRecord, Region, Session, TunnelIdentity } from '../index.js';
import { codeOf } from './support/sse.mjs';

// Nothing listens on the discard port, so a direct session's upgrade there is refused at once.
const UNREACHABLE = 'http://127.0.0.1:9';
const HOST_SEED = Buffer.alloc(32, 7).toString('base64');
const VM_PUBLIC_KEY = Buffer.alloc(32, 9).toString('base64');

const direct = () => Session.direct(UNREACHABLE, 'serve-agent-token');

/** A guest HTTP server on loopback that answers every GET with its path. `/slow` resolves
 * `arrived` and holds its answer until `close()`, so a forwarded connection stays open. */
async function upstream() {
  const held = [];
  let arrive;
  const arrived = new Promise((resolve) => {
    arrive = resolve;
  });
  const server = createServer((req, res) => {
    if (req.url === '/slow') {
      held.push(res);
      arrive();
      return;
    }
    res.end(`guest saw ${req.url}`);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    port: server.address().port,
    arrived,
    close: () => {
      for (const res of held) res.end();
      server.closeAllConnections();
      return new Promise((resolve) => server.close(resolve));
    },
  };
}

function hostAndPort(localAddress) {
  const at = localAddress.lastIndexOf(':');
  return { host: localAddress.slice(0, at), port: Number(localAddress.slice(at + 1)) };
}

/** Opens a connection and waits for the handle to close it, so it was accepted and ended. */
function connectAndWaitForTheClose(localAddress) {
  return new Promise((resolve, reject) => {
    const socket = connect(hostAndPort(localAddress));
    socket.setTimeout(10_000, () => reject(new Error('the handle never closed it')));
    socket.on('error', () => {});
    socket.on('close', resolve);
    socket.resume();
  });
}

test('a port-forward serves a request and stop() resolves with its report', async () => {
  const guest = await upstream();
  try {
    const forward = await direct().portForward(guest.port);
    const { host, port } = hostAndPort(forward.localAddress);
    assert.equal(host, '127.0.0.1');
    assert.notEqual(port, 0);
    assert.equal(forward.running, true);

    const answer = await fetch(`http://${forward.localAddress}/through`, {
      headers: { connection: 'close' },
    });
    assert.equal(answer.status, 200);
    assert.equal(await answer.text(), 'guest saw /through');

    const report = await forward.stop();
    assert.deepEqual(
      [report.served, report.refused, report.upgrades, report.stopped],
      [1, 0, 0, 'stopped'],
    );
    assert.deepEqual(report.ended, []);
    assert.equal(report.proxyTokenMints, 0, 'a direct session mints no proxy token');
    assert.equal(forward.running, false);
    const again = await forward.stop();
    assert.equal(again.served, 1, 'stop() is repeatable');
  } finally {
    await guest.close();
  }
});

test('a tunnel lists each failed connection and keeps serving', async () => {
  const tunnel = await direct().tunnel(8080);
  await connectAndWaitForTheClose(tunnel.localAddress);
  await connectAndWaitForTheClose(tunnel.localAddress);
  assert.equal(tunnel.running, true, "a failed connection doesn't end the tunnel");

  const report = await tunnel.stop();
  assert.deepEqual([report.served, report.refused], [2, 2]);
  assert.deepEqual([report.truncated, report.unproven], [0, 0]);
  assert.deepEqual(
    report.ended.map((end) => end.kind),
    ['failed', 'failed'],
  );
  for (const end of report.ended) {
    assert.ok(end.peer.startsWith('127.0.0.1:'), end.peer);
    assert.ok(end.detail.length > 0);
    assert.equal(end.code ?? null, null);
  }
});

test('the limit stops the tunnel on its own', async () => {
  const tunnel = await direct().tunnel(8080, { maxConnections: 1 });
  await connectAndWaitForTheClose(tunnel.localAddress);
  for (let tries = 0; tunnel.running && tries < 500; tries += 1) {
    await sleep(20);
  }
  assert.equal(tunnel.running, false, 'the tunnel kept serving past its limit');
  const report = await tunnel.stop();
  assert.deepEqual([report.served, report.stopped], [1, 'limit']);
});

test('stop(timeout) cuts a connection left open', async () => {
  const guest = await upstream();
  try {
    const forward = await direct().portForward(guest.port);
    // The guest holding its answer proves the forward accepted the connection, which stays
    // open until it's cut.
    const kept = connect(hostAndPort(forward.localAddress));
    kept.on('error', () => {});
    kept.write('GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n');
    await guest.arrived;
    const report = await forward.stop(0.2);
    kept.destroy();
    assert.equal(report.served, 1);
    assert.deepEqual(
      report.ended.map((end) => end.kind),
      ['failed'],
    );
    assert.match(report.ended[0].detail, /cut/);
  } finally {
    await guest.close();
  }
});

test('a bad bind is refused before anything listens', async () => {
  await assert.rejects(direct().tunnel(8080, { bind: 'localhost' }), (error) => {
    assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
    assert.match(error.message, /"localhost"/);
    return true;
  });
  await assert.rejects(direct().portForward(70000), (error) => {
    assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
    return true;
  });
});

test('verifyIdentity takes a TunnelIdentity only', async () => {
  await assert.rejects(async () => direct().tunnel(8080, {}, HOST_SEED), /TunnelIdentity/);
});

test('a tunnel identity round-trips and keeps its seed out of toString', () => {
  const identity = new TunnelIdentity(HOST_SEED, VM_PUBLIC_KEY);
  assert.equal(identity.hostSeed(), HOST_SEED);
  assert.equal(identity.vmPublicKey, VM_PUBLIC_KEY);
  const shown = `${identity}`;
  assert.ok(shown.includes(VM_PUBLIC_KEY) && !shown.includes(HOST_SEED), shown);
  assert.ok(!JSON.stringify(identity).includes(HOST_SEED));
  assert.throws(
    () => new TunnelIdentity('not base64!', VM_PUBLIC_KEY),
    (error) => codeOf(error) === 'ERR_INVALID_ARG' && /base64/.test(error.message),
  );
  assert.throws(
    () => new TunnelIdentity(HOST_SEED, Buffer.from('short').toString('base64')),
    (error) => codeOf(error) === 'ERR_INVALID_ARG',
  );
});

test("a name record's tunnel identity is the pair or nothing", () => {
  const plain = new NameRecord('ci', 'microvm-a', UNREACHABLE, 'tok', Region.usEast1());
  assert.equal(plain.tunnelIdentity(), null);
  const stored = {
    ...plain.toObject(),
    identityHostSeed: HOST_SEED,
    identityVmPublicKey: VM_PUBLIC_KEY,
  };
  const record = NameRecord.fromObject(stored);
  assert.equal(record.tunnelIdentity().hostSeed(), HOST_SEED);
  assert.equal(record.tunnelIdentity().vmPublicKey, VM_PUBLIC_KEY);
  assert.ok(!`${record}`.includes(HOST_SEED));
  const torn = NameRecord.fromObject({ ...stored, identityVmPublicKey: undefined });
  assert.throws(
    () => torn.tunnelIdentity(),
    (error) => codeOf(error) === 'ERR_INVALID_ARG' && /one half/.test(error.message),
  );
});
