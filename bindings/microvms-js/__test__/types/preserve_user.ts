// SPDX-License-Identifier: Apache-2.0
import { wrapDockerfilePreservingUser } from 'microvms';
const recipe: string = wrapDockerfilePreservingUser('FROM x\n', { inheritWorkdir: false });
// @ts-expect-error: the task must be Dockerfile text
wrapDockerfilePreservingUser(123);
void recipe;
