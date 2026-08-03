# RoadNet Pipeline Architecture v2 Migration Plan

日期：2026-08-03
前置审计：docs/performance/pipeline-architecture-v2-audit.md
范围：只提出迁移边界，不在本阶段实施生产 v2。

## 目标和不变项

目标路径：

    raw CSV
      -> typed matched Parquet shards
      -> one deterministic external sort
      -> Python service-label state machine
      -> labeled Parquet checkpoint
      -> Demand / Supply / TTE
      -> optional publication CSV

必须保持：

- v1 SQLite pipeline 是 correctness oracle 和随时可用的 fallback；
- Demand OD/tensor、Supply 三表、TTE 全部正式 outputs 的 exact equivalence；
- stage independence、fresh raw-only run、resume、overwrite、run ownership；
- failed-run recovery、transactional publish、manifest fingerprints、deterministic replay；
- 当前 orders_region_assigned.csv.gz 可以继续生成；
- 本阶段不修改默认行为、正式 config 或 public stage contracts。

v2 checkpoint 的 internal contract 需要在真正实施时单独冻结，至少包括：

- typed schema、nullable policy、schema version；
- global order key：driver_id, departure_time_ns, finish_time_ns, stage_id；
- stage_id 单调/唯一规则和 tie-break；
- shard_id/ordinal range、row count、min/max key、file hash；
- input/config/runtime fingerprint；
- checkpoint state snapshot：current driver、component_max_end、
  component_overlap、未关闭 component；
- complete marker、atomic publish 和 cleanup policy。

## 全局回滚规则

任何 phase 的 production gate 失败，都回滚到最近一个已通过 phase 的 reader/adapter；
不删除 v1 产物，不覆盖共享路径，不用历史 frozen artifact 代替 raw-only validation。
新文件全部写入 run-owned 目录；只有 contract、hash、manifest 和 publish validation
全部通过后才允许 transactional scope swap。

## Phase A：引入 columnar matched/labeled checkpoint，下游保持不变

实现范围：

- 保留当前 raw spatial matching 逻辑和 v1 label oracle；
- 将 matched 14-column staging 写成 typed Parquet shards；
- 使用 DuckDB external ORDER BY 和原有 Python state machine 生成 labeled Parquet；
- 同时由 compatibility exporter 生成当前 orders_region_assigned.csv.gz；
- Supply/TTE 仍读 CSV，Demand 的正式 OD/tensor 仍走 v1-compatible adapter；
- SQLite path 不删除，默认仍可选择 v1。

Output-equivalence gate：

- matched rows、stage_id order、14-column schema/dtypes/null/duplicate behavior exact；
- 每个 stage_id service label、exclusive/carpool counts exact；
- assigned CSV 解压后 bytes、header、15-column schema、row order exact；
- Demand OD CSV、OD tensor、cluster index、graph/POI assets exact；
- fresh raw-only run 和 v1 oracle 均通过现有 Demand contract。

Performance gate：

- Demand wall 不得比当前 level-1 baseline 3,050.770 s 回退超过 5%；
- checkpoint 写入和 external-sort temp 的峰值需低于可配置 disk budget；
- matched/labeled checkpoint 可在 100k、1M、5M、10M prototype scale 重放；
- P1/P0 等价门全部通过，生产 run 记录 sort、label、output、spill、RSS、bytes。

Rollback point：

- 任意 shard、label checkpoint 或 exporter failure 直接切回 v1 SQLite staging；
- CSV compatibility failure 不影响 v1 outputs；删除本次 run-owned partial v2 files。

Commit boundary：

- 一个 commit 只包含 columnar writer、checkpoint manifest、adapter 和 tests；
- 不与 Supply/TTE reader 改造、config default、public contract 删除混提交。

Full production validation：

- 从 raw-only fresh run 启动，不复用历史 SQLite 或 assigned artifact；
- 运行 v1 与 A path，比较全量 Demand contract 和 decompressed assigned bytes；
- 对同一 run 再执行 standalone Supply/TTE，确认现有下游没有回退；
- 测试 resume、overwrite、失败后重启、run ownership 和 transactional publish。

Disk cleanup rule：

- staging write 使用 run-owned temporary suffix，成功后只保留 matched/labeled checkpoint
  和 manifest；
- export 与合同校验完成前不删 CSV/Parquet；
- 失败只清理该 run 的 temp 和 incomplete marker；不触碰历史 run；
- 通过 A gate 后可按 retention policy 删除 SQLite temporary DB，但 v1 oracle 运行所需
  path 不从代码中删除。

## Phase B：Supply 改读 labeled Parquet，CSV 保留

实现范围：

- Supply 增加 labeled Parquet reader/adapter；
- projection 只传递 order_id、driver_id、departure、finish、origin、destination、
  service_type 七列；
- 保留 CSV reader、standalone CLI 和当前 Supply output contract；
- 先适配当前 full DataFrame/block algorithm，再评估 per-driver streaming；
- labeled checkpoint 仍由 A 生成，CSV 继续作为可选但默认兼容产物。

Output-equivalence gate：

