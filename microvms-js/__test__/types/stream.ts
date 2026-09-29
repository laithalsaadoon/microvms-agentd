// SPDX-License-Identifier: Apache-2.0
//
// `for await` over `ExecHandle.stream()`, the loop the method's own docs show (#262).
import type { ExecHandle, StreamEvent } from '@theagenticguy/microvms'

export async function drain(handle: ExecHandle): Promise<StreamEvent[]> {
  const seen: StreamEvent[] = []
  for await (const event of handle.stream()) seen.push(event)
  return seen
}

export async function firstKind(handle: ExecHandle): Promise<string> {
  for await (const event of handle.stream()) {
    // @ts-expect-error: an event is a StreamEvent, so this fails unless the stream yields `any`
    const wrong: number = event
    void wrong
    return event.kind
  }
  return 'none'
}
