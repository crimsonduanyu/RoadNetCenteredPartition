from __future__ import annotations

from pathlib import Path
from typing import Any
import math

import numpy as np
import pandas as pd
from scipy import sparse

from roadnet_partition.io.geospatial import (
    display_path, match_points_to_segments_with_distance, project_path,
)

def normalize_adjacency(matrix: sparse.csr_matrix, add_self_loops: bool = True, symmetric: bool = True) -> sparse.csr_matrix:
    work = matrix.astype(float).tocsr()
    if add_self_loops:
        work = work + sparse.eye(work.shape[0], dtype=float, format="csr")

    degree = np.asarray(work.sum(axis=1)).ravel()
    if symmetric:
        inv_sqrt = np.zeros_like(degree, dtype=float)
        positive = degree > 0
        inv_sqrt[positive] = 1.0 / np.sqrt(degree[positive])
        normalizer = sparse.diags(inv_sqrt, format="csr")
        return (normalizer @ work @ normalizer).tocsr()

    inv = np.zeros_like(degree, dtype=float)
    positive = degree > 0
    inv[positive] = 1.0 / degree[positive]
    return (sparse.diags(inv, format="csr") @ work).tocsr()

def edges_to_adjacency(edges: pd.DataFrame, cluster_count: int) -> sparse.csr_matrix:
    if edges.empty:
        return sparse.csr_matrix((cluster_count, cluster_count), dtype=float)

    rows = edges["cluster_index_a"].astype(int).to_numpy()
    cols = edges["cluster_index_b"].astype(int).to_numpy()
    weights = edges["weight"].astype(float).to_numpy()
    all_rows = np.concatenate([rows, cols])
    all_cols = np.concatenate([cols, rows])
    all_weights = np.concatenate([weights, weights])
    return sparse.coo_matrix((all_weights, (all_rows, all_cols)), shape=(cluster_count, cluster_count)).tocsr()

def save_graph_assets(
    graph_name: str,
    edges: pd.DataFrame,
    cluster_count: int,
    output_dir: Path,
    normalization_config: dict[str, Any],
) -> dict[str, Any]:
    edge_path = output_dir / f"cluster_graph_{graph_name}_edges.csv"
    raw_path = output_dir / f"cluster_graph_{graph_name}_adjacency_raw.npz"
    normalized_path = output_dir / f"cluster_graph_{graph_name}_adjacency_normalized.npz"

    edge_columns = ["cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight"]
    for column in edge_columns:
        if column not in edges.columns:
            edges[column] = pd.Series(dtype=float if column == "weight" else object)
    edges = edges.loc[:, [*edge_columns, *[column for column in edges.columns if column not in edge_columns]]]
    edges.to_csv(edge_path, index=False)

    adjacency = edges_to_adjacency(edges, cluster_count)
    normalized = normalize_adjacency(
        adjacency,
        add_self_loops=bool(normalization_config.get("add_self_loops", True)),
        symmetric=bool(normalization_config.get("symmetric", True)),
    )
    sparse.save_npz(raw_path, adjacency)
    sparse.save_npz(normalized_path, normalized)
    return {
        "name": graph_name,
        "edges": int(len(edges)),
        "edge_path": display_path(edge_path),
        "raw_adjacency_path": display_path(raw_path),
        "normalized_adjacency_path": display_path(normalized_path),
    }

