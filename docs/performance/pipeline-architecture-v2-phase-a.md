# RoadNet Pipeline Architecture v2 Phase A

日期：2026-08-03
仓库：`/home/dy/jupyter/workspace/RoadNetCenteredPartition`
Phase A 验证基线：`57047fd`（文档提交前 HEAD）

## 结论与停止边界

Phase A 通过。`parquet_duckdb_v2` 已建立可选的 typed matched/labeled Parquet
checkpoint 边界，并在相同 raw、配置和依赖环境下完成 fresh raw-only Demand
等价验证。正式 v2 run 的 Demand contract、Demand 全部正式文件、解压后的
`orders_region_assigned.csv.gz`、stage_id 顺序和 service label 均与 sqlite_v1
exact；Supply/TTE reader 也从该 compatibility CSV 各运行一次并通过原有 contract。

本阶段明确停止，不进入 Phase B/C/D/E：

- 默认 backend 仍是 `sqlite_v1`；SQLite writer、service-label v1 code 和旧路径均保留。
- Supply/TTE reader 未迁移，仍只读 compatibility CSV。
- `orders_region_assigned.csv.gz` 默认仍生成，public stage contract 未修改。
- 本阶段 v2 不创建 `service_labels` 表、不创建 label index、不执行
  `staged_orders × service_labels` JOIN。
- P2 只验证无副作用 fan-out 原型；生产 Supply/TTE fan-out 未实施。

推荐结论保持审计报告：主方向为 DuckDB + PyArrow 的 Candidate C 实现，长期目标
叠加 Candidate B 的一次 labeled stream；Candidate A 是阶段分离、低风险的迁移路径。
Phase A 只落地了这条边界和 Demand adapter，不把后续目标误报为已完成。

## 提交和实现边界

| commit | 内容 |
| --- | --- |
| `3da6b5c` | 澄清下游 timing：881.93 s 和 653.96 s 是完整 Supply/TTE stage wall，不是纯 CSV reread；CSV parse 只是其中一部分。 |
| `2ebd06a` | 环境定义固定 `duckdb=1.5.5`，增加 Arrow/version smoke test。 |
| `b4d4b15` | 定义并验证 `MatchedOrderCheckpointV1` / `LabeledOrderCheckpointV1`。 |
| `f96b088` | 增加可选 v2 matched/labeled Parquet、DuckDB deterministic sort 和 Python label stream。 |
| `f1ddfba` | 加强 fingerprint、resume、overwrite、失败清理和原子发布。 |
| `57047fd` | 限制 Arrow/OD streaming 内存，移除全量 OD hash group-by 的风险。 |

本报告提交后，文档提交本身是最后一个 Phase A validation boundary。`outputs/**`
下的 benchmark、真实 run 和 helper 均保持 gitignored，不纳入提交。

## DuckDB 运行时

环境定义为 `environment.yml` 中的 `duckdb=1.5.5`；没有升级其他科学依赖。当前
Linux Python 3.11 环境通过 import/version/Arrow batch smoke test。DuckDB 官方
Python distribution 和 conda-forge 提供 Linux/Windows Python wheels/packages；本次
实际执行为 Linux，Windows 只完成 packaging 设计审查，未在本机运行。

正式 v2 参数如下：

| 参数 | 值 |
| --- | --- |
| backend | `parquet_duckdb_v2` |
| DuckDB | `1.5.5` |
| memory limit | `512MB` |
| threads | `1` |
| Arrow batch size | `100,000` |
| target matched/labeled shard rows | `500,000`，但 labeled shard 只在 driver boundary 关闭 |
| temp disk budget | `40 GiB` |
| primary sort key | `driver_id, departure_time_ns, finish_time_ns, stage_id` |
| OD sort key | `slot_start_ns, origin_cluster_id, destination_cluster_id, service_type` |
| compatibility export sort key | `stage_id` |
| temp directory | 当前 run-owned `demand/columnar/duckdb-temp-*`，不使用系统 temp |

`order_staging_backend` 进入 config fingerprint 和 stage manifest。v2 失败时不会
静默 fallback 到 sqlite_v1 并继续报告为成功的 v2 run；backend、DuckDB version、
checkpoint schema/runtime/source fingerprint 不匹配时 resume 会拒绝。

