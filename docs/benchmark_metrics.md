# 路网中心分区 Benchmark 诊断指标设计

本文档整理面向“以 road segment 为基本单元、connector 作为关系边”的路网中心分区评估指标。每一个实验结果建议对应一行总表：

```text
graph_variant, algorithm, params, num_clusters, metrics...
```

其中：

- `graph_variant`：`road_only` / `road_poi` / `road_poi_order`
- `algorithm`：`leiden` / `louvain` / `metis` / `skater` / `region_growing` / `proposed`
- `params`：算法参数，例如 `resolution` 或 `K`

---

## 0. 基本记号

设普通路段集合为：

$$
\mathcal{S}=\{s_1,s_2,\ldots,s_N\}
$$

聚类/分区结果为：

$$
\mathcal{P}=\{C_1,C_2,\ldots,C_K\}
$$

其中，$C_k$ 表示第 $k$ 个路段簇，$z_i$ 表示路段 $s_i$ 的簇标签。

路段关系图为：

$$
G_R=(\mathcal{S},\mathcal{E})
$$

其中关系边可分为：

$$
\mathcal{E}
=
\mathcal{E}^{direct}
\cup
\mathcal{E}^{connector}
\cup
\mathcal{E}^{cont}
$$

分别表示：

- $\mathcal{E}^{direct}$：普通路段之间的直接拓扑邻接关系；
- $\mathcal{E}^{connector}$：由 connector / link / ramp 压缩得到的连接线介导关系；
- $\mathcal{E}^{cont}$：同一道路或同一交通走廊的连续性关系。

每条关系边 $(i,j)$ 有权重 $w_{ij}$。

---

# A. 基本规模指标

这组指标用于描述分区的基本规模，判断是否出现过多极小簇或超大簇。

## A1. 簇数量

$$
K = |\mathcal{P}|
$$

指标名：

```text
num_clusters
```

---

## A2. 每簇路段数

第 $k$ 个簇的路段数：

$$
n_k = |C_k|
$$

总体平均：

$$
\bar{n}=\frac{1}{K}\sum_{k=1}^{K}n_k
$$

建议输出：

```text
mean_segments_per_cluster
min_segments_per_cluster
max_segments_per_cluster
median_segments_per_cluster
```

---

## A3. 每簇道路总长度

设路段 $s_i$ 的长度为 $l_i$，则簇 $C_k$ 的道路总长度为：

$$
L_k=\sum_{s_i\in C_k}l_i
$$

总体平均道路长度：

$$
\bar{L}=\frac{1}{K}\sum_{k=1}^{K}L_k
$$

建议输出：

```text
mean_cluster_length_m
min_cluster_length_m
max_cluster_length_m
median_cluster_length_m
```

---

## A4. 小簇比例

定义小簇阈值，例如：

$$
n_k < n_{\min}
\quad \text{or} \quad
L_k < L_{\min}
\quad \text{or} \quad
O_k < O_{\min}
$$

其中 $O_k$ 是簇内订单数。

小簇比例为：

$$
SmallClusterRatio=
\frac{
\#\{C_k: n_k<n_{\min} \ \text{or} \ L_k<L_{\min} \ \text{or} \ O_k<O_{\min}\}
}{K}
$$

指标名：

```text
small_cluster_ratio
```

---

# B. 连通性指标

这组指标用于判断每个簇在路段关系图上是否连通。对路网运营分区来说，簇内部连通性是硬性要求之一。

## B1. 连通簇比例

对每个簇 $C_k$，取其在 $G_R$ 上的诱导子图：

$$
G_R[C_k]
$$

若 $G_R[C_k]$ 连通，则记：

$$
I_k^{conn}=1
$$

否则：

$$
I_k^{conn}=0
$$

连通簇比例：

$$
ConnRate=
\frac{1}{K}\sum_{k=1}^{K}I_k^{conn}
$$

指标名：

```text
connected_cluster_ratio
```

越高越好。

---

## B2. 碎片数量

设簇 $C_k$ 的诱导子图中连通分量数为：

$$
cc_k = \#CC(G_R[C_k])
$$

总碎片数：

$$
FragmentCount=
\sum_{k=1}^{K}(cc_k-1)
$$

建议输出：

```text
total_fragment_count
mean_components_per_cluster
max_components_per_cluster
```

越低越好。

---

