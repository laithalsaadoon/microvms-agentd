# microvms-edges

The production implementations of `microvms-app`'s ports: SigV4 over reqwest and the
credential chain, the tunnel and forward sockets, the name registry on disk, the daemon
release fetch and its Sigstore check, tokio's clock and the OS random pool. This is the one
library crate I/O belongs in.

- An implementation goes behind a port the app declares. Logic that decides what to do
  (order, retries, fail-closed rules) belongs in the app's use case; this crate performs the
  operation and reports what happened. The daemon fetch is the model: the policy is
  `microvms_app::provision::fetch_release`, and `GitHubRelease` here only downloads and
  classifies each answer.
- The ratchet's port-impl collector reads the app and core too, so a port implemented
  anywhere but here is drift or a recorded decision.
- The Sigstore crates (`sigstore-verify`, `sigstore-trust-root`, `sigstore-types`) are pinned
  exactly, with the embedded trusted root and no `tuf`. The reasons are in `Cargo.toml`. The
  root is as old as the build, so `.github/workflows/release.yml` runs
  `tests/release_bundle.rs` over the new bundle before it drafts the GitHub release. Every
  publishing job waits on the live gate, which waits on that step, so a refusal there
  publishes nothing. The fix is to bump the Sigstore pins together and tag again.
- `$MICROVM_RELEASE_DIR` makes `HttpsFetch` read a release from disk, which is how the live
  run that gates a release reads the draft's assets. The proof is the same attestation, and
  the directory source answers every missing bundle as absent, so it never reaches the
  `SHA256SUMS` fallback. Keep it that way: a directory that supplies both the asset and its
  checksum proves nothing by the checksum.
- `tests/fixtures/release-v0.7.0/` is a real release. The asset is gzipped so Scorecard doesn't
  count a binary; keep it that way if you replace it. The crate excludes `tests/` from its
  package.
- `mise run live:release` fetches and verifies a published release with no `gh` and no token.
  It's free and needs no AWS.

Tests: `cargo test -p microvms-edges`.
