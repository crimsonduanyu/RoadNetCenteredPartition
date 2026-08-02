# Demand gzip level 1 downstream impact v1 — Performance Batch 2C-2C

**日期：** 2026-08-02
**状态：** 完成；level 1 通过 Demand→Supply→TTE 下游验收。
**代码 HEAD：** `5e1e8ef`
**范围：** 只验证既有 Demand gzip level 1，不实施新的性能优化。

## 1. 实验边界

本批次没有修改 gzip level、Supply/TTE 算法、CSV parser、batch size、SQLite、
空间匹配、并行模型或 output contract。没有新增 profiling instrumentation；每个
下游阶段均使用 `/usr/bin/time -v`，并由正式 stage contract 验证输出。

现有 full-pipeline binding 只会把同一 run 内的 Demand 输出绑定给 Supply/TTE，不能
直接把两个不同 Demand run 注入同一个下游 run。因此使用项目支持的 standalone
Supply/TTE CLI，给每个新 run 显式绑定一个已完成 Demand run 的 assigned-orders 和
cluster index；输入文件的 size/SHA-256 由 run manifest 记录，输出写入独立的
run-owned 目录。两组 TTE 还共用同一个已存在的预计算 network-distance 与
representative-node 输入，避免距离资产重建差异进入比较。

没有使用 sudo、系统级清 cache 或旧 Supply/TTE 输出。执行顺序为：

1. level 9 Supply：`supply-prof-2c-2c-level9-a`
2. level 1 Supply：`supply-prof-2c-2c-level1-a`
3. level 9 TTE：`tte-prof-2c-2c-level9-a`
4. level 1 TTE：`tte-prof-2c-2c-level1-a`
5. 反序复测 level 1 Supply：`supply-prof-2c-2c-level1-b`
6. 反序复测 level 9 Supply：`supply-prof-2c-2c-level9-b`

文件系统 page cache 不能被本实验安全地清除，因此第一次四 run 与反序 Supply
复测应解释为交错对照，而不是严格冷 cache benchmark。`time -v` 的 file-system
inputs 也反映了这一点：多数 run 为 0，level 9 首次 Supply 为 1,240，level 9
首次 TTE 为 8。

## 2. Demand 输入证据

两个 Demand run 都是 complete，Demand contract 都是 passed，输入 fingerprint
相同：
`366afc90c6e2d874d797725368bbd464983c18945da0253424226e559fb3d039`。

| assigned-orders | level 9 | level 1 |
| --- | ---: | ---: |
| gzip bytes | 2,690,755,454 | 3,079,468,693 |
| uncompressed bytes | 10,523,799,657 | 10,523,799,657 |
| data rows | 46,002,707 | 46,002,707 |
| header bytes | 220 | 220 |
| header SHA-256 | `08b0dc6edc215da23a2d46665401b685d95fe763319fd1fe7a8eb298333622cc` | same |
| uncompressed SHA-256 | `087dcf1ad1606874f55e6c3b28a0401462641960406631ef6d6742d47ceebfed` | same |

level 1 增加 `388,713,239` bytes，即 `370.706 MiB` / `0.362 GiB` / `14.446%`。
Demand 的 cluster OD、OD tensor、cluster index、road/POI/distance graph 与
adjacency、POI features 和 category mapping 均为 exact-byte equal；差异仅在
assigned-orders gzip container 与运行 metadata。两个 Demand contract 的 rows、
cluster 数和 tensor shape 一致。

## 3. 资源测量

RSS 是 `/usr/bin/time -v` 报告的 kB 转为 GiB；所有 run exit status 为 0，stage
contract 均为 passed。

| stage / run | level | wall (s) | user (s) | sys (s) | CPU | peak RSS (GiB) | fs in | fs out | major / minor faults |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Supply `...level9-a` | 9 | 886.31 | 836.83 | 49.94 | 100% | 11.206 | 1,240 | 273,312 | 1 / 18,186,246 |
| Supply `...level1-a` | 1 | 881.93 | 833.10 | 49.38 | 100% | 11.237 | 0 | 273,328 | 0 / 18,038,765 |
| Supply `...level1-b` | 1 | 878.40 | 830.09 | 48.86 | 100% | 11.238 | 0 | 273,312 | 0 / 18,034,743 |
| Supply `...level9-b` | 9 | 880.26 | 832.75 | 48.07 | 100% | 11.239 | 0 | 273,312 | 0 / 17,688,587 |
| TTE `...level9-a` | 9 | 651.98 | 782.30 | 122.36 | 138% | 10.914 | 8 | 1,245,200 | 0 / 8,062,164 |
| TTE `...level1-a` | 1 | 653.96 | 784.67 | 123.36 | 138% | 11.057 | 0 | 1,245,200 | 0 / 7,382,496 |

