# Demand-Adaptive Road-Network Partitioning Algorithms

本文档给出两个拟用于实验比较的自适应路网分区算法：

1. **Demand-weighted Network Voronoi（需求加权路网 Voronoi 分区）**
2. **Demand-capacitated Road Region Growing（需求容量约束的路段区域增长）**

二者共同目标是：在以路段为基本单元的路网关系图上，使订单端点密集区域形成更细的子路网簇，订单端点稀疏区域形成更粗的子路网簇，同时保留路网连通性、道路连续性、连接线关系和 POI/订单语义一致性。

---

## 0. 基本符号与输入

### 0.1 路段关系图

给定路段关系图：

$$
G=(V,E)
$$

其中：

- $V$：ordinary road segments，即普通路段集合；
- $E$：路段之间的关系边，包括直接相邻边、connector-mediated 边和道路连续性边；
- $w_{ij}$：边 $(i,j)$ 的综合权重，表示两个路段保持同簇的倾向。

每个路段节点 $i\in V$ 有以下属性：

$$
x_i = \{x_i^{road}, x_i^{POI}, x_i^{order}, d_i, l_i, g_i\}
$$

其中：

- $x_i^{road}$：道路属性，例如道路等级、长度、方向、中心性等；
- $x_i^{POI}$：POI 语义特征；
- $x_i^{order}$：订单时间模式和上下车强度特征；
- $d_i$：订单端点权重；
- $l_i$：路段长度；
- $g_i$：路段几何对象。

### 0.2 订单端点权重

对每条路段定义订单端点权重：

$$
d_i = pickup_i + dropoff_i
$$

也可以使用平滑或截尾版本：

$$
d_i = \log(1+pickup_i+dropoff_i)
$$

但如果算法目标是容量均衡，则建议使用原始端点数或温和压缩后的端点数。

### 0.3 簇的需求容量

对一个簇 $C_k$，定义其总订单端点数：

$$
D(C_k)=\sum_{i\in C_k} d_i
$$

设定目标容量：

$$
D_{target}=\frac{\sum_{i\in V}d_i}{K}
$$

并允许上下浮动：

$$
D_{min} = \tau_{min}D_{target}, \quad
D_{max} = \tau_{max}D_{target}
$$

例如：

$$
\tau_{min}=0.5,\quad \tau_{max}=1.5
$$

或更严格：

$$
\tau_{min}=0.7,\quad \tau_{max}=1.3
$$

容量约束的直觉是：

> 高密度区域中，较少路段即可达到目标订单量，因此空间单元更细；低密度区域中，需要合并更多路段才能达到目标订单量，因此空间单元更粗。

### 0.4 语义相似度

给定路段 $i$ 与簇 $C_k$，定义其与簇的语义相似度：

$$
Sim(i,C_k)=
\alpha Sim_{road}(i,C_k)
+
\beta Sim_{POI}(i,C_k)
+
\gamma Sim_{order}(i,C_k)
+
\eta Sim_{cont}(i,C_k)
$$

其中：

- $Sim_{road}$：道路属性相似；
- $Sim_{POI}$：POI 类别组成、密度、熵等相似；
- $Sim_{order}$：订单强度、时间模式、上下车不平衡等相似；
- $Sim_{cont}$：与簇内相邻路段的道路连续性强度。

也可以直接使用边权聚合：

$$
Sim(i,C_k)=
\frac{\sum_{j\in N(i)\cap C_k}w_{ij}}{\sum_{j\in N(i)}w_{ij}+\epsilon}
$$

其中 $N(i)$ 为路段 $i$ 的邻居集合。

### 0.5 形态惩罚

为了防止区域过长、过散或形态异常，定义形态惩罚：

$$
ShapePenalty(C_k)
=
\lambda_1 \cdot \max(0, Diameter(C_k)-R_{max})
+
\lambda_2 \cdot \max(0, Elongation(C_k)-E_{max})
+
\lambda_3 \cdot \max(0, 1-ConvexHullRatio(C_k))
$$

其中：

- $Diameter(C_k)$：簇内路网近似直径；
- $Elongation(C_k)$：最小旋转矩形长短边比；
- $ConvexHullRatio(C_k)$：buffer 后区域面积与其凸包面积之比：

$$
ConvexHullRatio(C_k)=\frac{Area(C_k)}{Area(ConvexHull(C_k))}
$$

注意：凸包比例用于评价区域是否过度破碎或空洞过大，但不应强制区域必须为凸。

---

# 1. Algorithm A: Demand-weighted Network Voronoi

