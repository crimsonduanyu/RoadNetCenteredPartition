# Deferred validation gates

## Gate before Phase 9 acceptance

The current `dydl` environment does not contain `python-louvain`,
`python-igraph`/`leidenalg`, or `pymetis`, although `environment.yml` declares
them. Phase 4 validated the Louvain, Leiden and METIS adapters with fixed
deterministic fixtures, including argument, node/edge order, weight and seed
handling, but did not execute the real third-party implementations.

Before Phase 9 full-pipeline acceptance, create or synchronize an environment
that matches `environment.yml` and run real baseline smoke tests for Louvain,
Leiden and METIS. This gate does not block Phase 5A and Phase 5A must not install,
upgrade or otherwise change these dependencies.

## Demand historical cross-platform spatial assignment

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

Before Phase 9, choose and document one of:

- reproduce the Demand run in the historical environment;
- retain explicitly platform-specific historical and Linux baselines;
- after the structural refactor, separately design and version a deterministic
  spatial assignment v2.

This gate prevents publishing the Linux Demand rerun as a replacement for the
historical formal products and prevents claims of row-level historical
reproduction. It does not block Supply/TTE mechanical migration, provided both
old and new implementations in each comparison read the same frozen Demand
input. It must not be resolved during structural migration by changing the
nearest-neighbour tie-break, spatial predicate, floating tolerance, or
GeoPandas/GEOS dependency versions.
