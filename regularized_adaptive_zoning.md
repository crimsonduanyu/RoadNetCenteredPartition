# 正则化需求自适应路网运营分区方法：形式化描述与实验方案

## 1. 研究动机

面向网约车运营的空间分区不能简单等同于规则网格、六边形或行政区划。网约车订单的发生与服务依赖于道路、路口、辅路、接驳点和周边城市功能，因此更合理的基本对象应是道路段及其服务语义，而不是固定面积的土地单元。

已有初步实验表明，不同算法各有优势：

- **Louvain / Leiden** 能较好保持路网社区结构、道路连续性和 connector-mediated 关系，但对订单密度自适应不足，区域规模和 OD 稀疏性未必理想。
- **Demand Region Growing** 能更好地体现订单端点密度，降低 OD 稀疏性，并改善区域规模平衡，但容易切断道路连续性和 connector 关系。
- **METIS** 能控制规模平衡，但原始结果容易产生不连通区域。
- **SKATER** 能保持空间连通，但当前实验中容易出现严重规模失衡。

因此，需要构建一个统一的正则化优化框架，在以下目标之间取得平衡：

1. 保持路网社区结构；
2. 保持同一道路或同一交通走廊的连续性；
3. 保留 connector-mediated adjacency；
4. 使高订单密度区域切分更细、低订单密度区域切分更粗；
5. 控制区域形态，避免长条形或跨度过大的区域；
6. 保持 POI 和订单运营语义的簇内一致性；
7. 提高区域 OD 矩阵可用性，降低过度稀疏问题。

该方法可理解为：

> **用 Louvain/Leiden 的社区结构作为路网结构正则，用 Demand Region Growing 的容量自适应作为需求尺度正则，形成正则化需求自适应路网运营分区问题。**

---

## 2. 基本对象与符号

### 2.1 路段集合

给定研究区域内普通机动车可服务道路段集合：

$$
V=\{1,2,\ldots,n\}
$$

每个节点 $i \in V$ 表示一条 ordinary road segment。短 connector / link / ramp 不作为普通聚类节点，而是被压缩为普通路段之间的关系边。

### 2.2 路段关系图

构建路段关系图：

$$
G=(V,E)
$$

其中边 $(i,j)\in E$ 表示路段 $i$ 与路段 $j$ 存在可用于分区的关系，包括：

1. 直接拓扑邻接；
2. connector-mediated adjacency；
3. 同路连续性关系；
4. POI / 订单语义增强后的相邻关系。

每条边具有基础权重：

$$
w_{ij} \geq 0
$$

其中可分解为：

$$
w_{ij}
=
w^{topo}_{ij}
+
w^{connector}_{ij}
+
w^{continuity}_{ij}
+
w^{POI}_{ij}
+
w^{order}_{ij}
$$

其中：

- $w^{topo}_{ij}$：直接相邻关系；
- $w^{connector}_{ij}$：通过 connector / link / ramp 产生的连接关系；
- $w^{continuity}_{ij}$：同名道路、同一 OSM way、同一等级且方向连续的道路完整性关系；
- $w^{POI}_{ij}$：POI 功能语义相似性；
- $w^{order}_{ij}$：订单需求模式相似性。

### 2.3 路段需求权重

每个路段 $i$ 具有订单端点需求权重：

$$
d_i = pickup_i + dropoff_i
$$

也可以使用加权版本：

$$
d_i = \omega_p pickup_i + \omega_d dropoff_i
$$

若上车与下车同等重要，则 $\omega_p=\omega_d=1$。

### 2.4 路段语义特征

每个路段有 POI 和订单特征：

$$
x_i = [x_i^{POI}, x_i^{order}]
$$

其中：

- $x_i^{POI}$：POI 类别组成、POI 密度、POI 熵等；
- $x_i^{order}$：pickup / dropoff 强度、早晚高峰需求、夜间需求、工作日/周末差异、上下车不平衡等。

---

## 3. 决策变量

### 3.1 区域标签变量

设最终划分为 $K$ 个子路网簇：

$$
\mathcal{P}=\{C_1,C_2,\ldots,C_K\}
$$

