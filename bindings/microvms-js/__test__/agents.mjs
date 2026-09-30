// SPDX-License-Identifier: Apache-2.0
// The L3 surface, offline: every refusal is the core's and the token has one door.
//
// Nothing here talks to AWS. `AgentVm.create` resolves a credential chain and
// `mintBedrockToken` signs with one, so both stay out of a unit run.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  AgentVm,
  BearerToken,
  agentConstants,
  installAgentAccess,
  installedAgents,
  mintBedrockToken,
  mintBedrockTokenWithCredentials,
  promptAgent,
  Session,
  Region,
} from '../index.js';

test('the constants name the guest contract and both profiles', () => {
  const constants = JSON.parse(agentConstants());
  assert.equal(constants.uid, 1000);
  assert.equal(constants.workdir, '/workspace');
  assert.equal(constants.envFile, '/workspace/.agent-env');
  assert.equal(constants.markerFile, '/workspace/.agent-vm.json');
  assert.equal(constants.defaultPromptTimeoutSec, 900);
  assert.equal(constants.maxTokenLifetimeSec, 43200);
  assert.deepEqual(Object.keys(constants.profiles).sort(), ['claude-code', 'codex']);
  assert.equal(constants.profiles['codex'].defaultModel, 'global.openai.gpt-5.6-sol');
  assert.equal(constants.profiles['claude-code'].defaultModel, 'global.anthropic.claude-opus-5');
});

test('a bearer token has no constructor, so it only comes from minting', () => {
  assert.throws(() => new BearerToken());
  assert.equal(typeof mintBedrockToken, 'function');
});

test('an unknown agent is the core refusal, carried on err.cause.message', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  await assert.rejects(
    promptAgent(session, { agent: 'cursor' }, 'anything'),
    (err) => err.cause.message === 'ERR_INVALID_ARG' && /claude-code/.test(err.message),
  );
});

test('a blank task is refused before any call', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  await assert.rejects(
    promptAgent(session, { agent: 'codex' }, '   '),
    (err) => err.cause.message === 'ERR_INVALID_ARG',
  );
});

test('nothing but a minted BearerToken is accepted where a token is wanted', () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  // `null` and a look-alike object are both rejected by napi's argument conversion, which
  // runs synchronously before any Rust does: the class is nominal, which is the closure a
  // secret-carrying type needs. A synchronous throw, not a rejection, is the measured shape.
  for (const impostor of [null, { expose: () => 'bedrock-api-key-x' }]) {
    assert.throws(
      () => installAgentAccess(session, [{ agent: 'codex' }], impostor),
      (err) => err.code === 'InvalidArg' && /BearerToken/.test(err.message),
    );
  }
});

test('the exported surface is complete', () => {
  for (const item of [AgentVm, installedAgents, installAgentAccess, promptAgent, mintBedrockToken]) {
    assert.equal(typeof item, 'function');
  }
});


test('permission modes and remote deadlines are refused before any request', async () => {
  const session = Session.direct('http://127.0.0.1:9', 'token');
  for (const options of [
    { permissionMode: 'bad' }, { permissionMode: 'UNRESTRICTED' },
    { timeoutSec: 0 }, { timeoutSec: -1 }, { timeoutSec: NaN }, { timeoutSec: Infinity },
  ]) {
    await assert.rejects(
      promptAgent(session, { agent: 'codex' }, 'hello', options),
      (err) => err.cause.message === 'ERR_INVALID_ARG',
    );
  }
});

test('both agents and permission modes reach the wire with the remote deadline', async () => {
  const { createServer } = await import('node:http');
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const request = JSON.parse(body);
    requests.push(request);
    res.end(JSON.stringify({ exec_id: request.exec_id, phase: 'running' }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = Session.direct(`http://127.0.0.1:${server.address().port}`, 'token');
    for (const agent of ['claude-code', 'codex']) {
      for (const permissionMode of ['agent-default', 'unrestricted']) {
        const handle = await promptAgent(session, { agent }, "it's 'quoted'; $(false)", {
          permissionMode, timeoutSec: 17, execId: `${agent}-${permissionMode}`,
          reapGroupOnExit: true,
        });
        const request = requests.at(-1);
        assert.equal(handle.execId, request.exec_id);
        assert.equal(request.timeout_sec, 17);
        assert.equal(request.user, 1000);
        assert.equal(request.group, 1000);
        assert.equal(request.reap_group_on_exit, true);
        const command = request.command[0];
        assert.ok(command.includes("'it'\\''s "));
        if (permissionMode === 'unrestricted') {
          assert.match(command, /--dangerously-/);
          assert.doesNotMatch(command, /workspace-write|--allowedTools/);
        } else if (agent === 'codex') {
          assert.match(command, /-s workspace-write/);
        } else {
          assert.match(command, /--allowedTools Bash,Read,Edit,Write,Grep,Glob/);
        }
      }
    }
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});


test('explicit STS credentials cap token expiry without mutating environment', () => {
  const env = { ...process.env };
  const expiry = Math.floor(Date.now() / 1000) + 600;
  const token = mintBedrockTokenWithCredentials(Region.usEast1(), 'synthetic-access-id',
    'synthetic-secret', 'synthetic-session', expiry, 1200);
  assert.equal(token.expiresAt, expiry);
  assert.match(token.expose(), /^bedrock-api-key-/);
  assert.doesNotMatch(token.toString(), /synthetic/);
  assert.ok(Object.keys(process.env).length === Object.keys(env).length
    && Object.entries(env).every(([key, value]) => process.env[key] === value),
  "minting must not change the process environment");
  assert.throws(() => mintBedrockTokenWithCredentials(Region.usEast1(), 'id', 'secret',
    undefined, 1, 1200));
});
