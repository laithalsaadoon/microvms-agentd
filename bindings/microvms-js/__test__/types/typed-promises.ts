// SPDX-License-Identifier: Apache-2.0
import type { AgentVm, ControlPlane, Sandbox } from "microvms";

type Same<A, B> = (<T>() => T extends A ? 1 : 2) extends
  (<T>() => T extends B ? 1 : 2) ? true : false;

export const imageReturn: Same<ReturnType<AgentVm["findImage"]>, Promise<string | null>> = true;
export const suspendReturn: Same<ReturnType<ControlPlane["suspend"]>, Promise<void>> = true;
export const resumeReturn: Same<ReturnType<ControlPlane["resume"]>, Promise<void>> = true;
export const terminateReturn: Same<ReturnType<ControlPlane["terminate"]>, Promise<void>> = true;
export const preflightReturn: Same<ReturnType<Sandbox["preflight"]>, Promise<void>> = true;

// @ts-expect-error The missing-image result is null, not undefined.
export const absentImage: Exclude<Awaited<ReturnType<AgentVm["findImage"]>>, string> = undefined;
