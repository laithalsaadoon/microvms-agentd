// SPDX-License-Identifier: Apache-2.0
import { ControlPlane, Region, Sandbox, retryDetails } from 'microvms'
import type { LaunchMetrics, RetryOptions, Session } from 'microvms'
const options: RetryOptions = { maxAttempts: 60, maxElapsedSeconds: 120 }
void ControlPlane.create(Region.usEast1(), options)
void Sandbox.create(Region.usEast1(), options)
declare const session: Session
void session.launchMetrics().then((metrics: LaunchMetrics | null) => metrics?.runningAt)
const detail = retryDetails(new Error('failure'))
const throttled: boolean | undefined = detail?.throttled
void throttled
// @ts-expect-error Retry attempts are numeric.
options.maxAttempts = '60'
// @ts-expect-error A timestamp is numeric when observed.
const timestamp: string = ({} as LaunchMetrics).runningAt
