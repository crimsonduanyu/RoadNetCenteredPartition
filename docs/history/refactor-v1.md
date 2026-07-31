# Refactor and publication history v1

This document condenses the Phase 0–12 migration record. Detailed inventories,
audits and intermediate reports remain available in Git history.

## Architecture outcome

The project migrated from script-level legacy execution to the importable
`roadnet_partition` package with split configuration, owned run directories,
stage contracts, validation, transactional publishing and privacy-filtered
reproduction export. Legacy `src/lib`, `src/stages`, path injection and the old
unified runtime configuration were retired.

The supported flow is `partition → demand → supply → tte`. Ordinary execution
writes `outputs/runs`; canonical replacement is allowed only through publish.

## Canonical and validation decisions

- Linux is the current Fifth Ring canonical platform.
- Golden v1 is regression evidence, not a production input or release asset.
- The prior Windows canonical is retained as private baseline metadata plus a
  local-only payload archive.
- Demand migration is mechanically equivalent, but cross-platform nearest-road
  ties can select different overlapping segments. No deterministic assignment
  v2 was introduced.
- Supply legacy, migrated and historical formal aggregates matched exactly when
  run against the same frozen historical Demand input.
- TTE legacy, migrated and historical products matched byte-for-byte against
  the same frozen inputs.
- The Phase 9 full Linux run passed stage, publication and canonical validation.

## Publication figures

The priority historical commit was
`d2139c63a01e5506f190c03da2db5cdc2e79d495`. Its PNG/PDF save logic was restored
through current authoritative modules without restoring legacy business code.
The corrected target is a horizontal two-panel partition/order figure. The
recovered renderers remain available, while rendered figures are now generated
on demand under `outputs/figures/`.

The historical TTE distribution pair was introduced without a committed
producer. Its summary matches the formal `TTE_raw.parquet` contract (19,742,327
observed cells in the configured 3–80 minute range), so the figures are retained
as historical assets and explicitly marked non-regenerable.

## Artifact cleanup and raw-only release layout

Obsolete probes, duplicate Fourth/Fifth Ring aliases and reproducible Phase 5
payloads were removed after reader and hash audits. The remaining long-lived
artifact tree was backed up outside the repository before the raw-only release
removed it from the public working tree.

Ordinary execution now starts from private inputs in `data/raw/`, creates its
preparation data inside the owning run, and writes all runs, validation,
figures, reports and releases under ignored `outputs/`. Golden comparison is an
optional maintainer operation against an explicitly supplied external path.

## Known limits

Private Beijing data is not licensed or distributed by this repository. Golden
and baseline payloads remain local-only. Reproduction export does not grant
redistribution rights. Downstream confidence weighting for TTE support remains
outside this repository.
