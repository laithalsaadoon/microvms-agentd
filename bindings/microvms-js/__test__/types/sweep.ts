// SPDX-License-Identifier: Apache-2.0
import type { ControlPlane, MicrovmSummary, SweepReport } from 'microvms'

declare const plane: ControlPlane
const report: Promise<SweepReport> = plane.sweep('job-', { keep: ['job-current'], dryRun: true })
const timestamps: Promise<Array<number | null | undefined>> = plane.list().then(rows => rows.map((row: MicrovmSummary) => row.startedAt))
void report
void timestamps
// @ts-expect-error a keep entry is an image name or ARN
plane.sweep('job-', { keep: [123] })

type Same<A, B> = (<T>() => T extends A ? 1 : 2) extends
  (<T>() => T extends B ? 1 : 2) ? true : false
export const sweepReturn: Same<ReturnType<ControlPlane['sweep']>, Promise<SweepReport>> = true