## Checkpoint contract

### Typed schema

两个 checkpoint 都使用 Parquet 2.6 + Zstandard；列顺序、Arrow type 和 nullable
policy 固定如下。`service_type` 只存在于 labeled contract。

| 列 | Arrow type | nullable | 用途 |
| --- | --- | ---: | --- |
| `stage_id` | `int64` | 否 | row identity、完整排序 tie-break、public 首列 |
| `source_file` | `string` | 否 | publication provenance |
| `source_row` | `int64` | 否 | publication provenance |
| `order_id` | `string` | 是 | Supply order provenance/segment order |
| `driver_id` | `string` | 否 | label state、Supply chain |
| `departure_time_ns` | `int64` | 否 | label sort、Supply/TTE start |
| `finish_time_ns` | `int64` | 否 | overlap、Supply/TTE end |
| `slot_start_ns` | `int64` | 否 | Demand OD key |
| `pickup_seg_id` | `string` | 否 | publication provenance |
| `dropoff_seg_id` | `string` | 否 | publication provenance |
| `origin_cluster_id` | `string` | 否 | Demand/Supply/TTE endpoint |
| `destination_cluster_id` | `string` | 否 | Demand/Supply/TTE endpoint |
| `pickup_match_distance_m` | `float64` | 是 | publication provenance |
| `dropoff_match_distance_m` | `float64` | 是 | publication provenance |
| `service_type` | `string` | 否 | labeled contract；值为 `exclusive` 或 `carpool` |

因此：

- `MatchedOrderCheckpointV1` 是前 14 列；`LabeledOrderCheckpointV1` 是前 14 列加
  `service_type`。
- nullable policy 不是推断出来的：只有 `order_id`、两个 match distance 为 nullable；
  完整 sort key 和 label 输出不可为 null。
- invalid/missing time 的过滤沿用 v1；duplicate input rows 不因 `order_id` 被错误
  去重，`stage_id` 仍必须唯一；同一完整 sort key 的顺序由 `stage_id` 决定。

### Durable metadata and publish protocol

每个 manifest 固定包含：contract/schema version、完整 typed columns、nullable
policy、row count、sort key、stage_id uniqueness/validation mode、global ordinal
range、min/max sort key 和 stage_id（值写入 run-owned manifest，本文不复制行级值）、
每个 shard 的 deterministic `shard_id`、ordinal range、row count、file size、SHA-256、
schema fingerprint、source/config/runtime fingerprint、DuckDB version、completed
marker 和 atomic publish 状态。

写入协议为：

1. 写入 run-owned `.partial` Parquet 文件；
2. Parquet schema、size、SHA-256 通过后 atomic rename；
3. 只有完成的 shard 才写入 manifest；
4. 所有 shard 完成后写 `_CHECKPOINT_COMPLETE`，再将 manifest 状态改为
   `completed=true`, `status=complete`, `atomic_publish=complete`；
5. 任意失败清理 `.partial` 和 DuckDB temp，未完成 shard 不可被 resume 发现。

matched shard id 由 source file/chunk index 确定；labeled shard 使用 deterministic
ordinal/shard sequence。Arrow batch 可以跨 driver/component，但
`DriverBoundaryCheckpointWriter` 不会在一个 driver 的 component 中间 finalize
durable shard。component_max_end、component_overlap、current driver 等状态跨 Arrow
batch 保持；只有 driver 结束后才允许写入并发布包含该 driver 的 durable shard。

正式 v2 checkpoint 摘要如下。出于隐私和可复现性边界，表中不记录真实 driver 或
order 的 min/max 值，只记录它们确实被 manifest 记录并校验。

| contract | rows | shard count | shard bytes | global ordinal | stage_id validation | Arrow/Parquet schema fingerprint | publish |
| --- | ---: | ---: | ---: | --- | --- | --- | --- |
| MatchedOrderCheckpointV1 | 46,002,707 | 181 | 1,794,583,335 B | `[0, 46,002,707]` | sequential | `26543c10245a...1901fa1e` | complete |
| LabeledOrderCheckpointV1 | 46,002,707 | 93 | 2,267,525,665 B | `[0, 46,002,707]` | inherited source | `028e790e1b32...f45439` | complete |