## B3. 最大连通分量占比

设簇 $C_k$ 中最大连通分量的路段数为：

$$
|LCC(C_k)|
$$

则最大连通分量占比为：

$$
LCCRatio_k=
\frac{|LCC(C_k)|}{|C_k|}
$$

总体平均：

$$
MeanLCCRatio=
\frac{1}{K}\sum_{k=1}^{K}LCCRatio_k
$$

建议输出：

```text
mean_largest_cc_ratio
min_largest_cc_ratio
```

越高越好。

---

# C. 道路完整性指标

这组指标用于评价同一道路或同一走廊是否被过度切碎。

## C1. Road-name split count

对道路名称 $r$，设属于该道路的路段集合为：

$$
\mathcal{S}_r=\{s_i: name_i=r\}
$$

该道路被分到的簇数量为：

$$
Split(r)=|\{z_i:s_i\in \mathcal{S}_r\}|
$$

道路长度加权平均 split count：

$$
WeightedSplit=
\frac{
\sum_r L_r \cdot Split(r)
}{
\sum_r L_r
}
$$

其中：

$$
L_r=\sum_{s_i\in \mathcal{S}_r}l_i
$$

建议输出：

```text
mean_road_name_num_clusters
weighted_mean_road_name_num_clusters
```

越低越好。

---

## C2. Dominant cluster share

对道路 $r$，计算其在主导簇中的长度占比：

$$
DominantShare(r)=
\frac{
\max_k \sum_{s_i\in \mathcal{S}_r \cap C_k}l_i
}{
\sum_{s_i\in \mathcal{S}_r}l_i
}
$$

道路长度加权平均：

$$
WeightedDominantShare=
\frac{
\sum_r L_r \cdot DominantShare(r)
}{
\sum_r L_r
}
$$

建议输出：

```text
mean_dominant_cluster_share
weighted_mean_dominant_cluster_share
```

越高说明道路整体性越好。

---

## C3. Continuity edge cut ratio

对连续性关系边集合 $\mathcal{E}^{cont}$，计算被切断的连续性边权比例：

$$
ContCutRatio=
\frac{
\sum_{(i,j)\in \mathcal{E}^{cont}}w_{ij}\mathbf{1}[z_i\neq z_j]
}{
\sum_{(i,j)\in \mathcal{E}^{cont}}w_{ij}
}
$$

建议输出：

```text
continuity_edge_cut_ratio
continuity_edge_cut_weight
continuity_edge_total_weight
```

越低越好。

---

## C4. Same-road adjacent cut ratio

只考虑相邻且同名的路段对：

$$
\mathcal{E}^{same}=\{(i,j): (i,j)\in \mathcal{E}, name_i=name_j, name_i\neq \varnothing\}
$$

同路相邻切断率：

$$
SameRoadCutRatio=
\frac{
\sum_{(i,j)\in \mathcal{E}^{same}}\mathbf{1}[z_i\neq z_j]
}{
|\mathcal{E}^{same}|
}
$$

指标名：

```text
same_road_adjacent_cut_ratio
```

越低越好。

---

# D. Connector 保持指标

这组指标用于判断 connector / link / ramp 被压缩为关系边后，算法是否有效保留这些连接关系。

## D1. Connector edge cut ratio

对 connector-mediated 关系边集合 $\mathcal{E}^{connector}$，计算被切断比例：

$$
ConnectorCutRatio=
\frac{
\sum_{(i,j)\in \mathcal{E}^{connector}}w_{ij}\mathbf{1}[z_i\neq z_j]
}{
\sum_{(i,j)\in \mathcal{E}^{connector}}w_{ij}
}
$$

建议输出：

```text
connector_edge_cut_ratio
connector_edge_cut_weight
connector_edge_total_weight
```

越低越好。

---

## D2. Connector cut ratio by type

对每一种 connector 类型 $h$，例如：

```text
motorway_link
trunk_link
primary_link
secondary_link
tertiary_link
```

定义对应关系边集合：

$$
\mathcal{E}^{connector}_h
$$

类型 $h$ 的切断率为：

$$
ConnectorCutRatio_h=
\frac{
\sum_{(i,j)\in \mathcal{E}^{connector}_h}w_{ij}\mathbf{1}[z_i\neq z_j]
}{
\sum_{(i,j)\in \mathcal{E}^{connector}_h}w_{ij}
}
$$

