"""Stage 4 helper - cluster-to-cluster road-network distance matrix.

Replaces the centroid Haversine distance used by ``SpatialPruner`` with the
**network shortest-path distance between two clusters' representative OSM
junction nodes**, so the detour pruning respects road topology.

Design (all decided upstream; see plan):

- **Single node space = OSM junction graph** (node = ``osmid``, edge weight =
  OSM edge ``length`` in metres). Two layers, *isomorphic* node space:
  - ``H_raw``  : the full OSM drive graph (``graphml``), undirected, parallel /
    bidirectional edges collapsed to the **minimum length** per ``(u, v)``.
    A single connected component, so it always yields a finite distance.
  - ``H_filt`` : the curated drivable subgraph - the edges of the *classified*
    network (ordinary **and** connector ``*_link`` segments), as an
    edge-induced subgraph of ``H_raw`` so every length is inherited verbatim.
- **Representative node** of a cluster = the cluster's OSM node nearest to the
  cluster centroid (Euclidean, EPSG:32650); ties broken by smallest ``osmid``.
- **Distance** = ``single_source_dijkstra_path_length(weight="length")`` from
  each representative; pairs unreachable inside ``H_filt`` (different filtered
  components) fall back to ``H_raw`` (never to Haversine). Result is forced
  symmetric with a zero diagonal.

The 100x100 matrix is precomputed once and cached as parquet; ``SpatialPruner``
loads it via ``from_distance_matrix``. This module is pure (functions may do
I/O; no module-level side effects).
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable
from typing import Any

from roadnet_partition.io import environment as _environment  # noqa: F401
import numpy as np
import pandas as pd

from roadnet_partition.io.geospatial import PROJECT_ROOT


def project_path(path_value: str | Path | None) -> Path | None:
    if path_value is None:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def sort_cluster_ids(cluster_ids: Iterable[Any]) -> list[str]:
    def key(value: Any) -> tuple[int, int | str]:
        text = str(value)
        try:
            return (0, int(text))
        except ValueError:
            return (1, text)

    return sorted({str(cluster_id) for cluster_id in cluster_ids}, key=key)


def collapse_min_undirected(multigraph):
    """Collapse a (Multi)(Di)Graph to a simple undirected ``nx.Graph`` keeping,
    for each ``(u, v)``, the shortest ``length`` across parallel edges and both
    directions. Self-loops are dropped."""
    import networkx as nx

    graph = nx.Graph()
    for raw_u, raw_v, data in multigraph.edges(data=True):
        u, v = int(raw_u), int(raw_v)
        if u == v:
            continue
        length = float(data["length"])
        existing = graph.get_edge_data(u, v)
        if existing is None or length < existing["length"]:
            graph.add_edge(u, v, length=length)
    return graph


def load_osm_graph_undirected_min(graphml_path: Path):
    """Load the OSM graphml as a simple undirected graph with min-length edges.

    Returns ``(graph, node_xy)`` where ``node_xy`` maps an integer ``osmid`` to
    its ``(x, y)`` longitude/latitude (EPSG:4326). Parallel / bidirectional
    edges collapse to the shortest ``length``."""
    import osmnx as ox

    multigraph = ox.load_graphml(graphml_path)
    graph = collapse_min_undirected(multigraph)
    node_xy: dict[int, tuple[float, float]] = {}
    for node, data in multigraph.nodes(data=True):
        node_xy[int(node)] = (float(data["x"]), float(data["y"]))
    return graph, node_xy


def build_filtered_subgraph(graph_raw, classified_edges_path: Path):
    """Edge-induced subgraph of ``graph_raw`` over the classified drivable
    network (ordinary + connector segments). Lengths are inherited from
    ``graph_raw`` so both layers share one length definition."""
    import geopandas as gpd
    import networkx as nx

    classified = gpd.read_file(classified_edges_path)
    missing = {"u", "v"} - set(classified.columns)
    if missing:
        raise ValueError(f"Classified edges missing columns {sorted(missing)}: {classified_edges_path}")

    graph_filt = nx.Graph()
    inherited = 0
    fallback = 0
    for u_raw, v_raw, length in zip(classified["u"], classified["v"], classified.get("length", [None] * len(classified))):
        if pd.isna(u_raw) or pd.isna(v_raw):
            continue
        u, v = int(u_raw), int(v_raw)
        if u == v:
            continue
        raw = graph_raw.get_edge_data(u, v)
        if raw is not None:
            graph_filt.add_edge(u, v, length=raw["length"])
            inherited += 1
        elif length is not None and not pd.isna(length):
            # Rare: classified edge absent from raw graph; keep connectivity.
            graph_filt.add_edge(u, v, length=float(length))
            fallback += 1
    print(f"Filtered layer: {graph_filt.number_of_nodes():,} nodes, {graph_filt.number_of_edges():,} edges "
          f"(inherited={inherited:,}, classified-length fallback={fallback:,}).")
    return graph_filt


def cluster_osm_nodes(partition_gpkg: Path) -> dict[str, set[int]]:
    """Map each cluster id (str) to the set of OSM node ids (u, v of its segments)."""
    import geopandas as gpd

    partition = gpd.read_file(partition_gpkg)
    missing = {"u", "v", "cluster_id"} - set(partition.columns)
    if missing:
        raise ValueError(f"Partition missing columns {sorted(missing)}: {partition_gpkg}")
    nodes: dict[str, set[int]] = {}
    for cluster_id, u, v in zip(partition["cluster_id"], partition["u"], partition["v"]):
        cid = str(cluster_id)
        bucket = nodes.setdefault(cid, set())
        if not pd.isna(u):
            bucket.add(int(u))
        if not pd.isna(v):
            bucket.add(int(v))
    return nodes


def project_node_coords(node_xy: dict[int, tuple[float, float]], geographic_crs: str, projected_crs: str) -> dict[int, tuple[float, float]]:
    """Project node lon/lat (EPSG:4326) to the projected CRS (metres)."""
    import geopandas as gpd

    ids = list(node_xy.keys())
    xs = [node_xy[i][0] for i in ids]
    ys = [node_xy[i][1] for i in ids]
    points = gpd.GeoSeries(gpd.points_from_xy(xs, ys), crs=geographic_crs).to_crs(projected_crs)
    return {node_id: (float(p.x), float(p.y)) for node_id, p in zip(ids, points)}


def pick_representatives(
    cluster_nodes: dict[str, set[int]],
    node_xy_proj: dict[int, tuple[float, float]],
    centroids: dict[str, tuple[float, float]],
) -> dict[str, dict[str, float]]:
    """For each cluster pick the OSM node nearest its centroid (projected
    Euclidean); ties broken by smallest osmid. Returns
    ``{cluster_id: {"rep": osmid, "dist": metres}}``."""
    reps: dict[str, dict[str, float]] = {}
    for cid, members in cluster_nodes.items():
        if cid not in centroids:
            continue
        cx, cy = centroids[cid]
        best_node = None
        best_d2 = np.inf
        for node in sorted(members):  # sorted -> deterministic tie-break (min osmid)
            xy = node_xy_proj.get(node)
            if xy is None:
                continue
            d2 = (xy[0] - cx) ** 2 + (xy[1] - cy) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_node = node
        if best_node is not None:
            reps[cid] = {"rep": int(best_node), "dist": float(np.sqrt(best_d2))}
    return reps


def compute_distance_matrix(graph_filt, graph_raw, reps: dict[str, int], cluster_ids: list[str]) -> pd.DataFrame:
    """All-pairs representative network distance, filtered layer first with raw
    fallback for cross-component pairs; symmetric with zero diagonal."""
    import networkx as nx

    n = len(cluster_ids)
    index = {cid: i for i, cid in enumerate(cluster_ids)}
    rep_of = {cid: reps[cid] for cid in cluster_ids}
    matrix = np.full((n, n), np.inf, dtype=float)

    for cid in cluster_ids:
        i = index[cid]
        lengths = nx.single_source_dijkstra_path_length(graph_filt, rep_of[cid], weight="length")
        for other in cluster_ids:
            matrix[i, index[other]] = lengths.get(rep_of[other], np.inf)

    # Raw-graph fallback for any cross-component (inf) pairs.
    raw_rows_needed = [cid for cid in cluster_ids if not np.isfinite(matrix[index[cid]]).all()]
    for cid in raw_rows_needed:
        i = index[cid]
        lengths = nx.single_source_dijkstra_path_length(graph_raw, rep_of[cid], weight="length")
        for other in cluster_ids:
            j = index[other]
            if not np.isfinite(matrix[i, j]):
                matrix[i, j] = lengths.get(rep_of[other], np.inf)

    np.fill_diagonal(matrix, 0.0)
    matrix = (matrix + matrix.T) / 2.0  # enforce symmetry (undirected -> already symmetric up to FP)
    np.fill_diagonal(matrix, 0.0)
    return pd.DataFrame(matrix, index=cluster_ids, columns=cluster_ids)


def _load_validated_distance(matrix_path: Path, reps_path: Path) -> pd.DataFrame | None:
    """Load the run-owned distance matrix + reps if they exist and pass a contract
    (well-formedness) check; return the matrix, or None if absent/corrupt so the
    caller recomputes. The run ownership / resume system invalidates the stage
    (and thus these files) when inputs change, so a valid cache is safe to reuse."""
    if not matrix_path.is_file():
        return None
    try:
        matrix = pd.read_parquet(matrix_path)
        matrix.index = matrix.index.astype(str)
        matrix.columns = matrix.columns.astype(str)
        if matrix.shape[0] != matrix.shape[1] or list(matrix.index) != list(matrix.columns):
            return None
        values = matrix.to_numpy(dtype=float)
        # Distances are non-negative; INF is legitimate (unreachable cluster
        # pairs -- compute_distance_matrix initializes with np.inf and only the
        # raw-graph fallback fills cross-component pairs). NaN or negative
        # values indicate a corrupt matrix.
        if np.isnan(values).any() or (values < 0).any():
            return None
        if not np.allclose(np.diag(values), 0.0):
            return None
        if not np.allclose(values, values.T, equal_nan=True):
            return None
        # reps is validated when present (production writes matrix+reps together);
        # a caller that pre-places only the matrix still gets it loaded.
        if reps_path.is_file():
            reps = pd.read_csv(reps_path, dtype={"cluster_id": str})
            if set(reps["cluster_id"]) != set(matrix.index):
                return None
        return matrix
    except Exception:
        return None


def build_or_load(config: dict[str, Any]) -> pd.DataFrame:
    """Auto load-or-compute the cluster network-distance matrix.

    Behavior (TTE cache, auto):
      - If a run-owned matrix + reps already exist and pass a contract check
        (square, str index == columns, all-finite, symmetric, reps cluster ids
        match), load them.
      - Otherwise compute from the OSM graph + classified edges + partition
        and save both.

    The legacy ``distance.recompute`` flag is deprecated and ignored:
    build_or_load always auto-loads a valid cache or computes+saves. The run
    ownership / resume system invalidates the stage (and thus the cache) when
    inputs change, so a valid cache is always safe to reuse; raw-only first runs
    have no cache and compute+succeed.

    Returns a DataFrame indexed by cluster id (str) with the same columns.
    """
    stage_config = config["stage4_tte"]
    dist_cfg = stage_config.get("distance", {})

    output_dir = Path(project_path(stage_config["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    matrix_path = output_dir / str(dist_cfg.get("matrix_filename", "cluster_network_distance.parquet"))
    reps_path = output_dir / str(dist_cfg.get("representatives_filename", "cluster_representative_nodes.csv"))

    if dist_cfg.get("recompute", False):
        print(
            "distance.recompute=true is deprecated; build_or_load now auto-loads a "
            "valid run-owned cache or computes+saves. Ignoring the flag."
        )

    cached = _load_validated_distance(matrix_path, reps_path)
    if cached is not None:
        print(f"Loading cached network-distance matrix from {matrix_path} (passed contract check).")
        return cached

    graphml_path = Path(project_path(dist_cfg["graphml_path"]))
    classified_edges_path = Path(project_path(dist_cfg["classified_edges_path"]))
    partition_gpkg = Path(project_path(dist_cfg["partition_gpkg"]))
    cluster_index_path = Path(project_path(stage_config["inputs"]["cluster_index_path"]))
    projected_crs = config["crs"]["projected"]
    geographic_crs = config["crs"]["geographic"]

    print(f"Loading OSM graph from {graphml_path}...")
    graph_raw, node_xy = load_osm_graph_undirected_min(graphml_path)
    print(f"Raw layer: {graph_raw.number_of_nodes():,} nodes, {graph_raw.number_of_edges():,} edges.")
    graph_filt = build_filtered_subgraph(graph_raw, classified_edges_path)

    cluster_nodes = cluster_osm_nodes(partition_gpkg)
    node_xy_proj = project_node_coords(node_xy, geographic_crs, projected_crs)

    cluster_index = pd.read_csv(cluster_index_path, dtype={"cluster_id": str})
    centroids = {
        str(row.cluster_id): (float(row.centroid_x), float(row.centroid_y))
        for row in cluster_index.itertuples(index=False)
    }

    reps_full = pick_representatives(cluster_nodes, node_xy_proj, centroids)
    cluster_ids = sort_cluster_ids([cid for cid in reps_full])
    reps = {cid: reps_full[cid]["rep"] for cid in cluster_ids}
    print(f"Selected representatives for {len(cluster_ids)} clusters.")

    matrix = compute_distance_matrix(graph_filt, graph_raw, reps, cluster_ids)

    matrix.to_parquet(matrix_path)
    reps_df = pd.DataFrame(
        {
            "cluster_id": cluster_ids,
            "rep_osmid": [reps[cid] for cid in cluster_ids],
            "dist_to_centroid_m": [reps_full[cid]["dist"] for cid in cluster_ids],
        }
    )
    reps_df.to_csv(reps_path, index=False)
    finite = np.isfinite(matrix.to_numpy())
    print(f"Wrote {matrix_path} (shape={matrix.shape}, finite={finite.all()}, "
          f"max={np.nanmax(matrix.to_numpy()):.1f} m).")
    print(f"Wrote {reps_path}.")
    return matrix
