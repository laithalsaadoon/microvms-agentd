// SPDX-License-Identifier: Apache-2.0
//
// `downloadFile` with a line range and without one (#265).
import type { Session } from '@theagenticguy/microvms'

export async function window(session: Session): Promise<Buffer> {
  return session.downloadFile('/var/log/app.log', { startLine: 40, endLine: 60 })
}

export async function whole(session: Session): Promise<Buffer> {
  // @ts-expect-error: a line bound is a number, so this fails unless the options are `any`
  await session.downloadFile('/tmp/f', { startLine: '40' })
  return session.downloadFile('/tmp/f')
}