建议输出：

```text
connector_type
total_edges
cut_edges
cut_ratio
cut_weight_ratio
```

---

## D3. Direct edge cut ratio

对普通直接邻接边集合 $\mathcal{E}^{direct}$，计算：

$$
DirectCutRatio=
\frac{
\sum_{(i,j)\in \mathcal{E}^{direct}}w_{ij}\mathbf{1}[z_i\neq z_j]
}{
\sum_{(i,j)\in \mathcal{E}^{direct}}w_{ij}
}
$$

指标名：

```text
direct_edge_cut_ratio
```

它可以与：

```text
connector_edge_cut_ratio
continuity_edge_cut_ratio
```

一起比较，判断不同关系类型的保持程度。

---

# E. 区域形态指标

虽然分区本体是路段簇，但最终面向运营区域，仍然需要避免过度长条化、形态破碎或边界复杂。

建议先对簇内 road segments 做 buffer，例如 30m 或 50m，然后 dissolve 得到簇的近似面状区域 $P_k$。

设：

- $A_k$：簇 $C_k$ 的 buffer polygon 面积；
- $P_k^{peri}$：簇 $C_k$ 的 polygon 周长；
- $H_k$：簇 $C_k$ 的 convex hull；
- $A(H_k)$：convex hull 面积。

---

## E1. Compactness / Polsby-Popper compactness

$$
Compactness_k=
\frac{4\pi A_k}{(P_k^{peri})^2}
$$

总体平均：

$$
MeanCompactness=
\frac{1}{K}\sum_{k=1}^{K}Compactness_k
$$

建议输出：

```text
mean_compactness
median_compactness
min_compactness
```

越高越紧凑。

注意：路网 buffer 本身会比较细碎，因此该指标主要用于算法间相对比较。

---

## E2. Convex hull ratio

Convex hull ratio 用于衡量簇的面状紧凑程度和凹陷程度。

定义为：

$$
ConvexHullRatio_k=
\frac{A_k}{A(H_k)}
$$

其中：

- $A_k$：簇 buffer 后的实际面积；
- $A(H_k)$：其凸包面积。

总体平均：

$$
MeanConvexHullRatio=
\frac{1}{K}\sum_{k=1}^{K}ConvexHullRatio_k
$$

建议输出：

```text
mean_convex_hull_ratio
median_convex_hull_ratio
min_convex_hull_ratio
```

取值范围通常为：

$$
0<ConvexHullRatio_k\leq 1
$$

越接近 1，说明簇越接近凸形、越紧凑；越低，说明簇可能越细长、越弯曲或越破碎。

---

## E3. Elongation

对簇 polygon 的 minimum rotated rectangle，设长边为 $a_k$，短边为 $b_k$，且 $a_k \geq b_k$，则：

$$
Elongation_k=
\frac{a_k}{b_k+\epsilon}
$$

总体平均：

$$
MeanElongation=
\frac{1}{K}\sum_{k=1}^{K}Elongation_k
$$

建议输出：

```text
mean_elongation
median_elongation
max_elongation
elongated_cluster_ratio
```

其中：

$$
ElongatedClusterRatio=
\frac{
\#\{C_k: Elongation_k>\tau_{elong}\}
}{K}
$$

越低越好。

---

## E4. Network diameter

在簇内部路段图上，使用道路长度作为边权，计算簇内近似网络直径：

$$
Diameter_k=
\max_{s_i,s_j\in C_k}d_{G_R[C_k]}(s_i,s_j)
$$

总体平均：

$$
MeanDiameter=
\frac{1}{K}\sum_{k=1}^{K}Diameter_k
$$

建议输出：

```text
mean_network_diameter_m
median_network_diameter_m
max_network_diameter_m
large_diameter_cluster_ratio
```

其中：

$$
LargeDiameterClusterRatio=
\frac{
\#\{C_k: Diameter_k>\tau_{diameter}\}
}{K}
$$

越低越好。

---

## E5. Boundary complexity

$$
BoundaryComplexity_k=
\frac{P_k^{peri}}{\sqrt{A_k+\epsilon}}
$$

总体平均：

$$
MeanBoundaryComplexity=
\frac{1}{K}\sum_{k=1}^{K}BoundaryComplexity_k
$$

指标名：

```text
mean_boundary_complexity
```

越低通常表示边界更简单。