两者 checkpoint shard 合计 4,062,109,000 B；含 manifest/marker 的 checkpoint
materialization 为 4,062,331,929 B。matched manifest 和 labeled manifest 的
SHA-256 分别记录为 `c95c56...fa998` 和 `fb6568...ed1d39`。完整值在 run-owned
manifest 中，报告不保存行级数据。

## 字段依赖审计摘要

逐列 reader 追踪的完整矩阵保留在
[pipeline-architecture-v2-audit.md](pipeline-architecture-v2-audit.md)。Phase A
实现没有依据字段名猜测依赖：

| 字段组 | Demand OD | service labeling | Supply | TTE | graph/POI | publication/contract |
| --- | --- | --- | --- | --- | --- | --- |
| `stage_id` | 不参与 OD；保留 identity | 输入/输出 key | 不消费 | 不消费 | 不消费 | 首列、唯一、递增；manifest ordinal/hash |
| `driver_id`, `departure_time_ns`, `finish_time_ns` | 不参与 OD | 完整排序和 state machine | chain、trip/idle、slot expansion | trip time、slot | 不消费 | 14/15 列 contract |
| `slot_start_ns` | OD group key | 不参与 | 不消费，Supply 从 interval 重算 | 不消费，TTE 从 departure 重算 | 不消费 | publication |
| `origin_cluster_id`, `destination_cluster_id` | OD group key | 不参与 | trip endpoint、in-service、fleet | OD endpoint | 不消费 | cluster contract |
| `service_type` | exclusive/carpool accumulator | 生成 | exclusive/carpool branch | 不需要 | 不需要 | assigned/publication |
| `order_id` | 不需要 | 不需要 label state | order_ids/provenance | 不需要 | 不需要 | publication/contract |
| `source_file`, `source_row`, segment ids、match distances | 不需要 | 不需要 | 不需要 | 不需要 | 不消费 order rows | 当前 publication 无损再生、manifest/hash |

计算上的最小 pre-label 集合是完整排序 key 加
`slot_start_ns`, origin/destination 和 Supply 的 `order_id`；但为了保持当前
publication CSV 无损再生，durable matched checkpoint 保留全部 14 列。label stream
必须保留 driver 边界、half-open overlap、component_max_end、component_overlap、
stage_id tie-break、null/duplicate/invalid-time 语义，不能在 batch 边界丢失。

TTE 当前 reader 的真实 `usecols` 只有 departure、finish、origin、destination；
它不需要 `service_type`。Supply 当前真实 `usecols` 是 order_id、driver_id、
departure、finish、origin、destination、service_type；它需要全部有效订单行，但不需
要 publication-only provenance 列。graph/POI outputs 使用 partition、relation edges
和 POI，不读取 assigned order rows。

## 当前 materialization ledger

以下以 Phase A fresh v1 run 和既有正式 profile 为证据；logical bytes 与 physical
bytes 分开，SQLite 细粒度 page/index payload 没有被伪装成精确值。

