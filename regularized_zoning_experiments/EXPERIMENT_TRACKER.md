# 实验跟踪

## 当前状态

- [x] 建立独立实验目录。
- [x] 建立 `config_v1.yaml`，集中管理输入路径、输出路径、初始化、超参数网格、搜索参数和评估阈值。
- [x] 实现 `run_regularized_search.py`。
- [x] 实现 `evaluate_regularized.py`。
- [x] 完成语法编译检查。
- [x] 完成合成小图搜索单元检查。
- [x] 完成真实数据 smoke test：`louvain` 初始化、单个参数组合、`max_passes=0`。
- [x] 运行完整 48 组实验。
- [x] 运行完整实验评估。
- [x] 检查 `candidate_selection.csv` 中是否出现满足成功标准的候选：严格成功标准下暂无候选通过。
- [x] 绘制 V1 最佳超参数切分地图和实验相关可视化。
- [x] 改进路网切分地图配色：使用邻接感知高对比配色、halo 线条和更弱 connector underlay。
- [ ] 根据结果决定是否加入 shape regularization 或 merge/split 操作。

## Smoke Test

已验证真实数据链路可以写出：

- `run_manifest.csv`
- `objective_trace.csv`
- `metrics_regularized.csv`
- `connector_type_metrics_regularized.csv`
- `pareto_summary.csv`
- `candidate_selection.csv`

Smoke 输出位于：

```text
regularized_zoning_experiments/runs/smoke_v1/
```

`runs/` 已加入 `.gitignore`，不会作为源码提交。

## Full V1 Run

运行时间：

- regularized search: 约 39.4 分钟。
- benchmark evaluation: 约 5.1 分钟。

输出目录：

```text
regularized_zoning_experiments/runs/v1_fourth_ring_road_poi_order/
```

输出表：

- `tables/run_manifest.csv`: 48 个 regularized runs。
- `tables/objective_trace.csv`: 每次接受移动后的目标函数轨迹。
- `tables/metrics_regularized.csv`: 6 个 baseline + 48 个 regularized 结果。
- `tables/connector_type_metrics_regularized.csv`
- `tables/pareto_summary.csv`
- `tables/candidate_selection.csv`

基本检查：

- regularized rows: 48。
- baseline rows: 6。
- 所有 regularized 结果 `connected_cluster_ratio = 1.0`。
- 严格 `passes_region_growing_goal = 0`。
- 严格 `passes_louvain_goal = 0`。

关键观察：

- `regularized_region_growing_lc0p2_lr1p0` 相比 `demand_region_growing`：
  - connector cut: `0.141717 -> 0.053188`，明显降低。
  - OD sparsity: `0.798725 -> 0.797453`，略有改善。
  - order count CV: `0.865116 -> 0.853330`，略有改善。
  - continuity cut: `0.028813 -> 0.032294`，上升，因此未通过严格成功标准。
- `regularized_louvain_lc1p0_lr4p0` 相比 `louvain`：
  - connector cut: `0.082423 -> 0.025126`，明显降低。
  - OD sparsity: `0.822861 -> 0.814965`，改善。
  - order count CV: `1.170973 -> 1.062043`，改善。
  - continuity cut: `0.012019 -> 0.019798`，上升，因此未通过严格成功标准。
- `regularized_leiden_lc0p2_lr1p0` 相比 `leiden`：
  - connector cut: `0.062111 -> 0.023365`，明显降低。
  - OD sparsity: `0.838280 -> 0.834587`，改善。
  - order count CV: `1.202695 -> 1.148482`，改善。
  - capacity violation: `0.827338 -> 0.820144`，略有改善。
  - continuity cut: `0.011591 -> 0.015244`，上升。

结论：

- V1 正则化局部搜索能稳定保持连通性，并能显著降低 connector cut。
- V1 对 OD sparsity 和 order balance 有温和改善。
- 当前目标中 continuity 保持不足，局部移动会用 continuity cut 上升换取 connector cut 和容量收益。
- 下一轮优先调整 continuity：提高 `alpha_cont` 或将同名/continuity 边切断设为更强的移动惩罚；暂不急于加入 shape 或 merge/split。