---

# F. 规模平衡指标

这组指标用于判断不同簇之间的规模是否极端不均衡。对 OD 预测和调度来说，过多极小簇或超大簇都会影响稳定性。

## F1. Coefficient of variation, CV

对簇级变量 $x_k$，例如道路长度、订单量、POI 数量，定义：

$$
CV(x)=
\frac{
std(x_1,x_2,\ldots,x_K)
}{
mean(x_1,x_2,\ldots,x_K)+\epsilon
}
$$

建议输出：

```text
segment_count_cv
road_length_cv
pickup_count_cv
dropoff_count_cv
order_count_cv
poi_count_cv
```

越低表示越均衡。

注意：北京中心城区天然存在需求不均，因此 CV 不是越低越好，而是用于识别极端不平衡。

---

## F2. 最小订单量不足比例

设簇 $C_k$ 的总订单量为：

$$
O_k=Pickup_k+Dropoff_k
$$

低订单簇比例：

$$
LowOrderClusterRatio=
\frac{
\#\{C_k: O_k<O_{\min}\}
}{K}
$$

建议输出：

```text
clusters_below_min_order
ratio_clusters_below_min_order
```

越低越好。

---

# G. 语义同质性指标

这组指标用于评价 POI 和订单语义在簇内是否更加一致。

需要注意：如果 POI / Order 已经参与构图，那么这些指标属于内部一致性评价，不能单独证明泛化能力；更强的证据应来自留出期订单或下游预测任务。

---

## G1. POI within-cluster sum of squares, POI-WSS

设路段 $s_i$ 的 POI 特征向量为：

$$
\mathbf{x}^{POI}_i
$$

簇 $C_k$ 的 POI 均值向量为：

$$
\bar{\mathbf{x}}^{POI}_k=
\frac{1}{|C_k|}
\sum_{s_i\in C_k}\mathbf{x}^{POI}_i
$$

POI 簇内平方和：

$$
WSS_{POI}=
\sum_{k=1}^{K}
\sum_{s_i\in C_k}
\left\|
\mathbf{x}^{POI}_i-\bar{\mathbf{x}}^{POI}_k
\right\|^2
$$

单位路段归一化：

$$
WSS_{POI}^{norm}=
\frac{WSS_{POI}}{N}
$$

建议输出：

```text
poi_wss
poi_wss_per_segment
```

越低说明簇内 POI 语义越一致。

---

## G2. POI entropy

对簇 $C_k$，设第 $c$ 类 POI 的占比为：

$$
p_{kc}
$$

则簇 POI 熵为：

$$
Entropy^{POI}_k=
-\sum_c p_{kc}\log(p_{kc}+\epsilon)
$$

总体平均：

$$
MeanEntropy^{POI}=
\frac{1}{K}\sum_{k=1}^{K}Entropy^{POI}_k
$$

订单量或 POI 数量加权平均：

$$
WeightedMeanEntropy^{POI}=
\frac{
\sum_k N^{POI}_k Entropy^{POI}_k
}{
\sum_k N^{POI}_k
}
$$

建议输出：

```text
mean_cluster_poi_entropy
weighted_mean_cluster_poi_entropy
```

越低说明功能越单一，但混合功能区不一定差，因此该指标应结合 dominant POI purity 一起看。

---

## G3. Dominant POI purity

$$
Purity^{POI}_k=
\max_c p_{kc}
$$

总体平均：

$$
MeanPurity^{POI}=
\frac{1}{K}\sum_{k=1}^{K}Purity^{POI}_k
$$

建议输出：

```text
mean_dominant_poi_purity
weighted_mean_dominant_poi_purity
```

越高说明功能主导性越强。

---

## G4. Order feature WSS

设路段 $s_i$ 的订单特征向量为：

$$
\mathbf{x}^{Order}_i
$$

可包含：

```text
pickup_count
dropoff_count
pickup_dropoff_imbalance
morning_pickup_share
evening_dropoff_share
night_pickup_share
weekday_weekend_ratio
```

簇内订单特征平方和：

$$
WSS_{Order}=
\sum_{k=1}^{K}
\sum_{s_i\in C_k}
\left\|
\mathbf{x}^{Order}_i-\bar{\mathbf{x}}^{Order}_k
\right\|^2
$$

其中：