- Parquet adapter 与 CSV adapter 输入 projection exact；
- orders_loaded 46,002,707、drivers 199,668、clusters、slots exact；
- supply_inservice_od、supply_available_floor、supply_fleet_lower_bound 的 row order、
  schema、dtypes、nulls、integer values、decompressed bytes/SHA exact；
- chain grouping 的 order_id tie、carpool merge gap、idle windows 和 driver dedup exact；
- standalone Supply 和 full pipeline Supply 都通过 Supply contract。

Performance gate：

- Parquet path wall 不得超过 CSV path 5%，expected target 是至少减少一次 CSV
  decompression/parse 的可测时间；
- peak RSS 不得增加 5%；driver-block temp/disk 需在 manifest 中记录；
- 若开始 per-driver stream，必须另设 output-equivalence gate，不能以 projection
  equality 代替完整 chain equality。

Rollback point：

- stage3_supply.orders_path 通过 adapter flag 恢复 CSV；
- B reader 失败时保留 A 的 CSV 和 v1 Supply outputs；
- 不回滚已通过的 Demand checkpoint。

Commit boundary：

- 一个 commit 只改 Supply input adapter、reader tests、binding tests；
- 不把 fan-out reducer、CSV 删除或 TTE 改造放进 B commit。

Full production validation：

- A checkpoint + CSV 双输入 fresh standalone Supply；
- 反序重复 run，比较解压 output hash 和 run_summary；
- 测试 resume、overwrite、missing/partial checkpoint、wrong schema、wrong fingerprint；
- 验证无 cluster_index 的 standalone Supply 仍能从 checkpoint 得到 cluster universe。

Disk cleanup rule：

- A 的 CSV 在 B gate 通过前保留；
- B 通过后 CSV 仍按 publication retention 保留，但不作为 Supply 的 required input；
- partial Parquet scan 和 Arrow temp 只清理当前 run-owned directory。

## Phase C：TTE 改读 labeled Parquet，CSV 保留

实现范围：

- TTE 增加只读取 departure_time、finish_time、origin_cluster_id、
  destination_cluster_id 的 Parquet projection；
- service_type、driver_id、order_id、publication-only columns 不进入 TTE DataFrame；
- 保留现有 TTE raw median、count、support、hops、imputation 和 distance asset path；
- 保留 CSV adapter 以便 standalone 回退和 publication compatibility。

Output-equivalence gate：

- four-column projection 与 CSV parse 后的 values、dtypes、null behavior exact；
- keep clusters、OD column order、time index exact；
- TTE_raw、TTE_count、TTE_support、TTE_hops、TTE_imputed 和 distance assets
  exact bytes/SHA 或 contract 规定的 exact value equivalence；
- observed/inferred/missing masks、support、max_hops、raw range 全部 exact；
- standalone TTE 和 full pipeline TTE 均通过 TTE contract。

Performance gate：

- TTE Parquet path 不得比 CSV path 回退超过 5%；RSS 不增加 5%；
- 记录 parquet scan、projection、median、imputation 和 output wall；
- 确认 network-distance cache 不因输入 adapter 改变而重新构建。

Rollback point：

- stage4_tte.inputs.orders_path 恢复 CSV adapter；
- TTE 任何 matrix/axis failure 都回到 B-approved CSV path；
- 只删除当前失败 run-owned TTE partial outputs。

Commit boundary：

- 一个 commit 只改 TTE input adapter 和 contract tests；
- 不改变 imputation 默认值、distance recompute policy 或 public file names。

Full production validation：

- A checkpoint 同时跑 CSV/Parquet TTE；
- fresh raw-only full pipeline 和 standalone TTE 各跑一次；
- 逐文件 hash、matrix axes、dtype、mask、contract、resume/overwrite 全验；
- 记录 input physical bytes、decompressed bytes 和 TTE downstream reread count。

Disk cleanup rule：

- CSV 在 C gate 前保留；
- TTE 的 partial matrices 和 external temp 在失败时只清理当前 run；
- network-distance 和 representative-node assets 仍按既有 run ownership/retention 规则
  管理，不因 adapter 改造删除。

## Phase D：Demand/Supply/TTE 使用一次 labeled stream fan-out

实现范围：

- DuckDB external sort 只执行一次；
- Python label state machine 产生 labeled RecordBatch；
- Demand aggregator 直接累计 OD；
- Supply consumer 逐 driver 或 bounded block 累计 chain/idle/in-service；
- TTE consumer 累计四列 projection，并保留现有 exact median/imputation adapter；
- labeled Parquet checkpoint 与 consumer progress 同步写入；
- 在 combined run 中不重新读取 checkpoint；standalone 仍可从 checkpoint 恢复。

Output-equivalence gate：

- fan-out 与 Phase C/B/A 的 stage_id order、label digest、counts exact；
- Demand OD/tensor、Supply 三表、TTE 五类矩阵和 distance outputs exact；
- batch boundary、driver boundary、component boundary、tie-break、null/duplicate、
  invalid-time behavior exact；
- combined run 与三个 standalone rerun 的 formal outputs exact；
- 任一 consumer 失败后，从 checkpoint state snapshot 重新运行仍 exact。

Performance gate：

