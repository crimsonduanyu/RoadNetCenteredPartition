# Deferred validation gates

## Clustering runtime dependencies: passed in Phase 9

Phase 9 installed the dependencies declared by `environment.yml` without
upgrading unrelated scientific packages: `python-louvain 0.16`,
`python-igraph/igraph 1.0.0`, `leidenalg 0.12.0`, and `pymetis 2025.2.2`.
The active `dydl` environment uses Python 3.12.13 although the environment file
declares Python 3.11; the actual runtime is recorded rather than presented as
an exact environment-file reproduction.

The project Louvain, Leiden and METIS runners were called directly, without
adapters or monkeypatching, on a fixed connected graph with seed 42. Each
returned two complete connected clusters and repeated deterministically. The
real third-party runtime gate is therefore passed. See
`tests/test_phase9_clustering_runtime.py` and
`docs/refactor/phase9-preflight-v1.md`.

## Demand historical cross-platform spatial assignment: resolved_by_canonical_policy

The historical formal Demand products under
`data/processed/<scope>/order_pipeline/` were generated on Windows. The Phase
5A full validation was run on Linux. The legacy and migrated Demand
implementations are exact when exercised in the same environment, but the
historical Windows products and current Linux rerun differ in spatial segment
assignment, OD nonzero positions and POI graph edges.

The total assigned orders, service-type totals, timestamps, cluster index,
tensor axes and tensor totals remain equal. Phase 5A.1 found that every changed
origin/destination cluster assignment involved overlapping candidate segment
geometries with distances equal or within the declared `1e-6 m` plus `1e-12`
relative tolerance. It found no case where either candidate was clearly closer
under the current Linux geometry stack. Exact historical Python, GeoPandas,
Shapely, GEOS, pyproj and PROJ versions were not recorded.

The Phase 9 decision record retained these options:

- reproduce the Demand run in the historical environment;
- retain explicitly platform-specific historical and Linux baselines;
- after the structural refactor, separately design and version a deterministic
  spatial assignment v2.

On 2026-07-30 the project owner approved the Linux full-pipeline run as the new
canonical baseline. The gate is therefore `resolved_by_canonical_policy` for
the exact run and hashes recorded in
`configs/policies/fifth_ring_linux_canonical_v1.yaml`.

Historical Windows row-wise reproduction remains unachieved, and deterministic
cross-platform assignment remains unimplemented. This is a formal baseline
selection, not a nearest-neighbour tie-break repair. Any future run still needs
its own matching canonical decision or the then-current canonical contract.
