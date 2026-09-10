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
  promptAgent,
  Session,
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