每个路段 $i$ 被分配到一个簇：

$$
z_i \in \{1,2,\ldots,K\}
$$

其中：

$$
C_k=\{i\in V: z_i=k\}
$$

### 3.2 指示变量

定义同簇指示变量：

$$
y_{ij}=
\begin{cases}
1, & z_i=z_j \\
0, & z_i\neq z_j
\end{cases}
$$

也可定义切边指示变量：

$$
b_{ij}=1-y_{ij}
$$

---

## 4. 优化目标

目标函数由若干正则项组成：

$$
\min_{\mathcal{P}}
\mathcal{L}(\mathcal{P})
=
\lambda_c R_{cap}(\mathcal{P})
+
\lambda_g R_{graph}(\mathcal{P})
+
\lambda_r R_{road}(\mathcal{P})
+
\lambda_s R_{shape}(\mathcal{P})
+
\lambda_m R_{sem}(\mathcal{P})
$$

其中各项分别对应需求容量自适应、图结构保持、道路完整性保持、区域形态约束和语义同质性约束。

---

## 5. 目标函数各项定义

### 5.1 需求容量自适应项

对每个簇 $C_k$，定义其订单端点容量：

$$
D(C_k)=\sum_{i\in C_k} d_i
$$

设合理容量区间为：

$$
[D_{min},D_{max}]
$$

则容量惩罚项定义为：

$$
R_{cap}(\mathcal{P})
=
\sum_{k=1}^{K}
\left[
\max(0,D_{min}-D(C_k))^2
+
\max(0,D(C_k)-D_{max})^2
\right]
$$

该项的作用是：

- 若区域订单量过少，则鼓励其合并或扩大；
- 若区域订单量过大，则鼓励其拆分或边界收缩；
- 高订单密度区域自然形成更小空间尺度；
- 低订单密度区域自然形成更大空间尺度。

也可使用目标容量形式：

$$
R_{cap}^{target}(\mathcal{P})
=
\sum_{k=1}^{K}
\left(
\frac{D(C_k)-D_0}{D_0}
\right)^2
$$

其中 $D_0$ 是目标订单端点容量。

---

### 5.2 图结构保持项

图结构保持项惩罚切断强边：

$$
R_{graph}(\mathcal{P})
=
\sum_{(i,j)\in E}
w_{ij} \mathbf{1}[z_i\neq z_j]
$$

该项可视为 Louvain / Leiden 社区结构目标的 cut-based 近似。其含义是：

> 若两个路段之间的综合关系权重高，则不希望将它们分到不同区域。

这项越小，说明分区越尊重路网社区结构。

---

### 5.3 道路完整性与 connector 保持项

道路完整性项可进一步分解为 continuity cut 和 connector cut。

#### 5.3.1 Continuity cut

设 $E_{cont}\subseteq E$ 表示同一道路、同一 OSM way 或同一交通走廊的连续性边，则：

$$
R_{cont}(\mathcal{P})
=
\sum_{(i,j)\in E_{cont}}
w^{cont}_{ij}\mathbf{1}[z_i\neq z_j]
$$

该项用于避免同一条道路被过度切碎。

#### 5.3.2 Connector cut

设 $E_{connector}\subseteq E$ 表示 connector-mediated adjacency，则：

$$
R_{connector}(\mathcal{P})
=
\sum_{(i,j)\in E_{connector}}
w^{connector}_{ij}\mathbf{1}[z_i\neq z_j]
$$

该项用于避免忽视 connector / ramp / link 在路网连通中的作用。

#### 5.3.3 组合道路结构正则

$$
R_{road}(\mathcal{P})
=
\alpha_{cont} R_{cont}(\mathcal{P})
+
\alpha_{connector} R_{connector}(\mathcal{P})
$$

其中 $\alpha_{cont}$ 与 $\alpha_{connector}$ 分别控制道路连续性和 connector 保持的重要性。

---

### 5.4 区域形态约束项

为了防止生成长条形或跨度过大的区域，可加入形态惩罚。

#### 5.4.1 网络直径惩罚