- sort wall 只出现一次；combined run 不再读取 assigned CSV 或重复 parse checkpoint；
- total wall 以当前 4,586.660 s baseline 为分母，先设 conservative 不回退门，
  再按 expected/optimistic forecast 逐步收紧；
- peak RSS、temp spill、checkpoint bytes、bytes read/write 均有阶段级记录；
- Supply/TTE full algorithms 必须证明 fan-out consumer 没有隐式把全量数据复制成
  三份 pandas DataFrame；
- failed consumer recovery 的重放成本必须小于 raw spatial rematch。

Rollback point：

- combined fan-out 失败时，使用已通过 B/C 的独立 Parquet readers；
- 如 Parquet checkpoint 不完整，回到 A 的 v1-compatible CSV outputs；
- v1 SQLite fresh path 始终作为最后 correctness fallback。

Commit boundary：

- 先一个 commit 只引入 stream interface、state snapshot 和 no-op consumer；
- 再分别提交 Demand、Supply、TTE consumer；
- 每个 consumer commit 都必须带 full-output equivalence tests，不允许一次性大重写。

Full production validation：

- 100k、1M、5M、10M prototype gate 后执行真实 full raw-only run；
- 同一 raw/config 运行 v1、A、D；比较所有 stage contracts 和 manifests；
- 运行 standalone Supply/TTE、resume、overwrite、故障注入、checkpoint truncation、
  consumer restart、deterministic replay；
- 验证只在同一次 labeled stream 中产生一次 service label，并记录 consumer read count。

Disk cleanup rule：

- checkpoint publish 成功并完成所有 consumer contract 前，不删除 matched shards；
- combined run 成功后按 manifest retention 保留 labeled checkpoint，删除 DuckDB spill、
  incomplete batches 和 transient fan-out buffers；
- standalone 需要 replay 时保留 checkpoint；不得因 combined 成功直接删除唯一恢复源。

## Phase E：CSV 改为 optional publication export

实现范围：

- Supply/TTE 内部 contract 改绑定 labeled Parquet/stream；
- orders_region_assigned.csv.gz 由显式 publication/export flag 生成；
- exporter 保持当前 15 列、header、日期/float 格式、stage_id 递增和 gzip level，
  直到 public contract 正式版本化；
- publication inventory、manifest 和 release tooling 明确标记 CSV 是 optional artifact；
- 默认行为在另一个批准的 public contract 变更前仍保持生成 CSV。

Output-equivalence gate：

- flag=true 时 CSV 解压 bytes、row count、schema、stage_id order、service labels 与 v1
  exact；
- flag=false 时 Demand/Supply/TTE formal outputs、manifest bindings 和 publication
  allowlist 都通过；
- CSV regenerated from labeled checkpoint 与 CSV generated in combined stream exact；
- release tests、privacy scan、tracked-file size scan、contract validators 全通过。

Performance gate：

- flag=false 的完整 pipeline 不再为内部通信写 gzip CSV、不再触发 Supply/TTE CSV parse；
- flag=true 的额外成本必须单独报告，不得隐藏在 Demand wall；
- total bytes written/reread、temp disk、RSS 和 stage wall 记录两种模式；
- optional export 失败不能使已通过的 Demand/Supply/TTE computation 结果被误标为
  incomplete publish。

Rollback point：

- 重新打开 CSV export flag 和旧 stage bindings；
- public publication failure 时保持已验证 v2 internal outputs，回滚只影响 adapter；
- public contract 未批准前不删除 CSV validator、reader 或 release compatibility test。

Commit boundary：

- 一个 commit 只改变 optional output flag、publication inventory 和 documentation；
- public contract version、默认值变更、CSV deletion 必须另开 review/commit；
- 不把 deprecation 和 fan-out algorithm 混在同一个 commit。

Full production validation：

- raw-only fresh run 分别执行 flag=true/false；
- flag=true 与 v1 做解压 byte/hash、Demand contract 和 downstream exact comparison；
- flag=false 做 Supply/TTE standalone-from-checkpoint、resume/overwrite、publish swap、
  release validation；
- 验证历史 frozen artifacts 不是唯一输入，且新 run manifest 能完整重放。

Disk cleanup rule：

- flag=true 的 CSV 按 publication retention 保存；
- flag=false 不生成 CSV，任何 stale CSV 不得被 stage binding 自动发现；
- exporter 临时文件只在 atomic rename 成功后替换目标，失败清理当前 run-owned temp；
- 经过 public deprecation period 后才评估删除旧 CSV，且需新的 contract/rollback 决策。

## 验收顺序

1. 先通过 A，确认 columnar checkpoint 不改变 v1 结果。
2. 再通过 B/C，分别迁移 Supply/TTE，保留 CSV 作为回退。
3. 只有 B/C 都稳定后才启用 D 的一次 fan-out。
4. 最后才在 E 将 CSV 从 required internal input 降为 optional publication artifact。

每一阶段的 commit 都应包含：代码变更、最小单元/边界测试、full-production
validation record、output equivalence hashes、performance profile、rollback 说明。
本审计提交只包含审计文档，不包含上述生产实施。