## Visualization

已实现并运行：

```text
regularized_zoning_experiments/visualize_regularized_results.py
```

最佳超参数按 balanced score 自动选择：

- best run: `regularized_louvain_lc1p0_lr4p0`
- initialization: `louvain`
- `lambda_c = 1.0`
- `lambda_r = 4.0`
- balanced score: `0.355829`
- `num_clusters = 137`
- `connected_cluster_ratio = 1.0`

最佳结果核心指标：

- `od_sparsity = 0.814965`
- `connector_edge_cut_ratio = 0.025126`
- `continuity_edge_cut_ratio = 0.019798`
- `order_count_cv = 1.062043`
- `capacity_violation_ratio = 0.744526`
- `historical_avg_wape = 0.486472`

新增输出表：

- `tables/best_selection_summary.csv`
- `tables/balanced_score_ranking.csv`
- `tables/metrics_regularized_with_scores.csv`

新增图件目录：

```text
regularized_zoning_experiments/runs/v1_fourth_ring_road_poi_order/figures/
```

图件清单：

- `best_partition_map.png`
- `baseline_vs_best_maps.png`
- `best_connector_zoom.png`
- `tradeoff_od_sparsity_vs_continuity_cut.png`
- `tradeoff_order_cv_vs_connector_cut.png`
- `tradeoff_wape_vs_road_integrity.png`
- `tradeoff_network_diameter_vs_od_sparsity.png`
- `heatmap_louvain.png`
- `heatmap_leiden.png`
- `heatmap_demand_region_growing.png`
- `objective_trace_best.png`
- `objective_final_by_setting.png`
- `best_vs_baselines_metrics.png`

实现备注：

- 当前环境的 `matplotlib` 在 `savefig` 时触发 native crash，因此统计图也改为纯 PIL 绘制。
- 所有可视化均只写入本轮 `runs/v1_fourth_ring_road_poi_order/`，不会覆盖主流程 baseline figures。

## Map Color Contrast Update

已按簇邻接关系改进路网切分地图配色：

- 使用 `adjacency_contrast` 策略替代原先的 `TAB20` 循环配色。
- 根据 relation graph 构建 cluster adjacency graph。
- 按簇邻接度从高到低分配高对比颜色。
- 对 cluster road 先绘制白色 halo，再绘制彩色主线。
- connector underlay 降低透明度，避免干扰主分区颜色。

重新生成图件：

- `figures/best_partition_map.png`
- `figures/baseline_vs_best_maps.png`
- `figures/best_connector_zoom.png`

新增颜色诊断表：

- `tables/cluster_color_diagnostics_best.csv`

颜色诊断结果：

- best run: `regularized_louvain_lc1p0_lr4p0`
- best run cluster count: `137`
- 有邻接关系的簇数: `85`
- 最小相邻颜色距离: `118.633891`
- 5% 分位相邻颜色距离: `124.0`
- 中位相邻颜色距离: `158.173955`
- 配置阈值 `min_color_distance = 90.0`，当前满足阈值。

## Reproduce

```powershell
C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe regularized_zoning_experiments\run_regularized_search.py regularized_zoning_experiments\config_v1.yaml
C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe regularized_zoning_experiments\evaluate_regularized.py regularized_zoning_experiments\config_v1.yaml
C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe regularized_zoning_experiments\visualize_regularized_results.py regularized_zoning_experiments\config_v1.yaml
```

## 下一步

建议的下一轮实验：

- 新增 `alpha_cont` 网格，例如 `[2.0, 4.0, 8.0]`，保持 `alpha_conn=1.0`。
- 或新增 hard guard：若移动导致 continuity cut 增加超过阈值，则拒绝。
- 保留当前 V1 输出作为对照，不覆盖本轮结果。
