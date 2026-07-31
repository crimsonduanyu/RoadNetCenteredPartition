from __future__ import annotations

from collections import Counter, defaultdict
import math
from pathlib import Path
import pickle
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import Point
import yaml

from roadnet_partition.graphs.relations import (
    build_incident_index,
    ensure_edge_record,
    iter_incident_pairs,
    serialize_edge_records,
)
from roadnet_partition.io.geospatial import (
    OSM_NORMALIZE_FIELDS,
    angle_diff,
    compute_bearing,
    make_gpkg_safe,
    match_points_to_segments_with_distance,
    normalize_columns,
    normalize_osm_value,
    normalize_road_name,
    project_gdf,
    road_name_matches,
    validate_boundary_polygon,
)
from roadnet_partition.io.manifests import atomic_write_json, file_record
from roadnet_partition.zoning.algorithms.leiden import run_leiden


ACCESS_COLUMNS = ("access", "vehicle", "motor_vehicle", "psv", "motorcar", "taxi")
OUTPUT_NAMES = {
    "classified_edges": "road_edges_classified.gpkg",
    "segment_nodes": "segment_nodes.gpkg",
    "poi_features": "segment_poi_features.csv",
    "poi_category_mapping": "poi_category_mapping.csv",
    "order_features": "segment_order_features.csv",
    "order_od_pairs": "segment_order_od_pairs.csv",
    "hourly_od": "segment_order_od_hourly.csv",
    "relation_edges": "segment_relation_edges_road_poi_order.csv",
    "graph": "segment_relation_graph_road_poi_order.gpickle",
    "baseline_leiden": "segment_clusters_road_poi_order_leiden_res0p6.gpkg",
}