| 步骤 | rows / columns | logical / physical bytes | read / write | wall / RSS | sort / access | contract |
| --- | --- | --- | --- | --- | --- | --- |
| raw CSV read | 90,105,866 / selected raw columns | raw source 12,025,899,151 B | 1 sequential parse | `csv_parse` 126.37 s；空间 matching 仍是主要前置成本 | 无排序、顺序读 | external input/manifest |
| matched DataFrame | 46,002,707 / 14 | chunk transient，无 durable file | 181 chunk writes in v1 | v1 `sqlite_append` 135.67 s | 无随机访问 | implementation detail |
| SQLite `staged_orders` | 46,002,707 / 14 | physical peak 12,700,127,232 B；含 table/index/journal effects | 181 append、1 commit；后续多次全量 pass | index build 后共享 Demand peak 11.06 GiB | B-tree indexed access | v1 internal checkpoint |
| staged indexes | same / driver-time + slot-OD | included above | `idx_staged_driver_time` 36.07 s；`idx_staged_slot_od` 33.45 s | shared peak | ordered label scan / OD grouping | implementation detail |
| ordered SELECT | 46,002,707 / label state projection | not isolated | 1 ordered scan | `service_label_compute` 505.29 s | exact driver/time/stage order | label correctness |
| `service_labels` table | 46,002,707 / 2 | projection; exact physical not isolated | 4,601 insert batches | `service_labels_insert` 467.04 s | later PK lookup | v1 only; v2 absent |
| label index | same / `service_type` | exact physical not isolated | 1 index build | 15.16 s | no current reader depends on it | v1 unused implementation |
| staged × label JOIN | 46,002,707 / 15 projection | no durable file | export and OD each consume joined rows | `export_join_fetch` 71.20 s；OD fetch 73.62 s | stage_id lookup / OD group | v1 only; v2 absent |
| assigned CSV serialization | 46,002,707 / 15 | 10,523,799,657 B uncompressed；3,079,468,693 B gzip level 1 | 1 write | CSV 237.35 s；gzip 72.17 s；full export profile 496.89 s | stage_id order required | public compatibility output |
| Supply | 46,002,707 / 7 read columns | one 3,079,468,693 B gzip container; 10.52 GB decompressed payload | 1 parse + full stage work | baseline 881.93 s is complete stage wall, not pure reread | downstream driver/slot computation | current stage input |
| TTE | 46,002,707 / 4 read columns | same CSV container/payload | 1 parse + full stage work | baseline 653.96 s is complete stage wall, not pure reread | downstream matrix/imputation | current stage input |
| downstream materialization | OD 19,507,610 rows; Supply tables; TTE matrices | OD CSV 620,431,626 B; tensors/Parquet separate | formal output writes | included in stage wall | aggregators/matrix fills | public contracts |

按 logical pass 计算，同一订单在 v1 至少经历 raw parse 1 次、staged durable write
1 次、staged full traversal 3 次（label、export、OD，另有 bounds work）、label
projection write 1 次、label reads 2 次、assigned CSV write 1 次、Supply/TTE CSV
parse 2 次。SQLite profile 的 process/page/index amplification 使实际 disk write
高于简单 row payload 乘法。

## 候选架构和 Phase A 落地范围

| 候选 | 结论 |
| --- | --- |
| A: Parquet staging、阶段分离 | Phase A 已落地的最小迁移边界；Demand 仍从 labeled checkpoint 独立读取。 |
| B: labeled stream fan-out | P2 synthetic prototype 已验证 batch/state/aggregate 方向；生产 fan-out 未实施。 |
| C: DuckDB/Arrow hybrid | 推荐主引擎；DuckDB 负责 typed shard external ORDER BY，Arrow 负责 RecordBatch，Python 保留跨行 label state。 |

全局 driver/time 排序对当前 raw chunk 输入仍不可省略：必须保证每个 driver 连续，
并使用完整 `driver_id, departure_time_ns, finish_time_ns, stage_id`。DuckDB SQL 可以
安全承担 typed scan、projection、null/time filter、external sort，以及按 OD key 的
整数聚合；不能把 component overlap、half-open interval、driver boundary 和
跨 batch 状态机全部下推为 SQL。TTE 的 median/imputation 也仍保留 Python/NumPy
contract 路径。

SQLite 必须保留的语义是 durable intermediate、transaction boundary、run ownership、
resume/overwrite、完整 deterministic order 和 schema/null/duplicate 校验；SQLite
的 label table、label index、row-addressable JOIN 和 page cache 不是 v2 必需能力。

## P0/P1/P2 prototype benchmark

输入由生产聚合统计构造，保留真实 driver skew、exclusive/carpool 比例、driver
内重复时间和 tie-break，不记录真实 ID 或真实行。`bytes written` 是 prototype
process profile；`input MiB` 是 typed input 的 physical bytes。prototype 的
`/proc/self/io bytes_read` 在该环境为 0，不能解释成没有读盘，所以表使用
`input physical bytes`。

