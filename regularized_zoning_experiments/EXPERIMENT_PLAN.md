# 正则化需求自适应分区快速验证实验

## 目标

在 `fourth_ring + road_poi_order` 已有产物上，独立验证正则化需求自适应分区是否能在路网结构保持和订单需求适应之间取得更好的折中。

第一版目标函数为：

```text
L = lambda_c * R_cap
  + lambda_g * R_graph
  + lambda_r * (alpha_cont * R_cont + alpha_conn * R_conn)
```

其中 shape 和 semantic 暂不进入求解目标，只作为评估指标。

## 文件结构

- `config_v1.yaml`: 实验路径、初始化、超参数网格、搜索停止条件和评估阈值。
- `run_regularized_search.py`: 读取 config，运行正则化局部搜索，输出 cluster 结果和 objective trace。
- `evaluate_regularized.py`: 读取 baseline 与 regularized 结果，复用 `src/utils_metric.py` 计算统一指标。
- `visualize_regularized_results.py`: 自动选择 balanced-score 最佳候选，绘制最佳切分地图、trade-off 图、参数热力图和优化轨迹图。
- `runs/`: 生成结果目录，已加入 `.gitignore`。

## 实验矩阵

- scope: `fourth_ring`
- graph variant: `road_poi_order`
- initializations:
  - `louvain`
  - `leiden`
  - `demand_region_growing`
- grid:
  - `lambda_c: [0.2, 0.5, 1.0, 2.0]`
  - `lambda_r: [0.5, 1.0, 2.0, 4.0]`
- total regularized candidates: `3 * 16 = 48`

## 运行命令

使用项目 conda 环境：

```powershell
C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe regularized_zoning_experiments\run_regularized_search.py regularized_zoning_experiments\config_v1.yaml
C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe regularized_zoning_experiments\evaluate_regularized.py regularized_zoning_experiments\config_v1.yaml
C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe regularized_zoning_experiments\visualize_regularized_results.py regularized_zoning_experiments\config_v1.yaml
```

## 输出

默认输出到：

```text
regularized_zoning_experiments/runs/v1_fourth_ring_road_poi_order/
```

关键表：

- `tables/run_manifest.csv`
- `tables/objective_trace.csv`
- `tables/metrics_regularized.csv`
- `tables/pareto_summary.csv`
- `tables/candidate_selection.csv`
- `tables/best_selection_summary.csv`
- `figures/best_partition_map.png`
- `figures/baseline_vs_best_maps.png`
- `figures/best_connector_zoom.png`
- `figures/tradeoff_*.png`
- `figures/heatmap_*.png`
- `figures/objective_trace_best.png`
- `figures/best_vs_baselines_metrics.png`

## 成功标准

- 所有 regularized 结果满足 `connected_cluster_ratio = 1.0`。
- 至少一个 `regularized_region_growing` 相比原始 `demand_region_growing` 降低 continuity/connector cut，同时 OD sparsity 不明显变差。
- 至少一个 `regularized_louvain` 相比原始 `louvain` 降低 `order_count_cv` 或 `capacity_violation_ratio`，且结构 cut 增幅可控。
