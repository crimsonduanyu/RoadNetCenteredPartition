# Fifth-ring Windows baseline v1

This local-only archive preserves the 91-file `data/processed/fifth_ring/`
scope that was canonical immediately before the approved Linux canonical
publish on 2026-07-30.

- Source: historical Windows-generated canonical processed scope.
- Purpose: traceability for historical papers and downstream results.
- Privacy: private; payload files must remain local and are Git-ignored.
- Status: archived, not the default output of the current pipeline.
- Boundaries: this is neither Golden regression data nor a reproduction
  release, and it must not be exported automatically.
- Materialization: payload files are hard links to the pre-publish formal
  scope. Phase 7 publishing creates a separate staging copy and swaps whole
  directories, so the archive remains readable after the old formal scope is
  removed.

Verify with `sha256sum -c checksums.sha256` from this directory. The aggregate
inventory hash is recorded in `manifest.json` and matches the Phase 9
preflight inventory.