def build_cluster_road_edges(
    relation_edges: pd.DataFrame,
    segment_to_cluster: dict[str, str],
    cluster_to_index: dict[str, int],
    weight_column: str = "base_weight",
) -> pd.DataFrame:
    required = {"seg_id_a", "seg_id_b"}
    missing = required - set(relation_edges.columns)
    if missing:
        raise ValueError(f"Road relation edge file is missing columns: {sorted(missing)}")
    if weight_column not in relation_edges.columns:
        if "weight" not in relation_edges.columns:
            raise ValueError(f"Road relation edge file lacks both '{weight_column}' and 'weight'.")
        weight_column = "weight"

    frame = relation_edges[["seg_id_a", "seg_id_b", weight_column]].copy()
    frame["cluster_a"] = frame["seg_id_a"].astype(str).map(segment_to_cluster)
    frame["cluster_b"] = frame["seg_id_b"].astype(str).map(segment_to_cluster)
    frame = frame.dropna(subset=["cluster_a", "cluster_b"]).copy()
    frame = frame.loc[frame["cluster_a"] != frame["cluster_b"]].copy()
    if frame.empty:
        return empty_cluster_edge_frame()

    frame["cluster_index_a_raw"] = frame["cluster_a"].map(cluster_to_index).astype(int)
    frame["cluster_index_b_raw"] = frame["cluster_b"].map(cluster_to_index).astype(int)
    frame["cluster_index_a"] = frame[["cluster_index_a_raw", "cluster_index_b_raw"]].min(axis=1)
    frame["cluster_index_b"] = frame[["cluster_index_a_raw", "cluster_index_b_raw"]].max(axis=1)
    frame["weight_value"] = pd.to_numeric(frame[weight_column], errors="coerce").fillna(0.0).clip(lower=0.0)

    grouped = (
        frame.groupby(["cluster_index_a", "cluster_index_b"], as_index=False)
        .agg(weight=("weight_value", "sum"), num_segment_edges=("weight_value", "size"))
        .sort_values(["cluster_index_a", "cluster_index_b"])
        .reset_index(drop=True)
    )
    index_to_cluster = {index: cluster_id for cluster_id, index in cluster_to_index.items()}
    grouped["cluster_id_a"] = grouped["cluster_index_a"].map(index_to_cluster)
    grouped["cluster_id_b"] = grouped["cluster_index_b"].map(index_to_cluster)
    return grouped[["cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight", "num_segment_edges"]]

def empty_cluster_edge_frame(extra_columns: list[str] | None = None) -> pd.DataFrame:
    columns = ["cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight"]
    if extra_columns:
        columns.extend(extra_columns)
    return pd.DataFrame(columns=columns)

def build_similarity_topk_edges(similarity: np.ndarray, cluster_ids: list[str], top_k: int) -> pd.DataFrame:
    cluster_count = len(cluster_ids)
    if cluster_count <= 1 or top_k <= 0:
        return empty_cluster_edge_frame(["similarity"])

    edge_map: dict[tuple[int, int], float] = {}
    for index in range(cluster_count):
        scores = similarity[index].copy()
        scores[index] = -np.inf
        candidate_indices = np.argsort(scores)[::-1]
        selected = [int(candidate) for candidate in candidate_indices if scores[candidate] > 0][:top_k]
        for candidate in selected:
            left, right = sorted((index, candidate))
            edge_map[(left, right)] = max(edge_map.get((left, right), 0.0), float(scores[candidate]))

    rows = [
        {
            "cluster_id_a": cluster_ids[left],
            "cluster_id_b": cluster_ids[right],
            "cluster_index_a": left,
            "cluster_index_b": right,
            "weight": weight,
            "similarity": weight,
        }
        for (left, right), weight in sorted(edge_map.items())
    ]
    return pd.DataFrame(rows) if rows else empty_cluster_edge_frame(["similarity"])

