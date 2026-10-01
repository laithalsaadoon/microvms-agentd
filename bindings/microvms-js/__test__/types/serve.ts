// SPDX-License-Identifier: Apache-2.0
//
// A verified tunnel from a launch's identity, and a port-forward's report (#263).
import type {
  PortForwardReport,
  Sandbox,
  Session,
  TunnelReport,
} from '@theagenticguy/microvms'

export async function verified(sandbox: Sandbox, session: Session): Promise<TunnelReport> {
  const identity = await sandbox.tunnelIdentity()
  const tunnel = await session.tunnel(5432, { maxConnections: 1 }, identity ?? undefined)
  const address: string = tunnel.localAddress
  void address
  return tunnel.stop(5)
}

export async function refusals(session: Session): Promise<number[]> {
  const forward = await session.portForward(3000, { bind: '127.0.0.1:0' })
  const report: PortForwardReport = await forward.stop()
  // @ts-expect-error: `ended` is a list of ConnectionEnd, so this fails unless it's `any`
  const wrong: string = report.ended
  void wrong
  return report.ended.flatMap((end) => (end.code == null ? [] : [end.code]))
}