| rows | backend | wall s | user/sys s | CPU % | RSS GiB | input MiB | written MiB | temp spill MiB | checkpoint MiB | rows/s | sort s | label s | output s |
|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100,000 | P0 | 3.91 | 3.62/0.39 | 102.72 | 0.41 | 4.24 | 29.54 | 0.00 | 5.85 | 25,594 | 0.11 | 0.47 | 1.50 |
| 100,000 | P1 | 3.43 | 3.15/0.38 | 103.08 | 0.36 | 4.24 | 5.12 | 0.00 | 5.12 | 29,175 | 0.06 | 0.43 | 0.07 |
| 100,000 | P2 | 2.81 | 2.50/0.33 | 100.39 | 0.29 | 4.24 | 5.12 | 0.00 | 5.12 | 35,564 | 0.06 | 0.71 | 0.06 |
| 1,000,000 | P0 | 50.27 | 42.49/8.14 | 100.71 | 1.24 | 43.72 | 295.95 | 0.00 | 59.29 | 19,892 | 1.36 | 6.34 | 16.26 |
| 1,000,000 | P1 | 34.55 | 31.67/3.40 | 101.53 | 1.22 | 43.72 | 52.22 | 0.00 | 52.22 | 28,945 | 0.66 | 4.24 | 0.65 |
| 1,000,000 | P2 | 29.08 | 26.09/3.04 | 100.20 | 0.99 | 43.72 | 52.22 | 0.00 | 52.22 | 34,391 | 0.64 | 7.88 | 0.65 |
| 5,000,000 | P0 | 256.50 | 207.90/50.31 | 100.67 | 3.96 | 219.63 | 1,592.85 | 0.00 | 298.76 | 19,493 | 7.32 | 35.86 | 82.74 |
| 5,000,000 | P1 | 168.66 | 155.39/15.88 | 101.54 | 4.09 | 219.63 | 1,091.31 | 804.66 | 263.36 | 29,645 | 4.51 | 20.99 | 3.25 |
| 5,000,000 | P2 | 145.79 | 132.20/14.02 | 100.30 | 3.45 | 219.63 | 1,081.16 | 793.97 | 263.36 | 34,296 | 4.57 | 40.36 | 3.26 |
| 10,000,000 | P0 | 511.26 | 408.84/105.80 | 100.66 | 6.52 | 443.29 | 3,531.11 | 0.00 | 598.85 | 19,560 | 15.61 | 74.82 | 166.79 |
| 10,000,000 | P1 | 335.64 | 312.41/28.35 | 101.53 | 6.83 | 443.29 | 2,927.60 | 1,891.41 | 528.17 | 29,794 | 9.35 | 43.94 | 6.61 |
| 10,000,000 | P2 | 290.44 | 264.21/27.14 | 100.31 | 5.80 | 443.29 | 2,940.95 | 1,909.03 | 528.17 | 34,430 | 9.12 | 85.00 | 6.59 |

P0 是 SQLite table + label insert + JOIN/export；P1 是 Parquet + DuckDB sort + Python
label stream + labeled checkpoint；P2 在同一 labeled stream 上增加至少两个无副作用
Demand/Supply/TTE projection aggregators。100k、1M、5M、10M 的所有 equivalence
checks 均通过：sorted stage_id sequence、stage_id label pair、service labels、
exclusive/carpool、Demand/Supply/TTE aggregates、row count、schema/dtype、null、
duplicate、tie-break、driver/component 跨 batch。

P2 证明的是 stream wiring 和字段/聚合等价，不是生产 Supply chain 或 TTE
median/imputation 已经改写；因此不能用 P2 的局部倍数预测完整 pipeline。

## Production raw-only validation

两条正式路径都从同一 raw CSV fresh 开始，未 resume、未复用历史 staged/assigned
output；空间 matching、chunk size、stage_id 分配、segment mapping 和 tie-break 未改。

### Wall and resource

| run | external wall | internal Demand stage wall | user/sys s | max RSS | SQLite | temp/spill peak | checkpoint |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v1 `sqlite_v1` | 3,100.62 s | 2,855.32 s | 2,641.08 / 451.81 | 11.06 GiB | 12,700,127,232 B peak | n/a | SQLite staged |
| v2 `parquet_duckdb_v2`, export=true | 2,979.05 s | 2,723.22 s | 2,914.73 / 64.37 | 4.47 GiB | none | 11,041,865,728 B spill peak | 4,062,331,929 B |
| v2 experiment, export=false | 2,265.48 s | 2,263.52 s | 2,232.00 / 41.11 | 2.76 GiB | none | 11,521,622,016 B spill peak | 4,062,331,929 B |