Supply 的主要工作是 driver grouping 与 block compute，CPU utilization 为 100%，
且 level 1 没有稳定的读取惩罚；因此整体是 CPU-bound。TTE 的 CPU utilization
为 138%，主要时间在 raw aggregation 和三轮 imputation，level 1 的增加仅
1.98 s；大规模 Parquet 输出仍构成 I/O 工作，但没有证据表明 gzip 文件变大
明显拖慢 TTE。由于本批次没有加入阶段内 timing，不能把 whole-stage wall 拆成
gzip read、CSV parse 和后续计算的独立秒数；上面的判断依据是交错 wall、CPU、
page-fault 和 file-system counters。

## 4. 下游输出等价

### Supply

四个 Supply run 的 contract metrics 完全一致：orders `46,002,707`、drivers
`199,668`、clusters `100`、slots `13,248`、in-service rows `41,147,628`、
available/fleet rows 各 `1,324,800`。

三个输出 gzip container 的大小在所有 run 中分别为 `4,669,463`、`5,307,085`、
`129,841,557` bytes；对每个 container 解压后执行 SHA-256、byte size、header
和 data-row count 比较，四个 run 的结果均相同：

| output | uncompressed bytes | rows | uncompressed SHA-256 |
| --- | ---: | ---: | --- |
| `supply_available_floor.csv.gz` | 33,860,990 | 1,324,800 | `582442ad90cc478737116b270e663120215f3b53e1efc2fa49b939a4500d116f` |
| `supply_fleet_lower_bound.csv.gz` | 42,100,961 | 1,324,800 | `b8b1633f3005cf76f1bcfa69bcd828f4765642ef1314b698c66efa874db46de4` |
| `supply_inservice_od.csv.gz` | 1,145,370,062 | 41,147,628 | `775639f554fc0ab7a82b403e5af29f244cc034eab81e141e4b2c786f6e575b85` |

这同时证明 row counts、schema/header、列序和数值内容一致。`run_summary.json`
的 SHA-256 也在四个 run 中一致：
`4a10d3800668122d3d077a7871b0207969d85a6fbaec8cbf1f9dee9702ad9b45`。

### TTE

两个 TTE contract 都通过，shape 为 `[13,248, 10,000]`，observed cells
`19,343,240`、inferred cells `64,709,268`、missing cells `48,427,492`。
两个 run 的 distance matrix、representative nodes、`TTE_raw`、`TTE_count`、
`TTE_support`、`TTE_hops` 和 `TTE_imputed` 文件 size 与 SHA-256 全部相同，
因此 Parquet schema、dtypes、values、matrix axes 和 distance asset 均为
exact-byte equal。gzip container hash 可以不同，但这里的 Supply/TTE 输出
内容没有差异。

## 5. Wall 变化与全流程收益

主序列的 level 9 → level 1 对照为：

| stage | level 9 wall (s) | level 1 wall (s) | level 1 relative delta |
| --- | ---: | ---: | ---: |
| Supply | 886.31 | 881.93 | -4.38 s / -0.494% |
| TTE | 651.98 | 653.96 | +1.98 s / +0.304% |
| Supply + TTE | 1,538.29 | 1,535.89 | -2.40 s |

Supply 反序复测为 level 1 `878.40 s`、level 9 `880.26 s`；两次 Supply 的均值
为 level 9 `883.285 s`、level 1 `880.165 s`，level 1 快 `3.12 s` / `0.353%`。
按这个更保守的 Supply 均值，Supply+TTE 合计变化为 `-1.14 s`，仍不是回退。

已知 Demand wall 为 level 9 `3,490.946 s`、level 1 `3,050.770 s`，收益
`440.176 s`。因此从 Demand 到 TTE 的 raw-only wall：

- 主序列：level 9 `5,029.236 s` → level 1 `4,586.660 s`，净减少
  `442.576 s`；
- 使用 Supply 重复均值：level 9 `5,026.211 s` → level 1 `4,584.895 s`，
  净减少 `441.316 s`。

两种口径都满足“下游回退不超过 120 s”和“Demand 到 TTE 净收益至少 300 s”。
level 1 是全流程 Pareto 改进：Demand 省时约 7.34 分钟，下游没有显著 wall
回退，Supply/TTE RSS 增长均低于 5%，assigned gzip 体积增长低于 15% 优先上限，
且所有下游输出严格等价。

因此保留 level 1。由于这是一个有意的 `14.446%` 体积换速度取舍，后续维护中
有必要在配置注释或性能文档中保留该事实；本批次的审计文档已记录，不额外修改
README 或生产代码。

## 6. 验证与提交边界

- 完整测试套件：`270 passed, 59 warnings`。
- JSON 文档解析、`git diff --check`、privacy scan 和 tracked-file size scan 均通过。
- Demand：两个既有 run 未重新运行，均保留且 contract complete/passed。
- Supply/TTE：所有对照 run complete/passed；没有 resume，也没有复用旧
  Supply/TTE outputs。
- timing：每次 Supply/TTE 均由 `/usr/bin/time -v` 包围。
- output：未提交 Supply/TTE run outputs、timing files、gzip 实验文件或 raw data。
- 本批次没有新增通用 profiling code，因此没有 profiling infrastructure commit。
- 下一步优化实验不在本批次实施；level 3、stage 内 read/parse profiling 或
  pipeline 并行化均留待后续批次。