定义簇 $C_k$ 内部的路网直径：

$$
Diam(C_k)=\max_{i,j\in C_k} d_G(i,j)
$$

其中 $d_G(i,j)$ 为路网图上的最短路距离。

设最大允许直径为 $Diam_{max}$，则：

$$
R_{diam}(\mathcal{P})
=
\sum_{k=1}^{K}
\max(0,Diam(C_k)-Diam_{max})^2
$$

#### 5.4.2 形状紧凑性惩罚

对每个簇的 road buffer dissolve 后得到面状区域 $A_k$，可计算 compactness：

$$
Compactness(C_k)=\frac{4\pi Area(C_k)}{Perimeter(C_k)^2}
$$

若希望惩罚过低 compactness，可定义：

$$
R_{compact}(\mathcal{P})
=
\sum_k
\max(0,C_{min}-Compactness(C_k))^2
$$

#### 5.4.3 长条形惩罚

基于最小旋转矩形计算：

$$
Elongation(C_k)=
\frac{L_{major}(C_k)}{L_{minor}(C_k)}
$$

若最大允许长宽比为 $E_{max}$，则：

$$
R_{elong}(\mathcal{P})
=
\sum_k
\max(0,Elongation(C_k)-E_{max})^2
$$

#### 5.4.4 组合形态项

$$
R_{shape}(\mathcal{P})
=
\beta_d R_{diam}(\mathcal{P})
+
\beta_c R_{compact}(\mathcal{P})
+
\beta_e R_{elong}(\mathcal{P})
$$

第一版实现中可以只使用 $R_{diam}$，因为网络直径最贴合路网运营意义。

---

### 5.5 语义同质性项

若希望 POI 和订单特征不只通过边权影响局部分配，还直接影响簇内同质性，可定义：

$$
R_{sem}(\mathcal{P})
=
\sum_{k=1}^{K}
\sum_{i\in C_k}
\|x_i-\bar{x}_k\|^2
$$

其中：

$$
\bar{x}_k = \frac{1}{|C_k|}\sum_{i\in C_k}x_i
$$

也可分解为：

$$
R_{sem}(\mathcal{P})
=
\theta_{POI}R_{POI}(\mathcal{P})
+
\theta_{order}R_{order}(\mathcal{P})
$$

其中：

$$
R_{POI}(\mathcal{P})
=
\sum_k \sum_{i\in C_k}
d_{POI}(x_i^{POI},\bar{x}_k^{POI})
$$

$$
R_{order}(\mathcal{P})
=
\sum_k \sum_{i\in C_k}
d_{order}(x_i^{order},\bar{x}_k^{order})
$$

距离可选：

- Euclidean distance；
- cosine distance；
- Jensen-Shannon divergence；
- standardized WSS。

第一版可先不放入求解目标，只作为评估指标；第二版再纳入局部搜索目标。

---

## 6. 约束条件

### 6.1 唯一归属约束

每个 ordinary road segment 必须且只能属于一个簇：

$$
\sum_{k=1}^{K} \mathbf{1}[z_i=k]=1,\quad \forall i\in V
$$

### 6.2 路网连通性约束

每个簇在路段关系图上的诱导子图应连通：

$$
G[C_k]\ \text{is connected},\quad \forall k
$$

这是路网运营分区的硬约束。若求解中难以严格保证，可采用：

1. 初始化时使用连通分区；
2. 局部移动时检查移动后原簇与目标簇是否仍连通；
3. 后处理时将不连通簇拆分为 connected components。

### 6.3 需求容量软约束

需求容量可作为软约束进入目标，也可以设为硬约束：

$$
D_{min}\leq D(C_k)\leq D_{max},\quad \forall k
$$

第一版建议采用软约束，以避免可行域过小。

### 6.4 最小规模约束

为避免极小簇，可设置：

$$
|C_k|\geq n_{min}
$$

或：

$$
Length(C_k)=\sum_{i\in C_k} length_i \geq L_{min}
$$

或：

$$
D(C_k)\geq D_{min}
$$

### 6.5 最大空间跨度约束

为避免区域过大，可设置：