`external wall` 是 `/usr/bin/time -v` 的 raw-only command wall；`internal stage wall`
是 Demand profile 的 stage timer。正式 v1/v2 on 均通过 stage executor，因此正式 gate
使用两者同形的 external wall：v2 比 v1 快 121.57 s（-3.92%）；internal stage wall
快 132.10 s（-4.63%）。两者都没有超过 5% 回退门。v2 export=false 是 standalone
实验，不能和 stage-executor external wall 直接混作正式 gate；其 core stage 比
export=true 少 459.70 s，差值用于量化 exporter，不用于改变 public behavior。

`/usr/bin/time -v` 的 file-system input/output 是 cache-和进程树相关的物理计数，
不等同于逻辑 reread：v1 为 7,614,320/48,870,024 KiB，v2 on 为
24,417,048/91,323,904 KiB，v2 off 为 852,104/65,806,280 KiB。v2 profile 同时记录
process bytes written：on 为 46,757,355,520 B，off 为 33,692,594,176 B。

### v2 phase profile

| phase | export=true | export=false |
| --- | ---: | ---: |
| existing spatial matching + matched writer block | 1,563.42 s | 1,528.89 s |
| actual matched Parquet serialization phase | 50.59 s | 49.93 s |
| primary DuckDB external sort | 537.62 s；spill 10,226,237,440 B | 516.07 s；spill 11,521,622,016 B |
| Python service-label state | 156.41 s | 151.71 s |
| labeled Parquet writes | 213.46 s | 203.08 s |
| OD external sort | 189.58 s；spill 11,041,865,728 B | 181.61 s；spill 11,038,752,768 B |
| OD stream/CSV/tensor block | 191.15 s | 183.18 s |
| stage_id external reorder | 393.91 s；spill 9,040,330,752 B | not run |
| compatibility export block | 368.25 s | not run |
| CSV serialization / gzip sub-phases | 245.70 / 72.11 s | not run |

`stage_id_reorder_wall` 外层为 395.54 s，包含内部 compatibility export block；这些
数字不能相加。正式 compatibility exporter 明确对 labeled checkpoint 再做完整
`ORDER BY stage_id`，然后才执行原有 datetime/CSV/gzip serialization；没有直接按
driver/time labeled stream 写 public CSV。

### Exact-output gate

v1 和 v2 on 的结果：

- raw rows 90,105,866；matched/staged rows 46,002,707；exclusive 30,651,791；
  carpool 15,350,916；clusters 100；tensor shape `[13248, 100, 100]`；OD rows
  19,507,610。
- 14-column matched public digest：`c48ed7b0542b...e081fad3`，相同。
- sorted stage_id sequence digest：`3d42ce3efbbb...6a5369e3`，相同。
- stage_id→service digest：`08ac9c4fed09...c3872a7f8`，相同。
- assigned CSV 解压 payload SHA-256：`087dcf1ad160...ceebfed`，相同；压缩文件大小
  均为 3,079,468,693 B。
- cluster OD CSV SHA-256：`45b8ba6b8cf3...d27b1a`，相同。
- tensor、cluster index、road/POI/distance graph edges and adjacency、POI features
  和 category mapping 均 exact；14 个 formal artifacts 的 comparator 全部 passed。
- metadata 在 run path 归一化后 exact；绝对 run output path 的差异不属于 public
  contract。
- v2 matched/labeled manifest 标为 complete，backend 为 `parquet_duckdb_v2`，无
  `orders_region_staging.sqlite`，成功后 run-owned DuckDB temp 已清理。

export=false 额外验证了不写 assigned CSV 时，OD、tensor、cluster、graph/POI 等非
CSV Demand artifacts exact；metadata/output allowlist 会因实验 flag 和缺少 assigned
CSV 有意不同。因此 export=false 不进入 Phase A formal output-equivalence gate。

### Downstream compatibility

现有 Supply/TTE reader 未修改，均从 v2 on 的 compatibility CSV fresh 运行：

