# Demand assigned-orders export profile v1 — Performance Batch 2C-2A

**日期：** 2026-08-01

**状态：** 完成 measurement-only；未实施性能优化。

**仓库：** `/home/dy/jupyter/workspace/RoadNetCenteredPartition`

**profiling infrastructure commit：** `b79a752 RoadNet: refine Demand export profiling`
**正式 run-id：** `demand-prof-2c-2a`

## 1. Scope and invariants

The export still uses the original SQL text, 100,000-row export chunk size,
`.csv.gz` output, gzip default compression level, UTF-8 encoding, empty newline
translation, header/column order, timestamp format, float formatting, file
name, manifest, and Demand contract. `ROADNET_DEMAND_TIMING` remains opt-in;
the default-off path retains `pandas.read_sql_query` and `gzip.open`.

When timing is enabled, the same SQL result semantics are split into
`cursor.execute`, raw `fetchmany`, and `DataFrame.from_records`. A gzip file
object wrapper measures `GzipFile.write` without changing the text encoding or
compression parameters. `export_flush_close` includes the file open and final
text/gzip flush-close lifecycle; its time is negligible here.

The timing profile stores aggregate phase durations, chunk indexes/row counts,
byte counts, and process I/O only. It stores no order, driver, coordinate, or
output-row content.

## 2. Formal run

The command used the same raw-only configuration and environment as
`demand-prof-2c1`, with a fresh run directory, no resume, and isolated stages:

```text
ROADNET_DEMAND_TIMING=1 /usr/bin/time -v \
  conda run --no-capture-output --prefix \
  /home/dy/jupyter/workspace/RoadNetCenteredPartition-cleanroom/.conda/dydl \
  python -m roadnet_partition run \
  --config configs/pipelines/full.yaml \
  --run-id demand-prof-2c-2a --to-stage demand --isolate-stages
```

The run exited 0 and the Demand contract passed: 46,002,707 assigned rows,
19,507,610 OD rows, 100 clusters, and tensor shape `(13248, 100, 100)`.

| Metric | Measured value |
| --- | ---: |
| Baseline `demand-prof-2c1` Demand wall | 3,469.510 s / 57.825 min |
| Official Demand stage envelope | 3,490.946 s / 58.182 min |
| Instrumented `run_from_config` body wall | 3,255.515 s / 54.259 min |
| Full raw-only pipeline to Demand wall (`/usr/bin/time -v`) | 4,666 s / 77.767 min |
| User / system time | 4,220.43 s / 439.42 s |
| CPU utilization | 99% |
| Peak RSS | 11,641,608 kB = 11.102 GiB |
| Demand SQLite peak file | 12,700,127,232 bytes |
| Demand process I/O | read 328,019,968 bytes; write 29,300,445,184 bytes |

### Completeness boundary

The raw profile phase totals are inclusive for legacy phases. In particular,
`service_label_compute` contains the nested `service_labels_insert` calls;
counting both as independent wall time double-counts 440.126 s. For the
non-overlapping body calculation:

| Quantity | Seconds | Interpretation |
| --- | ---: | --- |
| Inclusive profile phase sum | 3,571.488 | Raw profile total; includes the known nested insert overlap |
| Exclusive body phase sum | 3,131.362 | Inclusive sum minus nested `service_labels_insert` |
| Body unclassified | 124.152 / 3.814% | Below the 5% maximum; slightly above the 3% target |
| Official-envelope residual | 359.584 / 10.300% | Includes 235.431 s outside `run_from_config` (worker setup/import/contract validation) plus the body residual |

Thus the body-level timing gate is within the allowed 5% maximum. The
official envelope residual is explicitly identified as a timing-scope gap, not
assigned to SQL, export, or an optimization hypothesis.

## 3. Assigned-orders export decomposition

The export had 461 data chunks (`460 × 100,000 + 2,707` rows); fetch was called
462 times because the terminating empty fetch is also measured.

