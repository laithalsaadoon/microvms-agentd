// SPDX-License-Identifier: Apache-2.0
// Same bounded protocol as retry.py. Tokens enter on stdin; errors expose typed fields only.
import { createRequire } from 'node:module';
import { createInterface } from 'node:readline';

const { Region, Sandbox, retryDetails, isRetryable } = createRequire(import.meta.url)(process.argv[2]);
const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const emit = (event, fields) => process.stdout.write(`${JSON.stringify({ event, ...fields })}\n`);

async function metricsOf(session) {
  const value = await session.launchMetrics();
  if (value == null) return null;
  // napi omits absent optional object fields. The driver protocol uses explicit null
  // timestamps, like Python, so omission is normalized before JSON serialization.
  return {
    attemptCount: value.attemptCount,
    backoffSeconds: value.backoffSeconds,
    runningAt: value.runningAt ?? null,
    readyAt: value.readyAt ?? null,
  };
}

async function run(request) {
  const phases = [];
  for (const policy of request.policies) {
    const sandboxes = await Promise.all(Array.from({ length: policy.count }, () =>
      Sandbox.create(Region.parse(request.region), {
        maxAttempts: policy.maxAttempts,
        maxElapsedSeconds: request.budgetSeconds,
      })));
    const startedAt = Date.now() / 1000;
    const outcomes = await Promise.all(sandboxes.map(async (sandbox) => {
      try {
        const session = await sandbox.run({
          imageIdentifier: request.image,
          executionRoleArn: request.role,
          agentToken: request.agentToken,
          clientToken: request.clientToken,
          identity: false,
          wait: false,
          maxIdleSec: request.lifetimeSeconds,
          suspendedSec: request.lifetimeSeconds,
          maxDurationSec: request.lifetimeSeconds,
        });
        emit('accepted', { vmId: await sandbox.microvmId() });
        return { accepted: true, metrics: await metricsOf(session) };
      } catch (error) {
        return {
          accepted: false,
          code: error?.cause?.message ?? null,
          retryable: isRetryable(error),
          details: retryDetails(error),
        };
      }
    }));
    let ready = null;
    for (let index = 0; index < sandboxes.length; index += 1) {
      if (outcomes[index].accepted) {
        const session = await sandboxes[index].waitUntilRunning(request.runningTimeoutSeconds);
        ready = await metricsOf(session);
        break;
      }
    }
    phases.push({ outcomes, ready, startedAt, observedAt: Date.now() / 1000 });
  }
  emit('report', { phases });
}

try {
  const { value } = await lines.next();
  await run(JSON.parse(value));
} catch {
  emit('failed', { errorType: 'driver_failure' });
  process.exitCode = 1;
} finally {
  lines.return?.();
  process.stdin.destroy();
}
