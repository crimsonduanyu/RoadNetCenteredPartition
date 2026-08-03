# RoadNet Pipeline Architecture v2 Audit

日期：2026-08-03
仓库：/home/dy/jupyter/workspace/RoadNetCenteredPartition
审计范围：只读代码审计、正式 profile 复核、gitignored prototype；不改生产 pipeline、正式 config、默认输出或 stage contract。

## 结论先行

推荐的 v2 目标形态是 Candidate C 的 DuckDB/Arrow 实现，加上 Candidate B 的一次
labeled-order stream；Candidate A 是最小风险的迁移路径。

- SQLite 不应继续承担 46,002,707 行的主 staging/materialization 层。它的持久 checkpoint、
  事务、resume、确定性排序和合同校验职责必须保留，但可以由 typed Parquet、run manifest、
  原子发布和 DuckDB external sort 组合承担。
- v2 可以取消 service_labels 表、service_labels index，以及 staged_orders × service_labels
  JOIN。标签在有序记录流中生成时直接附加到当前 component/batch。
- orders_region_assigned.csv.gz 应降为 optional publication/compatibility export；Supply、
  TTE 的内部默认输入应改为 labeled Parquet 或同一次 labeled stream。
- Demand、Supply、TTE 可以共享一次 labeled stream。Demand 和 Supply 需要 service_type；
  TTE 不需要 service_type，但可以消费同一流的四列 projection。
- 全局的时间排序不能直接删除。现有 service-label state machine 要求每个 driver 的记录
  连续，并按 driver_id, departure_time_ns, finish_time_ns, stage_id 排序。对任意乱序 raw
  chunks，这意味着 external sort，或等价的完整 driver 分区加确定性 merge。
- 主引擎推荐 DuckDB + PyArrow；备选和正确性 oracle 保留当前 SQLite v1 path。

本结论不是生产实施批准。当前默认行为、正式输出和 v1 pipeline 均保持不变。

## 1. 审计控制与正式基线

开始检查确认：

