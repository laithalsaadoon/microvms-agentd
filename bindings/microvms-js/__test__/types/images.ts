// SPDX-License-Identifier: Apache-2.0
//
// Image administration on `ControlPlane` (#264): a rollback reads the version back, and the
// build listing hands `getImageBuild` its id.
import type { ControlPlane, ImageBuild, ImageVersion } from '@theagenticguy/microvms'

export async function retire(plane: ControlPlane, arn: string, version: string): Promise<boolean> {
  const updated: ImageVersion = await plane.setImageVersionStatus(arn, version, 'INACTIVE')
  // @ts-expect-error: `isActive` is a boolean, so this fails unless the readback is `any`
  const wrong: string = updated.isActive
  void wrong
  return updated.isActive
}

export async function sizes(plane: ControlPlane, arn: string, version: string): Promise<Array<number | null | undefined>> {
  const builds: ImageBuild[] = await plane.listImageBuilds(arn, version)
  const out: Array<number | null | undefined> = []
  for (const build of builds) {
    const read = await plane.getImageBuild(arn, version, build.buildId)
    out.push(read.memorySnapshotSizeInBytes)
  }
  const deleted: boolean = await plane.deleteImage(arn, { attempts: 1, backoff: 0 })
  void deleted
  return out
}
