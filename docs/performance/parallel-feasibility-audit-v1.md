# Parallel Feasibility Audit v1

结论先行：Demand spatial matching 和 TTE within-hop loop 都有可见的 process-level 加速，但两者在 4/8 workers 都超过了本批次的进程树 RSS 门槛；Supply 的 block-local COO 归并在整数结果上严格等价，4 workers 有 2.66× 加速，但同样不满足 RSS 门槛。因此本批次不推荐进入生产并行实现，也没有修改生产算法。

## 1. 最值得实施的并行候选

按计算收益排序：

1. Demand spatial matching：process 4/8 分别为 3.43×/5.01×，8 相比 4 仍有 1.46× 增益；但 RSS 为串行的 4.37×/7.36×，未通过内存验收。Thread 4/8 也有 3.28×/4.50×，但 RSS 为 2.56×/3.86×。
2. TTE within-hop column loop：process 4/8 为 1.99×/2.19×，严格保持两轮 frozen snapshot 和 `np.argmin` first-min；RSS 为 3.59×/6.04×，未通过内存验收。线程路径没有加速。
3. Supply block-local COO：process 4 为 2.66×，A/B/Fc/Fg 四个整数结果与串行严格相等；worker 没有复制完整 dense A，局部 COO 仅 0.42 MiB。但 4-worker RSS 为串行的 3.41×，不能推荐直接落地。

这里的“值得实施”表示计算结构值得保留为后续候选，不表示本批次已经达到生产准入。当前没有候选同时满足速度、RSS、确定性和兼容路径的全部条件。

## 2. 实际 speedup 曲线

### Demand spatial matching

输入为正式 59,096-segment 分区和确定性合成坐标；每个 chunk 500,000 行，共 8 chunks、4,000,000 行；pickup 与 dropoff 都调用当前 `shapely_points + sjoin_nearest` 路径。计时包含点构造、CRS 转换、空间索引使用、nearest query、结果返回和父进程恢复，不写 SQLite。