| Phase | Calls | Total (s) | Total (min) | p50 (s) | p95 (s) | Max (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `export_join_execute` | 1 | 0.000165 | 0.000003 | 0.000165 | 0.000165 | 0.000165 |
| `export_join_fetch` | 462 | 71.319684 | 1.188661 | 0.1552 | 0.1607 | 0.1836 |
| `export_frame_build` | 461 | 87.240571 | 1.454010 | 0.1874 | 0.2039 | 0.2130 |
| `export_datetime_format` | 461 | 28.173338 | 0.469556 | 0.0609 | 0.0642 | 0.0669 |
| `export_other_format` | 461 | 0.354948 | 0.005916 | 0.0008 | 0.0009 | 0.0011 |
| `export_csv_serialize` | 461 | 229.496396 | 3.824940 | 0.4996 | 0.5199 | 0.5393 |
| `export_gzip_compress_write` | 462 | 519.310730 | 8.655179 | 1.1294 | 1.1692 | 1.1990 |
| `export_flush_close` | 2 | 0.000284 | 0.000005 | 0.0001 | 0.0002 | 0.0002 |
| **phase sum** |  | **935.896116** | **15.598269** |  |  |  |

The measured export wall metric was **935.970939 s / 15.599516 min**. The phase
sum differs by 0.074823 s (**0.008%**), well within the required 2%.

Against the original `demand-prof-2c1` export total
(`export_join_fetch + export_csv_gzip = 923.861162 s`), the new total differs
by **1.311%**. The formal output had 10,523,799,657 uncompressed bytes,
2,690,755,454 compressed bytes, and an uncompressed/compressed ratio of
**3.9111**.

### Answers to the analysis questions

1. The old ~12.7-minute combined CSV/gzip phase is now measured as **3.825 min
   CSV serialization**, **0.470 min datetime formatting**, **0.006 min other
   formatting**, and **8.655 min gzip compression/write**. The CSV serializer is
   about 30.1% of the old combined phase; gzip is about 68.1%.
2. SQLite fetch and Python consumption do **not** overlap: the path is
   sequential `fetchmany → DataFrame → formatting → CSV/gzip`, with no producer,
   consumer, thread, or process. Fetch alone is 71.320 s; frame construction is
   87.241 s.
3. For the 460 full-size chunks, the first-20 versus last-20 median total
   export-chunk time is 2.0181 s versus 2.0519 s (1.68%); full-chunk p95/p50 is
   1.020. There is no material progress-related blow-up. The final 2,707-row
   tail is 0.0689 s and is size-driven.
4. The run is primarily **CPU-bound with a material I/O component**: total
   `/usr/bin/time -v` CPU utilization was 99%, while the export wrote 10.52 GB
   uncompressed text and 2.69 GB compressed output; staging also produced about
   29.3 GB of process writes.
5. The theoretical ceiling for eliminating the entire assigned-orders export
   is **935.971 s / 15.600 min** of the measured Demand body. For only the
   formatting plus CSV/gzip work, the measured ceiling is **777.336 s / 12.956
   min**. These are ceilings, not proposed changes.

## 4. Synthetic scaling microbenchmark

Synthetic rows used the production export schema and generated identifiers,
timestamps, cluster labels, segment labels, and distances; no real order or
driver data was used. Export chunk size remained 100,000. Each scale used one
warm-up and five measured runs; values below are medians, with process peak RSS
observed across the runs.

| Rows | Datetime (s) | CSV serialization (s) | Gzip/write (s) | Peak RSS (MiB) | Uncompressed bytes | Compressed bytes | Ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | 0.0713 | 0.3732 | 0.6999 | 324.7 | 18,869,228 | 2,299,468 | 8.2059 |
| 500,000 | 0.3192 | 1.8891 | 3.4656 | 445.4 | 95,678,562 | 11,519,867 | 8.3055 |
| 1,000,000 | 0.6196 | 3.8030 | 6.8937 | 449.8 | 191,690,229 | 23,044,856 | 8.3181 |

The synthetic result scales close to linearly at the fixed export chunk size,
with gzip/write dominating CSV serialization. It is a scaling probe only and
does not replace the formal production run.

## 5. Output equivalence

Compared with `demand-prof-2c1`:

- assigned-orders decompressed SHA-256, byte count, header, schema, column
  order, row count, and service-label content: **equal**;
- assigned-orders rows: **46,002,707** in both runs;
- cluster OD CSV: **equal**;
- OD tensor arrays, shapes, dtypes, and values: **equal**;
- cluster index, road/POI/distance graph edges and adjacency arrays, POI
  features, and POI category mapping: **equal**;
- normalized metadata: **equal** after replacing run-owned paths;
- gzip container bytes were not required to be identical and no gzip parameter
  was changed.

## 6. Follow-up experiments (not implemented)

At most three follow-ups are warranted:

1. Keep the exact CSV contract while testing a lower-overhead formatting path;
   the current CSV serialization is 3.825 min.
2. Test gzip compression levels separately, recording compressed size and
   decompressed equivalence; gzip/write is 8.655 min.
3. Test a producer-consumer fetch/serialize/gzip pipeline, with strict output
   equivalence and resource measurements; the current sequential fetch is
   1.189 min and has no overlap.

No follow-up optimization, CSV-writer replacement, gzip-level change, batch-size
change, query/index change, or parallelism was implemented in Batch 2C-2A.

## 7. Verification

- `252 passed, 59 warnings` from the full test suite;
- tiny timing off/on output equivalence passed;
- privacy scan passed: timing profile contains no order, driver, coordinate, or
  output-row content;
- `git diff --check` passed;
- formal Demand contract passed;
- run outputs and timing profiles remain gitignored and uncommitted.
