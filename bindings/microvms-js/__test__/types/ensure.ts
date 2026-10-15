// SPDX-License-Identifier: Apache-2.0
import type { EnsureImageOptions } from 'microvms'

declare const options: EnsureImageOptions
options.repairGuestIdentity = true
options.repairGuestIdentity = false
// @ts-expect-error Identity repair is a boolean option.
options.repairGuestIdentity = 'true'
