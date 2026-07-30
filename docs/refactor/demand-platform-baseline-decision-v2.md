# Demand platform baseline decision v2

## Approved decision

On 2026-07-30 the project owner selected Option B from
`demand-platform-baseline-decision-v1.md`: the validated Linux full-pipeline
result is the new `fifth_ring` canonical baseline.

The machine-readable approval is
`configs/policies/fifth_ring_linux_canonical_v1.yaml`. It authorizes only:

- run ID `20260730T020500Z-fifth-ring-full-02fce2f7`;
- pipeline fingerprint `02fce2f7318c690806ed93672a7e26d06bcfa116b8e0a709ac0cc213f551c97d`;
- Demand assigned-orders SHA-256
  `fb5b14e869e07cf11a8661650555b7a6f114566ef3d66167fbdd22ed26e4bb06`;
- final validation-report SHA-256
  `7721d0540709282efeedee3983d9a379d78c421a336f0ec34913d29f8a4b5a8f`.

The prior Windows canonical is preserved as the private/local-only archive
`artifacts/baselines/fifth-ring-windows-v1/`, with 91 files and inventory
SHA-256 `0815dc61d0e8ac950229372a4b0f7fae76adae8c410a1535328c112c8fad9540`.

## Run identity repair

The Phase 9 command supplied an explicit directory label based on a guessed
local timestamp, while the runner generated its internal ID at UTC start.
This produced one run with directory
`20260730T020500Z-fifth-ring-full-02fce2f7` and internal ID
`20260729T180105Z-fifth_ring-full-02fce2f7`; it was not two algorithm runs.

Before publication, the run-owned marker, manifest, binding provenance and
final bound stage fingerprints were repaired so directory basename, marker and
manifest now all use `20260730T020500Z-fifth-ring-full-02fce2f7`. All stage
output hashes remained unchanged, and full Golden validation passed after the
repair.

## Exact difference location

The difference occurs in `roadnet_partition/downstream/demand.py`, which calls
`roadnet_partition/io/geospatial.py` for point-to-segment nearest-neighbour
matching:

```text
pickup/dropoff point
→ nearest candidate segment
→ segment-to-cluster mapping
→ origin_cluster/destination_cluster
→ OD aggregation
```

Partition mapping, order filtering, service types, time range and time slots
do not differ. The changed cluster assignment occurs before OD aggregation.
All 252,032 changed origin/destination diagnostic records have overlapping
segments and multiple nearest candidates; 96.578% have exactly equal distance,
all remaining cases are within the fixed tolerance, and the maximum distance
difference is `5.684341886080802e-14 m`. No order was lost and neither platform
candidate is clearly closer. Supply and TTE differences are propagation of the
upstream Demand assignment and both stages are exact when given the same Demand
input.

This is a canonical-baseline policy decision. It is not a deterministic
tie-break fix. Historical Windows row-wise Demand reproduction is not claimed,
and cross-platform deterministic assignment v2 remains deferred as a separate
algorithm/data-product version.

## Publish result

The first real transaction reached post-switch validation, detected that the
source-run Partition contract referenced a production input inside the replaced
scope, and restored the Windows scope. The input was already materialized with
the same SHA-256 under `data/interim/fifth_ring/frozen_inputs/`; publishing was
hardened to accept only that hash-equal relocation during formal contract
validation.

The retry completed the whole-scope transaction. The formal scope now contains
30 allowlisted Linux files plus `source_manifest.json`, totaling
4,310,941,229 bytes for the allowlisted files. Independent post-publish checks
passed all four stage contracts, all source-run file hashes and bindings, and
the complete 91-file Windows archive. No staging or transaction backup remains.

