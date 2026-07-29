# Phase 3 public-module migration inventory

This inventory records the mechanical implementation moves performed in Phase 3.
Legacy modules remain import-compatible bridges until Phase 10.

| Legacy module | Canonical implementation | Current importers before migration | Import-time behavior and state | Serialization audit |
|---|---|---|---|---|
| `src/env_setup.py` | `roadnet_partition.io.environment` | numbered scripts, `lib.geo`, `lib.metrics`, `lib.network_distance`, `lib.order_dataset`, `lib.tte_dataset` | Computes Conda prefix and initializes Windows DLL/GDAL environment. New initializer runs at the same pre-GeoPandas import point and is idempotent. | No custom type or serialized module-path reference found. |
| `lib.geo` | `roadnet_partition.io.geospatial` | numbered scripts, stage 1/3 wrappers, adaptive clustering, regularized code and visualization scripts | Project-root constants are evaluated at import; imports geospatial environment before GeoPandas. No cache. File writes occur only when functions are called. | No custom class and no serialized module-path reference found. |
| `lib.graph` | `roadnet_partition.graphs.relations` | `src/02_build_segment_relation_graph.py` | No module side effects, cache or global mutable state. | No custom type and no serialized module-path reference found. |
| `lib.clustering` | `roadnet_partition.zoning.algorithms.common` | numbered/adaptive clustering and clustering tests | The default tie-break lambda is evaluated at function definition time and was copied unchanged. | No custom class and no serialized module-path reference found. |
| `lib.network_distance` | `roadnet_partition.graphs.distance` | `lib.tte_dataset` and network-distance tests | Geospatial environment initialization at import; heavy NetworkX/OSMnx/GeoPandas imports remain local. Matrix cache/file behavior is unchanged. | No custom class and no serialized module-path reference found. |
| `lib.metrics` | `roadnet_partition.zoning.metrics` | benchmark script and regularized evaluation/visualization | Geospatial environment initialization, `EPS`, and dataclass defaults occur at import. Cache/file behavior remains function-scoped. | `MetricThresholds` is the only custom type. The old module explicitly exports the new class, so historical pickle lookup through `lib.metrics.MetricThresholds` remains valid. No historical artifact containing these module paths was found by binary/text scan. |

## Public-name bridges

- `lib.geo`: project/data constants plus all 24 historical helper functions.
- `lib.graph`: all six relation-edge helpers.
- `lib.clustering`: `allocate_component_cluster_counts`.
- `lib.network_distance`: all eight distance functions plus the three historically imported project/config/sort helpers.
- `lib.metrics`: `EPS`, `MetricThresholds`, and all 25 public functions.
- `env_setup`: `conda_prefix` and the new repeat-safe initializer.

No target module had `from ... import *`, dynamic source-file loading, `Path.cwd()`,
or `os.getcwd()` usage. Existing dynamic imports elsewhere target Supply/Demand
modules and were outside this phase. New package modules do not import `lib`,
numbered scripts, or `src/stages`.

## Mechanical changes permitted by package relocation

- `geo.py`: only the environment import and `PROJECT_ROOT` parent depth changed.
- `network_distance.py`: the old dependency on three helpers imported from
  `order_dataset.py` was replaced with behavior-identical local definitions so
  the new package does not depend on an unmigrated `lib` module.
- `metrics.py`: only environment and geospatial import paths changed.
- Function bodies, signatures, default values, sorting, random behavior, numeric
  operations, logging text and file-writing behavior otherwise remain unchanged.

`roads/segment.py` is a typed re-export of the clearly segment-oriented helpers;
their unique implementations remain in `io/geospatial.py`, avoiding a behavioral
split during this phase.
