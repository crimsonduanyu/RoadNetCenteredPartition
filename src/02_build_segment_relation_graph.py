from __future__ import annotations

from collections import defaultdict
import shutil
import pickle

import env_setup  # noqa: F401
import geopandas as gpd
import numpy as np
import networkx as nx
import pandas as pd

from utils_geo import DATA_INTERIM, DATA_PROCESSED, DATA_RAW, OUTPUTS_GRAPHS, angle_diff, compute_bearing, ensure_directories, load_config, validate_boundary_polygon
from utils_graph import build_incident_index, ensure_edge_record, iter_incident_pairs, serialize_edge_records


def continuity_score(record_a: dict, record_b: dict, config: dict) -> tuple[float, dict]:
    continuity = config["continuity"]
    score = 0.0

    name_a = record_a.get("name")
    name_b = record_b.get("name")
    same_name = bool(name_a and name_b and name_a == name_b)
    if same_name:
        score += float(continuity["same_name_bonus"])

    same_osmid = record_a.get("osmid") == record_b.get("osmid") and record_a.get("osmid") is not None
    if same_osmid:
        score += float(continuity["same_osm_id_bonus"])

    same_highway = record_a.get("highway") == record_b.get("highway") and record_a.get("highway") is not None
    if same_highway:
        score += float(continuity["same_highway_bonus"])

    current_angle_diff = angle_diff(record_a.get("bearing"), record_b.get("bearing"))
    if current_angle_diff is not None and current_angle_diff <= float(continuity["angle_threshold_deg"]):
        score += 1.0

    diagnostics = {
        "same_name": same_name,
        "same_osmid": same_osmid,
        "same_highway": same_highway,
        "angle_diff": current_angle_diff,
    }
    return score, diagnostics


