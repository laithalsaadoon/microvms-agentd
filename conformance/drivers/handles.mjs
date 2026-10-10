// SPDX-License-Identifier: Apache-2.0
//
// The Node binding's tunnel, port-forward and import handles, driven for the live suite: the
// same JSON-lines protocol as `conformance/drivers/handles.py`, whose docstring has it. The one
// argument is the path of the built addon's `index.js`. Everything here goes through the
// binding's public API alone, and nothing printed carries the agent token.

import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';

const binding = createRequire(import.meta.url)(process.argv[2]);
const { NameRecord, NameRegistry, Region, Session } = binding;

const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();

/** The next line on stdin, or `null` once the pipe closed. */
async function nextLine() {
  const { value, done } = await lines.next();
  return done ? null : value;
}

function emit(event, fields) {
  process.stdout.write(`${JSON.stringify({ event, ...fields })}\n`);
}

/** The binding's error code: the cause's message, as `__test__/support/sse.mjs` reads it. */
function codeOf(error) {
  return error?.cause?.message ?? null;
}

async function sessionFrom(spec) {
  if (spec.attach) {
    const { region, microvmId, endpoint, agentToken } = spec.attach;
    return Session.attach(Region.parse(region), microvmId, endpoint, agentToken);
  }
  return Session.direct(spec.direct.endpoint, spec.direct.agentToken);
}

async function serve(request) {
  const session = await sessionFrom(request.session);
  const options = { maxConnections: request.maxConnections };
  const handle =
    request.kind === 'tunnel'
      ? await session.tunnel(request.guestPort, options)
      : await session.portForward(request.guestPort, options);
  emit('listening', { localAddress: handle.localAddress, running: handle.running });
  // The suite's request goes through the handle while this waits; its `stop` line, or the
  // pipe closing, ends the wait.
  await nextLine();
  const report = await handle.stop(request.stopTimeout);
  const ended = report.ended.map(({ peer, kind, code, detail }) => ({
    peer,
    kind,
    code: code ?? null,
    detail,
  }));
  emit('stopped', { report: { ...report, ended }, running: handle.running });
}

async function importRecord(request) {
  const session = await sessionFrom(request.session);
  const wanted = request.record;
  const record = new NameRecord(
    wanted.name,
    wanted.microvmId,
    wanted.endpoint,
    wanted.agentToken,
    Region.parse(wanted.region),
  );
  const registry = new NameRegistry(request.stateDir);
  const replaced = [];
  let error = null;
  try {
    for (let time = 0; time < request.times; time += 1) {
      replaced.push(await registry.importRecord(record, session));
    }
  } catch (failure) {
    error = { code: codeOf(failure), message: String(failure.message).slice(0, 300) };
  }
  const found = registry.get(wanted.name);
  emit('imported', {
    replaced,
    error,
    found:
      found === null
        ? null
        : {
            name: found.name,
            microvmId: found.microvmId,
            endpoint: found.endpoint,
            region: found.region,
            tokenMatches: found.agentToken() === wanted.agentToken,
          },
    listed: registry.list().map((listed) => listed.name),
  });
}

async function main() {
  const request = JSON.parse(await nextLine());
  try {
    if (request.op === 'serve') {
      await serve(request);
    } else if (request.op === 'import') {
      await importRecord(request);
    } else {
      emit('error', { code: null, message: `unknown op ${JSON.stringify(request.op)}` });
      return 2;
    }
  } catch (failure) {
    emit('error', { code: codeOf(failure), message: String(failure.message).slice(0, 300) });
    return 1;
  }
  return 0;
}

process.exitCode = await main();
// The readline interface keeps stdin open, which would keep the process alive.
lines.return?.();
process.stdin.destroy();
