# SPDX-License-Identifier: Apache-2.0
"""The live checks: one module per area, each section a `drive_*` function.

A section takes the `Cli`, the suite's kept VM (`launched`) or what it creates itself, and the
run's `Results`, and records each check under its name. `suite.run_suite` runs the sections in
order against one account and tears the kept VM down in its `finally`. A lane's offline twins,
where it has them, are in the `selftest` module of the same name.
"""