## 1.1 核心思想

普通 Voronoi 分区是：给定若干中心点，平面上每个位置分配给最近中心点。

在本研究中，空间对象不是连续土地面，而是 road segment graph。因此应使用 **network Voronoi**：

> 给定若干路网种子路段，每条普通路段分配给路网距离最近的种子。

为了实现订单密度自适应，种子点的选择与订单端点密度相关：

- 订单密集区域生成更多 seed；
- 订单稀疏区域生成更少 seed；
- 每个 seed 的服务范围由路网距离、道路关系权重和语义相似度共同决定。

最终效果是：高订单密度区域被切得更细，低订单密度区域被切得更粗。

---

## 1.2 输入

- 路段关系图 $G=(V,E)$；
- 节点需求权重 $d_i$；
- 边权 $w_{ij}$；
- 目标簇数 $K$；
- 路段几何；
- 可选 POI/order 语义特征；
- 参数：
  - seed selection method；
  - network distance metric；
  - capacity tolerance；
  - semantic boundary refinement 开关。

---

## 1.3 Step 1: 需求加权种子选择

目标是选择 $K$ 个种子路段：

$$
S=\{s_1,s_2,\ldots,s_K\}
$$

种子应满足：

1. 高需求路段更容易成为种子；
2. 种子之间不能过于接近；
3. 种子应覆盖主要订单热点；
4. 种子最好位于可服务性强的 ordinary road segments 上。

### 方法 A1：需求加权 farthest-point sampling

初始化：

$$
s_1=\arg\max_i d_i
$$

之后第 $t$ 个种子选择：

$$
s_t = \arg\max_{i\in V}
\left[
d_i^\theta \cdot \min_{s\in S} dist_G(i,s)
\right]
$$

其中：

- $dist_G(i,s)$：路网最短距离；
- $\theta$：需求权重指数，建议取 $0.5\sim 1.0$。

该方法能避免所有种子集中在单一热点中，同时仍然偏向需求密集区域。

### 方法 A2：需求加权 k-means++ 类似采样

每轮选择新 seed 的概率为：

$$
P(i)\propto d_i^\theta \cdot D(i,S)^2
$$

其中：

$$
D(i,S)=\min_{s\in S}dist_G(i,s)
$$

该方法具有随机性，应固定 random seed，并重复多次取最优结果。

### 方法 A3：按空间分区预选热点

先用粗分区或网格将研究区分成若干 macro areas，在每个 macro area 内按照总需求量分配 seed 数：

$$
K_a =
\left\lceil
K \cdot \frac{\sum_{i\in A_a}d_i}{\sum_{i\in V}d_i}
\right\rceil
$$

再在每个 macro area 内选择高需求 seed。该方法对 seed 空间覆盖更稳定。

---

## 1.4 Step 2: 定义路网距离

普通最短路距离可以用路段长度：

$$
cost_{ij}^{dist}=length_{ij}
$$

但为了鼓励强关系边不被切断，可以将边权转成阻抗：

$$
cost_{ij}=\frac{length_{ij}}{w_{ij}+\epsilon}
$$

或：

$$
cost_{ij}=length_{ij}\cdot \exp(-\lambda w_{ij})
$$

这样：

- 边权高的相邻路段距离更近；
- connector / continuity / POI/order 相似度强的边更不容易被切断。

---

## 1.5 Step 3: Network Voronoi 分配

对每个 seed $s_k$，在路段图上运行多源 Dijkstra。

每个路段 $i$ 分配到最近 seed：

$$
z_i=\arg\min_k dist_G(i,s_k)
$$

如果加入语义修正，可以使用：

$$
z_i =
\arg\min_k
\left[
dist_G(i,s_k)
+
\lambda_{sem}\cdot SemDist(i,s_k)
\right]
$$

其中：

$$
SemDist(i,s_k)=1-Sim(x_i,x_{s_k})
$$

也可以使用当前簇中心语义：

$$
z_i =
\arg\min_k
\left[
dist_G(i,s_k)
+
\lambda_{sem}\cdot SemDist(i,C_k)
\right]
$$

但这样需要迭代更新。

---

## 1.6 Step 4: 容量检查与 seed 调整

Network Voronoi 初始分配后，每个簇 $C_k$ 有容量：

$$
D(C_k)=\sum_{i\in C_k}d_i
$$

若：

$$
D(C_k)>D_{max}
$$

说明该区域订单过密或覆盖过大，应增加 seed 或拆分。

若：

$$
D(C_k)<D_{min}
$$