$$
\bar{\mathbf{x}}^{Order}_k=
\frac{1}{|C_k|}
\sum_{s_i\in C_k}\mathbf{x}^{Order}_i
$$

单位路段归一化：

$$
WSS_{Order}^{norm}=
\frac{WSS_{Order}}{N}
$$

建议输出：

```text
order_wss
order_wss_per_segment
```

越低说明簇内订单模式越一致。

---

## G5. Pickup-dropoff imbalance variance

路段 $s_i$ 的上下车不平衡度：

$$
imb_i=
\frac{
pickup_i-dropoff_i
}{
pickup_i+dropoff_i+\epsilon
}
$$

簇内不平衡度方差：

$$
Var^{imb}_k=
\frac{1}{|C_k|}
\sum_{s_i\in C_k}
(imb_i-\bar{imb}_k)^2
$$

总体平均：

$$
MeanVar^{imb}=
\frac{1}{K}\sum_{k=1}^{K}Var^{imb}_k
$$

指标名：

```text
mean_within_cluster_imbalance_var
```

越低说明簇内运营角色更一致。

---

# H. OD 可用性指标

这组指标用于判断分区是否适合构建区域 OD 矩阵、进行 OD 需求预测和调度分析。

给定订单 $q$，其上车点匹配到路段 $s_o$，下车点匹配到路段 $s_d$，则区域 OD 为：

$$
(z_o,z_d)=(z(s_o),z(s_d))
$$

---

## H1. OD sparsity

设总可能 OD 对数为：

$$
K^2
$$

某个时间片 $t$ 内非零 OD 对集合为：

$$
\mathcal{OD}_t^+=\{(o,d): y_{odt}>0\}
$$

时间片 $t$ 的 OD 稀疏率为：

$$
Sparsity_t=
1-
\frac{
|\mathcal{OD}_t^+|
}{
K^2
}
$$

总体平均：

$$
MeanODSparsity=
\frac{1}{T}
\sum_{t=1}^{T}Sparsity_t
$$

建议输出：

```text
od_sparsity
mean_nonzero_od_pairs_per_slot
mean_nonzero_od_ratio_per_slot
```

`od_sparsity` 越低越好。

---

## H2. Mean orders per nonzero OD

所有时间片内，非零 OD 对上的平均订单量：

$$
MeanFlow^+=
\frac{
\sum_{t}\sum_{o,d}y_{odt}
}{
\sum_t |\mathcal{OD}_t^+|
}
$$

建议输出：

```text
mean_orders_per_nonzero_od
median_orders_per_nonzero_od
```

越高说明每个有效 OD 对样本更充足。

---

## H3. Effective OD pair ratio

定义一周累计流量达到阈值 $\tau_{OD}$ 的 OD 对为有效 OD 对：

$$
\mathcal{OD}^{eff}=
\{(o,d): \sum_t y_{odt} \geq \tau_{OD}\}
$$

有效 OD 对比例：

$$
EffectiveODRatio=
\frac{
|\mathcal{OD}^{eff}|
}{
K^2
}
$$

建议输出：

```text
num_effective_od_pairs
effective_od_pair_ratio
```

越高说明分区产生的 OD 矩阵更可用。

---

## H4. OD flow HHI

设累计 OD 流量为：

$$
Y_{od}=\sum_t y_{odt}
$$

总流量为：

$$
Y=\sum_{o,d}Y_{od}
$$

OD 流量占比为：

$$
p_{od}=\frac{Y_{od}}{Y+\epsilon}
$$

HHI 为：

$$
HHI_{OD}=
\sum_{o,d}p_{od}^2
$$

指标名：

```text
od_flow_hhi
```

HHI 越高说明流量越集中到少数 OD 对；过高可能意味着区域过粗或流量集中，过低且稀疏率高可能意味着区域过碎。

---

## H5. 历史平均预测误差

这是轻量但很重要的下游评价。

用训练期历史平均预测测试期 OD 需求：

$$
\hat{y}_{odt}
=
\frac{1}{|\mathcal{T}_{train}(t)|}
\sum_{\tau\in \mathcal{T}_{train}(t)}
y_{od\tau}
$$

其中 $\mathcal{T}_{train}(t)$ 可以是训练集中相同星期类型、相同时段的时间片集合。

测试期 MAE：

$$
MAE=
\frac{1}{|\Omega|}
\sum_{(o,d,t)\in \Omega}
|y_{odt}-\hat{y}_{odt}|
$$

