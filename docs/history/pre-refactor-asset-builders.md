# Pre-refactor asset builders

Phase 10 retired the numbered `src/00_*` through `src/05_*` scripts and the
`adaptive_clustering.py` compatibility bridge. They were the original manual
asset-building chain:

```text
OSM harvest → road filtering → POI/order features → relation graph
→ baseline clustering → visualization/benchmarking
```

These scripts read the unified root configuration and wrote directly into
shared `data/` and `outputs/` locations. They are not part of the accepted
four-stage pipeline and had no active reader. The exact source remains in Git
history.

Current formal runs consume frozen, checksum-audited inputs declared by split
dataset/stage configs. Partition algorithms and metrics live under
`roadnet_partition.zoning`; graph helpers live under
`roadnet_partition.graphs`; current runs never rebuild or overwrite the frozen
production inputs implicitly.