| mode | workers | wall (s) | speedup | efficiency | rows/s | process-tree CPU (host %) | startup (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| single | 1 | 104.034 | 1.00× | 1.000 | 38,460 | 10.0 | 0.000 |
| threads | 2 | 53.067 | 1.96× | 0.981 | 75,417 | 17.7 | 0.000 |
| threads | 4 | 31.705 | 3.28× | 0.820 | 126,273 | 29.7 | 0.000 |
| threads | 8 | 23.117 | 4.50× | 0.563 | 173,252 | 47.3 | 0.001 |
| processes (fork) | 2 | 53.020 | 1.96× | 0.981 | 75,517 | 18.7 | 0.023 |
| processes (fork) | 4 | 30.311 | 3.43× | 0.858 | 132,286 | 34.0 | 0.044 |
| processes (fork) | 8 | 20.766 | 5.01× | 0.626 | 193,616 | 60.4 | 0.078 |

4 workers 到 8 workers 的增益：threads 1.38×，processes 1.46×。因此 Demand 的 CPU 计算本身满足速度曲线要求，但不满足 RSS 要求。

### TTE within-hop column loop

使用 192 timestamps × 552 OD columns 的代表性合成矩阵，执行 2 个串行 rounds；每一轮只读 round-start `source_value`/`support_src` snapshot，结果按 target column 确定性提交。

| mode | workers | wall (s) | speedup | efficiency | process-tree CPU (host %) | startup (s) |
|---|---:|---:|---:|---:|---:|---:|
| single | 1 | 1.240 | 1.00× | 1.000 | 9.5 | 0.000 |
| threads | 2 | 1.321 | 0.94× | 0.469 | 9.9 | 0.001 |
| threads | 4 | 1.402 | 0.88× | 0.221 | 10.0 | 0.001 |
| threads | 8 | 1.397 | 0.89× | 0.111 | 10.2 | 0.002 |
| processes (fork) | 2 | 0.883 | 1.41× | 0.702 | 12.1 | 0.026 |
| processes (fork) | 4 | 0.624 | 1.99× | 0.496 | 12.4 | 0.048 |
| processes (fork) | 8 | 0.567 | 2.19× | 0.273 | 8.7 | 0.079 |

8 workers 相比 4 workers 的 process 增益为 1.10×，只有明确但很小的增益；4-worker 速度达到阈值，但 RSS 未达到阈值。

### Supply block-local COO

使用 1,200 synthetic drivers、19,200 rows、4 个 driver blocks、130 slots 和 48 clusters。worker 只返回局部 COO partial；完整 dense A 只在 parent reducer 中存在。

| mode | workers | wall (s) | speedup | efficiency | process-tree peak RSS (MiB) | partial COO (MiB) |
|---|---:|---:|---:|---:|---:|---:|
| serial | 1 | 0.481 | 1.00× | 1.000 | 169.7 | 0.42 |
| processes (fork) | 1 | 0.454 | 1.06× | 1.061 | 263.1 | 0.42 |
| processes (fork) | 2 | 0.262 | 1.84× | 0.918 | 367.7 | 0.42 |
| processes (fork) | 4 | 0.181 | 2.66× | 0.664 | 578.7 | 0.42 |

Supply 的 4-worker 速度满足 1.8×，但本实验按用户要求只测到 4 workers，未取得 8-vs-4 曲线；同时 RSS 已失败，所以不推荐生产改造。

## 3. 内存增长曲线

Demand 的串行 process-tree peak RSS 为 686.7 MiB。

| mode | 1 worker | 2 workers | 4 workers | 8 workers |
|---|---:|---:|---:|---:|
| threads RSS (MiB) | 686.7 | 1,318.9 | 1,759.5 | 2,648.9 |
| threads / serial | 1.00× | 1.92× | 2.56× | 3.86× |
| processes RSS (MiB) | 686.7 | 1,963.2 | 2,999.9 | 5,055.2 |
| processes / serial | 1.00× | 2.86× | 4.37× | 7.36× |

TTE 的串行 peak RSS 为 142.5 MiB；threads 2/4/8 为 146.2/146.2/146.7 MiB，processes 2/4/8 为 331.8/511.2/860.7 MiB，即 processes 为 2.33×/3.59×/6.04×。每轮 snapshot 约 0.81 MiB；fork 实验虽利用了 copy-on-write，进程池和每列返回仍使总树 RSS 快速增长。

Supply 的串行 peak RSS 为 169.7 MiB；process 2/4 为 367.7/578.7 MiB，即 2.17×/3.41×。worker dense A bytes 始终为 0；parent reducer 的 dense A 为 2.29 MiB，局部 COO 总量为 0.42 MiB。这个结果支持“局部 sparse partial + 单 reducer”的可行性判断，但不支持当前并行执行规模的内存准入。

## 4. threads vs processes

Demand：threads 4/8 已分别达到 3.28×/4.50×，接近但略慢于 processes 4/8 的 3.43×/5.01×，且没有输入 pickle。线程共享 segment frame，但每个并发 chunk 同时保留点、join 和返回数组，RSS 仍随 worker 数增长。processes 的 fork startup 为 0.023/0.044/0.078 s；每个 500k-row 输入 payload 的完整 pickle 约 16.0 MB，完整返回 payload 约 56.0 MB，传输和每个 worker 的 GeoPandas/GEOS 中间对象是主要内存风险。

TTE：threads 受 Python/pandas 列循环与 validation 工作限制，2/4/8 workers 均不如 serial；processes 能取得 1.41×/1.99×/2.19×。本实验使用 Linux fork 让 worker 只读继承 round snapshot；spawn 需要显式复制或序列化 snapshot，跨平台成本会更高。

Supply：只测 processes，因为实验目标是验证 block-local accumulator 和 sparse reduction；4 blocks 可并行且整数结果相等，但 DataFrame block pickle、每个进程的 pandas 中间表和父 reducer 仍造成 RSS 放大。

CPU 指标同时报告了 `user_cpu_s`/`sys_cpu_s` 和进程树 CPU utilization；host 百分比以 12 logical CPUs 为分母，one-core 百分比保留在 JSON 中。Supply 的单次运行低于 0.5 s，CPU 采样粒度使其 CPU 百分比仅作辅助，不用于夸大 speedup。

## 5. 确定性与输出风险

Demand 每个 worker 只返回 `chunk_id`、连续 `row_id`、pickup/dropoff `seg_id`、distance 和计算时间；父进程按 `chunk_id` 后按 `row_id` 恢复。所有模式完成顺序都实际出现过非自然顺序，但恢复顺序固定为 0…7。与 single 逐 chunk 比较的 `seg_id`、distance、row order、unmatched mask 全部严格相等。

Demand 另有重复坐标和两个 segment 等距的 tiny probe：threads/processes 与 serial 的选择完全相同。没有对 segment GeoDataFrame 排序，没有加入 seg_id tie-break，没有改变 `max_distance`，也没有合并 pickup/dropoff query。因而本实验没有观察到新的 tie-break 风险；生产实现仍必须保留现有 segment 行序和 `sjoin_nearest` 参数。

TTE 每轮在计算开始前冻结 source snapshots；target columns 互斥，父进程按原始 column 顺序提交。`value`、`support`、`hops` 三个数组在 2/4/8 threads/processes 与 serial 逐元素严格相等，包含 `np.argmin` first-min 行为。

Supply 将 driver 完整地放入一个 block，局部去重后才产生 COO；parent 按 block id 确定性归并，A/B/Fc/Fg 四个整数数组全部严格相等。若改为直接共享 dense A，写入顺序、重复索引和 crash recovery 都会引入额外风险，因此本实验没有这样做。

## 6. 工程复杂度

### A. worker spatial matching + 单 reducer 写 SQLite

- 内存：worker 数量乘以当前 chunk 的 GeoPandas/GEOS 中间对象；reducer 需要保留未按序完成的 chunk，必须有有界 buffer 或临时 compact 结果。
- 磁盘：低；仍只有正式 staging SQLite 和最终输出。
- crash recovery：需要 chunk checkpoint、事务边界和重跑幂等规则；worker 完成但 reducer 尚未 commit 时必须可重做。
- resume：可以按输入文件/chunk 恢复，但 stage rows 不能依赖完成顺序。
- chunk ordering：reducer 必须按 `(source_file, source_row)` 或固定 chunk/row 顺序写入，不能按 future 完成顺序写。
- stage_id：现有 SQLite `AUTOINCREMENT` 只有在单 reducer 按确定顺序 append 时才保持正式输出顺序；worker 不应自行生成 stage_id。
- service-label 语义：应在 staging 完整后继续使用现有按 driver、departure、finish、stage_id 的 ordered select；不能在 worker 内局部 label 后直接合并。
- fork/spawn：Linux fork 可降低 segment frame 的显式复制，但 fork 后使用 GEOS/索引需要谨慎；spawn 需要重新加载或序列化 segment frame，启动和 RSS 更高。
- 复杂度：中等；需要 bounded result queue、checkpoint、单写者 back-pressure 和失败重试。

### B. worker 写独立 Parquet/SQLite shards + 确定性归并

- 内存：worker 可以在完成一个 chunk 后释放中间对象，parent 不必缓存所有返回行；比 A 更容易做有界内存。
- 磁盘：高，需要保存所有 shard 及 manifest；Parquet 更适合追加完成后的不可变 shard，SQLite shard 还会有重复 schema/index 成本。
- crash recovery：最好；完成的 shard 可校验 hash 后复用，失败只重做缺失或损坏 chunk。
- resume：自然按 shard manifest 恢复，但必须验证输入/config/segment partition fingerprint。
- chunk ordering：merge 阶段必须按 chunk_id、row_id 稳定读取；文件系统完成顺序不可成为排序依据。
- stage_id：必须在确定性归并或单 SQLite reducer 中生成；不能使用 worker 本地自增 id 直接拼接。
- service-label 语义：必须在所有 shard 归并到 staging 后执行现有全局 ordered service labeling；局部 label 会切断跨 shard 的同 driver overlap component。
- fork/spawn：shard 方案更容易跨平台，但 spawn 仍需处理 segment frame 的加载/传输；应把只读 partition 初始化和 shard manifest 设计成显式 contract。
- 复杂度：高于 A；新增 shard schema、manifest、checksum、merge、cleanup、resume 和输出等价验证。

本批次只设计 A/B，没有实施其中任何一种。

## 7. 建议实施或放弃

按本批次验收阈值逐项判断：

- Demand：4 workers speedup 通过，8 相比 4 有增益，输出严格相等且保留 single-worker 路径；RSS 不通过，放弃当前生产并行方案。
- TTE：process 4 speedup 通过，8 相比 4 有小幅增益，输出严格相等且 rounds 仍串行；RSS 不通过，放弃当前生产并行方案。
- Supply：process 4 speedup 通过、输出严格相等且局部 COO 结构成立；未测 8 workers，且 RSS 不通过，放弃当前生产并行方案。

最终建议：本 Batch 不进入并行实现阶段；保留 `workers=1` 的兼容思路和上述 benchmark 证据，未来若要重启评估，先解决 bounded result/shard reducer、共享或分块只读空间索引、spawn 复制成本和进程树 RSS，再重新跑同一严格等价套件。没有在本阶段实施 nearest-query、SQLite、service-label、STRtree cache、export 或生产算法改造。

## 验证记录

- 完整测试：`251 passed, 53 warnings`。
- Demand：8 chunks × 500,000 rows；single、threads 2/4/8、processes 2/4/8 全部完成，strict equality 全部通过。
- TTE：192 × 552、2 rounds；single、threads 2/4/8、processes 2/4/8 的 value/support/hops 全部严格相等。
- Supply：1,200 drivers、19,200 rows、4 blocks；A/B/Fc/Fg 全部严格相等，worker dense A 为 0。
- 代码状态：没有生产算法改动；benchmark harness 和 raw benchmark outputs 保持 gitignored。
- 隐私：交付文档不含私有绝对路径、raw 数据、订单/司机/坐标样本或本机秘密。