$$
Diam(C_k)\leq Diam_{max}
$$

或使用软约束 $R_{diam}$。

### 6.6 不跨越强屏障约束

若有河流、铁路、高速封闭主路、大型封闭园区等屏障信息，可禁止跨越强屏障合并：

$$
z_i \neq z_j \quad \text{if } (i,j) \text{ crosses hard barrier}
$$

第一版可以不显式加入该约束，而通过边权降低或阻断关系边实现。

---

## 7. 与 Louvain / Region Growing 的关系

### 7.1 退化为 Louvain / Leiden 风格

若：

$$
\lambda_c=0,\quad \lambda_s=0,\quad \lambda_m=0
$$

且仅保留图结构项，则目标主要退化为：

$$
\min R_{graph}(\mathcal{P})
$$

这对应保留路网强关系、类似 community-preserving partition 的目标。

若使用 modularity 形式，则对应：

$$
\max Q(\mathcal{P})
$$

### 7.2 退化为 Demand Region Growing 风格

若：

$$
\lambda_c \gg \lambda_g,\lambda_r
$$

则目标更强调订单容量均衡，接近 Demand Region Growing 的思想。

### 7.3 折中解

当：

$$
\lambda_c,\lambda_g,\lambda_r,\lambda_s
$$

均为正且适当平衡时，算法会在：

- 路网社区结构；
- 订单容量自适应；
- 道路完整性；
- 区域形态；

之间寻找折中解。

---

## 8. 求解策略

该优化问题属于组合优化问题，精确求解困难。建议采用启发式局部搜索。

### 8.1 初始化

可使用以下初始化方案：

1. Leiden 初始化；
2. Louvain 初始化；
3. Demand Region Growing 初始化；
4. Demand Network Voronoi 初始化；
5. METIS + 连通性修复初始化。

建议第一阶段重点比较：

- Leiden initialization；
- Demand Region Growing initialization。

二者代表不同初始偏好：

- Leiden：结构好，需求自适应弱；
- Region Growing：需求自适应强，结构连续性弱。

### 8.2 局部搜索操作

主要操作包括：

#### 8.2.1 单路段移动

选择边界路段 $i$，尝试将其从当前簇 $C_a$ 移动到相邻簇 $C_b$：

$$
z_i:a\rightarrow b
$$

若移动后满足连通性约束且目标函数下降，则接受：

$$
\Delta \mathcal{L}<0
$$

#### 8.2.2 小簇合并

若某簇 $C_k$ 订单容量过低：

$$
D(C_k)<D_{min}
$$

则尝试将其合并到最优邻接簇：

$$
C_j^*=\arg\min_{C_j\in N(C_k)}
\mathcal{L}(\mathcal{P}_{merge(k,j)})
$$

#### 8.2.3 大簇拆分

若某簇容量过大且内部异质性高：

$$
D(C_k)>D_{max}, \quad H(C_k)>H_{max}
$$

则在该簇诱导子图上重新运行 Leiden / Region Growing，将其拆成多个子簇。

拆分数量可设为：

$$
s_k=\left\lceil \frac{D(C_k)}{D_0}\right\rceil
$$

并设置上限：

$$
s_k\leq s_{max}
$$

#### 8.2.4 边界交换

对于两个相邻簇 $C_a,C_b$，尝试交换边界路段集合，以减少目标函数。

---

## 9. 推荐求解流程

```text
Input:
  road-segment relation graph G=(V,E,W)
  demand weights d_i
  POI/order features x_i
  parameters λ_c, λ_g, λ_r, λ_s, λ_m
  capacity thresholds D_min, D_max
  shape threshold Diam_max

Step 1:
  Generate initial partition P0 using Leiden or Demand Region Growing.

Step 2:
  Compute objective L(P0).

Step 3:
  Repeat until convergence:
    1. Identify boundary segments.
    2. For each candidate move i -> neighboring cluster:
         - check connectivity feasibility;
         - compute ΔR_cap;
         - compute ΔR_graph;
         - compute ΔR_road;
         - approximate ΔR_shape;
         - compute ΔL.
    3. Accept the best improving move.
    4. Merge clusters with D(C_k)<D_min if beneficial.
    5. Split clusters with D(C_k)>D_max and high heterogeneity.
    6. Stop if no improvement or max_iter reached.

Output:
  final partition P*
  objective components
  benchmark metrics
```

