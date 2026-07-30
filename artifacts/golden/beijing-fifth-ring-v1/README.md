# Beijing Fifth Ring Golden v1

This directory is a regression-validation asset, not a production input tree
or a reproduction release. It freezes the canonical Regularized partition,
the Leiden initialization used to generate it, and separately classified
historical comparisons.

The payload was classified from `IntermediateDataForReproduce/` on
2026-07-29. Known provenance comes from the legacy README, current readers and
writers, and the Phase 0 semantic baseline. Historical runtime versions that
were not recorded remain unknown; none have been inferred.

Large geospatial payloads are local-only and ignored by Git. On this migration
host they are read-only hard links to the legacy files. Production inputs that
already have byte-identical files under `data/` are external references in the
manifest rather than duplicated here. Five inputs formerly stored in the
processed scope now live under `data/interim/fifth_ring/frozen_inputs/`; their
content hashes did not change during the Linux canonical publish. These assets
contain derived location, POI, or order information and are not approved for
public distribution.

Verify local payload checksums with:

```bash
cd artifacts/golden/beijing-fifth-ring-v1
sha256sum -c checksums.sha256
```

Validate a completed pipeline run explicitly with:

```bash
conda run -n dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden artifacts/golden/beijing-fifth-ring-v1
```

Golden assets are never scanned as ordinary downstream inputs. Golden is not a
reproduction release, and successful validation does not grant permission to
publish it. Any update must create a new version directory rather than mutate
`beijing-fifth-ring-v1`.
