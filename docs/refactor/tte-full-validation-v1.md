# TTE full validation v1

Phase 5C ran the pre-bridge legacy TTE implementation and the migrated
`roadnet_partition.downstream.tte` implementation against the same frozen
historical inputs. The two implementations wrote only to separate ignored
validation directories:

```text
outputs/refactor-validation/phase5c-tte/legacy-historical-demand/
outputs/refactor-validation/phase5c-tte/new-historical-demand/
```

The historical formal directory `data/processed/fifth_ring/tte/` was read
only. No validation output was published.

## Frozen inputs

Both runs used exactly the same assigned orders, cluster index, network
distance, representative nodes, time range, slot frequency, aggregation, and
imputation configuration. Network-distance recomputation was disabled, and
both validation directories were pre-seeded with the same two distance assets.

| Input | SHA-256 |
|---|---|
| `data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz` | `f6dfbbf09dc0fb8cc8d14c00e929e54484303ef997599b5266420048eaea6969` |
| `data/processed/fifth_ring/order_pipeline/cluster_index.csv` | `5b7bf80aca473aec87e42394d9a0a66581d00a044ecae4f8224eb2fbc7301826` |
| `data/processed/fifth_ring/tte/cluster_network_distance.parquet` | `69fe2a603efd637bc8c1207fd1cfab417aefa699231d25b04fd73c8e8ac6953a` |
| `data/processed/fifth_ring/tte/cluster_representative_nodes.csv` | `1d550ba86a50dacf27ab41111ea9650bee6ad5d83fdc6fa91ceed89d15b9d6e1` |

This isolates the TTE migration from the deferred Demand cross-platform
assignment difference: neither side used the Linux Demand validation rerun.

## Exact three-way result

The legacy rerun, migrated rerun, and historical formal TTE products are
byte-for-byte identical. Each hash below is the same in all three directories.

| Output | Shape | Logical dtype | Three-way SHA-256 |
|---|---:|---|---|
| `TTE_raw.parquet` | `13248 x 10000` | `float64` | `ea4a4bf868194abeef83a8b56961ac21790a78b1cc011e4bee84ec0c93269f85` |
| `TTE_count.parquet` | `13248 x 10000` | `int32` | `7e48c378773ea735f3cce2c82df38ddaf8679b7537d8ad7675db5899b069d638` |
| `TTE_imputed.parquet` | `13248 x 10000` | `float32` | `045dd9dbf14800f4f6bf6c2b92b3c0e26ee65a1179e8d9a6e28ecb83a5a283e7` |
| `TTE_hops.parquet` | `13248 x 10000` | `int16` | `21358909bf0edd4b911fd98960bd6e8301edb1e29a196d1f5fbc1ebaf99bb325` |
| `TTE_support.parquet` | `13248 x 10000` | `int32` | `a8fa61c5b435f895f0b2aa2364514f6749d1e88449538433643ccab64d642f56` |
| `cluster_network_distance.parquet` | `100 x 100` | `float64` | `69fe2a603efd637bc8c1207fd1cfab417aefa699231d25b04fd73c8e8ac6953a` |
| `cluster_representative_nodes.csv` | 100 rows | CSV contract | `1d550ba86a50dacf27ab41111ea9650bee6ad5d83fdc6fa91ceed89d15b9d6e1` |

All five matrices also have identical Parquet schema and metadata shape,
logical dtype, datetime index, OD columns and ordering. They share the same
13,248 ten-minute slots, 100-cluster universe, and 10,000 ordered OD columns.

For both legacy-versus-new and new-versus-historical comparisons:

- NaN/mask mismatch count: 0 for every matrix;
- value mismatch count: 0 for every matrix;
- maximum absolute error: 0;
- maximum relative error: 0;
- values exceeding `0`, `1e-12`, `1e-9`, or `1e-6`: 0;
- first differing position: none;
- every corresponding batch hash: identical.

## Streaming comparison method

`scripts/analysis/compare_tte_outputs.py` opens each Parquet file through
`pyarrow.parquet.ParquetFile` and iterates over 64-row record batches. It
compares one pair of directories at a time, first legacy versus migrated and
then migrated versus historical, so the three complete matrix sets are never
loaded together.

For every batch the comparator checks the index and column axes, dtype, NaN
mask, exact discrete or floating values, mismatch count, maximum absolute and
relative error, predefined error thresholds, first difference, and a hash of
the index, dtype, and contiguous values. File schema, Parquet metadata shape,
whole-file SHA-256, network-distance axes and representative-node rows are
checked separately. The ignored detailed evidence is stored at:

```text
outputs/refactor-validation/phase5c-tte/legacy-vs-new.json
outputs/refactor-validation/phase5c-tte/new-vs-historical.json
outputs/refactor-validation/phase5c-tte/legacy-contract.json
outputs/refactor-validation/phase5c-tte/new-contract.json
```

## Contract statistics

The legacy and migrated contract reports are identical:

```text
matrix shape              13,248 x 10,000
raw observed cells             19,742,327
raw missing cells              112,737,673
count total                     45,665,595
newly inferred cells            82,071,287
final finite cells             101,813,614
final missing cells             30,666,386
diagonal observed cells            811,154
diagonal missing cells             513,646
```

The diagonal observations remain real measured values; missing diagonal cells
remain missing and are not imputed. The hop distribution is:

```text
hops -1   30,666,386
hops  0   19,742,327
hops  1   79,604,455
hops  2    2,092,889
hops  3      373,943
```

The contract checks also passed for cluster and OD order, shared time axes,
raw/count observed-mask relationships, support/hops relationships, distance
reachability, diagonal behavior, and final imputed/missing masks.

## Resources, privacy, and Git policy

Each complete TTE directory occupies about 710 MiB; the two validation reruns
therefore add about 1.4 GiB. The runs were performed sequentially. At the final
check the host had about 243 GiB free disk and 53 GiB available memory. The
64-row streaming comparison avoided holding more than one pair of batches in
memory.

All files under `outputs/refactor-validation/phase5c-tte/` are ignored by Git.
The assigned-order input, TTE Parquet files, network assets, validation logs,
and detailed comparison JSON remain local and must not be committed or treated
as public data. This tracked document contains only paths, hashes, shapes,
aggregate counts, and error summaries; it contains no order or driver IDs,
coordinates, source rows, or matrix values.

## Deferred validation gates

No `TTE historical cross-platform reproduction` gate is required: the current
Linux legacy and migrated reruns are exact with each other and byte-for-byte
reproduce the historical formal TTE products when all sides use the same
frozen historical Demand input. This conclusion does not resolve or weaken the
separate Demand historical cross-platform spatial-assignment gate.

`docs/refactor/deferred-validation-gates.md` therefore remains unchanged and
continues to contain only the real third-party clustering execution gate and
the Demand historical cross-platform spatial-assignment gate.