---

## 10. 参数设计

### 10.1 容量阈值

可设目标容量：

$$
D_0=\frac{\sum_i d_i}{K}
$$

并设：

$$
D_{min}=aD_0,\quad D_{max}=bD_0
$$

例如：

```text
a ∈ {0.3, 0.5, 0.7}
b ∈ {1.5, 2.0, 3.0}
```

### 10.2 正则权重

建议初始设置：

```text
λ_c ∈ {0.1, 0.5, 1.0, 2.0}
λ_g = 1.0
λ_r ∈ {1.0, 2.0, 4.0}
λ_s ∈ {0.0, 0.2, 0.5, 1.0}
λ_m ∈ {0.0, 0.2, 0.5}
```

其中：

- 增大 $\lambda_c$：更强调需求容量均衡；
- 增大 $\lambda_g$：更强调保留综合图强边；
- 增大 $\lambda_r$：更强调道路连续性和 connector 保持；
- 增大 $\lambda_s$：更惩罚大跨度和长条区域；
- 增大 $\lambda_m$：更强调 POI/order 簇内语义同质性。

### 10.3 形态阈值

可根据四环尺度设置：

```text
Diam_max ∈ {3 km, 5 km, 8 km}
```

具体应根据实际簇网络直径分布调整。

---

## 11. 实验方案

### 11.1 数据变体

继续保留三类图构建方式：

1. Road-only；
2. Road + POI；
3. Road + POI + Order。

正则化方法优先在 Road + POI + Order 上测试，Road-only 和 Road + POI 作为消融。

### 11.2 基准算法

至少比较：

1. Louvain；
2. Leiden；
3. SKATER；
4. METIS；
5. Demand Network Voronoi；
6. Demand Region Growing；
7. Regularized Leiden；
8. Regularized Region Growing；
9. Regularized Hybrid Search。

其中：

- Regularized Leiden：Leiden 初始化 + 正则化局部搜索；
- Regularized Region Growing：Demand Region Growing 初始化 + 正则化局部搜索；
- Regularized Hybrid Search：多初始解取最优。

### 11.3 指标体系

继续使用已有指标，包括：

#### 基本规模

- number of clusters；
- mean/min/max segments per cluster；
- mean cluster length；
- size balance CV。

#### 连通性

- connected cluster ratio；
- total fragment count；
- mean components per cluster。

#### 道路完整性

- continuity edge cut ratio；
- same-road adjacent cut ratio；
- mean clusters per named road；
- dominant cluster share。

#### Connector 保持

- connector edge cut ratio；
- connector cut ratio by type。

#### 形态

- compactness；
- elongation；
- convex hull ratio；
- network diameter。

#### 语义同质性

- POI intra-edge homogeneity；
- order intra-edge homogeneity；
- POI WSS；
- order WSS；
- POI entropy；
- dominant POI purity；
- pickup/dropoff imbalance variance。

#### OD 可用性

- cluster OD sparsity；
- effective OD pair ratio；
- mean orders per nonzero OD；
- historical-average MAE；
- historical-average RMSE；
- historical-average WAPE。

### 11.4 Trade-off 分析

重点画以下 trade-off：

1. **OD 稀疏性 vs continuity cut**

$$
x = cluster\_od\_sparsity
$$

$$
y = continuity\_edge\_cut\_ratio
$$

2. **容量均衡 vs connector cut**

$$
x = size\_balance\_cv
$$

$$
y = connector\_edge\_cut\_ratio
$$

3. **OD WAPE vs road integrity**

$$
x = historical\_avg\_wape
$$

$$
y = mean\_clusters\_per\_named\_road
$$

4. **shape vs OD usability**

$$
x = mean\_network\_diameter
$$

$$
y = cluster\_od\_sparsity
$$

理想方法应位于 Pareto frontier 附近。

### 11.5 消融实验