def cosine_similarity(vector_a: np.ndarray, vector_b: np.ndarray) -> float:
    norm_a = float(np.linalg.norm(vector_a))
    norm_b = float(np.linalg.norm(vector_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(vector_a, vector_b) / (norm_a * norm_b))


def bounded_inverse_difference(value_a: float, value_b: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return float(max(0.0, 1.0 - abs(value_a - value_b) / scale))


def load_poi_similarity_inputs(config: dict, segment_ids: list[str]) -> dict[str, dict] | None:
    path = DATA_PROCESSED / "segment_poi_features.csv"
    if not path.exists():
        print(f"POI feature file not found at {path}; POI similarity will be zero.")
        return None

    features = pd.read_csv(path)
    category_cols = [column for column in features.columns if column.startswith("poi_cat_")]
    if not category_cols:
        return None

    features = features.set_index("seg_id").reindex(segment_ids).fillna(0)
    counts = features[category_cols].to_numpy(dtype=float)
    totals = counts.sum(axis=1)
    composition = np.divide(counts, totals[:, None], out=np.zeros_like(counts), where=totals[:, None] > 0)
    density = np.log1p(features["poi_density"].to_numpy(dtype=float))
    entropy = features["poi_entropy"].to_numpy(dtype=float)

    density_scale = float(max(density.max() - density.min(), 1.0))
    inputs = {}
    for idx, seg_id in enumerate(segment_ids):
        inputs[seg_id] = {
            "has_poi": bool(totals[idx] > 0),
            "composition": composition[idx],
            "density": float(density[idx]),
            "entropy": float(entropy[idx]),
            "density_scale": density_scale,
        }
    return inputs


def load_order_similarity_inputs(segment_ids: list[str]) -> dict[str, dict] | None:
    path = DATA_PROCESSED / "segment_order_features.csv"
    if not path.exists():
        print(f"Order feature file not found at {path}; order similarity will be zero.")
        return None

    features = pd.read_csv(path).set_index("seg_id").reindex(segment_ids).fillna(0)
    vector_cols = [
        "pickup_count",
        "dropoff_count",
        "order_total",
        "pickup_dropoff_imbalance",
        "morning_peak_pickups",
        "evening_peak_pickups",
        "night_pickups",
        "weekday_pickups",
        "weekend_pickups",
        "weekday_weekend_diff",
    ]
    values = features[vector_cols].to_numpy(dtype=float)
    values = np.sign(values) * np.log1p(np.abs(values))
    totals = features["order_total"].to_numpy(dtype=float)

    inputs = {}
    for idx, seg_id in enumerate(segment_ids):
        inputs[seg_id] = {
            "has_order": bool(totals[idx] > 0),
            "vector": values[idx],
        }
    return inputs


def poi_similarity(seg_a: str, seg_b: str, poi_inputs: dict[str, dict] | None, config: dict) -> float:
    if not poi_inputs:
        return 0.0
    record_a = poi_inputs[seg_a]
    record_b = poi_inputs[seg_b]
    if not record_a["has_poi"] or not record_b["has_poi"]:
        return 0.0

    similarity_config = config["semantic_graph"]["similarity"]
    composition = cosine_similarity(record_a["composition"], record_b["composition"])
    density = bounded_inverse_difference(record_a["density"], record_b["density"], record_a["density_scale"])
    entropy = 1.0 - min(1.0, abs(record_a["entropy"] - record_b["entropy"]))
    return float(
        float(similarity_config["poi_composition_weight"]) * composition
        + float(similarity_config["poi_density_weight"]) * density
        + float(similarity_config["poi_entropy_weight"]) * entropy
    )


def order_similarity(seg_a: str, seg_b: str, order_inputs: dict[str, dict] | None) -> float:
    if not order_inputs:
        return 0.0
    record_a = order_inputs[seg_a]
    record_b = order_inputs[seg_b]
    if not record_a["has_order"] or not record_b["has_order"]:
        return 0.0
    return cosine_similarity(record_a["vector"], record_b["vector"])


def save_variant_graph(
    variant_name: str,
    variant_config: dict,
    edge_map: dict[tuple[str, str], dict],
    ordinary: gpd.GeoDataFrame,
) -> tuple[nx.Graph, pd.DataFrame]:
    variant_edge_map = {}
    poi_weight = float(variant_config["poi_weight"])
    order_weight = float(variant_config["order_weight"])

    for pair, record in edge_map.items():
        variant_record = record.copy()
        variant_record["relation_types"] = set(record["relation_types"])
        variant_record["connector_ids"] = set(record["connector_ids"])
        variant_record["connector_highways"] = set(record["connector_highways"])
        variant_record["poi_weight"] = poi_weight * float(record["poi_similarity"])
        variant_record["order_weight"] = order_weight * float(record["order_similarity"])
        variant_record["weight"] = (
            float(record["base_weight"])
            + variant_record["poi_weight"]
            + variant_record["order_weight"]
        )
        variant_edge_map[pair] = variant_record

    relation_edges = pd.DataFrame(serialize_edge_records(variant_edge_map))
    graph = nx.Graph()
    for row in ordinary.to_dict("records"):
        row.pop("geometry")
        graph.add_node(row["seg_id"], **row)

    for edge_record in relation_edges.to_dict("records"):
        attrs = edge_record.copy()
        seg_a = attrs.pop("seg_id_a")
        seg_b = attrs.pop("seg_id_b")
        graph.add_edge(seg_a, seg_b, **attrs)

    edge_output_path = DATA_PROCESSED / f"segment_relation_edges_{variant_name}.csv"
    graph_output_path = OUTPUTS_GRAPHS / f"segment_relation_graph_{variant_name}.gpickle"
    relation_edges.to_csv(edge_output_path, index=False)
    with graph_output_path.open("wb") as handle:
        pickle.dump(graph, handle)

    if variant_name == "road_only":
        relation_edges.to_csv(DATA_PROCESSED / "segment_relation_edges.csv", index=False)
        shutil.copyfile(graph_output_path, OUTPUTS_GRAPHS / "segment_relation_graph.gpickle")

    print(f"Saved {variant_name} relation edges to {edge_output_path}")
    print(f"Saved {variant_name} graph to {graph_output_path}")
    return graph, relation_edges


def main() -> None:
    ensure_directories()
    config = load_config()

    classified_path = DATA_INTERIM / "road_edges_classified.gpkg"
    boundary_path = DATA_RAW / "beijing_fifth_ring_boundary.gpkg"
    nodes_output_path = DATA_PROCESSED / "segment_nodes.gpkg"

    print(f"Loading classified edges from {classified_path}...")
    edges = gpd.read_file(classified_path)
    boundary = gpd.read_file(boundary_path)
    validate_boundary_polygon(boundary)
    edges["bearing"] = edges.geometry.map(compute_bearing)

    ordinary = edges.loc[edges["segment_role"] == "ordinary"].copy()
    connectors = edges.loc[edges["segment_role"] == "connector"].copy()

    ordinary.to_file(nodes_output_path, driver="GPKG")

    ordinary_records = ordinary.drop(columns="geometry").to_dict("records")
    ordinary_by_seg_id = {record["seg_id"]: record for record in ordinary_records}
    segment_ids = ordinary["seg_id"].tolist()
    ordinary_incident = build_incident_index(ordinary_records)
    poi_inputs = load_poi_similarity_inputs(config, segment_ids)
    order_inputs = load_order_similarity_inputs(segment_ids)

    edge_map: dict[tuple[str, str], dict] = {}
    direct_pairs = set()
    connector_pairs = set()
    continuity_pairs = set()

    direct_weight = float(config["graph_weights"]["direct_adjacency_weight"])
    connector_weight = float(config["graph_weights"]["connector_adjacency_weight"])
    continuity_weight = float(config["graph_weights"]["continuity_weight"])

    incident_groups = defaultdict(set)
    for record in ordinary_records:
        incident_groups[record["u"]].add(record["seg_id"])
        incident_groups[record["v"]].add(record["seg_id"])

    for _, seg_ids in incident_groups.items():
        for seg_a, seg_b in iter_incident_pairs(seg_ids):
            edge_record = ensure_edge_record(edge_map, seg_a, seg_b)
            edge_record["direct_weight"] += direct_weight
            edge_record["base_weight"] += direct_weight
            edge_record["weight"] = edge_record["base_weight"]
            edge_record["has_direct"] = True
            edge_record["relation_types"].add("direct")
            direct_pairs.add((edge_record["seg_id_a"], edge_record["seg_id_b"]))

    for connector in connectors.to_dict("records"):
        left_segments = ordinary_incident.get(connector["u"], set())
        right_segments = ordinary_incident.get(connector["v"], set())
        if not left_segments or not right_segments:
            continue
        for seg_a in sorted(left_segments):
            for seg_b in sorted(right_segments):
                if seg_a == seg_b:
                    continue
                edge_record = ensure_edge_record(edge_map, seg_a, seg_b)
                edge_record["connector_weight"] += connector_weight
                edge_record["base_weight"] += connector_weight
                edge_record["weight"] = edge_record["base_weight"]
                edge_record["has_connector"] = True
                edge_record["relation_types"].add("connector")
                edge_record["connector_count"] += 1
                edge_record["connector_ids"].add(connector["seg_id"])
                if connector.get("highway") is not None:
                    edge_record["connector_highways"].add(connector["highway"])
                connector_pairs.add((edge_record["seg_id_a"], edge_record["seg_id_b"]))

    for pair, edge_record in edge_map.items():
        record_a = ordinary_by_seg_id[edge_record["seg_id_a"]]
        record_b = ordinary_by_seg_id[edge_record["seg_id_b"]]
        score, diagnostics = continuity_score(record_a, record_b, config)
        edge_record["same_name"] = diagnostics["same_name"]
        edge_record["same_osmid"] = diagnostics["same_osmid"]
        edge_record["same_highway"] = diagnostics["same_highway"]
        edge_record["angle_diff"] = diagnostics["angle_diff"]
        edge_record["continuity_score"] = score
        if score > 0:
            current_continuity_weight = continuity_weight * score
            edge_record["continuity_weight"] += current_continuity_weight
            edge_record["base_weight"] += current_continuity_weight
            edge_record["weight"] = edge_record["base_weight"]
            edge_record["has_continuity"] = True
            edge_record["relation_types"].add("continuity")
            continuity_pairs.add(pair)

        edge_record["poi_similarity"] = poi_similarity(edge_record["seg_id_a"], edge_record["seg_id_b"], poi_inputs, config)
        edge_record["order_similarity"] = order_similarity(edge_record["seg_id_a"], edge_record["seg_id_b"], order_inputs)

    variant_summaries = {}
    for variant_name, variant_config in config["semantic_graph"]["variants"].items():
        graph, relation_edges = save_variant_graph(variant_name, variant_config, edge_map, ordinary)
        variant_summaries[variant_name] = {
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "poi_edges": int((relation_edges["poi_weight"] > 0).sum()),
            "order_edges": int((relation_edges["order_weight"] > 0).sum()),
        }

    print("segment graph is built from Fifth-Ring-retained roads only")
    print(f"number of segment graph nodes: {len(ordinary):,}")
    print(f"number of direct adjacency edges: {len(direct_pairs):,}")
    print(f"number of connector-mediated edges: {len(connector_pairs):,}")
    print(f"number of continuity-enhanced edges: {len(continuity_pairs):,}")
    for variant_name, summary in variant_summaries.items():
        print(
            f"{variant_name}: edges={summary['edges']:,}, "
            f"poi-weighted edges={summary['poi_edges']:,}, order-weighted edges={summary['order_edges']:,}"
        )
    print(f"Saved ordinary segment nodes to {nodes_output_path}")


if __name__ == "__main__":
    main()
