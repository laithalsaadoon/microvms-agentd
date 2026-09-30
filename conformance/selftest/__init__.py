# SPDX-License-Identifier: Apache-2.0
"""The offline half, `run_rs.py --self-test`: the suite's own helpers and their negative
twins, against a stub `microvm`, with no AWS call.

`suite.self_test` runs everything in a fixed order. It holds the envelope, CLI driver,
`Results` and stream-reader checks inline, with a few lane helpers' twins beside them; the
other twins are in modules named for what they cover. A negative twin runs against a throwaway
`Results(probe=True)`, so its failure is the assertion and prints as `PROBE` rather than
`FAIL`.
"""