def load_config(path: Path, project_root: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(f"invalid preparation config: {path}")
    config = dict(raw)
    config["source_path"] = path.resolve()
    config["project_root"] = project_root.resolve()
    config["inputs"] = {
        name: (path.parent / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        for name, value in raw["inputs"].items()
    }
    dataset_path = (path.parent / raw["dataset_config"]).resolve()
    dataset = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
    config["crs"] = dataset["crs"]
    config["study_area"] = dataset["study_area"]
    for name, value in config["inputs"].items():
        if not value.is_file():
            raise FileNotFoundError(f"preparation input {name} is missing: {value}")
    return config


def input_records(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f"preparation.{name}": file_record(path) for name, path in config["inputs"].items()}


def check_raw(config_path: Path, project_root: Path) -> dict[str, dict[str, Any]]:
    config = load_config(config_path, project_root)
    required_csv_columns = {
        "poi": {config["poi"]["lon_column"], config["poi"]["lat_column"], config["poi"]["category_column"]},
        "zoning_orders": {
            config["orders"]["pickup_lon_column"], config["orders"]["pickup_lat_column"],
            config["orders"]["dropoff_lon_column"], config["orders"]["dropoff_lat_column"],
            config["orders"]["time_column"],
        },
    }
    for name, required in required_csv_columns.items():
        columns = set(pd.read_csv(config["inputs"][name], nrows=0).columns)
        missing = sorted(required - columns)
        if missing:
            raise ValueError(f"{name} is missing columns: {missing}")
    for name in ("raw_edges", "boundary", "ring_segments"):
        frame = gpd.read_file(config["inputs"][name], rows=1)
        if frame.crs is None:
            raise ValueError(f"{name} has no CRS")
    return input_records(config)


def output_paths(output_dir: Path) -> dict[str, Path]:
    return {name: output_dir / filename for name, filename in OUTPUT_NAMES.items()}


def _string(value: Any) -> str | None:
    value = normalize_osm_value(value)
    if value is None or pd.isna(value):
        return None
    return str(value).strip().lower()


def _access_allowed(record: pd.Series, config: dict[str, Any]) -> bool:
    allowed_values = set(config["road_filter"]["allow_access_values"])
    excluded_values = set(config["road_filter"]["exclude_access_values"])
    designated = set(config["road_filter"]["allow_designated_access_columns"])
    decision = True
    for column in ACCESS_COLUMNS:
        if column not in record.index:
            continue
        raw = _string(record[column])
        values = [] if not raw else [part.strip() for part in raw.replace("|", ";").split(";") if part.strip()]
        if not values:
            continue
        if any(value in excluded_values for value in values):
            decision = False
        elif any(value in allowed_values for value in values) or (column in designated and "designated" in values):
            decision = True
        else:
            decision = False
    return decision


def _length_ratio(geometry: Any, polygon: Any) -> float:
    if geometry is None or geometry.is_empty or geometry.length == 0:
        return 0.0
    return float(geometry.intersection(polygon).length / geometry.length)


def _preprocess_roads(config: dict[str, Any], paths: dict[str, Path]) -> gpd.GeoDataFrame:
    edges = gpd.read_file(config["inputs"]["raw_edges"])
    boundary = project_gdf(gpd.read_file(config["inputs"]["boundary"]), config["crs"]["projected"])
    ring = project_gdf(gpd.read_file(config["inputs"]["ring_segments"]), config["crs"]["projected"])
    validate_boundary_polygon(boundary)
    edges = project_gdf(normalize_columns(edges, list(OSM_NORMALIZE_FIELDS)), config["crs"]["projected"]).copy()
    edges["highway"] = edges["highway"].map(normalize_osm_value)
    edges["length"] = edges.geometry.length.astype(float)

    area = config["study_area"]
    polygon = boundary.geometry.iloc[0]
    tolerance = float(area.get("boundary_tolerance_m", 0))
    inside_polygon = polygon.buffer(tolerance) if tolerance else polygon
    inside = edges.geometry.map(lambda geometry: _length_ratio(geometry, inside_polygon)) >= float(area["inside_length_ratio_threshold"])
    names = [normalize_road_name(name) for name in area["ring_name_patterns"]]
    excluded = [normalize_road_name(name) for name in area.get("exclude_name_patterns", [])]
    corridor = ring.geometry.union_all().buffer(float(area.get("ring_overlap_tolerance_m", 0)))
    ring_mask = edges["name"].map(lambda value: road_name_matches(value, names, excluded)) & edges.geometry.intersects(corridor)
    edges = edges.loc[inside | ring_mask].copy()

    road_filter = config["road_filter"]
    edges = edges.loc[edges["highway"].isin(road_filter["keep_highway"]) & ~edges["highway"].isin(road_filter["exclude_highway"])].copy()
    if "service" in edges:
        service = edges["service"].map(_string)
        edges = edges.loc[~((edges["highway"] == "service") & service.isin(road_filter["exclude_service_values"]))].copy()
    edges = edges.loc[edges.apply(lambda record: _access_allowed(record, config), axis=1)].reset_index(drop=True)
    edges["seg_id"] = [f"seg_{index:07d}" for index in range(len(edges))]
    connector = edges["highway"].isin(road_filter["connector_highway"]) & (edges["length"] <= float(config["connector_rules"]["max_connector_length_m"]))
    edges["segment_role"] = np.where(connector, "connector", "ordinary")
    make_gpkg_safe(edges).to_file(paths["classified_edges"], driver="GPKG")
    ordinary = edges.loc[~connector].copy()
    make_gpkg_safe(ordinary).to_file(paths["segment_nodes"], driver="GPKG")
    return edges


def _entropy(counts: np.ndarray) -> float:
    probabilities = counts[counts > 0] / counts.sum() if counts.sum() > 0 else np.array([])
    return 0.0 if len(probabilities) <= 1 else -float(np.sum(probabilities * np.log(probabilities))) / math.log(len(counts))


def _build_poi_features(config: dict[str, Any], paths: dict[str, Path], ordinary: gpd.GeoDataFrame) -> None:
    settings = config["poi"]
    lon, lat, category = settings["lon_column"], settings["lat_column"], settings["category_column"]
    poi = pd.read_csv(config["inputs"]["poi"], usecols=[lon, lat, category]).dropna()
    poi = poi.loc[np.isfinite(poi[lon]) & np.isfinite(poi[lat])].copy()
    points = gpd.GeoDataFrame(
        poi[[category]].copy(), geometry=[Point(xy) for xy in zip(poi[lon], poi[lat])], crs=config["crs"]["geographic"],
    ).to_crs(ordinary.crs)
    categories = sorted(points[category].astype(str).unique())
    mapping = {value: f"poi_cat_{index:02d}" for index, value in enumerate(categories)}
    points["poi_category_col"] = points[category].astype(str).map(mapping)
    buffers = ordinary[["seg_id", "geometry"]].copy()
    buffers["geometry"] = buffers.geometry.buffer(float(settings["buffer_m"]))
    joined = gpd.sjoin(points[["poi_category_col", "geometry"]], buffers, how="inner", predicate="within")
    columns = list(mapping.values())
    counts = joined.groupby(["seg_id", "poi_category_col"]).size().unstack(fill_value=0).reindex(index=ordinary["seg_id"], columns=columns, fill_value=0)
    features = pd.DataFrame({"seg_id": ordinary["seg_id"].astype(str)}).merge(counts.reset_index(), on="seg_id", how="left").fillna(0)
    features[columns] = features[columns].astype(int)
    features["poi_total"] = features[columns].sum(axis=1).astype(int)
    lengths = ordinary[["seg_id", "length"]].copy()
    features = features.merge(lengths, on="seg_id", how="left")
    features["poi_density"] = features["poi_total"] / (features["length"].clip(lower=1.0) / 1000.0)
    features["poi_entropy"] = [_entropy(row) for row in features[columns].to_numpy(dtype=float)]
    reverse = {column: value for value, column in mapping.items()}
    features["dominant_poi_type"] = features[columns].idxmax(axis=1).map(reverse)
    features.loc[features["poi_total"] == 0, "dominant_poi_type"] = None
    features = features.drop(columns="length")
    features.to_csv(paths["poi_features"], index=False)
    pd.DataFrame([{"category_col": column, "poi_type": value} for value, column in mapping.items()]).to_csv(paths["poi_category_mapping"], index=False)
    return features


def _add_counts(target: Counter[str], values: pd.Series) -> None:
    target.update(values.dropna().astype(str))


def _build_order_features(config: dict[str, Any], paths: dict[str, Path], ordinary: gpd.GeoDataFrame) -> None:
    settings = config["orders"]
    ids = ordinary["seg_id"].astype(str).tolist()
    id_set = set(ids)
    pickup_counts: Counter[str] = Counter()
    dropoff_counts: Counter[str] = Counter()
    morning: Counter[str] = Counter()
    evening: Counter[str] = Counter()
    night: Counter[str] = Counter()
    weekday: Counter[str] = Counter()
    weekend: Counter[str] = Counter()
    od: Counter[tuple[str, str]] = Counter()
    hourly: Counter[tuple[pd.Timestamp, str, str]] = Counter()
    time_col = settings["time_column"]
    pickup_lon, pickup_lat = settings["pickup_lon_column"], settings["pickup_lat_column"]
    dropoff_lon, dropoff_lat = settings["dropoff_lon_column"], settings["dropoff_lat_column"]
    start, end = pd.Timestamp(settings["start_time"]), pd.Timestamp(settings["end_time"])
    usecols = [pickup_lon, pickup_lat, dropoff_lon, dropoff_lat, time_col]
    for index, chunk in enumerate(pd.read_csv(config["inputs"]["zoning_orders"], usecols=usecols, chunksize=int(settings["chunksize"])), 1):
        chunk[time_col] = pd.to_datetime(chunk[time_col], errors="coerce")
        chunk = chunk.loc[(chunk[time_col] >= start) & (chunk[time_col] < end)].reset_index(drop=True)
        if chunk.empty:
            continue
        pickup = match_points_to_segments_with_distance(chunk, pickup_lon, pickup_lat, ordinary, config["crs"]["geographic"], float(settings["max_match_distance_m"]))["seg_id"]
        dropoff = match_points_to_segments_with_distance(chunk, dropoff_lon, dropoff_lat, ordinary, config["crs"]["geographic"], float(settings["max_match_distance_m"]))["seg_id"]
        _add_counts(pickup_counts, pickup)
        _add_counts(dropoff_counts, dropoff)
        hours = chunk[time_col].dt.hour
        is_weekend = chunk[time_col].dt.dayofweek >= 5
        _add_counts(morning, pickup.loc[hours.isin(settings["morning_peak_hours"])])
        _add_counts(evening, pickup.loc[hours.isin(settings["evening_peak_hours"])])
        _add_counts(night, pickup.loc[hours.isin(settings["night_hours"])])
        _add_counts(weekday, pickup.loc[~is_weekend])
        _add_counts(weekend, pickup.loc[is_weekend])
        valid = pickup.notna() & dropoff.notna() & pickup.astype(str).isin(id_set) & dropoff.astype(str).isin(id_set)
        for timestamp, origin, destination in zip(chunk.loc[valid, time_col], pickup.loc[valid].astype(str), dropoff.loc[valid].astype(str)):
            od[(origin, destination)] += 1
            if timestamp.dayofweek < 5:
                hourly[(timestamp.floor("h"), origin, destination)] += 1
        print(f"preparation orders: chunk {index}", flush=True)
    features = pd.DataFrame({"seg_id": ids})
    for name, counts in (
        ("pickup_count", pickup_counts), ("dropoff_count", dropoff_counts),
        ("morning_peak_pickups", morning), ("evening_peak_pickups", evening),
        ("night_pickups", night), ("weekday_pickups", weekday), ("weekend_pickups", weekend),
    ):
        features[name] = features["seg_id"].map(counts).fillna(0).astype(int)
    features["order_total"] = features["pickup_count"] + features["dropoff_count"]
    features["pickup_dropoff_imbalance"] = features["pickup_count"] - features["dropoff_count"]
    features["weekday_weekend_diff"] = features["weekday_pickups"] - features["weekend_pickups"]
    features.to_csv(paths["order_features"], index=False)
    pd.DataFrame(
        [{"origin_seg_id": a, "destination_seg_id": b, "order_count": count} for (a, b), count in sorted(od.items())],
        columns=["origin_seg_id", "destination_seg_id", "order_count"],
    ).to_csv(paths["order_od_pairs"], index=False)
    pd.DataFrame(
        [{"slot_start": slot, "origin_seg_id": a, "destination_seg_id": b, "order_count": count} for (slot, a, b), count in sorted(hourly.items())],
        columns=["slot_start", "origin_seg_id", "destination_seg_id", "order_count"],
    ).to_csv(paths["hourly_od"], index=False)
    return features


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return 0.0 if denominator == 0 else float(np.dot(left, right) / denominator)


def _build_relation_graph(config: dict[str, Any], paths: dict[str, Path], ordinary: gpd.GeoDataFrame, connectors: gpd.GeoDataFrame, poi_features: pd.DataFrame | None = None, order_features: pd.DataFrame | None = None) -> nx.Graph:
    ordinary = ordinary.copy()
    ordinary["bearing"] = ordinary.geometry.map(compute_bearing)
    records = ordinary.drop(columns="geometry").to_dict("records")
    by_id = {record["seg_id"]: record for record in records}
    incident = build_incident_index(records)
    edge_map: dict[tuple[str, str], dict[str, Any]] = {}
    direct_weight = float(config["graph_weights"]["direct_adjacency_weight"])
    connector_weight = float(config["graph_weights"]["connector_adjacency_weight"])
    continuity_weight = float(config["graph_weights"]["continuity_weight"])
    groups: dict[Any, set[str]] = defaultdict(set)
    for record in records:
        groups[record["u"]].add(record["seg_id"])
        groups[record["v"]].add(record["seg_id"])
    for group in groups.values():
        for left, right in iter_incident_pairs(group):
            edge = ensure_edge_record(edge_map, left, right)
            edge["direct_weight"] += direct_weight
            edge["base_weight"] += direct_weight
            edge["has_direct"] = True
            edge["relation_types"].add("direct")
    for connector in connectors.to_dict("records"):
        for left in sorted(incident.get(connector["u"], set())):
            for right in sorted(incident.get(connector["v"], set())):
                if left == right:
                    continue
                edge = ensure_edge_record(edge_map, left, right)
                edge["connector_weight"] += connector_weight
                edge["base_weight"] += connector_weight
                edge["has_connector"] = True
                edge["relation_types"].add("connector")
                edge["connector_count"] += 1
                edge["connector_ids"].add(connector["seg_id"])
                if connector.get("highway") is not None:
                    edge["connector_highways"].add(connector["highway"])

    poi_source = poi_features if poi_features is not None else pd.read_csv(paths["poi_features"])
    poi = poi_source.set_index("seg_id").reindex(ordinary["seg_id"]).fillna(0)
    poi_columns = [column for column in poi if column.startswith("poi_cat_")]
    poi_counts = poi[poi_columns].to_numpy(dtype=float)
    totals = poi_counts.sum(axis=1)
    composition = np.divide(poi_counts, totals[:, None], out=np.zeros_like(poi_counts), where=totals[:, None] > 0)
    density = np.log1p(poi["poi_density"].to_numpy(dtype=float))
    entropy = poi["poi_entropy"].to_numpy(dtype=float)
    density_scale = max(float(density.max() - density.min()), 1.0)
    poi_index = {seg_id: index for index, seg_id in enumerate(ordinary["seg_id"])}
    order_source = order_features if order_features is not None else pd.read_csv(paths["order_features"])
    order = order_source.set_index("seg_id").reindex(ordinary["seg_id"]).fillna(0)
    vector_columns = ["pickup_count", "dropoff_count", "order_total", "pickup_dropoff_imbalance", "morning_peak_pickups", "evening_peak_pickups", "night_pickups", "weekday_pickups", "weekend_pickups", "weekday_weekend_diff"]
    order_vectors = np.sign(order[vector_columns].to_numpy(dtype=float)) * np.log1p(np.abs(order[vector_columns].to_numpy(dtype=float)))
    order_totals = order["order_total"].to_numpy(dtype=float)
    similarity = config["similarity"]
    continuity = config["continuity"]
    for edge in edge_map.values():
        left, right = edge["seg_id_a"], edge["seg_id_b"]
        a, b = by_id[left], by_id[right]
        same_name = bool(a.get("name") and b.get("name") and a.get("name") == b.get("name"))
        same_osmid = a.get("osmid") is not None and a.get("osmid") == b.get("osmid")
        same_highway = a.get("highway") is not None and a.get("highway") == b.get("highway")
        bearing = angle_diff(a.get("bearing"), b.get("bearing"))
        score = float(continuity["same_name_bonus"]) * same_name + float(continuity["same_osm_id_bonus"]) * same_osmid + float(continuity["same_highway_bonus"]) * same_highway + (1.0 if bearing is not None and bearing <= float(continuity["angle_threshold_deg"]) else 0.0)
        edge.update({"same_name": same_name, "same_osmid": same_osmid, "same_highway": same_highway, "angle_diff": bearing, "continuity_score": score})
        if score > 0:
            edge["continuity_weight"] = continuity_weight * score
            edge["base_weight"] += edge["continuity_weight"]
            edge["has_continuity"] = True
            edge["relation_types"].add("continuity")
        i, j = poi_index[left], poi_index[right]
        poi_similarity = 0.0
        if totals[i] > 0 and totals[j] > 0:
            poi_similarity = float(similarity["poi_composition_weight"]) * _cosine(composition[i], composition[j]) + float(similarity["poi_density_weight"]) * max(0.0, 1.0 - abs(density[i] - density[j]) / density_scale) + float(similarity["poi_entropy_weight"]) * (1.0 - min(1.0, abs(entropy[i] - entropy[j])))
        order_similarity = _cosine(order_vectors[i], order_vectors[j]) if order_totals[i] > 0 and order_totals[j] > 0 else 0.0
        edge["poi_similarity"] = poi_similarity
        edge["order_similarity"] = order_similarity
        edge["poi_weight"] = float(similarity["poi_weight"]) * poi_similarity
        edge["order_weight"] = float(similarity["order_weight"]) * order_similarity
        edge["weight"] = edge["base_weight"] + edge["poi_weight"] + edge["order_weight"]
    relation_edges = pd.DataFrame(serialize_edge_records(edge_map))
    relation_edges.to_csv(paths["relation_edges"], index=False)
    graph = nx.Graph()
    for record in records:
        graph.add_node(record.pop("seg_id"), **record)
    for record in relation_edges.to_dict("records"):
        left, right = record.pop("seg_id_a"), record.pop("seg_id_b")
        graph.add_edge(left, right, **record)
    with paths["graph"].open("wb") as handle:
        pickle.dump(graph, handle)
    return graph


def run(config_path: Path, project_root: Path, output_dir: Path) -> dict[str, Path]:
    config = load_config(config_path, project_root)
    paths = output_paths(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and all(paths[name].is_file() and file_record(paths[name])["sha256"] == record["sha256"] for name, record in manifest.get("outputs", {}).items() if name in paths) and set(manifest.get("outputs", {})) == set(paths):
            print("preparation: reused", flush=True)
            return paths
    edges = _preprocess_roads(config, paths)
    ordinary = edges.loc[edges["segment_role"] == "ordinary"].copy()
    connectors = edges.loc[edges["segment_role"] == "connector"].copy()
    poi_features = _build_poi_features(config, paths, ordinary)
    order_features = _build_order_features(config, paths, ordinary)
    graph = _build_relation_graph(config, paths, ordinary, connectors, poi_features, order_features)
    baseline = run_leiden(graph, {"clustering": config["baseline"]})
    clusters = ordinary.copy()
    clusters["cluster_id"] = clusters["seg_id"].map(baseline)
    make_gpkg_safe(clusters).to_file(paths["baseline_leiden"], driver="GPKG")
    atomic_write_json(manifest_path, {
        "schema_version": 1,
        "config": file_record(config_path),
        "inputs": input_records(config),
        "outputs": {name: file_record(path) for name, path in paths.items()},
    })
    print("preparation: complete", flush=True)
    return paths
