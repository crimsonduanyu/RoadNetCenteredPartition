# Demand gzip compression-level profile v1 — Performance Batch 2C-2B

**日期：** 2026-08-02  
**状态：** 完成；level 1 已通过离线和正式性能门槛。  
**正式 run-id：** `demand-prof-2c-2b-level1`  
**配置：** `gzip_compresslevel: 1`

## 1. Scope and invariants

本批次只改变 Demand assigned-orders export 的 gzip compression level。没有
修改 CSV writer、CSV bytes、datetime formatting、export batch size、SQL、SQLite
索引、空间查询、service labeling、并行模型或 output contract。输出仍为
`.csv.gz`；gzip container bytes 可以改变，但解压 bytes 必须完全一致。

export batch size 仍为 **100,000 rows**，SQL 文本不变，编码仍为 UTF-8，
`newline=""`、header、列序、float/date formatting 和输出文件名不变。

## 2. Current gzip behavior audit

在生产配置显式化之前，代码没有传入 compression level。受控调用捕获了
`zlib.compressobj` 的实际 level：timing-off 的 `gzip.open` 和 timing-on 的
`_TimedGzipFile` 都使用 **level 9**。本机 Python signatures 也显示
`gzip.open(..., compresslevel=9)` 与 `gzip.GzipFile(..., compresslevel=9)`。

| Item | Confirmed behavior |
| --- | --- |
| timing off | `gzip.open(path, "wt", compresslevel=level, encoding="utf-8", newline="")` |
| timing on | `GzipFile(filename=path, mode="wb", compresslevel=level)` wrapped by `TextIOWrapper(encoding="utf-8", newline="")` |
| gzip filename header | FNAME flag set; basename `orders_region_assigned.csv` |
| gzip mtime header | `mtime=None`, generated at open time; not forced to a fixed value |
| gzip OS header | 255 |
| write/close | text flush, text close, gzip close; no fsync added |
| pre-change level evidence | controlled `zlib.compressobj` capture: `[9, 9]` |

最终配置在 Demand stage 配置根部增加了整数 `gzip_compresslevel`。解析器只接受
`0..9` 的整数（拒绝 bool、浮点、字符串和越界值），并显式传入现有 writer；
没有使用隐式全局变量。

## 3. Offline compression experiment

输入是已通过 contract 的 baseline assigned-orders gzip，解压后的实验输入只保留
在 gitignored 的 `outputs/perf-audit/demand-gzip-2c2b/` 下。输入聚合摘要：

| Item | Value |
| --- | ---: |
| uncompressed bytes | 10,523,799,657 |
| rows | 46,002,707 |
| SHA-256 | `087dcf1ad1606874f55e6c3b28a0401462641960406631ef6d6742d47ceebfed` |
| final newline | yes |
| header bytes including newline | 220 |
| header SHA-256 including newline | `8c61944c7613deaa88d4293dc9f54633aa322df3be3c709d8b71f6c0d960a9ca` |
| write path | same `GzipFile → TextIOWrapper(UTF-8, newline="")` path |
| input write chunk | 8,192 bytes for every candidate |

每个 level 先 warm-up 一次，再对完整输入压缩一次；完整压缩由
`/usr/bin/time -v` 包围。`gzip raw-write wall` 是 `_TimedGzipFile.write` 累计时间，
与正式 timing profile 的 `export_gzip_compress_write` 口径一致；`time -v wall`
还包含原始输入读取和 text wrapper 开销。

| Level | time -v wall (s) | user (s) | sys (s) | CPU | peak RSS (MiB) | gzip raw-write (s) | MiB/s | compressed bytes | ratio | time Δ vs 9 | size Δ vs 9 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 74.65 | 67.34 | 7.28 | 99% | 25.50 | 67.889 | 147.83 | 3,079,468,693 | 3.4174 | -86.78% | +14.45% |
| 3 | 120.26 | 113.16 | 7.05 | 99% | 25.38 | 113.215 | 88.65 | 2,959,259,672 | 3.5562 | -77.95% | +9.98% |
| 6 | 254.59 | 243.51 | 11.04 | 99% | 24.95 | 247.123 | 40.61 | 2,736,612,170 | 3.8456 | -51.87% | +1.70% |
| 9 | 521.09 | 509.36 | 11.65 | 99% | 25.30 | 513.400 | 19.55 | 2,690,755,454 | 3.9111 | 0.00% | 0.00% |

最优两个候选重复一次：level 1 raw-write 为 `68.098 s`（首次 `67.889 s`），
level 3 为 `113.027 s`（首次 `113.215 s`）；重复结果的 compressed bytes
完全相同。

每个候选均重新解压，并逐项验证：

- SHA-256 为 `087dcf1ad1606874f55e6c3b28a0401462641960406631ef6d6742d47ceebfed`；
- bytes 为 `10,523,799,657`；
- rows 为 `46,002,707`；
- header 完全一致；
- level 1、3、6、9 全部通过 byte-level 校验。

### Pareto choice

level 1 是 compressed size 增长不超过 15% 的候选中最快者：增长 **14.446%**，
低于 25% hard limit；raw-write wall 下降 **86.777%**，峰值 RSS 相比 level 9
没有增加超过 5%。level 3 体积更小但明显更慢。因此选择 level 1，而不是仅因
它是最低 level 就直接选择。

## 4. Formal production validation

正式运行使用相同 raw-only pipeline、配置基础、依赖环境和隔离 stage；没有 resume，
没有复用旧 Demand outputs，且使用 `ROADNET_DEMAND_TIMING=1` 和
`/usr/bin/time -v`。Demand stage contract passed。