| stage | wall | user/sys s | max RSS | contract metrics |
| --- | ---: | ---: | ---: | --- |
| Supply | 977.28 s（`/usr/bin/time` 978.83 s） | 927.00 / 52.32 | 11.23 GiB | 46,002,707 orders；199,668 drivers；13,248 slots；41,147,628 in-service；available/fleet 各 1,324,800 |
| TTE | 702.48 s（`/usr/bin/time` 704.28 s） | 828.11 / 129.98 | 11.15 GiB | shape `[13248,10000]`；observed 19,343,240；inferred 84,241,693；missing 28,895,067 |

这里的 977.28 s 和 702.48 s 同样是完整 stage wall，包含 CSV parse、reader 的
DataFrame/chain 或 TTE matrix/imputation work；不能标成纯 “Supply reread” 或纯
“TTE reread”。它们证明 Phase A compatibility CSV 没有让现有下游 reader 回退，
但不是 Supply/TTE Parquet migration benchmark。

## 收益判断与冻结 forecast

SQLite 是主要的可避免 materialization 架构瓶颈，但不是完整 Demand wall 的唯一
瓶颈：spatial matching 本身仍占大量时间，CSV export 和下游 stage compute 也仍在。
Phase A 只证明边界正确和资源门通过，不据此重新估计审计 forecast。

已冻结的完整 pipeline conservative/expected/optimistic forecast 保持原审计值：

| 档位 | Demand | Supply | TTE | total | peak RSS | total bytes written | total bytes reread | temp disk |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| conservative | 2,850 s / 47.5 min | 850 s / 14.2 min | 640 s / 10.7 min | 4,340 s / 72.3 min（约 -5.4%） | 约 11.5 GiB | 约 18–24 GB | 约 20–27 GB logical | 8–12 GB |
| expected | 2,450 s / 40.8 min | 760 s / 12.7 min | 600 s / 10.0 min | 3,810 s / 63.5 min（约 -16.9%） | 约 9–10.5 GiB | 约 10–16 GB | 约 5–10 GB | 10–16 GB |
| optimistic | 2,200 s / 36.7 min | 650 s / 10.8 min | 520 s / 8.7 min | 3,370 s / 56.2 min（约 -26.5%） | 约 7–9 GiB | 约 6–10 GB | 约 2–5 GB | 12–20 GB |

归因不变：

1. 取消 v1 `service_labels` insert 可避免当前 467.04 s 的写入路径，但 label state、
   Parquet writer 和 manifest hash 仍有成本。
2. 取消 label index 可避免当前 15.16 s build；该 index 没有被当前 reader 使用。
3. 取消 JOIN 可避免当前约 71.20 s export label fetch 和相关 row lookup；不能把
   整个 156.8 s frame/fetch 视为纯 JOIN savings。
4. 取消 CSV 内部通信可避免 10,523,799,657 B text serialization、3,079,468,693 B
   gzip container 和 Supply/TTE 两次 CSV parse；Phase A export=true 仍支付兼容
   exporter 成本。
5. Supply/TTE parse savings 只能节省完整 stage 的一部分；其 chain、matrix、
   median/imputation work 仍存在。
6. Parquet write、DuckDB external sort、spill、hash 和 checkpoint 是新增成本；正式
   v2 已实测 checkpoint 4.062 GB、spill 峰值 11.04 GB，均低于 40 GiB 门。
7. Fan-out 会增加每个 reducer 的 compact state 和耦合；P2 证明 wiring，不证明生产
   Supply/TTE 算法的内存上界。

## 引擎选择

| 引擎 | external sort/spill | RecordBatch | deterministic | Python copy | dependency/package | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| DuckDB | 原生 external ORDER BY，`temp_directory` 可显式绑定 | `to_arrow_reader` | 完整显式 key；不依赖 stable sort | batch-level Arrow→pandas，可控 | 1.5.5 fixed；Linux/Windows package | 主引擎 |
| PyArrow dataset | typed scan/write 强 | 原生 batch | scan 不替代 global sort | 由 consumer 决定 | 已有依赖，跨平台 | checkpoint/transport layer |
| Polars lazy | 版本/配置相关，需另测 spill contract | 支持 streaming 变体 | 必须自带完整 tie-break | 可能省 pandas copy | 新依赖和 packaging 变更 | 不采用 |
| SQLite v1 | B-tree/temp store，不是本方案的 external columnar sort | 无 Arrow stream | 当前 oracle，成熟 | Python cursor/DataFrame | 已有、跨平台 | 默认 backend/oracle |

