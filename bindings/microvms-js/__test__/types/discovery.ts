// SPDX-License-Identifier: Apache-2.0
import { Session, Discovery } from 'microvms';
async function probe(session: Session) {
  const discovery: Discovery | null = await session.discovery();
  if (discovery) {
    const user: string | undefined = discovery.defaultUser;
    // @ts-expect-error: the configured user is a string, not a numeric uid
    const numeric: number = discovery.defaultUser;
    void user; void numeric;
  }
}
void probe;
