# Surface contracts

`capabilities.toml` maps operations across Rust, the CLI, Python, and TypeScript.
Its `inventory` file maps public types, callable signatures, and inputs, including
positional arguments, global flags, aliases, and options-object fields.

`mise run parity:check` reads each generated surface independently and compares it
with the reviewed inventory. A new item requires a contract row. A changed shape
requires review of its requiredness, position, type, and invocation semantics.
Rust's request structs and Python's keywords can implement the same contract as a
TypeScript options object without having identical syntax. Each syntax is held
to its own recorded shape.

When changing a surface:

1. Regenerate the affected source with `mise run core-api`, `mise run manifest`,
   `mise run stubs`, or `mise run dts`.
2. Run `./tools/check-parity.py --inventory` to inspect the observed shapes. This
   prints observations and never edits or enrolls the inventory.
3. Update the affected `shapes` entry and place each new name in a contract row.
   Link operation inputs to the corresponding capability when applicable. Each
   item belongs to exactly one row.
4. A surface without that representation names an exemption with a specific
   reason. Rust composition ports, language iterator protocols, and a CLI's
   command-selection flags can require different representations. An absent
   implementation is not an exemption. Unused reasons and dangling names fail.
5. Run `mise run parity:check`. Keep negative tests and seeded faults with changes
   to collectors or the checker.

The inventory's schema version is explicit. Unknown declaration forms fail the
reader instead of silently shrinking the inventory. TypeDoc describes module
exports; the same pinned TypeScript compiler also reads ambient global types and
checks that no explicit export was omitted. Griffe preserves overload-only
functions, explicit reexports, and Python's runtime protocol methods. Rustdoc
references are resolved to canonical paths before comparison, so two same-named
types in different modules cannot silently substitute for each other.
