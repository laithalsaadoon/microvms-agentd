// SPDX-License-Identifier: Apache-2.0
import { Session, ExecOptions, CompletionRequest } from 'microvms';

const options: ExecOptions = { poweroffOnExit: true };
const completion: CompletionRequest = { poweroffOnExit: false };
declare const session: Session;
void session.run('true', options);
void session.spawn('true', { exec: options });
void session.runSync('true', options);
void session.runToCompletion('true', completion);
// @ts-expect-error The opt-in is a boolean, never a truthy string.
const invalid: ExecOptions = { poweroffOnExit: 'true' };
void invalid;
