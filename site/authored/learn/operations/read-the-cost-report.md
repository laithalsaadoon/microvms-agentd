---
title: Read the cost report
description: What each line of a cost report means, where the rates come from, why a total may read "at least", and how to plan a run with microvm cost before spending anything.
editUrl: false
sidebar:
  order: 6
---

```bash
microvm cost --estimate --memory 1024 --running-sec 1800 --image-gb 2
microvm cost --estimate --compare --memory 2048 --hold-sec 28800 --cycles 4
microvm cost --estimate --memory 2048 --running-sec 3600 --max-cost 1.50 --on-breach abort
```

Use `microvm cost` to estimate a workload before launching it. Read the price
sources and unpriced items before using the total as a budget. A partial
total is a lower bound on the cost.

## 1. Check the rate sources

Rates are pinned, dated, and per region, and only the ARM rates apply, because the service's architecture enum has one member. Dollar figures are estimates derived from published rates, never an invoice. `mise run live:rates` checks the pinned table against the AWS Pricing API, and the report carries a staleness note when the pinned date is old.

Anything the engine cannot price is reported as unpriced with a reason, and an unpriced line omits `usd` rather than reporting zero. The server-side image build is the usual case: AWS has not published whether a build bills as compute, so `--build-sec` puts an unpriced line on the report.

## 2. What bills

A VM in `RUNNING` bills for vCPU and memory each second. The baseline set by
`--memory` is the floor you pay for, even when the VM is idle. The guest has
four times that capacity from launch. Usage above the baseline adds a charge
for what it consumes. Suspend the VM or call `terminate` when it can stop.

Snapshots bill three ways. Image storage bills per GB-hour with a one-week minimum retention, so a 2 GB image deleted sixty seconds after creation still bills about a week of storage. A suspended VM incurs snapshot storage charges. Each suspend/resume cycle
adds a snapshot write and read; use the calculator with the intended region,
size, duration, and cycle count to compare alternatives.

Rates vary by region. `us-east-2` and `us-west-2` match `us-east-1` on every line; `eu-west-1` and `ap-northeast-1` are higher, most of all on the snapshot dimensions, so a design that leans on a suspended pool should price in its own region. Data transfer bills at standard AWS rates and is not on the report. The measurements are in [Platform](/internals/platform/), under "What actually costs money".

## 3. A total may be a lower bound

When any line lacks a price, the total reads `at least $X`. In `--json`, the
budget result has `basis: "lower-bound"`. A report with all prices has
`basis: "exact"`.

If the floor exceeds your budget, the full cost does too. If the floor is
within budget, the full cost may still exceed it. Read the unpriced lines
beside the total before deciding to proceed.

## 4. Plan before you spend

`--estimate` treats the durations as a plan rather than as timings, and every duration on such a report is labeled projected, so an estimate cannot print as a report of something that ran. `--running-sec`, `--suspended-sec`, and `--build-sec` are the phases; `--image-gb` adds storage with its one-week minimum retention; `--cycles` counts suspend/resume cycles, each paying a snapshot write plus a read. Left off, the report counts none, because a report claims only what you say happened, so a report of a run that suspended takes `--cycles`.

`--compare` also prints running versus suspended for the same hold, with the break-even, over `--hold-sec` (default 3600). The comparison prices one suspend/resume cycle when `--cycles` is left off, to include the cost of that cycle, and a `--cycles` you give feeds both the report and the comparison.

`--max-cost` is a budget in USD the report's total is checked against, and `--on-breach` says what a breach does: `warn` warns and exits 0, `abort` aborts with `ERR_PRECONDITION` (exit 12). The pair is required together, because whether a breach of a lower-bound total should stop a script is the caller's judgment and has no default. Under `--json` the `budget` key carries `maxUsd`, `onBreach`, `basis`, `breached`, and `overageAtLeastUsd`.

## 5. Size for the bill

`--memory` selects a size class, and the guest reports the class's peak in `/proc/meminfo`:

| Baseline (billed while running) | Peak (provisioned ceiling) |
| ------------------------------- | -------------------------- |
| `512` MiB, 0.25 vCPU            | 2 GB, 1 vCPU               |
| `1024` MiB, 0.5 vCPU            | 4 GB, 2 vCPU               |
| `2048` MiB, 1 vCPU (default)    | 8 GB, 4 vCPU               |
| `4096` MiB, 2 vCPU              | 16 GB, 8 vCPU              |
| `8192` MiB, 4 vCPU              | 32 GB, 16 vCPU             |

For peaky workloads such as builds, test runs, and agent sessions, pick a low minimum and let the peaks ride the always-present headroom, which bills only by what is consumed. Guest swap is absent, so pressure past the peak goes straight to the OOM killer. The value is also a `microvm.toml` key, `memory`, validated against the same closed set.

## 6. On the run envelope

A `run` envelope carries the report under `cost`, and the human rendering ends with a `cost:` line. `buildSeconds` and `runningSeconds` beside it are the measured phases the report was built from. Because the image snapshot's one-week minimum applies whether or not you keep the image, reusing it with `--image` is the economical habit; [your first run](/learn/tutorial/first-run/) shows the loop.
