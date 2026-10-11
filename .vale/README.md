# Prose checks

Vale is pinned in `mise.toml` and `mise.lock`. `.vale.ini` pins the upstream
style archives by release URL. The unpacked styles are committed, so checking
prose requires no package download. `Microvms` adds American spelling and a
repository vocabulary. Each severity override has its reason beside it.

Run `mise run prose:lint` to see every finding. Run `mise run prose:check` to
reject errors and warning counts above `.vale/warnings.json`. The latter runs
in `check` and in CI's security job. Its runner lists maintained files through
git and excludes the generated traceability table, the changelog, and site
build output.

Warning ceilings apply to each rule independently. A rule without an entry has
a ceiling of zero. Lower a ceiling after fixing its warnings; the runner refuses
an increase relative to the merge base. Do not raise another rule's ceiling to
offset improvements elsewhere.

To update styles, change the release URLs deliberately, run `vale sync`, inspect
the upstream rule changes and licenses, and commit the unpacked styles. Run the
prose and documentation checks before opening the change for review. The vendored
UnderRegime rule uses `rule15` in its comment examples so the documentation
checker does not read an upstream example as a local decision citation.
