// SPDX-License-Identifier: Apache-2.0
//
// `Session.runToCompletion` and `ExecResult.posixExitCode` / `notes` (BIND-6..10, #222).
//
// The composition is `microvms-core`'s (`src/session/complete.rs`), checked there by a Stateright
// model, Gherkin scenarios, a fuzz harness, and unit tests. What is asserted here is the binding's
// half: the options object, the callback reaching JS with output events in order, the new result
// fields, a throwing callback, and the deadline paths as the binding reaches them. The server is a
// loopback transcription of the five exec routes' shapes, not `agentd`. The cut-stream fallback is
// not repeated: with the core's default reconnect budget it takes about a minute of real time, and
// the core tiers drive it under a paused clock.

import assert from 'node:assert/strict';
import http from 'node:http';
import { test } from 'node:test';

import { Session } from '../index.js';
import { codeOf, outputFrame } from './support/sse.mjs';

const RUNNING = JSON.stringify({ exec_id: 'x-rtc', phase: 'running' });

function exitEvent(total, { exitCode = 0, signal = null, timedOut = false } = {}) {
  const body = JSON.stringify({
    exit_code: exitCode,
    signal,
    timed_out: timedOut,
    truncated: false,
    writers_may_be_alive: false,
    offset: total,
  });
  return `event: exit\ndata: ${body}\n\n`;
}

function outcome(phase, exitCode, { signal = null, stdout = '', timedOut = false } = {}) {
  return JSON.stringify({
    exec_id: 'x-rtc',
    phase,
    exit_code: exitCode,
    signal,
    timed_out: timedOut,
    stdout,
    stderr: '',
    truncated: false,
    writers_may_be_alive: false,
  });
}