说明该区域订单过少，应合并或迁移边界。

### 高容量簇细分

对高容量簇 $C_k$，设细分数量：

$$
m_k =
\left\lceil
\frac{D(C_k)}{D_{target}}
\right\rceil
$$

并限制：

$$
m_k \leq m_{max}
$$

在 $C_k$ 内部重新选择 $m_k$ 个 seeds，并做局部 network Voronoi。

### 低容量簇合并

对低容量簇 $C_k$，寻找邻接簇集合：

$$
\mathcal{N}(C_k)=\{C_l: \exists (i,j)\in E, i\in C_k, j\in C_l\}
$$

选择合并对象：

$$
C_l^* =
\arg\max_{C_l\in \mathcal{N}(C_k)}
\left[
Sim(C_k,C_l)
-
\lambda_D \cdot CapPenalty(C_k\cup C_l)
-
\lambda_S \cdot ShapePenalty(C_k\cup C_l)
\right]
$$

其中：

$$
CapPenalty(C)=
\left|
\frac{D(C)-D_{target}}{D_{target}}
\right|
$$

---

## 1.7 Step 5: 边界局部优化

对每条边界路段 $i$，考虑将其从当前簇 $C_a$ 移动到邻接簇 $C_b$。

移动必须满足：

1. $C_a \setminus \{i\}$ 仍然连通；
2. $C_b \cup \{i\}$ 连通；
3. 容量约束不严重违反；
4. 形态不明显恶化。

定义目标函数：

$$
Obj(\mathcal{P})=
\lambda_{cut} Cut(\mathcal{P})
+
\lambda_{sem} WSS_{sem}(\mathcal{P})
+
\lambda_{cap} CapVar(\mathcal{P})
+
\lambda_{shape} Shape(\mathcal{P})
$$

其中：

$$
Cut(\mathcal{P})=
\sum_{(i,j)\in E}w_{ij}\mathbf{1}[z_i\neq z_j]
$$

$$
CapVar(\mathcal{P})=
\sum_k
\left(
\frac{D(C_k)-D_{target}}{D_{target}}
\right)^2
$$

若移动使目标函数下降，则接受。

---

## 1.8 输出

输出：

- `segment_clusters_demand_network_voronoi.gpkg`
- `cluster_summary_demand_network_voronoi.csv`
- `benchmark_metrics_demand_network_voronoi.csv`
- 种子点图；
- 初始 Voronoi 图；
- 容量修正后图；
- 最终图。

---

## 1.9 伪代码

```text
Input:
  G=(V,E), node demand d_i, edge weight w_ij, target K

1. Select K demand-weighted seeds S
2. Convert edge weights to network costs
3. Run multi-source Dijkstra from seeds
4. Assign each segment to nearest seed
5. Compute cluster demand D(C_k)
6. For clusters with D(C_k)>D_max:
      split internally using additional seeds
7. For clusters with D(C_k)<D_min:
      merge into best adjacent cluster
8. Perform boundary local search under connectivity and capacity constraints
9. Output final clusters
```

---

## 1.10 优点与风险

### 优点

- 密度自适应是算法内生机制；
- 高密度区域自然切细，低密度区域自然变粗；
- 与自适应 Voronoi / 自适应剖分思想接近；
- 可解释性强；
- 可直接控制 $K$；
- 可利用路网距离而非欧氏距离。

### 风险

- seed 选择对结果影响较大；
- 如果只使用路网距离，语义边界可能不清晰；
- 如果容量约束过强，可能牺牲道路连续性；
- 如果形态约束不足，可能出现长条区域；
- 普通 Network Voronoi 结果未必满足订单容量均衡，需要后续调整。

---

# 2. Algorithm B: Demand-capacitated Road Region Growing

## 2.1 核心思想

Demand-capacitated Road Region Growing 是一种直接在路段关系图上生成连通区域的启发式算法。

它从若干种子路段开始，逐步吸收相邻路段，直到每个区域达到目标订单容量。区域扩张时综合考虑：

- 路网连接强度；
- POI 语义相似；
- 订单模式相似；
- 道路连续性；
- 区域形态；
- 订单容量。

其核心是：

> 每个区域不是按照面积相近划分，而是按照运营信息量相近划分。

---

## 2.2 输入

- 路段关系图 $G=(V,E)$；
- 节点订单端点权重 $d_i$；
- 节点 POI/order 特征；
- 边权 $w_{ij}$；
- 目标区域数 $K$ 或目标容量 $D_{target}$；
- 参数：
  - seed selection method；
  - $D_{min},D_{max}$；
  - similarity weights；
  - shape constraints；
  - maximum network diameter；
  - minimum segment count。

