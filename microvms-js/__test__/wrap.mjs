// SPDX-License-Identifier: Apache-2.0
// `wrapDockerfile` and `baseImageFromDockerfile` (IMAGE-5): thin, and core's refusals intact.
//
// Pure functions over Dockerfile text; no credentials and no AWS call. The stanza, the guards,
// and their messages are core's (IMAGE-1 through IMAGE-4); this file checks that the binding
// passes the options through and throws core's refusals as `ERR_INVALID_ARG`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { baseImageFromDockerfile, defaultBaseImage, wrapDockerfile } from '../index.js';
import { codeOf } from './support/sse.mjs';

test('a bare FROM wraps to the default stanza', () => {
  const wrapped = wrapDockerfile('FROM x\n');
  assert.ok(wrapped.startsWith('FROM x\nCOPY agentd /agentd\nRUN chmod 0755 /agentd\n'), wrapped);
  assert.ok(
    wrapped.endsWith(
      'ENV AGENTD_PORT=9000\nENV AGENTD_LOG=info\nEXPOSE 9000\nENTRYPOINT []\nCMD ["/agentd"]\n',
    ),
    wrapped,
  );
  assert.ok(!wrapped.includes('USER'), wrapped);
});

test('the options reach the stanza', () => {
  const wrapped = wrapDockerfile('FROM python:3.12-slim\nUSER app', {
    port: 8080,
    workdir: '/srv/task',
  });
  const added = wrapped.slice('FROM python:3.12-slim\nUSER app\n'.length);
  assert.ok(added.startsWith('USER root\nCOPY agentd /agentd\n'), added);
  assert.ok(added.includes('RUN mkdir -p /srv/task\nWORKDIR /srv/task\n'), added);
  assert.ok(added.includes('ENV AGENTD_PORT=8080\n'), added);
});

for (const [task, options, cause] of [
  ['RUN echo hello\n', undefined, /no FROM/],
  ['FROM x\nRUN make \\\n', undefined, /line continuation/],
  ['FROM x\nRUN <<EOF\necho open\n', undefined, /heredoc/],
  ['FROM x\nENV AGENTD_SSE_KEEPALIVE_SECS=90\n', undefined, /AGENTD_SSE_KEEPALIVE_SECS/],
  ['FROM x\n', { workdir: 'relative' }, /absolute path/],
  ['FROM x\n', { port: 0 }, /port/],
  ['FROM x\n', { inheritWorkdir: true }, /nothing to inherit/],
]) {
  test(`core refuses ${JSON.stringify(task)} ${JSON.stringify(options)} as ERR_INVALID_ARG`, () => {
    assert.throws(
      () => wrapDockerfile(task, options),
      (error) => {
        assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
        assert.match(error.message, cause);
        return true;
      },
    );
  });
}

test('a derived base keeps the managed name and takes the FROM', () => {
  const digest = 'c439fb4994ea7ca529233d6256446d3f8b7b4efb58956073e015303a170011de';
  const base = baseImageFromDockerfile(wrapDockerfile(`FROM python:3.12-slim@sha256:${digest}\n`));
  assert.equal(base.name, defaultBaseImage().name);
  assert.equal(base.dockerRef, `python:3.12-slim@sha256:${digest}`);
  assert.equal(base.workingDir, '');
});

test('a Dockerfile with no FROM has no base', () => {
  assert.throws(
    () => baseImageFromDockerfile('RUN echo hello\n'),
    (error) => {
      assert.equal(codeOf(error), 'ERR_INVALID_ARG', error.message);
      assert.match(error.message, /no FROM/);
      return true;
    },
  );
});