#### 消融一：去掉容量正则

$$
\lambda_c=0
$$

观察是否退化为 Leiden-like 结构。

#### 消融二：去掉道路结构正则

$$
\lambda_g=\lambda_r=0
$$

观察是否变成 Region-Growing-like 结果，并检查 continuity/connector cut 是否升高。

#### 消融三：去掉形态正则

$$
\lambda_s=0
$$

观察是否出现长条区域或大跨度簇。

#### 消融四：去掉语义正则

$$
\lambda_m=0
$$

观察 POI/order 同质性是否下降。

#### 消融五：不同初始化

比较：

- Leiden initialization；
- Demand Region Growing initialization；
- Network Voronoi initialization。

检查目标函数和指标是否对初始化敏感。

### 11.6 训练/测试订单隔离

为避免信息泄露，建议：

```text
构图与优化使用:
  2017-10-16 至 2017-10-20

评价使用:
  2017-10-23 至 2017-10-27
```

或：

```text
构图使用工作日前四天，评价使用周五。
```

这样可验证分区对未来订单的泛化能力。

### 11.7 参数选择策略

不建议单纯用某一个指标选最优。应采用多指标筛选：

1. 必须满足连通性：
   $$
   connected\_cluster\_ratio=1
   $$

2. continuity cut 不显著高于 Leiden：
   $$
   continuity\_cut \leq continuity\_cut_{Leiden}+\epsilon
   $$

3. connector cut 不显著高于 Louvain：
   $$
   connector\_cut \leq connector\_cut_{Louvain}+\epsilon
   $$

4. OD sparsity 明显低于 Leiden / Louvain：
   $$
   OD\_sparsity < OD\_sparsity_{Louvain}
   $$

5. size balance CV 优于 Louvain / Leiden。

最终选择在 Pareto frontier 上表现均衡的参数。

---

## 12. 预期结果与解释

若正则化方法有效，应观察到：

1. 相比 Leiden / Louvain：
   - OD 稀疏率降低；
   - size balance CV 降低；
   - 订单容量更均衡。

2. 相比 Demand Region Growing：
   - continuity edge cut ratio 降低；
   - connector edge cut ratio 降低；
   - mean clusters per named road 降低；
   - 区域形态更稳定。

3. 相比 METIS：
   - 连通性显著提高；
   - 避免空间不连续区域；
   - OD 指标虽可能不如原始 METIS 极端好，但更可信。

4. 相比 SKATER：
   - 避免巨型主簇和极端规模失衡；
   - OD 可用性明显提升。

最终希望得到的是：

> **在路网结构保持和需求自适应之间取得 Pareto 改善的分区方法。**

---

## 13. 可写入论文的方法贡献

该方法可以被描述为：

> 本研究提出一种正则化需求自适应路网运营分区方法。该方法以 road segment 为基本单元，以 connector-mediated adjacency 和 road-continuity relations 构建路段关系图，并将 POI 与网约车订单语义注入边权。不同于传统社区发现方法仅关注图结构紧密性，也不同于需求驱动区域生长方法主要追求订单容量均衡，本文将路网社区结构保持、道路连续性、connector 关系保持、需求容量自适应和区域形态控制统一到一个正则化优化框架中，通过初始化分区和局部边界搜索求解。该方法能够在保持路网连通和道路结构解释性的同时，使高订单密度区域获得更细粒度分区、低订单密度区域形成更粗粒度分区，从而提升区域 OD 表征和后续网约车运营分析的可用性。

---

## 14. 下一步实现建议

第一版实现可以只做：

$$
\mathcal{L}
=
\lambda_c R_{cap}
+
\lambda_g R_{graph}
+
\lambda_r R_{road}
$$

暂时不加入 $R_{shape}$ 和 $R_{sem}$，先验证：

1. 是否能降低 Region Growing 的 continuity / connector cut；
2. 是否能保持低 OD sparsity；
3. 是否能保持所有簇连通。

第二版再加入：

$$
R_{shape}
$$

第三版再加入：

$$
R_{sem}
$$

这样实现风险较低，也便于逐步消融。