---

## 2.3 Step 1: 选择种子路段

种子选择可以采用三种方式。

### 方法 B1：高需求局部峰值

计算每条路段的局部需求密度：

$$
\rho_i = \frac{d_i}{l_i+\epsilon}
$$

选择局部峰值作为 seed：

$$
\rho_i > \rho_j,\quad \forall j\in N_r(i)
$$

其中 $N_r(i)$ 是路网半径 $r$ 内的邻域。

若候选 seed 超过 $K$，按 $\rho_i$ 排序取前 $K$。

### 方法 B2：需求加权 farthest-point seeds

同 Algorithm A 中的需求加权 farthest-point sampling。

### 方法 B3：预分区种子

先用 Leiden / METIS / 粗网格生成 $K$ 个初始区域，在每个初始区域中选择需求最高或中心性最高路段作为 seed。

这种方法稳定性较好，适合工程实现。

---

## 2.4 Step 2: 初始化区域

每个 seed $s_k$ 形成一个初始区域：

$$
C_k = \{s_k\}
$$

已分配集合：

$$
A = \{s_1,\ldots,s_K\}
$$

未分配集合：

$$
U = V \setminus A
$$

每个区域维护：

- 总订单端点数 $D(C_k)$；
- POI 特征中心 $\bar{x}_{POI,k}$；
- 订单特征中心 $\bar{x}_{order,k}$；
- 路段集合；
- 边界候选路段集合；
- 近似形态指标。

---

## 2.5 Step 3: 区域增长规则

对每个区域 $C_k$，候选增长集合为：

$$
B(C_k)=\{i\in U: \exists j\in C_k, (i,j)\in E\}
$$

即所有未分配且与区域相邻的路段。

对候选路段 $i$，计算加入 $C_k$ 的得分：

$$
Score(i,C_k)
=
\alpha \cdot Conn(i,C_k)
+
\beta \cdot Sim_{POI}(i,C_k)
+
\gamma \cdot Sim_{order}(i,C_k)
+
\eta \cdot Cont(i,C_k)
-
\lambda_D \cdot CapPenalty(i,C_k)
-
\lambda_S \cdot ShapePenalty(i,C_k)
$$

其中：

### 连接强度

$$
Conn(i,C_k)=
\sum_{j\in N(i)\cap C_k} w_{ij}
$$

或归一化为：

$$
Conn(i,C_k)=
\frac{\sum_{j\in N(i)\cap C_k} w_{ij}}
{\sum_{j\in N(i)} w_{ij}+\epsilon}
$$

### POI 相似度

$$
Sim_{POI}(i,C_k)=
cos(x_i^{POI},\bar{x}_{k}^{POI})
$$

### 订单模式相似度

$$
Sim_{order}(i,C_k)=
cos(x_i^{order},\bar{x}_{k}^{order})
$$

### 道路连续性

$$
Cont(i,C_k)=
\sum_{j\in N(i)\cap C_k}w^{cont}_{ij}
$$

### 容量惩罚

加入候选路段后：

$$
D'(C_k)=D(C_k)+d_i
$$

容量惩罚：