def build_cluster_poi_graph(
    partition,
    cluster_ids: list[str],
    cluster_to_index: dict[str, int],
    config: dict[str, Any],
    output_dir: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    poi_config = config["order_pipeline"]["poi"]
    poi_path = project_path(config["order_pipeline"]["inputs"].get("poi_path"))
    if poi_path is None:
        return empty_cluster_edge_frame(["similarity"]), {"enabled": False, "valid_poi_rows": 0, "matched_poi_rows": 0}
    if not poi_path.exists():
        raise FileNotFoundError(f"Configured POI file does not exist: {poi_path}")

    lon_col = poi_config["lon_column"]
    lat_col = poi_config["lat_column"]
    category_col = poi_config["category_column"]
    poi = pd.read_csv(poi_path, usecols=[lon_col, lat_col, category_col])
    for column in [lon_col, lat_col]:
        poi[column] = pd.to_numeric(poi[column], errors="coerce")
    poi = poi.dropna(subset=[lon_col, lat_col, category_col]).copy()
    valid = np.isfinite(poi[lon_col]) & np.isfinite(poi[lat_col])
    poi = poi.loc[valid].copy()
    if poi.empty:
        return empty_cluster_edge_frame(["similarity"]), {"enabled": True, "valid_poi_rows": 0, "matched_poi_rows": 0}

    match = match_points_to_segments_with_distance(
        poi,
        lon_col,
        lat_col,
        partition[["seg_id", "geometry"]],
        config["crs"]["geographic"],
        float(poi_config["max_match_distance_m"]),
    )
    matched = poi.loc[match["seg_id"].notna()].copy()
    matched["seg_id"] = match.loc[match["seg_id"].notna(), "seg_id"].astype(str).to_numpy()
    segment_to_cluster = dict(zip(partition["seg_id"].astype(str), partition["cluster_id"].astype(str)))
    matched["cluster_id"] = matched["seg_id"].map(segment_to_cluster)
    matched = matched.dropna(subset=["cluster_id"]).copy()

    categories = sorted(matched[category_col].astype(str).unique().tolist())
    if categories:
        counts = (
            matched.groupby(["cluster_id", category_col])
            .size()
            .unstack(fill_value=0)
            .reindex(index=cluster_ids, columns=categories, fill_value=0)
        )
    else:
        counts = pd.DataFrame(index=cluster_ids)

    feature_frame = counts.reset_index().rename(columns={"index": "cluster_id"})
    rename_map = {category: f"poi_cat_{index:02d}" for index, category in enumerate(categories)}
    feature_frame = feature_frame.rename(columns=rename_map)
    category_map = pd.DataFrame(
        [{"category_col": column, "poi_type": category} for category, column in rename_map.items()]
    )
    feature_frame["poi_total"] = counts.sum(axis=1).to_numpy(dtype=int) if not counts.empty else 0
    feature_frame.to_csv(output_dir / "cluster_poi_features.csv", index=False)
    category_map.to_csv(output_dir / "cluster_poi_category_mapping.csv", index=False)

    values = counts.to_numpy(dtype=float) if not counts.empty else np.zeros((len(cluster_ids), 0), dtype=float)
    norms = np.linalg.norm(values, axis=1)
    similarity = np.zeros((len(cluster_ids), len(cluster_ids)), dtype=float)
    positive = norms > 0
    if values.shape[1] > 0 and bool(positive.any()):
        normalized = np.divide(values, norms[:, None], out=np.zeros_like(values), where=norms[:, None] > 0)
        similarity = normalized @ normalized.T
    edges = build_similarity_topk_edges(similarity, cluster_ids, int(poi_config["similarity_top_k"]))
    return edges, {"enabled": True, "valid_poi_rows": int(len(poi)), "matched_poi_rows": int(len(matched))}

def build_cluster_distance_graph(cluster_index: pd.DataFrame, cluster_ids: list[str], config: dict[str, Any]) -> pd.DataFrame:
    distance_config = config["order_pipeline"]["distance_graph"]
    top_k = int(distance_config["top_k"])
    decay = float(distance_config["decay_distance_m"])
    if decay <= 0:
        raise ValueError("order_pipeline.distance_graph.decay_distance_m must be positive.")
    if len(cluster_ids) <= 1 or top_k <= 0:
        return empty_cluster_edge_frame(["distance_m"])

    coords = cluster_index.sort_values("cluster_index")[["centroid_x", "centroid_y"]].to_numpy(dtype=float)
    edge_map: dict[tuple[int, int], float] = {}
    distance_map: dict[tuple[int, int], float] = {}
    for index in range(len(cluster_ids)):
        delta = coords - coords[index]
        distances = np.sqrt((delta * delta).sum(axis=1))
        distances[index] = np.inf
        for candidate in np.argsort(distances)[:top_k]:
            distance = float(distances[candidate])
            if not math.isfinite(distance):
                continue
            left, right = sorted((index, int(candidate)))
            weight = float(math.exp(-distance / decay))
            edge_map[(left, right)] = max(edge_map.get((left, right), 0.0), weight)
            distance_map[(left, right)] = min(distance_map.get((left, right), float("inf")), distance)

    rows = [
        {
            "cluster_id_a": cluster_ids[left],
            "cluster_id_b": cluster_ids[right],
            "cluster_index_a": left,
            "cluster_index_b": right,
            "weight": edge_map[(left, right)],
            "distance_m": distance_map[(left, right)],
        }
        for left, right in sorted(edge_map)
    ]
    return pd.DataFrame(rows) if rows else empty_cluster_edge_frame(["distance_m"])

