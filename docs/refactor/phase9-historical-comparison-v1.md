# Phase 9 historical comparison v1

This report separates same-input implementation evidence from outputs produced
with different Demand assignments. No formal data or Golden asset was modified.
Detailed matrix and row-level comparisons remain ignored; this document contains
only aggregate counts, hashes, shapes and error summaries.

## Partition

The formal full run produced 59,096 segments and 100 clusters in EPSG:32650.
`validate --golden` passed the canonical geometry contract and grouping hash:

```text
11ac2e21b2f6f22498c250ee7eeaefe0f2c65ef5e5952e1c6722bac9154633c7
```

Partition therefore matches Golden v1 and the historical canonical mapping.
Demand's runtime binding points to this run-owned GPKG, not its standalone
published fallback.

## Demand

The full pipeline Demand is exact with the Phase 5A Linux baseline:

- 46,002,707 assigned orders; 30,651,791 exclusive and 15,350,916 carpool;
- 100-cluster index, 13,248 slots, and tensor shape `13248 x 100 x 100`;
- cluster index, OD CSV, tensor, road/POI/distance edges and adjacency assets are
  byte-for-byte identical;
- the two assigned-order gzip containers have different headers, but their
  decompressed 10,528,450,111 bytes share SHA-256
  `466ecd3bf5bcb8f07179c4db4a80d2f1eb05a2724e0d280a716e7a629c75bb30`;
- metadata differs only in expected config, binding and output paths.

The full runner therefore introduced no same-platform Demand difference.

Because this result is the Phase 5A Linux result, its comparison with the
historical Windows formal Demand repeats the previously audited platform
pattern rather than establishing row-level reproduction:

- pickup segment differs on 6,434,254 rows and dropoff segment on 8,102,197;
- origin cluster differs on 124,399 rows and destination cluster on 127,633;
- 251,455 distinct orders are affected (`0.546609%`);
- tensor total is unchanged at 46,002,707, while 457,784 cells differ with
  maximum absolute difference 9;
- historical POI graph has 718 edges and Linux has 719.

The Phase 5A.1 diagnostic found all changed choices on overlapping,
equal-distance segment candidates and no clearly closer alternative. Linux
Demand is not accepted as the replacement canonical by this report.

## Supply

Two valid comparisons are kept distinct:

1. Phase 5B ran the migrated Supply against historical Demand and reproduced
   historical Supply exactly, including complete row hashes.
2. Phase 9 ran pipeline and standalone Supply against the same Linux full-run
   Demand. Their three gzip containers have different gzip headers, but all
   decompressed bytes and `run_summary.json` are exact.

The Linux-Demand Supply differs from the historical-Demand Supply as follows:

| Aggregate | Linux full run | Historical | Difference |
|---|---:|---:|---:|
| in-service rows | 41,890,744 | 41,912,550 | -21,806 |
| vehicles in service | 150,762,542 | 150,762,602 | -60 |
| available total | 40,837,163 | 40,837,201 | -38 |
| cluster fleet lower bound | 174,925,628 | 174,918,972 | +6,656 |
| global fleet lower bound | 142,011,203 | 142,011,203 | 0 |

Both sides retain 46,002,707 orders, 199,668 drivers, 13,248 slots and 100
clusters. These differences propagate from the Demand assignment; they do not
contradict the exact Phase 5B same-input Supply migration result.

## TTE

Phase 5C established byte-for-byte equality among legacy, migrated and
historical TTE when all used historical Demand. Phase 9 pipeline and standalone
TTE both used the Linux full-run Demand and their seven files are byte-for-byte
identical.

The Linux-Demand matrices and historical-Demand matrices have the same
`13248 x 10000` shape, axes, schemas and dtypes. A 64-row streaming comparison
reported:

| Matrix | mismatched values/cells | mask mismatches | maximum absolute difference |
|---|---:|---:|---:|
| count | 454,932 | 0 | 9 |
| raw | 452,732 | 98,090 | 60.18333333333334 |
| support | 1,535,523 | 0 | 74 |
| hops | 120,796 | 0 | 4 |
| imputed | 3,742,018 | 22,270 | 96.48040008544922 |

Linux has 19,730,545 observed cells, 82,080,529 inferred cells and 30,668,926
missing cells. Historical Demand produces 19,742,327, 82,071,287 and
30,666,386 respectively. The network-distance and representative-node assets
are unchanged. These are different-input results and are not evidence of a TTE
algorithm change.

## Conclusion

Partition is historical/Golden exact. Demand is Linux same-platform exact but
not historical Windows row-level exact. Supply and TTE are exact between
pipeline and standalone when given the same Linux Demand, and their historical
same-input migration evidence remains exact. Real publish remains blocked by
the Demand platform baseline decision.