| 检查 | 结果 |
| --- | --- |
| pwd | /home/dy/jupyter/workspace/RoadNetCenteredPartition |
| git 根目录 | /home/dy/jupyter/workspace/RoadNetCenteredPartition |
| 起始 HEAD | 674ea08 |
| 起始工作树 | clean |
| sanitized 目录 | 未读取、未修改、未同步、未维护 |
| .qoder/settings.local.json | 未读取、未修改 |
| prototype | outputs/perf-architecture-v2/，由 outputs/** gitignore |

正式 profile 使用的是当前已验证的 level-1 assigned-orders export：

| 指标 | 正式值 |
| --- | ---: |
| raw rows | 90,105,866 |
| staged/matched rows | 46,002,707 |
| drivers | 199,668 |
| exclusive / carpool | 30,651,791 / 15,350,916 |
| Demand wall | 3,050.770 s / 50.846 min |
| Supply wall | 881.93 s / 14.699 min |
| TTE wall | 653.96 s / 10.899 min |
| Demand + Supply + TTE | 4,586.660 s / 76.444 min |
| Demand peak RSS | 11.06 GiB |
| SQLite peak file | 12,700,127,232 B |
| assigned CSV uncompressed | 10,523,799,657 B |
| assigned CSV gzip level 1 | 3,079,468,693 B |
| Demand process I/O write | 26,637,164,544 B |

这些 profile 将空间匹配、SQLite materialization、labeling、export 和下游 compute 混在
同一个正式阶段中；因此本报告同时给出代码级数据流账本和单独的 P0/P1/P2 原型测量。

## 2. 代码级字段依赖审计

追踪入口和 reader：

- Demand staging、labeling、export 和 OD：src/roadnet_partition/downstream/demand.py:305-648。
- Supply reader：src/roadnet_partition/downstream/supply.py:29-76；后续 chain/slot 逻辑
  在 supply.py:116-534。
- TTE reader 和 raw matrix：src/roadnet_partition/downstream/tte.py:550-610、
  tte.py:457-524。
- assigned contract：src/roadnet_partition/downstream/demand_contracts.py:12-117。
- stage bindings/manifests：src/roadnet_partition/pipeline/stages.py:37-49, 147-187,
  232-277。
- publication inventory：src/roadnet_partition/pipeline/publishing.py:35-57, 74-108。
- tests：test_order_region_pipeline.py、test_demand_tiny_equivalence.py、
  test_demand_contracts.py、test_supply_pipeline.py、test_supply_migration.py、
  test_tte_dataset.py、test_tte_migration.py、test_phase6a_cli_e2e.py、
  test_phase7_release.py。

标记含义：R=读取/消费，W=生成，C=合同或校验，P=publication，M=manifest，
T=test。图/POI 的订单行列均为 N/A：build_cluster_poi_graph 读的是 POI 文件和
partition；road/distance graph 读 relation edges、partition 和 cluster index。

### 2.1 staged_orders 和 assigned orders 逐列矩阵

| 字段 | Demand OD | label state | Supply | TTE | graph/POI | publication CSV | contracts | manifests | tests |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| stage_id | N/A；保留作 row identity | R；label 输出 key | N/A | N/A | N/A | P，首列 | C：非空、唯一、递增 | M：文件级 size/SHA，不逐列 | T：顺序、label 对应 |
| source_file | N/A | N/A | N/A | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：header/schema |
| source_row | N/A | N/A | N/A | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：schema |
| order_id | N/A | N/A | R；tie sort、order_ids 内部 provenance | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：Supply grouping |
| driver_id | N/A | R；分 driver、state transition | R；chain/group/driver distinct | N/A | N/A | P | C：required non-null | M：整文件 hash | T：overlap/chain |
| departure_time_ns / departure_time | N/A；slot 已生成 | R；sort key | R；trip start、gap、slot expansion | R；trip time、slot | N/A | P，ns 转 timestamp | C：departure 可解析 | M：整文件 hash | T：日期/边界 |
| finish_time_ns / finish_time | N/A | R；overlap end | R；trip end、gap、slot expansion | R；trip time | N/A | P，ns 转 timestamp | C：当前 validator 间接检查字段存在；Supply 过滤非正区间 | M：整文件 hash | T：边界/过滤 |
| slot_start_ns / slot_start | R；OD group key | N/A | N/A；Supply 从 trip interval 重算 | N/A；TTE 从 departure floor | N/A | P | C：exact schema only | M：整文件 hash | T：slot bounds |
| pickup_seg_id | N/A | N/A | N/A | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：schema |
| dropoff_seg_id | N/A | N/A | N/A | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：schema |
| origin_cluster_id | R；OD group key | N/A | R；trip origin、fleet/in-service | R；OD endpoint | N/A | P | C：required、属于 cluster index | M：Supply 无 cluster index 时用于 cluster universe | T：OD/chain |
| destination_cluster_id | R；OD group key | N/A | R；trip destination、in-service | R；OD endpoint | N/A | P | C：required、属于 cluster index | M：Supply cluster universe fallback | T：OD/chain |
| pickup_match_distance_m | N/A | N/A | N/A | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：schema |
| dropoff_match_distance_m | N/A | N/A | N/A | N/A | N/A | P | C：exact schema only | M：整文件 hash | T：schema |
| service_type | R；exclusive/carpool pivot | W；由 state machine 产生 | R；exclusive/carpool branches | N/A | N/A | P | C：required non-null、值属于两类 | M：metadata service counts；文件 hash | T：label、Supply |

结论不是根据字段名猜测：Supply 的 load_orders 明确 usecols 只有 order_id、
driver_id、departure_time、finish_time、origin_cluster_id、destination_cluster_id、
service_type；TTE 的 pd.read_csv 明确 usecols 只有 departure_time、finish_time、
origin_cluster_id、destination_cluster_id。Demand OD 的 SQL 明确只读 slot_start_ns、
origin_cluster_id、destination_cluster_id、service_type。图和 POI reader 不读取
staged/assigned order rows。

### 2.2 最小保留字段与延迟字段

空间匹配后、服务标注前的计算最小集合是：

    stage_id
    order_id
    driver_id
    departure_time_ns
    finish_time_ns
    slot_start_ns
    origin_cluster_id
    destination_cluster_id

其中 stage_id 用于完整排序 key、确定性 replay 和 v1 public contract；order_id 只因
Supply 当前 tie/order provenance 需要而保留。label stream 再增加 service_type。若要
无损重新生成当前 publication CSV，matched checkpoint 还必须保留 source_file、
source_row、pickup_seg_id、dropoff_seg_id、pickup_match_distance_m、
dropoff_match_distance_m；否则这些字段只能重新执行空间匹配。

可以延迟到出口 adapter：

- departure_time、finish_time、slot_start 的字符串格式化；
- service_type 以外的 publication-only 列的 CSV formatting；
- orders_region_assigned.csv.gz 的 gzip 和 CSV serialization。

不能在流式处理中丢失：

- stage_id 及完整 tie-break key；
- driver、departure、finish；
- Supply 所需 order_id、origin、destination；
- Demand 所需 slot、origin、destination；
- null、duplicate、invalid-time 的处理结果；
- 每个 Arrow batch 之间的 current driver、component_max_end、
  component_overlap 和未关闭 component。

若 v2 要保留当前 15 列 publication contract，最小 durable checkpoint 不是上面的计算
最小集合，而是 14 个 staged 字段加最终 service_type；这只是 publication adapter 的
兼容成本，不是 Demand/Supply/TTE 的计算依赖。

## 3. 当前 materialization 数据流账本

说明：formal profile 没有单独记录每个 SQLite table/index 的页大小，故下表将
measured、lower bound 和 estimate 明确分开。logical bytes 对 variable-length
字符串按 reader 输入或已知 uncompressed payload 表示，不把 DataFrame object
overhead 假装成文件 bytes。

| 步骤 | rows / columns | logical bytes | physical bytes | read / write | format | wall | peak RSS | sort / random access | contract / status |
| --- | --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |
| raw CSV read | 90,105,866 / 8 selected input columns | raw file，约 12 GB | raw source，约 12 GB | 1 parse | CSV text | csv_parse 132.8 s；空间匹配另 35.1 min | shared Demand peak 11.06 GiB | no sort；sequential | external input + manifest |
| matched DataFrame | 46,002,707 eventual / 14 staged columns | not separately measured；500k-row chunk transient | 0 durable bytes | 181 DataFrame-to-SQL writes | pandas DataFrame | sqlite_append 126.9 s | shared peak | no random access | implementation detail |
| SQLite staged_orders | 46,002,707 / 14 | not separately captured；10M typed surrogate scales to about 7.489 GB | peak 12,700,127,232 B，含 PK/index/journal effects | 181 appends，1 final commit；后续 3 个 full logical scans | SQLite table | append 126.9 s + index build | shared peak | B-tree indexed access | internal checkpoint；非 public |
| staged indexes | same rows / driver-time and slot-OD keys | index payload not isolated | included in 12,700,127,232 B | 2 index builds | SQLite B-tree | 34.4 s + 32.0 s | shared peak | driver-time covering scan；slot-OD grouping | implementation detail |
| ordered SELECT | 46,002,707 / 4 label columns | 4-column projection；not isolated | index/table pages not isolated | 1 full scan | SQLite cursor | service_label_compute 263.0 s；execute itself lazy、约 0 s | shared peak | requires exact ORDER BY；sequential covering index | load-bearing for label |
| service_labels table | 46,002,707 / 2 | stage_id + service_type projection；10M typed surrogate scales to about 0.922 GB | formal run not isolated；10M prototype table 200,507,392 B | 4,601 executemany batches；1 table write | SQLite table | current level-1 nested insert 445.974 s；older 2B profile 225.2 s | shared peak | no needed random lookup after direct attach | internal；可删除 |
| service_labels index | same rows / service_type key | index payload not isolated；10M typed surrogate scales to about 0.808 GB | formal run not isolated；10M prototype 175,644,672 B | 1 index build | SQLite B-tree | 14.3 s | shared peak | index is not referenced by current export/OD SQL | unused implementation detail；可删除 |
| assigned JOIN | 46,002,707 / 15 | 15-column row projection | no separate file；feeds CSV | 1 staged read + 1 label lookup per row | SQLite JOIN + pandas chunks | export fetch 70.206 s + frame build 86.598 s | shared peak | ORDER BY stage_id；PK label lookup | feeds current public contract |
| assigned CSV serialization | 46,002,707 / 15 | 10,523,799,657 B uncompressed | 3,079,468,693 B gzip level 1 | 1 write | CSV text + gzip | export total 485.091 s；CSV 227.973 s；gzip 71.082 s | shared peak | stage_id order required by contract | current public/compatibility output |
| Supply reread | 46,002,707 / 7 selected columns | decompressed CSV payload，10.52 GB source stream | 1 read of 3,079,468,693 B container | 1 parse | gzip CSV → pandas DataFrame | 881.93 s | 11.237 GiB | no input random access；internal driver sorting | stage input binding |
| TTE reread | 46,002,707 / 4 selected columns | same decompressed source stream | 1 read of 3,079,468,693 B container | 1 parse | gzip CSV → pandas DataFrame | 653.96 s | 11.057 GiB | no input random access；group/pivot downstream | stage input binding |
| downstream materialization | Demand OD 19,507,610 rows；Supply 3 tables；TTE wide matrices | OD CSV about 606 MB；Demand dense OD tensors 3 × 13,248 × 100 × 100 int32 ≈ 1.59 GB | exact total not captured in this profile | one write per formal output family | CSV.gz / NPZ / Parquet | included in stage walls | TTE/Supply peaks above | aggregators and matrix fills | formal stage contracts |

### 3.1 同一订单的读写/解析次数

按“逻辑 pass”而非 OS syscall 计数：

| 载体 | 每个订单参与的次数 |
| --- | ---: |
| raw CSV parse | 1 |
| staged_orders durable write | 1 |
| staged_orders full logical reads | 3：label ordered scan、assigned export、OD groupby；另有一次 bounds aggregate |
| service_labels write | 1 个两列 projection |
| service_labels reads | 2：assigned export JOIN、OD groupby JOIN |
| assigned CSV write | 1 |
| Supply/TTE assigned CSV parses | 2 |

因此当前完整路径至少有 6 次 full-order logical traversal（raw parse、3 次 staged
pass、2 次 downstream parse），并另外写入/读取 1 个 label projection。按 durable
materialization 计是 staged_orders、service_labels、assigned CSV 三次内部落盘；
assigned CSV 还被两个下游各自解析一次。SQLite 的 26.64 GB process write 说明实际
page/index/journal write amplification 远高于简单的 row payload 乘法。

## 4. 三个候选架构

### Candidate A：Columnar staging，阶段仍分离

    raw chunks
      -> typed, partitioned Parquet matched shards
      -> DuckDB external ORDER BY
      -> labeled Parquet checkpoint
      -> Demand / Supply / TTE independently scan checkpoint

这是最适合先落地的边界保持方案。它取消 SQLite staging 和 CSV internal transport，
但仍允许每个阶段按 manifest 独立 resume、standalone execution 和失败恢复。代价是
Demand、Supply、TTE 在分别执行时仍会各读一次 labeled checkpoint；它不实现一次
fan-out。

建议 matched shard 按 source file/chunk 的 deterministic shard_id 写入；labeled
checkpoint 按 global sorted output 的 shard_id 或 ordinal range 写入。date、slot 只能
作为可选下游索引，不能作为 label checkpoint 的主分区，因为同一 driver/component
可能跨日期或 slot。

### Candidate B：Labeled stream fan-out

    raw chunks
      -> columnar matched shards
      -> one external sort
      -> Python service-label state machine
      -> labeled RecordBatch
           -> Demand aggregator
           -> Supply aggregator
           -> TTE aggregator
           -> labeled Parquet checkpoint
           -> optional CSV exporter

它直接消除 service_labels table 和 JOIN。label state machine 在 component close 时
给当前 component 的 rows 附加 service_type；下游 reducer 只消费带 label 的记录，不
需要等待一张 stage_id-to-label lookup table。

Fan-out 必须是无副作用 consumer，写 checkpoint 采用临时文件、flush、hash、原子 rename。
如果任一 consumer 失败，checkpoint 仍可供 standalone rerun；consumer 进度和 state
snapshot 不能以“已写半个文件”代替。

### Candidate C：DuckDB/Arrow hybrid

Candidate C 是 Candidate B 的 engine implementation：

- spatial match 输出 typed Parquet shards；
- DuckDB 对 shard 做 external ORDER BY；
- fetch_record_batch 输出 Arrow RecordBatch；
- Python 保留跨行 service-label state machine；
- labeled batches 写 partitioned Parquet；
- Demand/Supply/TTE 通过 DuckDB/Arrow scan 或 stream consumer 执行；
- publication CSV 只由显式 exporter 产生。

不能把 service labeling 全部下推为 SQL。可安全下推的是 typed projection、null/time
过滤、source shard scan、external sort、按 slot/origin/destination/service_type 的
Demand count；必须保留 Python 顺序状态的是 driver boundary、half-open interval
overlap、component_max_end、component_overlap、跨 batch state，以及任何需要逐 driver
chain 的 Supply reducer。TTE 的 median/imputation 仍应保留现有 Python/NumPy contract
路径，直到另一个 full-output equivalence 实验完成。

## 5. 核心问题逐项回答

1. **全局 driver/time 排序是否不可避免？**
   对当前任意 raw chunk 顺序，是的，至少需要 full driver-local order 加确定性
   merge；不能用 date、slot 或输入 chunk 顺序替代。若未来上游保证每个 driver
   完整且已按完整 key 排序，可把 external sort 降为 shuffle/merge，但正确性门不变。

2. **SQLite 的哪些作用必须由替代架构保留？**
   durable intermediate、transaction boundary、run-owned resume checkpoint、完整
   order key 的 deterministic order、schema/null/duplicate validation、失败后清理。
   SQLite 的 row-addressable JOIN、service label table、两个 B-tree index 和 implicit
   page cache 不必保留。

3. **是否可以不创建 service_labels 表？**
   可以。标签在 Python state machine emit 时附加到 row/batch；v1 SQLite 只作为 oracle。

4. **是否可以不执行 staged_orders × service_labels JOIN？**
   可以。Demand OD 直接累计；Supply/TTE 直接消费 projection；CSV adapter 如需旧
   contract 则另做一次可选、确定性 stage_id export。

5. **是否可以在 label 生成时直接附加到当前 batch？**
   可以，但要等 component 关闭后再回写 component rows；component 可以跨 Arrow
   batch，因此 state 必须跨 batch 保存。不能只对当前 batch 的局部 rows 独立标注。

6. **Demand、Supply、TTE 是否可以消费同一有序 labeled batch？**
   可以。P2 已用无副作用 Demand、Supply projection、TTE projection 在同一 labeled
   stream 上验证；生产 Supply chain 和 TTE median/imputation 仍需 full-output gate。

7. **TTE 是否需要 service label？**
   不需要。当前 reader 只读 departure_time、finish_time、origin_cluster_id、
   destination_cluster_id；service_type 可在 TTE projection 中丢弃。

8. **Supply 是否需要全部 assigned-order 行？**
   需要所有有效订单行，但不需要全部 15 列。当前真正消费 7 列：order_id、driver_id、
   departure、finish、origin、destination、service_type。source provenance、segment
   ids、match distances、slot_start 不参与 Supply 计算。

9. **Demand OD tensor 是否可以在 labeled stream 上直接累计？**
   可以。键是 slot_start、origin、destination、service_type；用整数 accumulator
   直接累计再按既有 cluster/slot axis materialize OD CSV、tensor。

10. **publication CSV 是否可以变为 optional export？**
    可以作为内部通信格式的 v2 结论；当前 public/publication contract 暂不删除。
    若 checkpoint 按 label sort 保存，旧 CSV 的 stage_id 递增顺序需要一个独立的
    stage_id external reorder/export pass；不能声称零成本 byte-identical regeneration。

11. **labeled Parquet 应如何分区？**
    主分区使用 deterministic shard_id / global ordinal range，且 manifest 保存
    sort key、min/max stage_id、min/max driver、row count、schema fingerprint、hash。
    不以 date 或 slot 做主分区；driver hash 只适合匹配前的完整-driver shuffle，不适合
    直接作为最终 checkpoint partition，除非每个 driver 完整归属且有 deterministic
    merge。

12. **fresh、resume、standalone、CSV regeneration、failed recovery、replay 如何同时支持？**
    fresh 从 raw 生成 matched shards；resume 以 shard manifest 和 checkpoint state
    snapshot 续跑；Supply/TTE standalone 读 labeled checkpoint；CSV regeneration
    显式启用 stage_id exporter；失败只保留 run-owned temp/partial 状态；replay 使用
    完整 sort key、固定 schema/dtype/null policy、输入/config fingerprints 和每个
    batch 的稳定边界。任意 driver 跨 checkpoint boundary 时必须恢复
    current_driver/component state，不能从文件首行重新猜。

## 6. P0/P1/P2 原型

### 6.1 方法与限制

prototype 仅位于 outputs/perf-architecture-v2/，没有生产代码修改。输入使用匿名
stage/order/driver IDs，不记录任何真实 row。生成器使用正式聚合统计作为校准：
46,002,707 rows、199,668 drivers、exclusive/carpool 比例、lognormal driver-size
surrogate、driver-level overlap/non-overlap intervals，并故意注入完整 sort key
tie-break rows。它不是随机独立行，也不是 raw spatial matching benchmark。

- P0：Parquet input 读入 SQLite staged_orders；建立 driver-time/slot-OD indexes；
  ordered SELECT；Python label state machine；service_labels insert/index；JOIN；
  gzip CSV export。
- P1：typed Parquet shards；DuckDB external ORDER BY；Arrow batches；原有 Python
  state machine；labeled Parquet writer；无 label table、无 JOIN。
- P2：P1 加同批 fan-out；Demand projection、Supply projection、TTE projection 同时
  consume labeled batch，并写 labeled checkpoint。

P1 的 wall 包含一个 post-write checkpoint reread 作为等价性验证；P2 在流中做聚合、
不再做这次验证 reread。因此 P1/P2 差值不是纯生产差值。P1/P2 的 Supply/TTE 只是
字段投影和小型聚合 probe，不是完整 Supply chain、fleet/idle 算法或 TTE
median/imputation 的等价证明。

### 6.2 资源表

bytes_read 是 prototype 运行时 /proc/self/io 的报告值；本环境四个规模均为 0，
所以不把它解释为没有读盘。真实 input physical bytes、bytes_written、spill 和
checkpoint size 由 manifest/filesystem 单独记录。RSS 为 Linux ru_maxrss。

| rows | backend | wall s | user s | sys s | CPU | peak RSS GiB | bytes read | bytes written | temp spill B | checkpoint B | rows/s |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | P0 | 3.907 | 3.624 | 0.389 | 102.7% | 0.406 | 0 | 30,973,952 | 0 | 6,135,681 | 25,594 |
| 100,000 | P1 | 3.428 | 3.152 | 0.382 | 103.1% | 0.359 | 0 | 5,373,952 | 0 | 5,372,736 | 29,175 |
| 100,000 | P2 | 2.812 | 2.497 | 0.326 | 100.4% | 0.289 | 0 | 5,373,952 | 0 | 5,372,736 | 35,564 |
| 1,000,000 | P0 | 50.272 | 42.486 | 8.140 | 100.7% | 1.236 | 0 | 310,325,248 | 0 | 62,171,204 | 19,892 |
| 1,000,000 | P1 | 34.548 | 31.674 | 3.404 | 101.5% | 1.215 | 0 | 54,759,424 | 0 | 54,757,313 | 28,945 |
| 1,000,000 | P2 | 29.078 | 26.094 | 3.043 | 100.2% | 0.993 | 0 | 54,759,424 | 0 | 54,757,313 | 34,391 |
| 5,000,000 | P0 | 256.498 | 207.902 | 50.315 | 100.7% | 3.958 | 0 | 1,670,221,824 | 0 | 313,273,151 | 19,493 |
| 5,000,000 | P1 | 168.664 | 155.392 | 15.878 | 101.5% | 4.086 | 0 | 1,144,320,000 | 843,743,232 | 276,153,719 | 29,645 |
| 5,000,000 | P2 | 145.790 | 132.204 | 14.023 | 100.3% | 3.449 | 0 | 1,133,674,496 | 832,536,576 | 276,153,719 | 34,296 |
| 10,000,000 | P0 | 511.260 | 408.838 | 105.797 | 100.7% | 6.519 | 0 | 3,702,632,448 | 0 | 627,938,403 | 19,560 |
| 10,000,000 | P1 | 335.642 | 312.411 | 28.351 | 101.5% | 6.835 | 0 | 3,069,808,640 | 1,983,283,200 | 553,830,194 | 29,794 |
| 10,000,000 | P2 | 290.444 | 264.212 | 27.143 | 100.3% | 5.798 | 0 | 3,083,808,768 | 2,001,764,352 | 553,830,194 | 34,430 |

Input physical/logical bytes were 4,446,828 / 16,280,146 B at 100k and
464,818,187 / 1,628,002,593 B at 10M. Exact values for all scales remain in the ignored
benchmark manifest. At 10M P0 SQLite was 2,488,098,816 B; P1/P2 labeled checkpoint was
553,830,194 B.

### 6.3 Sort, label and output breakdown

| rows | backend | sort / ordered stream s | label s | label insert s | output s | JOIN fetch s | verification s |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | P0 | index build 0.111 | 0.469 | 0.081 | 1.501 | 0.185 | — |
| 100,000 | P1 | sort first batch 0.058 | 0.429 | — | 0.066 | — | 1.856 |
| 100,000 | P2 | sort first batch 0.058 | 0.714 | — | 0.064 | — | 0.906 |
| 1,000,000 | P0 | index build 1.355 | 6.340 | 6.111 | 16.256 | 1.722 | — |
| 1,000,000 | P1 | sort first batch 0.664 | 4.239 | — | 0.652 | — | 20.502 |
| 1,000,000 | P2 | sort first batch 0.641 | 7.881 | — | 0.652 | — | 10.526 |
| 5,000,000 | P0 | index build 7.323 | 35.862 | 41.962 | 82.737 | 8.229 | — |
| 5,000,000 | P1 | sort first batch 4.508 | 20.989 | — | 3.254 | — | 99.669 |
| 5,000,000 | P2 | sort first batch 4.575 | 40.363 | — | 3.262 | — | 48.894 |
| 10,000,000 | P0 | index build 15.605 | 74.818 | 89.510 | 166.794 | 16.213 | — |
| 10,000,000 | P1 | sort first batch 9.350 | 43.937 | — | 6.609 | — | 191.298 |
| 10,000,000 | P2 | sort first batch 9.123 | 84.997 | — | 6.588 | — | 89.826 |

P0 的 stage_s 为 58.869 s（10M）；P0 还实际写入 label table/index。P1/P2 的
external sort 由 DuckDB 配置为 512 MB memory limit 和 run-owned temp directory。
10M spill 约 2.0 GB；小规模未发生 spill。

### 6.4 严格等价门

100k、1M、5M、10M 四个规模全部 passed。每个规模 P0、P1、P2 均通过：

- sorted stage_id order；
- 每个 stage_id 的 service label；
- stage_id/label pair 和 exclusive/carpool counts；
- Demand OD projection aggregate；
- Supply projection aggregate；
- TTE projection aggregate；
- row count、schema、dtypes、null behavior、duplicate behavior；
- tie-break stage_id；
- driver state 和 component state 跨 Arrow batch boundary。

原型 manifest 的 null count 和 duplicate count 为 0，故这里证明的是三条路径对该
输入的相同保留行为；生产切换前仍应加入带 null、duplicate、invalid interval 的
显式 adversarial fixtures。10M 运行中 driver_cross_batch 和 component_cross_batch
均为 1,219，证明 state 没有被 batch 边界截断。

严格门不是“整数允许误差”：比较使用 exact integer/category aggregate 和 digest；
没有用 tolerance 掩盖差异。

## 7. 引擎评估

| 引擎 | external sort / spill | RecordBatch 流 | 稳定性要求 | temp / Python copy | 依赖与平台 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| DuckDB | 支持 external ORDER BY；10M prototype spill 约 2.0 GB | fetch_record_batch 可用 | ORDER BY 本身不承诺任意 tie 稳定；显式完整 key 含 stage_id 后成为 total order | temp_directory 可 run-owned；当前 prototype dict conversion 有 copy overhead，生产应按 Arrow columns 读 | Linux/Windows wheel 生态成熟；当前 environment.yml 未声明 DuckDB，需正式加入/锁版本 | 主引擎 |
| PyArrow Dataset | scan/Parquet 强；Dataset 本身没有可依赖的全局 external ORDER BY | RecordBatch 原生 | sort_indices stability 不能替代完整 key | temp 行为需自建 external-sort；Arrow 到 Python copy 可控 | 已在 environment.yml 和 manifest allowlist；Linux/Windows | 与 DuckDB 配套 |
| Polars lazy | lazy/streaming scan 强；全局 sort spill 和 streaming 语义依版本/配置 | 可转 Arrow，但状态机仍在 Python | 必须完整 key；maintain-order 不是跨引擎合同 | 需要确认 sink/temp；额外 Python/Rust boundary | 当前不是正式依赖，增加 packaging/version surface | 备选实验，不推荐主引擎 |
| SQLite | B-tree index 可避免当前 ORDER BY 的临时 sort；事务/随机访问强 | Python fetchmany 可用，不是 Arrow-native | 完整 ORDER BY key 可确定；当前索引 scan 已证明 | page/journal write amplification；12.7 GB peak | stdlib、Linux/Windows 最稳 | v1 oracle/fallback |

主引擎不是因为流行，而是因为它同时覆盖 typed Parquet scan、external sort/spill、
Arrow batch 边界和 SQL/文件级 determinism；Python 只保留不能安全下推的顺序状态。

## 8. 兼容性、合同与收益判断

### 8.1 v1/v2 合同边界

| 项目 | v1 当前状态 | v2 建议 |
| --- | --- | --- |
| SQLite staged_orders | internal implementation | deprecated internal path；保留 oracle/fallback |
| service_labels table/index | internal implementation | 删除；不成为 v2 contract |
| staged × label JOIN | internal implementation | 删除；label attach/stream projection |
| labeled Parquet + manifest | 不存在 | new internal contract：schema、sort key、partition、hash、state snapshot |
| orders_region_assigned.csv.gz | Demand output、Supply/TTE input、publication inventory | public compatibility adapter；v2 内部不依赖 |
| Demand OD/tensor、Supply tables、TTE files | formal public stage contracts | unchanged；必须 exact output-equivalence |
| run ownership/manifest/resume/overwrite/transactional publish | existing pipeline contract | preserve；checkpoint files加入同样规则 |
| raw-only fresh run | supported | preserve；不能依赖历史 frozen artifacts |

orders_region_assigned.csv.gz 当前被 stage binding、assigned contract、publication
inventory 和 release tests 视为正式路径；因此“optional”是 v2 internal transport
结论，不是本审计阶段删除 public output。迁移阶段 E 之前默认仍可生成。

### 8.2 完整 pipeline 收益预测

以下是以当前正式 baseline 为分母的工程预测，不是已完成生产 benchmark。每档都假设
spatial matching 本身不改变，且 P1/P2 的完整 Supply/TTE 算法需要单独接入并通过合同。
数字是 central estimate；上下浮动应按阶段 gate 重新测量。

| 档位 | Demand wall | Supply wall | TTE wall | total wall | peak RSS | total bytes written | total bytes reread | temp disk |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| conservative | 2,850 s / 47.5 min | 850 s / 14.2 min | 640 s / 10.7 min | 4,340 s / 72.3 min，约 -5.4% | 约 11.5 GiB | 约 18–24 GB materialization/process lower bound | 约 20–27 GB logical；standalone checkpoint rereads included | 8–12 GB |
| expected | 2,450 s / 40.8 min | 760 s / 12.7 min | 600 s / 10.0 min | 3,810 s / 63.5 min，约 -16.9% | 约 9–10.5 GiB | 约 10–16 GB | 约 5–10 GB；combined fan-out 一次流 | 10–16 GB |
| optimistic | 2,200 s / 36.7 min | 650 s / 10.8 min | 520 s / 8.7 min | 3,370 s / 56.2 min，约 -26.5% | 约 7–9 GiB | 约 6–10 GB | 约 2–5 GB；无 CSV internal reread | 12–20 GB |

假设和归因：

1. 取消 service_labels insert 可避免当前 level-1 nested 445.974 s 的写入路径，
   但不把全部时间当成可省：label state、Parquet writer 和 manifest hash 仍有成本。
2. 取消 service_labels index 可避免约 14.3 s index build 和约 10M prototype
   175,644,672 B 的线性增长；该 index 在当前查询中没有 reader。
3. 取消 JOIN 可避免每行 stage_id lookup、export/OD 的 label join；JOIN fetch 当前
   约 70.206 s，frame build 约 86.598 s 是更宽的出口成本，故预测不把 156.8 s
   全部承诺为纯 JOIN savings。
4. 取消 CSV internal communication 可避免一次 10,523,799,657 B text serialization、
   3,079,468,693 B gzip container 和 Supply/TTE 两次 CSV parse；optional public CSV
   仍会付出相同 exporter 成本。
5. Supply/TTE 的重复 parse savings 只是 whole-stage 的一部分；主要 Supply chain、
   TTE median/imputation compute 仍存在，所以不能套用 P2 wall reduction。
6. Parquet matched/labeled write、DuckDB external sort、spill 和 hashes 是新成本；
   10M prototype input physical 464,818,187 B、labeled checkpoint 553,830,194 B、
   spill 约 2.0 GB，生产值需用真实字符串/分区重新测。
7. fan-out 需要各 reducer 的 compact state；若 Supply 继续保留当前 full DataFrame/block
   algorithm，内存节省会小于理论值。完整 streaming Supply 是后续实现，不是本报告
   已证明的结果。

SQLite 因而是可避免 materialization 的主要架构瓶颈，但不是完整 Demand wall 的唯一
或第一瓶颈：formal profile 仍显示 spatial matching 约 35.1 min，当前 level-1
export 约 8.1 min，service labeling/SQLite passes 也有显著成本。v2 应同时消除
materialization duplication，而不是声称“换数据库”能消灭 35.1 min spatial work。

## 9. 停止边界

本阶段已完成审计、gitignored prototype、benchmark、architecture recommendation
和 migration plan。没有：

- 修改生产 pipeline、正式 config、默认输出或 stage contract；
- 删除 SQLite path、assigned CSV 或任何 public output；
- 读取/修改 sanitized 目录；
- 读取/修改 .qoder/settings.local.json；
- 提交 prototype、vendor、synthetic input、run result 或 raw data。