$$
CapPenalty(i,C_k)=
\left|
\frac{D'(C_k)-D_{target}}{D_{target}}
\right|
$$

若 $D(C_k)<D_{min}$，可以降低容量惩罚，鼓励继续增长。

若 $D(C_k)>D_{max}$，禁止继续增长，除非所有邻域区域都不可行。

### 形态惩罚

加入路段后，如果导致网络直径或 elongation 超阈值，则惩罚：

$$
ShapePenalty(i,C_k)=
\max(0,Diameter(C_k\cup \{i\})-R_{max})
+
\max(0,Elongation(C_k\cup \{i\})-E_{max})
$$

---

## 2.6 Step 4: 增长策略

### 策略 B-A：轮流增长

每轮遍历所有未满容量区域：

1. 对每个区域 $C_k$ 找到最高分候选路段；
2. 选择得分最高的区域-路段对；
3. 分配该路段；
4. 更新区域属性；
5. 重复直到所有路段分配完成。

优点：区域增长较均衡。缺点：计算较慢。

### 策略 B-B：优先队列增长

为所有候选分配维护优先队列：

$$
(i,C_k,Score)
$$

每次取最高分有效候选。

优点：效率较高。缺点：需要处理候选失效和更新。

### 策略 B-C：容量优先增长

优先增长当前容量最低的区域：

$$
k=\arg\min_k D(C_k)
$$

然后为该区域选择最佳邻接路段。

优点：更容易达到容量均衡。缺点：可能牺牲局部语义最优。

建议第一版使用 **容量优先增长**，因为它最符合“低需求区粗、高需求区细”的目标。

---

## 2.7 Step 5: 处理未分配路段

由于路网断裂、限制条件或 seed 分布问题，可能存在未分配路段。

处理规则：

1. 若未分配路段与某个区域相邻，则合并到得分最高的相邻区域；
2. 若未分配路段形成孤立连通分量，则整体合并到最近区域；
3. 若该孤立分量足够大且订单量足够，可生成新区域；
4. 最终保证所有 ordinary road segments 都被分配。

---

## 2.8 Step 6: 小区域合并

若某个区域：

$$
D(C_k)<D_{min}
$$

或者：

$$
|C_k|<N_{min}
$$

则合并到邻接最优区域：

$$
C_l^*=
\arg\max_{C_l\in \mathcal{N}(C_k)}
\left[
Sim(C_k,C_l)
+
\lambda_{conn}Conn(C_k,C_l)
-
\lambda_{shape}ShapePenalty(C_k\cup C_l)
\right]
$$

合并后更新区域编号。

---

## 2.9 Step 7: 大区域拆分

若某个区域：

$$
D(C_k)>D_{max}
$$

且内部异质性较高：

$$
H(C_k)>H_{max}
$$

则在 $C_k$ 的诱导子图内部重新执行 region growing 或 network Voronoi。

拆分数：

$$
m_k=
\left\lceil
\frac{D(C_k)}{D_{target}}
\right\rceil
$$

同时限制：

$$
2\leq m_k \leq m_{max}
$$

如果区域需求高但内部语义非常一致，可以不拆，避免过度切分。

---

## 2.10 Step 8: 边界局部搜索

对所有边界路段尝试移动。

若将路段 $i$ 从 $C_a$ 移动到 $C_b$，必须满足：

1. $C_a\setminus \{i\}$ 仍然连通；
2. $C_b\cup \{i\}$ 连通；
3. 容量约束改善或不显著恶化；
4. 语义 WSS 下降或 cut weight 下降；
5. 形态不明显恶化。

局部目标：

$$
Obj(\mathcal{P})=
\lambda_{cut} Cut(\mathcal{P})
+
\lambda_{poi} WSS_{POI}(\mathcal{P})
+
\lambda_{order} WSS_{order}(\mathcal{P})
+
\lambda_{cap} CapVar(\mathcal{P})
+
\lambda_{shape} Shape(\mathcal{P})
$$

若移动后：

$$
\Delta Obj<0
$$

则接受移动。

---

## 2.11 输出

输出：

- `segment_clusters_demand_region_growing.gpkg`
- `cluster_summary_demand_region_growing.csv`
- `benchmark_metrics_demand_region_growing.csv`
- 每轮增长日志；
- seed 分布图；
- 最终分区图；
- 低容量/高容量区域诊断。

---

## 2.12 伪代码

```text
Input:
  G=(V,E), node demand d_i, node features x_i, edge weights w_ij,
  target K or D_target

1. Select K seeds using demand density and spatial separation
2. Initialize each seed as one region
3. While unassigned segments exist:
      Select region with lowest relative capacity
      Find unassigned neighboring candidate segments
      Compute Score(i,C_k) for each candidate
      Assign best candidate to C_k if constraints allow
      Update capacity, feature centers, boundary set
4. Assign remaining unassigned segments
5. Merge under-capacity regions
6. Split over-capacity and heterogeneous regions
7. Run boundary local search
8. Output final clusters
```

---

## 2.13 优点与风险

### 优点

- 订单密度直接进入算法过程；
- 天然保证路网连通；
- 可解释性强；
- 高密度区自然细分；
- 低密度区自然合并；
- 能同时考虑 POI、订单、道路连续性和形态；
- 更像“运营区”生成算法，而不是普通图社区发现。

### 风险

- seed 选择会影响结果；
- 参数较多；
- 贪心增长可能陷入局部最优；
- 需要处理未分配路段；
- 对边界局部搜索依赖较强；
- 如果容量约束过强，可能破坏 POI/订单语义一致性。

---

# 3. 两个算法的实验设计建议

## 3.1 与现有算法比较

建议将两个新算法加入 benchmark：

| 类别 | 算法 |
|---|---|
| 社区发现 | Louvain |
| 社区发现 | Leiden |
| 均衡图划分 | METIS |
| 空间区域化 | SKATER |
| 新方法 | Demand-weighted Network Voronoi |
| 新方法 | Demand-capacitated Road Region Growing |

---

## 3.2 对比指标

应使用统一指标：

1. 基本规模：`num_clusters`, `mean_cluster_length_m`, `size_balance_cv`
2. 连通性：`connected_cluster_ratio`, `total_fragment_count`
3. 道路完整性：`continuity_edge_cut_ratio`, `same_road_adjacent_cut_ratio`, `mean_clusters_per_named_road`
4. connector 保持：`connector_edge_cut_ratio`
5. 形态：`mean_compactness`, `mean_elongation`, `mean_convex_hull_ratio`, `mean_network_diameter_m`
6. 订单容量：`order_count_cv`, `ratio_clusters_below_min_order`, `capacity_violation_ratio`
7. 语义同质性：`poi_wss_per_segment`, `order_wss_per_segment`, `mean_cluster_poi_entropy`, `mean_within_cluster_imbalance_var`
8. OD 可用性：`cluster_od_sparsity`, `effective_od_pair_ratio`, `mean_orders_per_nonzero_od`, `historical_avg_wape`

---

## 3.3 消融实验

### Network Voronoi 消融

| 版本 | seed 是否需求加权 | 距离是否使用边权 | 是否容量修正 | 是否语义修正 |
|---|---:|---:|---:|---:|
| NV-basic | 否 | 否 | 否 | 否 |
| NV-demand-seed | 是 | 否 | 否 | 否 |
| NV-weighted-distance | 是 | 是 | 否 | 否 |
| NV-capacitated | 是 | 是 | 是 | 否 |
| NV-full | 是 | 是 | 是 | 是 |

### Region Growing 消融

| 版本 | 容量约束 | POI | Order | Shape penalty |
|---|---:|---:|---:|---:|
| RG-road | 否 | 否 | 否 | 否 |
| RG-capacity | 是 | 否 | 否 | 否 |
| RG-POI | 是 | 是 | 否 | 否 |
| RG-POI-order | 是 | 是 | 是 | 否 |
| RG-full | 是 | 是 | 是 | 是 |

---

# 4. 推荐参数初值

## 4.1 容量参数

$$
D_{target}=\frac{\sum_i d_i}{K}
$$

建议：

```text
K = 137
D_min = 0.5 * D_target
D_max = 1.5 * D_target
```

更严格版本：

```text
D_min = 0.7 * D_target
D_max = 1.3 * D_target
```

## 4.2 距离与边权

Network Voronoi 中：

$$
cost_{ij}=\frac{length_{ij}}{w_{ij}+\epsilon}
$$

建议：

```text
epsilon = 1e-6
```

## 4.3 Region Growing 得分权重

第一版建议：

```text
alpha_conn = 1.0
beta_poi = 0.5
gamma_order = 0.5
eta_continuity = 1.0
lambda_capacity = 1.0
lambda_shape = 0.2
```

如果发现分区仍然过度依赖路网：

```text
beta_poi = 1.0
gamma_order = 1.0
```

如果发现区域形态很差：

```text
lambda_shape = 0.5 or 1.0
```

---

# 5. 实现注意事项

## 5.1 不要直接使用平面 Voronoi 替代路网 Voronoi

普通泰森多边形生成的是欧氏空间最近邻区域，虽然形态简洁甚至凸，但它不保证：

- 路网连通；
- 道路可达性；
- 环路/高架/河流/封闭区域约束；
- 网约车接驳合理性。

本研究应优先使用 **network Voronoi**。

## 5.2 凸包比例是评价指标，不是硬约束

Convex hull ratio 可以衡量区域是否过于破碎：

$$
ConvexHullRatio=\frac{Area(C_k)}{Area(ConvexHull(C_k))}
$$

但不应要求所有区域都是凸的。北京四环内真实交通服务区域可能天然非凸。

## 5.3 训练/测试订单应分离

如果使用订单密度指导分区，则建议：

```text
构图/分区订单：2017-10-16 至 2017-10-19
评价订单：2017-10-20 至 2017-10-22
```

避免用同一批订单同时构图和评价导致信息泄露。

## 5.4 保证连通性

Region Growing 天然连通。  
Network Voronoi 通常在连通图上能生成连通区域，但如果使用复杂语义修正或容量调整，仍需检查：

```text
connected_cluster_ratio
total_fragment_count
```

