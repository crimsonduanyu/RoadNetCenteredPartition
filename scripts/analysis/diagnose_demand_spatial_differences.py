#!/usr/bin/env python3
"""Diagnose historical/Linux Demand spatial assignment differences read-only."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import shapely


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORMAL = PROJECT_ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DEFAULT_LINUX = PROJECT_ROOT / "outputs/validation/phase5a-demand/phase5a-full-v1/demand/orders_region_assigned.csv.gz"
DEFAULT_ORDERS = PROJECT_ROOT / "data/raw/beijing_orders_2017-06_2017-08.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs/validation/phase5a-demand/phase5a-spatial-diagnostics-v1.json"

ASSIGNED_COLUMNS = [
    "stage_id",
    "source_row",
    "pickup_seg_id",
    "dropoff_seg_id",
    "origin_cluster_id",
    "destination_cluster_id",
]


def distance_classes(
    old_distance: np.ndarray,
    new_distance: np.ndarray,
    absolute_tolerance_m: float,
    relative_tolerance: float,
) -> dict[str, np.ndarray]:
    threshold = absolute_tolerance_m + relative_tolerance * np.maximum(
        1.0, np.maximum(np.abs(old_distance), np.abs(new_distance))
    )
    delta = old_distance - new_distance
    exact = old_distance == new_distance
    approximate = np.abs(delta) <= threshold
    return {
        "exact": exact,
        "approximate": approximate,
        "old_clearly_closer": delta < -threshold,
        "new_clearly_closer": delta > threshold,
        "threshold": threshold,
        "delta": delta,
    }


def read_assignment_differences(formal_path: Path, linux_path: Path, chunksize: int) -> tuple[pd.DataFrame, dict[str, int]]:
    dtype = {column: "string" for column in ASSIGNED_COLUMNS if column not in {"stage_id", "source_row"}}
    formal_chunks = pd.read_csv(formal_path, usecols=ASSIGNED_COLUMNS, dtype=dtype, chunksize=chunksize)
    linux_chunks = pd.read_csv(linux_path, usecols=ASSIGNED_COLUMNS, dtype=dtype, chunksize=chunksize)
    records: list[pd.DataFrame] = []
    total_rows = 0
    origin_differences = 0
    destination_differences = 0
    differing_stage_ids: set[int] = set()

    for formal, linux in zip(formal_chunks, linux_chunks, strict=True):
        if len(formal) != len(linux):
            raise ValueError("Historical and Linux assigned-order chunk lengths differ")
        if not formal[["stage_id", "source_row"]].equals(linux[["stage_id", "source_row"]]):
            raise ValueError("Historical and Linux assigned-order identities are not aligned")
        total_rows += len(formal)

        for side, old_seg, new_seg, old_cluster, new_cluster in (
            ("origin", "pickup_seg_id", "pickup_seg_id", "origin_cluster_id", "origin_cluster_id"),
            ("destination", "dropoff_seg_id", "dropoff_seg_id", "destination_cluster_id", "destination_cluster_id"),
        ):
            mask = formal[old_cluster] != linux[new_cluster]
            count = int(mask.sum())
            if side == "origin":
                origin_differences += count
            else:
                destination_differences += count
            if not count:
                continue
            differing_stage_ids.update(formal.loc[mask, "stage_id"].astype(int))
            records.append(
                pd.DataFrame(
                    {
                        "side": side,
                        "stage_id": formal.loc[mask, "stage_id"].astype("int64").to_numpy(),
                        "source_row": formal.loc[mask, "source_row"].astype("int64").to_numpy(),
                        "old_seg_id": formal.loc[mask, old_seg].astype(str).to_numpy(),
                        "new_seg_id": linux.loc[mask, new_seg].astype(str).to_numpy(),
                        "old_cluster_id": formal.loc[mask, old_cluster].astype(str).to_numpy(),
                        "new_cluster_id": linux.loc[mask, new_cluster].astype(str).to_numpy(),
                    }
                )
            )

    differences = pd.concat(records, ignore_index=True) if records else pd.DataFrame()
    return differences, {
        "assigned_rows": total_rows,
        "origin_cluster_differences": origin_differences,
        "destination_cluster_differences": destination_differences,
        "distinct_orders_with_cluster_difference": len(differing_stage_ids),
    }


def read_selected_coordinates(order_path: Path, source_rows: Iterable[int], chunksize: int) -> pd.DataFrame:
    targets = np.array(sorted(set(int(value) for value in source_rows)), dtype=np.int64)
    columns = ["starting_lng", "starting_lat", "dest_lng", "dest_lat"]
    selected: list[pd.DataFrame] = []
    for chunk in pd.read_csv(order_path, usecols=columns, chunksize=chunksize):
        start = int(chunk.index[0])
        stop = int(chunk.index[-1]) + 1
        left = np.searchsorted(targets, start, side="left")
        right = np.searchsorted(targets, stop, side="left")
        if left != right:
            selected.append(chunk.loc[targets[left:right]].copy())
    if not selected:
        raise ValueError("No source coordinates were found for differing assignments")
    coordinates = pd.concat(selected).sort_index()
    if len(coordinates) != len(targets):
        missing = len(targets) - len(coordinates)
        raise ValueError(f"Missing coordinates for {missing} differing source rows")
    return coordinates


def near_candidate_counts(
    points: gpd.GeoSeries,
    segments: gpd.GeoDataFrame,
    radii: np.ndarray,
    batch_size: int,
) -> np.ndarray:
    counts = np.zeros(len(points), dtype=np.int32)
    spatial_index = segments.sindex
    for start in range(0, len(points), batch_size):
        stop = min(start + batch_size, len(points))
        pairs = spatial_index.query(
            points.iloc[start:stop],
            predicate="dwithin",
            distance=radii[start:stop],
        )
        counts[start:stop] = np.bincount(pairs[0], minlength=stop - start)
    return counts


def count_true(values: np.ndarray) -> int:
    return int(np.count_nonzero(values))


def representative_samples(frame: pd.DataFrame, limit_per_class: int = 2) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    categories = (
        "exact",
        "approximate_not_exact",
        "old_clearly_closer",
        "new_clearly_closer",
        "overlapping_candidates",
        "intersection_neighborhood",
        "endpoint_projection",
        "multiple_near_candidates",
    )
    for side in ("origin", "destination"):
        for category in categories:
            subset = frame.loc[(frame["side"] == side) & frame[category]].head(limit_per_class)
            for _, row in subset.iterrows():
                samples.append(
                    {
                        "sample": f"sample-{len(samples) + 1:03d}",
                        "side": side,
                        "category": category,
                        "old_cluster_id": row["old_cluster_id"],
                        "new_cluster_id": row["new_cluster_id"],
                        "old_distance_m": round(float(row["old_distance_m"]), 9),
                        "new_distance_m": round(float(row["new_distance_m"]), 9),
                        "absolute_distance_delta_m": round(abs(float(row["distance_delta_m"])), 9),
                        "candidate_intersects": bool(row["candidate_intersects"]),
                        "candidate_overlap": bool(row["overlapping_candidates"]),
                        "endpoint_projection": bool(row["endpoint_projection"]),
                        "near_candidate_count": int(row["near_candidate_count"]),
                    }
                )
    return samples


def environment_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "python_build": " ".join(platform.python_build()),
        "operating_system": platform.platform(),
        "geopandas": gpd.__version__,
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "pyproj": pyproj.__version__,
        "proj": pyproj.proj_version_str,
    }


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    differences, counts = read_assignment_differences(args.formal, args.linux, args.chunk_size)
    if differences.empty:
        raise ValueError("No origin or destination cluster differences were found")

    coordinates = read_selected_coordinates(args.orders, differences["source_row"], args.chunk_size)
    pickup = differences["side"].eq("origin")
    differences["lon"] = np.where(
        pickup,
        coordinates.loc[differences["source_row"], "starting_lng"].to_numpy(),
        coordinates.loc[differences["source_row"], "dest_lng"].to_numpy(),
    )
    differences["lat"] = np.where(
        pickup,
        coordinates.loc[differences["source_row"], "starting_lat"].to_numpy(),
        coordinates.loc[differences["source_row"], "dest_lat"].to_numpy(),
    )

    segments = gpd.read_file(args.partition)[["seg_id", "cluster_id", "geometry"]].copy()
    segments["seg_id"] = segments["seg_id"].astype(str)
    segments["cluster_id"] = segments["cluster_id"].astype(str)
    if segments["seg_id"].duplicated().any():
        raise ValueError("Partition segment IDs are not unique")
    segments = segments.set_index("seg_id", drop=False)
    old_candidates = segments.reindex(differences["old_seg_id"])
    new_candidates = segments.reindex(differences["new_seg_id"])
    old_geometry = old_candidates["geometry"]
    new_geometry = new_candidates["geometry"]
    if old_geometry.isna().any() or new_geometry.isna().any():
        raise ValueError("A differing segment ID is absent from the partition")
    if not np.array_equal(old_candidates["cluster_id"].to_numpy(), differences["old_cluster_id"].to_numpy()):
        raise ValueError("A historical segment does not map to its recorded cluster")
    if not np.array_equal(new_candidates["cluster_id"].to_numpy(), differences["new_cluster_id"].to_numpy()):
        raise ValueError("A Linux segment does not map to its recorded cluster")

    points = gpd.GeoSeries(
        gpd.points_from_xy(differences.pop("lon"), differences.pop("lat")),
        crs=args.source_crs,
    ).to_crs(segments.crs)
    point_array = points.array
    old_array = gpd.GeoSeries(old_geometry.array, crs=segments.crs).array
    new_array = gpd.GeoSeries(new_geometry.array, crs=segments.crs).array
    old_distance = shapely.distance(point_array, old_array)
    new_distance = shapely.distance(point_array, new_array)
    classes = distance_classes(old_distance, new_distance, args.absolute_tolerance_m, args.relative_tolerance)

    differences["old_distance_m"] = old_distance
    differences["new_distance_m"] = new_distance
    differences["distance_delta_m"] = old_distance - new_distance
    differences["exact"] = classes["exact"]
    differences["approximate"] = classes["approximate"]
    differences["approximate_not_exact"] = classes["approximate"] & ~classes["exact"]
    differences["old_clearly_closer"] = classes["old_clearly_closer"]
    differences["new_clearly_closer"] = classes["new_clearly_closer"]

    candidate_intersects = shapely.intersects(old_array, new_array)
    intersections = shapely.intersection(old_array, new_array)
    overlapping = shapely.length(intersections) > args.absolute_tolerance_m
    best_distance = np.minimum(old_distance, new_distance)
    intersection_neighborhood = candidate_intersects & (
        shapely.distance(point_array, intersections) <= best_distance + classes["threshold"]
    )
    old_location = shapely.line_locate_point(old_array, point_array)
    new_location = shapely.line_locate_point(new_array, point_array)
    old_length = shapely.length(old_array)
    new_length = shapely.length(new_array)
    endpoint_projection = (
        (np.minimum(old_location, old_length - old_location) <= args.absolute_tolerance_m)
        | (np.minimum(new_location, new_length - new_location) <= args.absolute_tolerance_m)
    )
    near_counts = near_candidate_counts(
        points,
        segments,
        best_distance + classes["threshold"],
        args.spatial_batch_size,
    )

    differences["candidate_intersects"] = candidate_intersects
    differences["overlapping_candidates"] = overlapping
    differences["intersection_neighborhood"] = intersection_neighborhood
    differences["endpoint_projection"] = endpoint_projection
    differences["near_candidate_count"] = near_counts
    differences["multiple_near_candidates"] = near_counts > 1

    pair_counts = (
        differences.groupby(["side", "old_cluster_id", "new_cluster_id"], observed=True)
        .size()
        .sort_values(ascending=False)
        .head(args.top_cluster_pairs)
    )
    diagnostics = {
        "schema_version": 1,
        "baseline_roles": {
            "historical_formal": "Windows-generated formal downstream source; read-only and not regenerated",
            "linux_refactor": "Linux same-environment refactor regression baseline; validation-only and not publishable",
        },
        "tolerances": {
            "absolute_m": args.absolute_tolerance_m,
            "relative": args.relative_tolerance,
            "clearly_closer_rule": "absolute distance difference exceeds absolute_m + relative * max(1, distances)",
        },
        "counts": {
            **counts,
            "origin_difference_ratio": counts["origin_cluster_differences"] / counts["assigned_rows"],
            "destination_difference_ratio": counts["destination_cluster_differences"] / counts["assigned_rows"],
            "diagnostic_records": len(differences),
            "distance_exact": count_true(differences["exact"].to_numpy()),
            "distance_approximate_including_exact": count_true(differences["approximate"].to_numpy()),
            "distance_approximate_not_exact": count_true(differences["approximate_not_exact"].to_numpy()),
            "old_candidate_clearly_closer": count_true(differences["old_clearly_closer"].to_numpy()),
            "new_candidate_clearly_closer": count_true(differences["new_clearly_closer"].to_numpy()),
            "candidate_geometries_intersect": count_true(candidate_intersects),
            "candidate_geometries_overlap": count_true(overlapping),
            "nearest_projection_at_candidate_endpoint": count_true(endpoint_projection),
            "point_in_candidate_intersection_neighborhood": count_true(intersection_neighborhood),
            "multiple_segments_within_nearest_tolerance": count_true(near_counts > 1),
        },
        "near_candidate_count_distribution": {
            "0": int(np.count_nonzero(near_counts == 0)),
            "1": int(np.count_nonzero(near_counts == 1)),
            "2": int(np.count_nonzero(near_counts == 2)),
            "3-4": int(np.count_nonzero((near_counts >= 3) & (near_counts <= 4))),
            "5-9": int(np.count_nonzero((near_counts >= 5) & (near_counts <= 9))),
            "10+": int(np.count_nonzero(near_counts >= 10)),
        },
        "distance_delta_m": {
            "maximum_absolute": float(np.max(np.abs(old_distance - new_distance))),
            "p50_absolute": float(np.quantile(np.abs(old_distance - new_distance), 0.50)),
            "p95_absolute": float(np.quantile(np.abs(old_distance - new_distance), 0.95)),
            "p99_absolute": float(np.quantile(np.abs(old_distance - new_distance), 0.99)),
        },
        "top_cluster_pairs": [
            {
                "side": side,
                "old_cluster_id": old_cluster,
                "new_cluster_id": new_cluster,
                "count": int(count),
            }
            for (side, old_cluster, new_cluster), count in pair_counts.items()
        ],
        "anonymous_samples": representative_samples(differences),
        "environment": {
            "historical": {
                "operating_system": "Windows (evidenced by stored source paths)",
                "python": "not recorded",
                "geopandas": "not recorded",
                "shapely": "not recorded",
                "geos": "not recorded",
                "pyproj": "not recorded",
                "proj": "not recorded",
            },
            "linux_validation": environment_versions(),
        },
        "privacy": "No coordinates, order/driver IDs, source rows, or segment IDs are included.",
    }
    return diagnostics


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal", type=Path, default=DEFAULT_FORMAL)
    parser.add_argument("--linux", type=Path, default=DEFAULT_LINUX)
    parser.add_argument("--orders", type=Path, default=DEFAULT_ORDERS)
    parser.add_argument("--partition", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-crs", default="EPSG:4326")
    parser.add_argument("--chunk-size", type=int, default=500_000)
    parser.add_argument("--spatial-batch-size", type=int, default=20_000)
    parser.add_argument("--absolute-tolerance-m", type=float, default=1e-6)
    parser.add_argument("--relative-tolerance", type=float, default=1e-12)
    parser.add_argument("--top-cluster-pairs", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    result = diagnose(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
