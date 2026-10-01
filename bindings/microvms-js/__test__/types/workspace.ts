// SPDX-License-Identifier: Apache-2.0
//
// `downloadDir` and `syncDir`, typed the way their docs show (#260).
import type { DownloadedFile, Session, SyncReport } from '@theagenticguy/microvms'

export async function bringBack(session: Session, dir: string): Promise<string[]> {
  const written: DownloadedFile[] = await session.downloadDir('/workspace/dist', dir, ['**'])
  return written.map((file) => `${file.path} (${file.size} bytes)`)
}

export async function syncOnce(session: Session, dir: string): Promise<number> {
  const report: SyncReport = await session.syncDir(dir, { full: false, deleteTimeout: 60 })
  // @ts-expect-error: a count is a number, so this fails unless the report is `any`
  const wrong: string = report.uploadedMembers
  void wrong
  return report.uploadedMembers
}