| Metric | Baseline `demand-prof-2c-2a` | Level 1 `demand-prof-2c-2b-level1` | Change |
| --- | ---: | ---: | ---: |
| Demand stage wall | 3,490.946 s / 58.182 min | 3,050.770 s / 50.846 min | -440.176 s / -12.609% |
| full raw-only pipeline envelope | 4,654.330 s | 4,204.714 s | -449.617 s |
| `/usr/bin/time -v` CPU | 99% | 99% | unchanged |
| peak RSS | 11,641,608 kB / 11.102 GiB | 11,457,552 kB / 10.927 GiB | -1.581% |
| assigned rows | 46,002,707 | 46,002,707 | equal |
| assigned compressed bytes | 2,690,755,454 | 3,079,468,693 | +14.446% |
| compression ratio | 3.9111 | 3.4174 | lower by design |

Formal gates all passed: gzip phase reduction exceeds 35%, Demand wall reduction
exceeds 3 minutes, size growth is below 25%, RSS did not increase, and all output
equivalence checks passed.

### Complete export decomposition

| Phase | Baseline (s) | Level 1 (s) |
| --- | ---: | ---: |
| `export_join_execute` | 0.000 | 0.000 |
| `export_join_fetch` | 71.320 | 70.206 |
| `export_frame_build` | 87.241 | 86.598 |
| `export_datetime_format` | 28.173 | 28.801 |
| `export_other_format` | 0.355 | 0.354 |
| `export_csv_serialize` | 229.496 | 227.973 |
| `export_gzip_compress_write` | 519.311 | 71.082 |
| `export_flush_close` | 0.0003 | 0.0002 |
| eight-phase subtotal | 935.896 | 485.015 |
| `export_wall_s` metric | 935.971 | 485.091 |

The eight-phase subtotal differs from `export_wall_s` by `0.008%` before and
`0.016%` after, both below the 2% requirement. Gzip wall fell by **448.228 s /
86.312%**. The level change did not materially change CSV serialization or datetime
formatting.

The full Demand profile has one known nested timing relationship:
`service_labels_insert` is inside `service_label_compute`. After removing that
`445.974 s` overlap, the level-1 body phase sum is `2,686.919 s` against the
`2,814.252 s` instrumented body wall; unclassified time is `127.333 s / 4.525%`.
This is within the 5% maximum but above the 3% target. The official stage envelope
also includes worker setup/import/contract validation outside `run_from_config`.

## 5. Output equivalence

The new and baseline Demand contracts are exactly equal:

- assigned rows: `46,002,707`;
- OD rows: `19,507,610`;
- clusters: `100`;
- tensor shape: `(13,248, 100, 100)`;
- graph rows: road `265`, POI `730`, distance `584`;
- exclusive rows: `30,651,791`; carpool rows: `15,350,916`.

The assigned-orders gzip streams were decompressed and compared byte-for-byte:

- decompressed bytes: `10,523,799,657`;
- decompressed SHA-256: `087dcf1ad1606874f55e6c3b28a0401462641960406631ef6d6742d47ceebfed`;
- rows/header/column order/schema/service labels: equal.

Cluster OD, OD tensors, cluster index, road/POI/distance edge files and adjacency
arrays, POI features, and POI category mapping are exact-byte equal. Metadata is
equal after normalizing run-owned paths. The gzip container SHA-256 changed
(`7c3b7ba6…` → `8e559b93…`) and its size changed because compression level changed;
this is expected and does not weaken decompressed-content equality.

## 6. Analysis answers

1. In the old CSV/gzip portion, CSV serialization was `229.496 s / 3.825 min`
   and gzip compression/write was `519.311 s / 8.655 min`. Their measured sum is
   `748.807 s`; CSV was 30.64% and gzip 69.36% of that sum. At level 1, the same
   phases are `227.973 s` and `71.082 s`.
2. Datetime formatting was `28.173 s / 0.470 min` before and `28.801 s` after;
   it is not affected by the level change.
3. Gzip compression/write was the dominant removable part: `519.311 → 71.082 s`,
   an `86.312%` reduction.
4. SQLite fetch and Python consumption do not overlap. The path is sequential:
   `fetchmany → DataFrame → datetime/other formatting → CSV/gzip`; there is no
   producer-consumer thread, process, or queue. The formal level-1 fetch total is
   `70.206 s`, followed by frame build `86.598 s`.
5. For 460 full export chunks, first-20 versus last-20 median total times were
   `1.0360 s` versus `1.0663 s` (+2.92%). Full-chunk p50/p95 were `1.0521/1.0965 s`;
   the final 2,707-row tail was `0.0375 s`. There is no material late-run blow-up.
6. The run is primarily CPU-bound: `/usr/bin/time -v` reported 99% CPU. There is
   also a material I/O component from staging SQLite and writing 10.52 GB of
   uncompressed export text, but the level experiment's response is consistent
   with CPU-bound deflate work.
7. The absolute ceiling for removing the baseline gzip phase is `519.311 s /
   8.655 min` from Demand. Level 1 recovered `448.228 s`; its remaining measured
   gzip phase is `71.082 s`. These are measurement ceilings, not additional changes
   made in this batch.

## 7. Follow-up experiments (not implemented)

At most three next experiments are justified:

1. Test a lower-overhead CSV formatting path while preserving exact CSV bytes.
2. Test fetch/serialize/gzip producer-consumer pipelining with strict ordering and
   resource accounting.
3. Test export batch-size sensitivity while holding SQL and output contract fixed.

No follow-up experiment was implemented in Batch 2C-2B.

## 8. Verification record

- targeted gzip tests: passed;
- full test suite: `270 passed, 59 warnings`;
- Demand contract: passed;
- timing-on/off tiny output equivalence: passed;
- run outputs, candidate gzip files, and timing profiles are gitignored and not committed;
- no raw order, driver, coordinate, or output-row content is included here.