选择 DuckDB + PyArrow 不是因为流行，而是因为 Phase A 已实测 external spill、显式
temp ownership、Arrow RecordBatch 和 deterministic full-key sort；SQLite v1 保留为
正确性 oracle 和 rollback path。

## Contracts、resume 和迁移兼容性

| 类型 | Phase A 状态 |
| --- | --- |
| internal contract change | 新增 matched/labeled Parquet checkpoint、manifest 和 backend binding。 |
| public contract change | 无；Demand/Supply/TTE outputs、assigned CSV schema/order、stage manifest contract 不变。 |
| compatibility adapter | labeled checkpoint → stage_id external reorder → v1-compatible CSV；Phase A 默认开启。 |
| deprecated path | 无路径在本阶段删除；sqlite_v1、service_labels v1 和 CSV 都保留。未来可在后续 phase 逐步降级为 oracle/optional。 |

run ownership、transactional publish、raw-only fresh run、resume、overwrite 和
failed-run recovery 沿用 stage executor。resume 需要 backend、DuckDB version、config、
source、runtime、matched→labeled manifest hash 和 schema fingerprint exact；overwrite
只使当前 owned stage 失效并重建；成功后删 DuckDB temp，保留已完成 checkpoint 和
manifest；失败保留失败 manifest/evidence，但清理 `.partial`/temp，避免半完成文件
被发现。

Phase A 的迁移状态：

| phase | 状态 | output/performance gate | rollback |
| --- | --- | --- | --- |
| A | 已完成 | v1/v2 formal outputs exact；Demand external wall -3.92%；RSS 下降；temp <40 GiB；full production raw-only validation passed | `sqlite_v1` 默认路径，回到 `57047fd` |
| B | 未开始 | Supply labeled Parquet 与 CSV 双读 exact；完整 Supply stage wall 和 RSS gate | 保留 CSV reader |
| C | 未开始 | TTE labeled projection 与 CSV exact；matrix/imputation contract gate | 保留 CSV reader |
| D | 未开始 | 一次 labeled stream fan-out；Demand/Supply/TTE full aggregate/output exact；state/RSS gate | 继续独立 checkpoint readers |
| E | 未开始 | CSV 只作显式 publication export；public compatibility gate 仍可按需生成 | 恢复 export=true |

每个后续 phase 必须单独 commit、full production validation、output-equivalence gate、
performance gate、rollback point 和 disk cleanup rule；本次没有开始 B/C/D/E。

## Tests and failure evidence

完整测试：`288 passed, 77 warnings, 75.76s`。重点覆盖包括：

- DuckDB 1.5.5 import/version/Arrow smoke；14-column matched schema、typed/null policy、
  invalid time、duplicate row、完整 tie-break；
- driver/component 跨 Arrow batch、driver-boundary durable shard、interrupted `.partial`
  不可见、atomic rename、wrong schema/version/hash/fingerprint、incomplete marker；
- v1/v2 backend binding、resume、overwrite、wrong DuckDB version、failure cleanup、
  no silent fallback；
- v2 label digest 与 v1 exact、stage_id compatibility reorder、解压 assigned bytes exact；
- Demand tiny/zero-match equivalence、OD/tensor/graph/POI contract；Supply/TTE/全仓库
  regression。

真实 validation 中有一条早期 v2 run 因旧 OD hash group-by 在 512 MB DuckDB limit 下
OOM；该 run 被标记 failed，不进入 equivalence 结果。随后改为 external OD sort +
Arrow/Python streaming accumulator，fresh rerun 通过并清理 temp。这是失败恢复证据，
不是静默 fallback。

## Final checks

- 只读正式仓库 `/home/dy/jupyter/workspace/RoadNetCenteredPartition`；
- `.qoder/settings.local.json` 未读取、未修改；
- 受保护的另一仓库未读取、未修改、未同步、未维护；
- prototype/run outputs 仍在 gitignored `outputs/`；本次只提交本报告和 JSON；
- 提交前执行 privacy scan、tracked-file size scan、`git diff --check` 和 worktree check；
- Phase A 完成后停止，不执行后续架构迁移。
