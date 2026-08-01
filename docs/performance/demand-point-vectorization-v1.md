# Demand Point Vectorization v1

**Batch:** Performance 2C-1
**Status:** passed
**Date:** 2026-08-01

## Change

Demand pickup/dropoff matching now constructs coordinates with Shapely's
vectorized `shapely.points` API:

```python
# before
geometry=[Point(xy) for xy in zip(x, y)]

# after
geometry=shapely_points(x, y)
```

The existing finite-coordinate mask, `row_id`, CRS assignment, `to_crs`,
`sjoin_nearest` arguments, duplicate removal, unmatched handling, and segment
mapping are unchanged. The shared helper is also used by Preparation order
matching and POI matching; their call sites and logic were not changed. The
constructor replacement was checked for geometry and production-output
equivalence before acceptance.

## Microbenchmark

The timed function includes GeoDataFrame creation and CRS transformation. Each
size used one warm-up and five measured repetitions in isolated processes.
Peak RSS includes the process/import baseline. The projected saving assumes
the production pattern of 181 chunks × two point-construction calls.

| Points | Legacy median / p95 (s) | Shapely median / p95 (s) | Legacy / Shapely peak RSS (MiB) | Median reduction | Projected saving |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 0.0516 / 0.0517 | 0.0075 / 0.0078 | 135.6 / 135.5 | 85.5% | 0.27 min |
| 100,000 | 0.5460 / 0.5772 | 0.1046 / 0.1062 | 180.0 / 179.7 | 80.8% | 2.66 min |
| 250,000 | 1.4019 / 1.4051 | 0.2902 / 0.2948 | 254.9 / 254.5 | 79.3% | 6.71 min |
| 500,000 | 2.7576 / 2.7789 | 0.5207 / 0.5342 | 378.9 / 379.2 | 81.1% | 13.50 min |

`geopandas.points_from_xy` was also measured and was within the same range;
`shapely.points` had the best median at three of four sizes and was selected.

## Formal raw-only validation

The fresh run `demand-prof-2c1` used the same raw inputs, configuration, and
dependency versions as `demand-prof-2b`, ran to Demand without resume, and
completed its Demand contract successfully.

| Metric | Baseline | Vectorized | Change |
| --- | ---: | ---: | ---: |
| Demand wall | 4,190.942 s (69.849 min) | 3,469.510 s (57.825 min) | −721.432 s (−12.024 min, −17.2%) |
| Pickup point construction | 524.922 s (8.749 min) | 118.327 s (1.972 min) | −77.5% |
| Dropoff point construction | 516.527 s (8.609 min) | 104.445 s (1.741 min) | −79.8% |
| Point construction total | 1,041.448 s (17.357 min) | 222.773 s (3.713 min) | −818.676 s (−13.645 min, −78.6%) |
| Nearest query total | 1,063.018 s (17.717 min) | 967.140 s (16.119 min) | −1.598 min |
| Total order spatial matching | 2,128.229 s (35.470 min) | 1,213.060 s (20.218 min) | −15.253 min |
| Peak RSS | 11.06 GiB | 10.93 GiB | −1.15% |
| CPU utilization | 99% | 99% | unchanged |

Total order spatial matching is the sum of pickup/dropoff point construction,
nearest query, spatial-index build, and segment mapping. The wall reduction is
smaller than the point-phase reduction because nearest query, service labeling,
SQLite work, and assigned-orders export remain outside this change.

## Equivalence evidence

- `rows_read`: 90,105,866; `staged_rows`: 46,002,707.
- Pickup matched: 53,422,582; dropoff matched: 52,478,350; both matched:
  46,002,707.
- Service counts: exclusive 30,651,791; carpool 15,350,916.
- Assigned-orders decompressed content: SHA-256 equal, 46,002,707 data rows;
  header and column order equal. This proves per-order segment assignment,
  distance, cluster ID, service label, and unmatched filtering are unchanged.
- Cluster OD: 19,507,610 rows, schema/values byte-identical.
- OD tensor: all arrays equal in shape, dtype, and value: `(13248, 100, 100)`
  `int32` tensors plus the cluster and slot coordinate arrays.
- Cluster index, road/POI/distance graph edges, all adjacency assets, POI
  features, and category mapping are byte-identical.
- Independently regenerated partition: 59,096 rows; CRS, index, row order,
  `seg_id`, `cluster_id`, and every geometry WKB are equal.
- Targeted tests compare normal, duplicate, boundary, equidistant, missing,
  non-finite, non-default-index, pickup, and dropoff cases. The equidistant
  case compares the actual selected segment, not only geometric distance.

## Verification and scope boundary

`251` tests passed. `git diff --check` and the privacy scan passed. No STRtree
cache, segment reorder, new tie-break, distance change, combined pickup/dropoff
query, parallelism, SQLite change, or output-contract change was introduced.
