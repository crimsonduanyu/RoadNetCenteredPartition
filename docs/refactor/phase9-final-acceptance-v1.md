# Phase 9 final acceptance v1

## Result

```text
PASS_WITH_PUBLISH_GATE
```

The formal-scale pipeline, validation, same-platform wiring, resume, TTE
overwrite, dependency smoke, publish dry-run and release dry-run passed. Real
publish remains blocked because the user has not selected a Demand canonical
platform policy.

## Git and environment

- source commit used by the run: `e54077b746607001e6fdf45ed09530a03c851179`
- source tracked worktree: clean
- Linux `7.0.0-28-generic`, x86-64; Python `3.12.13`
- GeoPandas `1.1.3`, Shapely `2.1.2`, GEOS `3.14.1`
- pyproj `3.7.2`, PROJ `9.7.1`, pandas `3.0.3`, NumPy `2.4.6`
- clustering runtime smoke: real Louvain, Leiden and METIS passed

The complete preflight, formal inventory hash and dependency versions are in
`docs/refactor/phase9-preflight-v1.md`.

## Formal full pipeline

Run directory:

```text
outputs/refactor-validation/phase9/full-runs/20260730T020500Z-fifth-ring-full-02fce2f7
```

Internal run ID: `20260729T180105Z-fifth_ring-full-02fce2f7`.

The initial isolated run completed all required stages through TTE in
6,060.502 seconds (`1:41:00.502`):

| Stage | Wall time | Manifest output bytes | Result |
|---|---:|---:|---|
| Partition | 209.498 s | 27,559,578 | complete |
| Demand | 4,191.621 s | 3,397,438,739 | complete |
| Supply | 967.386 s | 142,959,886 | complete |
| TTE | 691.997 s | 743,616,285 | complete |

The run directory occupies about 4.1 GiB. Logs total about 475 KiB; Demand and
Partition stderr are empty, Supply stderr is empty, and TTE stderr contains
progress-bar output rather than credentials or private rows. Reliable peak
memory instrumentation was not attached, so no peak is claimed. Periodic
samples observed approximately 9.4 GiB Demand, 11.6 GiB Supply and 9.8 GiB TTE
RSS at their high points. Child processes exited after each stage.

## Manifest, binding and validation

The final manifest has `status=complete`, `completed_through=tte`, and
`all_required_stages_complete=true`. Runtime binding checks passed for:

- Partition GPKG → Demand partition input;
- Demand assigned orders and cluster index → Supply;
- Demand assigned orders and cluster index → TTE.

Every binding records this run ID, producer logical key, path, size and SHA-256;
pipeline bindings won over standalone fallbacks. No cross-run implicit input was
used.

`roadnet-partition validate --golden` passed with no warning or error:

- all stage output hashes and contracts passed;
- Demand: 46,002,707 assigned rows, 100 clusters, tensor
  `13248 x 100 x 100`;
- Supply: 46,002,707 orders, 13,248 slots, 100 clusters;
- TTE: `13248 x 10000`, 19,730,545 observed and 82,080,529 inferred cells;
- Golden: 59,096 segments, 100 clusters, EPSG:32650, canonical grouping hash
  `11ac2e21b2f6f22498c250ee7eeaefe0f2c65ef5e5952e1c6722bac9154633c7`.

## Same-platform and historical comparisons

The full Demand is exact with the Phase 5A Linux baseline after normalizing
gzip headers and path-only metadata. Independent formal Supply and TTE runs
using the same full-run Demand reproduced pipeline outputs exactly: all Supply
decompressed table bytes match and all seven TTE file SHA-256 values match.

Historical comparison is reported separately in
`docs/refactor/phase9-historical-comparison-v1.md`. Historical Windows Demand
still differs for 251,455 orders; downstream Linux-Demand differences are
therefore attributed to upstream assignment, not Supply/TTE migration.

## Resume and overwrite

Full-run resume completed in 30.871 seconds. All four stages printed `reused`;
42 stage files retained identical mtime, size and SHA-256. Invocation history
was appended without rewriting stage outputs.

TTE-only overwrite completed in 712.019 seconds. The 33 upstream stage files
retained identical mtime, size and SHA-256. All seven regenerated TTE outputs
retained their prior size and SHA-256. A pre-overwrite manifest backup exists,
and invocation history records `overwrite`, `requested_from=tte`, and
`requested_to=tte`.

## Publish and release dry-runs

Publish without `--overwrite` correctly rejected the existing formal target.
`--overwrite --dry-run` then passed technical preflight for 30 allowlisted files
totalling 4,310,941,229 bytes, with about 254.9 GB free and a complete-scope
staging/overwrite transaction plan. It created no staging directory and wrote
no publish history. Policy status remains:

```text
blocked_by=demand_platform_baseline_decision
```

The formal minimal reproduction dry-run created no release. It listed only two
Partition files and three small metadata candidates; their real-data
classification is `unknown`, so the privacy gate blocked real export. A
synthetic tiny run did perform a real minimal export and verified every checksum
(`1 passed`). No Git or LFS operation was invoked.

## Safety and remaining gate

All 91 files in `data/processed/fifth_ring` retained their preflight sizes and
SHA-256 values. Golden manifest/checksum hashes are unchanged. The old entry,
root config, compatibility bridge and `IntermediateDataForReproduce/` remain.

The only acceptance gate is the explicit Demand platform baseline decision in
`docs/refactor/demand-platform-baseline-decision-v1.md`. Until the user selects
and approves a policy, real publish is forbidden. No algorithm, tie-break,
contract or formal data was changed in Phase 9.

## Final command checks

Editable installation with `--no-deps` succeeded. The complete suite collected
271 tests and finished with:

```text
271 passed, 52 warnings in 48.41s
```

All warnings are the already deferred Shapely `unary_union` deprecation; Phase
9 did not change that algorithm path. CLI help checks for `run`, `validate`,
`publish`, and `export-reproduction` succeeded. Static import, no-shell,
privacy, transaction and rollback checks are included in the passing suite.