/** A loopback server answering `route(method, path)` for every request, logging each. */
async function serve(route) {
  const log = [];
  const server = http.createServer((request, response) => {
    request.resume();
    request.on('end', () => {
      const path = request.url.split('?')[0];
      log.push(`${request.method} ${path}`);
      const [status, body, type] = route(request.method, path);
      response.writeHead(status, {
        'content-type': type,
        'content-length': Buffer.byteLength(body),
      });
      response.end(body);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    log,
    session: Session.direct(`http://127.0.0.1:${port}`, 'agent-token'),
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

const json = (body) => [200, body, 'application/json'];

function streamed(frames, ack) {
  return (method, path) => {
    if (path === '/v1/exec/start') return json(RUNNING);
    if (path.endsWith('/stream')) return [200, frames.join(''), 'text/event-stream'];
    if (path.endsWith('/ack')) return json(ack);
    return json(ack.replace('"acked"', '"exited"'));
  };
}

test('BIND-8: chunks reach onOutput in order and one ack is the result', async (t) => {
  const server = await serve(
    streamed(
      [outputFrame(0, 'ab'), outputFrame(2, 'cd'), exitEvent(4)],
      outcome('acked', 0, { stdout: 'abcd' }),
    ),
  );
  t.after(server.close);
  const chunks = [];
  const result = await server.session.runToCompletion(
    ['bash', '-c', 'printf abcd'],
    { execId: 'x-rtc' },
    (chunk) => {
      chunks.push(chunk);
    },
  );
  assert.deepEqual(
    chunks.map((chunk) => [chunk.kind, chunk.offset, chunk.text]),
    [
      ['output', 0, 'ab'],
      ['output', 2, 'cd'],
    ],
  );
  assert.equal(result.stdout, 'abcd');
  assert.equal(result.posixExitCode, 0);
  assert.deepEqual(result.notes, []);
  assert.equal(result.synthesized, false);
  assert.deepEqual(server.log, [
    'POST /v1/exec/start',
    'GET /v1/exec/x-rtc/stream',
    'POST /v1/exec/x-rtc/ack',
  ]);
});

test('BIND-8: without a callback the call waits and acks', async (t) => {
  const server = await serve(streamed([], outcome('acked', 3, { stdout: 'x' })));
  t.after(server.close);
  const result = await server.session.runToCompletion('true', { execId: 'x-rtc' });
  assert.equal(result.posixExitCode, 3);
  assert.ok(!server.log.some((line) => line.includes('stream')), server.log.join());
});

test('BIND-8: a throwing onOutput still gets the exec acked, then rejects', async (t) => {
  const server = await serve(
    streamed(
      [outputFrame(0, 'a'), outputFrame(1, 'b'), exitEvent(2)],
      outcome('acked', 0, { stdout: 'ab' }),
    ),
  );
  t.after(server.close);
  const seen = [];
  await assert.rejects(
    server.session.runToCompletion('true', { execId: 'x-rtc' }, (chunk) => {
      seen.push(chunk.text);
      throw new Error('the harness callback failed');
    }),
    (error) => {
      assert.match(error.message, /the harness callback failed/);
      assert.match(error.message, /waited for and acked/);
      assert.equal(codeOf(error), 'ERR_UNEXPECTED');
      return true;
    },
  );
  assert.deepEqual(seen, ['a'], 'delivery must stop at the first throw');
  assert.equal(server.log.at(-1), 'POST /v1/exec/x-rtc/ack', server.log.join());
});

test('BIND-6/BIND-7: a daemon deadline maps to 124 with a note', async (t) => {
  const server = await serve(
    streamed(
      [outputFrame(0, 't'), exitEvent(1, { exitCode: null, signal: 15, timedOut: true })],
      outcome('acked', null, { signal: 15, stdout: 't', timedOut: true }),
    ),
  );
  t.after(server.close);
  const result = await server.session.runToCompletion(
    'sleep 60',
    { timeoutSec: 1, execId: 'x-rtc' },
    () => {},
  );
  assert.equal(result.exitCode ?? null, null, "a signal death has no exit code");
  assert.equal(result.posixExitCode, 124);
  assert.equal(result.ok, false);
  assert.ok(result.notes.some((note) => note.includes('timeout_sec')), result.notes.join());
});

test('BIND-6: a signal death with no deadline maps to 128 plus the signal', async (t) => {
  const server = await serve(streamed([], outcome('acked', null, { signal: 9 })));
  t.after(server.close);
  const result = await server.session.runToCompletion('oom', { execId: 'x-rtc' });
  assert.equal(result.posixExitCode, 137);
});

function deadlineRoute({ killOk, dies }) {
  let killed = false;
  return (method, path) => {
    if (path === '/v1/exec/start') return json(RUNNING);
    if (path.endsWith('/kill')) {
      if (!killOk) {
        return [500, JSON.stringify({ error: 'internal', detail: 'sim kill' }), 'application/json'];
      }
      killed = true;
      return json(JSON.stringify({ exec_id: 'x-rtc', killed: true }));
    }
    if (killed && dies) {
      const phase = path.endsWith('/ack') ? 'acked' : 'exited';
      return json(outcome(phase, null, { signal: 15, stdout: 'partial' }));
    }
    return json(RUNNING);
  };
}

test('BIND-9: past the client deadline the group is killed, then acked', async (t) => {
  const server = await serve(deadlineRoute({ killOk: true, dies: true }));
  t.after(server.close);
  const result = await server.session.runToCompletion('sleep 60', {
    timeoutSec: 0.2,
    clientGraceSec: 0.3,
    execId: 'x-rtc',
  });
  const kill = server.log.indexOf('POST /v1/exec/x-rtc/kill');
  assert.ok(kill > 0, server.log.join());
  assert.equal(server.log.at(-1), 'POST /v1/exec/x-rtc/ack');
  assert.equal(result.synthesized, false);
  assert.equal(result.stdout, 'partial');
  assert.equal(result.posixExitCode, 124);
  assert.ok(result.notes.some((note) => note.includes('client deadline')), result.notes.join());
});

test('BIND-10: a failed kill and a failed ack synthesize 124', async (t) => {
  const server = await serve(deadlineRoute({ killOk: false, dies: false }));
  t.after(server.close);
  const result = await server.session.runToCompletion('sleep 60', {
    timeoutSec: 0.2,
    clientGraceSec: 0.3,
    execId: 'x-rtc',
  });
  assert.ok(server.log.includes('POST /v1/exec/x-rtc/kill'), server.log.join());
  assert.equal(result.synthesized, true);
  assert.equal(result.posixExitCode, 124);
  assert.equal(result.phase, 'running');
  assert.ok(result.notes.some((note) => note.includes('synthesized')), result.notes.join());
});

test('a bad clientGraceSec or timeoutSec is refused before anything starts', async (t) => {
  const server = await serve(streamed([], outcome('acked', 0)));
  t.after(server.close);
  for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
    await assert.rejects(server.session.runToCompletion('true', { clientGraceSec: bad }), (error) => {
      assert.equal(codeOf(error), 'ERR_INVALID_ARG');
      return true;
    });
    await assert.rejects(server.session.runToCompletion('true', { timeoutSec: bad }), (error) => {
      assert.equal(codeOf(error), 'ERR_INVALID_ARG');
      return true;
    });
  }
  assert.deepEqual(server.log, [], 'a refused call started an exec');
});