RMSE：

$$
RMSE=
\sqrt{
\frac{1}{|\Omega|}
\sum_{(o,d,t)\in \Omega}
(y_{odt}-\hat{y}_{odt})^2
}
$$

WAPE：

$$
WAPE=
\frac{
\sum_{(o,d,t)\in \Omega}
|y_{odt}-\hat{y}_{odt}|
}{
\sum_{(o,d,t)\in \Omega}
|y_{odt}|+\epsilon
}
$$

建议输出：

```text
historical_avg_mae
historical_avg_rmse
historical_avg_wape
```

越低越好。

---

# I. 推荐总表字段

建议最终 `benchmark_metrics.csv` 至少包含以下字段：

```text
graph_variant
algorithm
params
num_clusters

mean_segments_per_cluster
mean_cluster_length_m
small_cluster_ratio

connected_cluster_ratio
total_fragment_count
mean_largest_cc_ratio

continuity_edge_cut_ratio
same_road_adjacent_cut_ratio
weighted_mean_road_name_num_clusters
weighted_mean_dominant_cluster_share

connector_edge_cut_ratio
direct_edge_cut_ratio

mean_compactness
mean_convex_hull_ratio
mean_elongation
max_elongation
mean_network_diameter_m
mean_boundary_complexity

segment_count_cv
road_length_cv
pickup_count_cv
dropoff_count_cv
order_count_cv
poi_count_cv
ratio_clusters_below_min_order

poi_wss_per_segment
mean_cluster_poi_entropy
mean_dominant_poi_purity

order_wss_per_segment
mean_within_cluster_imbalance_var

od_sparsity
effective_od_pair_ratio
mean_orders_per_nonzero_od
od_flow_hhi

historical_avg_mae
historical_avg_rmse
historical_avg_wape
```

---

# J. 指标方向总结

| 指标 | 方向 | 含义 |
|---|---:|---|
| `connected_cluster_ratio` | ↑ | 簇内部路网连通性越高越好 |
| `total_fragment_count` | ↓ | 飞地/碎片越少越好 |
| `continuity_edge_cut_ratio` | ↓ | 道路连续性关系被切断越少越好 |
| `connector_edge_cut_ratio` | ↓ | connector 介导关系被切断越少越好 |
| `weighted_mean_dominant_cluster_share` | ↑ | 同一道路越集中在一个簇越好 |
| `mean_compactness` | ↑ | 区域越紧凑越好 |
| `mean_convex_hull_ratio` | ↑ | 越接近凸形、越少凹陷/长条越好 |
| `mean_elongation` | ↓ | 长条化越弱越好 |
| `mean_network_diameter_m` | ↓ | 簇内网络跨度越小越好 |
| `road_length_cv` | ↓ | 道路长度规模越均衡越好 |
| `pickup_count_cv` | ↓ | 上车订单分布越均衡越好 |
| `poi_wss_per_segment` | ↓ | POI 语义簇内越一致越好 |
| `order_wss_per_segment` | ↓ | 订单模式簇内越一致越好 |
| `od_sparsity` | ↓ | OD 矩阵越不稀疏越好 |
| `effective_od_pair_ratio` | ↑ | 有效 OD 对比例越高越好 |
| `historical_avg_wape` | ↓ | 下游 OD 预测误差越低越好 |

---

# K. 使用建议

一个分区方案如果要作为网约车运营分区，至少应满足：

1. `connected_cluster_ratio` 接近 1；
2. `total_fragment_count` 较低；
3. `continuity_edge_cut_ratio` 不高；
4. `connector_edge_cut_ratio` 不高；
5. `mean_elongation` 和 `mean_network_diameter_m` 不极端；
6. `small_cluster_ratio` 较低；
7. `od_sparsity` 不过高；
8. `historical_avg_wape` 不劣于规则网格 / H3 baseline；
9. Road + POI + Order 相比 Road-only 在语义同质性和下游 OD 指标上有可解释提升。

最终不应只依赖单个指标判断分区优劣，而应综合：

$$
\text{路网连通性}
+
\text{道路完整性}
+
\text{连接线关系保持}
+
\text{形态合理性}
+
\text{语义同质性}
+
\text{OD 可用性}
+
\text{下游预测/调度表现}
$$

来进行评价。
