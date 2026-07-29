from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml


ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SCOPE = ROOT / "data/processed/fifth_ring"


def run(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=True).stdout.strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_lines(lines) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(str(line).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_json(name: str, value: object) -> None:
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def inventory(root: Path) -> list[dict]:
    return [
        {"path": str(path.relative_to(ROOT)), "size": path.stat().st_size}
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def repository_baseline() -> dict:
    status = run("git", "status", "--porcelain=v1")
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=ROOT, check=True, stdout=subprocess.PIPE
    ).stdout
    packages = {}
    for name in [
        "geopandas", "networkx", "numpy", "pandas", "pyarrow", "pyyaml",
        "scikit-learn", "scipy", "shapely", "pytest", "osmnx", "igraph", "leidenalg",
    ]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    tracked = run("git", "ls-files").splitlines()
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git": {
            "commit": run("git", "rev-parse", "HEAD"),
            "branch": run("git", "branch", "--show-current"),
            "dirty": bool(status),
            "changed_files": status.splitlines(),
            "diff_sha256": sha256_bytes(diff),
        },
        "runtime": {
            "platform": platform.platform(),
            "python": sys.version,
            "executable": sys.executable,
            "packages": packages,
        },
        "tracked_files": [
            {"path": item, "size": (ROOT / item).stat().st_size}
            for item in tracked
            if (ROOT / item).is_file()
        ],
        "data_inventory": {
            "raw": inventory(ROOT / "data/raw"),
            "processed_fifth_ring": inventory(SCOPE),
            "intermediate_for_reproduce": inventory(ROOT / "IntermediateDataForReproduce"),
        },
    }


def partition_baseline() -> dict:
    path = ROOT / "IntermediateDataForReproduce/segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.gpkg"
    frame = gpd.read_file(path)
    pairs = sorted(zip(frame["seg_id"].astype(str), frame["cluster_id"].astype(str)))
    groups = sorted(
        (sorted(group["seg_id"].astype(str).tolist()) for _, group in frame.groupby("cluster_id", sort=True)),
        key=lambda members: (len(members), members),
    )
    relation_path = SCOPE / "segment_relation_edges_road_poi_order.csv"
    relation = pd.read_csv(relation_path, usecols=["seg_id_a", "seg_id_b"])
    edges = sorted(
        f"{min(str(a), str(b))}\t{max(str(a), str(b))}"
        for a, b in relation.itertuples(index=False, name=None)
    )
    graph_nodes = sorted(set(relation["seg_id_a"].astype(str)) | set(relation["seg_id_b"].astype(str)))
    return {
        "canonical_path": str(path.relative_to(ROOT)),
        "rows": len(frame),
        "columns": list(frame.columns),
        "segment_count": int(frame["seg_id"].nunique()),
        "segment_ids_sha256": digest_lines(sorted(frame["seg_id"].astype(str))),
        "segment_to_cluster_sha256": digest_lines(f"{a}\t{b}" for a, b in pairs),
        "cluster_grouping_sha256": digest_lines("\t".join(members) for members in groups),
        "cluster_count": int(frame["cluster_id"].nunique()),
        "cluster_sizes": sorted(int(v) for v in frame.groupby("cluster_id").size()),
        "crs": str(frame.crs),
        "geometry": {
            "types": dict(Counter(frame.geometry.geom_type.fillna("None"))),
            "valid": int(frame.geometry.is_valid.sum()),
            "invalid": int((~frame.geometry.is_valid).sum()),
            "empty": int(frame.geometry.is_empty.sum()),
            "missing": int(frame.geometry.isna().sum()),
            "bounds": [float(v) for v in frame.total_bounds],
        },
        "relation_graph": {
            "path": str(relation_path.relative_to(ROOT)),
            "nodes": len(graph_nodes),
            "edges": len(edges),
            "node_ids_sha256": digest_lines(graph_nodes),
            "edge_set_sha256": digest_lines(edges),
        },
    }


def demand_baseline() -> dict:
    root = SCOPE / "order_pipeline"
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    cluster_index = pd.read_csv(root / "cluster_index.csv")
    result = {
        "metadata": metadata,
        "cluster_index": {
            "rows": len(cluster_index),
            "columns": list(cluster_index.columns),
            "cluster_ids": cluster_index["cluster_id"].astype(str).tolist(),
            "mapping_sha256": digest_lines(
                f"{row.cluster_index}\t{row.cluster_id}" for row in cluster_index.itertuples(index=False)
            ),
        },
        "od_tensor": {},
        "cluster_graphs": {},
        "output_inventory": inventory(root),
    }
    tensor_path = root / "od_tensor_10min.npz"
    with np.load(tensor_path, allow_pickle=False) as archive:
        result["od_tensor"]["slot_start"] = {
            "shape": list(archive["slot_start"].shape),
            "first": str(archive["slot_start"][0]),
            "last": str(archive["slot_start"][-1]),
            "sha256": sha256_bytes(np.ascontiguousarray(archive["slot_start"]).tobytes()),
        }
        result["od_tensor"]["cluster_ids"] = archive["cluster_ids"].astype(str).tolist()
        for key in ["Y_exclusive", "Y_carpool", "Y_total"]:
            values = archive[key]
            result["od_tensor"][key] = {
                "shape": list(values.shape),
                "dtype": str(values.dtype),
                "sum": int(values.sum(dtype=np.int64)),
                "nonzero": int(np.count_nonzero(values)),
                "values_sha256": sha256_bytes(np.ascontiguousarray(values).tobytes()),
                "nonzero_mask_sha256": sha256_bytes(np.packbits(values != 0).tobytes()),
            }
    for name in ["road", "poi", "distance"]:
        path = root / f"cluster_graph_{name}_edges.csv"
        frame = pd.read_csv(path)
        edges = sorted(
            f"{min(str(a), str(b))}\t{max(str(a), str(b))}"
            for a, b in frame[["cluster_id_a", "cluster_id_b"]].itertuples(index=False, name=None)
        )
        nodes = sorted(set(frame["cluster_id_a"].astype(str)) | set(frame["cluster_id_b"].astype(str)))
        result["cluster_graphs"][name] = {
            "path": str(path.relative_to(ROOT)),
            "rows": len(frame),
            "columns": list(frame.columns),
            "nodes": len(nodes),
            "node_ids_sha256": digest_lines(nodes),
            "edge_set_sha256": digest_lines(edges),
        }
    return result


def csv_stream_summary(path: Path, keys: list[str], value_columns: list[str], cluster_columns: list[str]) -> dict:
    rows = 0
    duplicate_adjacent = 0
    sorted_keys = True
    previous = None
    first_key = None
    last_key = None
    time_min = None
    time_max = None
    clusters: set[str] = set()
    totals = {column: 0 for column in value_columns}
    dtypes = None
    columns = None
    for chunk in pd.read_csv(path, chunksize=500_000):
        columns = columns or list(chunk.columns)
        dtypes = dtypes or {column: str(dtype) for column, dtype in chunk.dtypes.items()}
        rows += len(chunk)
        key_values = list(chunk[keys].itertuples(index=False, name=None))
        if key_values:
            first_key = first_key or key_values[0]
            last_key = key_values[-1]
            if previous is not None:
                duplicate_adjacent += int(key_values[0] == previous)
                sorted_keys = sorted_keys and previous <= key_values[0]
            if len(key_values) > 1:
                duplicate_adjacent += sum(a == b for a, b in zip(key_values, key_values[1:]))
                sorted_keys = sorted_keys and all(a <= b for a, b in zip(key_values, key_values[1:]))
            previous = key_values[-1]
        times = pd.to_datetime(chunk["slot_start"])
        local_min, local_max = times.min(), times.max()
        time_min = local_min if time_min is None else min(time_min, local_min)
        time_max = local_max if time_max is None else max(time_max, local_max)
        for column in cluster_columns:
            clusters.update(chunk[column].dropna().astype(str).unique())
        for column in value_columns:
            totals[column] += int(chunk[column].sum())
    return {
        "path": str(path.relative_to(ROOT)),
        "columns": columns,
        "dtypes": dtypes,
        "rows": rows,
        "primary_key": keys,
        "keys_monotonic": sorted_keys,
        "adjacent_duplicate_keys": duplicate_adjacent,
        "primary_key_unique": bool(sorted_keys and duplicate_adjacent == 0),
        "first_key": first_key,
        "last_key": last_key,
        "time_min": str(time_min),
        "time_max": str(time_max),
        "cluster_ids": sorted(clusters, key=lambda value: (len(value), value)),
        "totals": totals,
    }


def supply_baseline() -> dict:
    root = SCOPE / "supply"
    return {
        "run_summary": json.loads((root / "run_summary.json").read_text(encoding="utf-8")),
        "outputs": {
            "supply_inservice_od.csv.gz": csv_stream_summary(
                root / "supply_inservice_od.csv.gz",
                ["slot_start", "origin_cluster_id", "destination_cluster_id"],
                ["vehicles_in_service"],
                ["origin_cluster_id", "destination_cluster_id"],
            ),
            "supply_available_floor.csv.gz": csv_stream_summary(
                root / "supply_available_floor.csv.gz",
                ["slot_start", "cluster_id"],
                ["available_vehicles"],
                ["cluster_id"],
            ),
            "supply_fleet_lower_bound.csv.gz": csv_stream_summary(
                root / "supply_fleet_lower_bound.csv.gz",
                ["slot_start", "cluster_id"],
                ["fleet_lower_bound_cluster", "global_fleet_lower_bound"],
                ["cluster_id"],
            ),
        },
    }


def parquet_matrix_summary(path: Path, kind: str) -> dict:
    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    value_names = [name for name in names if name != "__index_level_0__"]
    digest = hashlib.sha256()
    mask_digest = hashlib.sha256()
    index_digest = hashlib.sha256()
    null_count = 0
    finite_count = 0
    value_sum = 0.0
    value_min = None
    value_max = None
    distribution = Counter()
    diagonal_positions = [i for i, name in enumerate(value_names) if name.split("->", 1)[0] == name.split("->", 1)[1]]
    diagonal_count = 0
    diagonal_null = 0
    diagonal_sum = 0.0
    diagonal_min = None
    diagonal_max = None
    index_first = None
    index_last = None
    rows = 0
    for batch in parquet.iter_batches(batch_size=64):
        frame = batch.to_pandas()
        if "__index_level_0__" in frame.columns:
            index = pd.to_datetime(frame.pop("__index_level_0__"))
        else:
            index = pd.Series(pd.to_datetime(frame.index))
        index_first = index_first or index.iloc[0]
        index_last = index.iloc[-1]
        index_digest.update("\n".join(index.astype(str)).encode("utf-8"))
        index_digest.update(b"\n")
        values = frame.to_numpy(copy=False)
        rows += values.shape[0]
        if kind in {"raw", "imputed"}:
            numeric = values.astype(np.float64, copy=False)
            missing = np.isnan(numeric)
            finite = np.isfinite(numeric)
            mask_digest.update(np.packbits(missing).tobytes())
            canonical = np.nan_to_num(numeric, nan=np.nan, posinf=np.inf, neginf=-np.inf)
            digest.update(np.ascontiguousarray(canonical).tobytes())
            null_count += int(missing.sum())
            finite_count += int(finite.sum())
            if finite.any():
                selected = numeric[finite]
                value_sum += float(selected.sum(dtype=np.float64))
                local_min, local_max = float(selected.min()), float(selected.max())
                value_min = local_min if value_min is None else min(value_min, local_min)
                value_max = local_max if value_max is None else max(value_max, local_max)
            diagonal = numeric[:, diagonal_positions]
            diagonal_missing = np.isnan(diagonal)
            diagonal_finite = np.isfinite(diagonal)
            diagonal_count += diagonal.size
            diagonal_null += int(diagonal_missing.sum())
            if diagonal_finite.any():
                selected = diagonal[diagonal_finite]
                diagonal_sum += float(selected.sum(dtype=np.float64))
                local_min, local_max = float(selected.min()), float(selected.max())
                diagonal_min = local_min if diagonal_min is None else min(diagonal_min, local_min)
                diagonal_max = local_max if diagonal_max is None else max(diagonal_max, local_max)
        else:
            numeric = values.astype(np.int64, copy=False)
            digest.update(np.ascontiguousarray(numeric).tobytes())
            if kind == "support":
                mask_digest.update(np.packbits(numeric >= 1).tobytes())
            else:
                mask_digest.update(np.packbits(numeric >= 0).tobytes())
            unique, counts = np.unique(numeric, return_counts=True)
            distribution.update({str(int(k)): int(v) for k, v in zip(unique, counts)})
    result = {
        "path": str(path.relative_to(ROOT)),
        "shape": [rows, len(value_names)],
        "dtype": str(parquet.schema_arrow.field(value_names[0]).type),
        "column_count": len(value_names),
        "columns_sha256": digest_lines(value_names),
        "cluster_order": sorted({name.split("->", 1)[0] for name in value_names}, key=lambda v: value_names.index(f"{v}->{v}")),
        "index": {"first": str(index_first), "last": str(index_last), "sha256": index_digest.hexdigest()},
        "values_sha256": digest.hexdigest(),
        "mask_sha256": mask_digest.hexdigest(),
    }
    if kind in {"raw", "imputed"}:
        result.update({
            "null_count": null_count,
            "non_null_count": rows * len(value_names) - null_count,
            "finite_count": finite_count,
            "finite_sum": value_sum,
            "finite_min": value_min,
            "finite_max": value_max,
            "diagonal": {
                "cells": diagonal_count,
                "null_count": diagonal_null,
                "finite_count": diagonal_count - diagonal_null,
                "finite_sum": diagonal_sum,
                "finite_min": diagonal_min,
                "finite_max": diagonal_max,
            },
        })
    else:
        result["distribution"] = dict(sorted(distribution.items(), key=lambda item: int(item[0])))
    return result


def tte_baseline() -> dict:
    root = SCOPE / "tte"
    result = {}
    for filename, kind in [
        ("TTE_raw.parquet", "raw"),
        ("TTE_imputed.parquet", "imputed"),
        ("TTE_hops.parquet", "hops"),
        ("TTE_support.parquet", "support"),
        ("TTE_count.parquet", "count"),
    ]:
        print(f"summarizing {filename}", flush=True)
        result[filename] = parquet_matrix_summary(root / filename, kind)
    result["output_inventory"] = inventory(root)
    return result


def references_for(term: str) -> list[str]:
    completed = subprocess.run(
        ["rg", "-l", term, "README.md", "CLAUDE.md", "docs", "tests", "src", "scripts", "tmp.ipynb"],
        cwd=ROOT, text=True, capture_output=True
    )
    return sorted(line for line in completed.stdout.splitlines() if line)


def supply_output_audit() -> dict:
    root = SCOPE / "supply"
    names = {
        "supply_inservice_od.csv.gz": "current formal",
        "supply_in_service_od.csv.gz": "historical legacy",
        "supply_available_floor.csv.gz": "current formal",
        "supply_available_by_cluster.csv.gz": "historical legacy",
        "supply_fleet_lower_bound.csv.gz": "current formal",
        "trip_segments.csv.gz": "historical intermediate",
        "driver_chains.csv.gz": "historical intermediate",
        "idle_windows.csv.gz": "historical intermediate",
        "run_summary.partial.json": "historical partial status",
    }
    return {
        name: {
            "classification": classification,
            "exists": (root / name).exists(),
            "size": (root / name).stat().st_size if (root / name).exists() else None,
            "mtime_utc": datetime.fromtimestamp((root / name).stat().st_mtime, timezone.utc).isoformat()
            if (root / name).exists() else None,
            "repository_references": references_for(name),
            "external_consumers": "unknown; requires manual confirmation by user",
        }
        for name, classification in names.items()
    }


def analysis_script_audit() -> dict:
    classifications = {
        "gap_distribution.py": ("retain and migrate", "records the production Supply gap distribution"),
        "gap_crossday_crosstab.py": ("merge then migrate", "related cross-day Supply diagnostic"),
        "tte_distribution_report.py": ("retain and migrate", "generates currently checked-in TTE reports"),
        "step_finalize_full.py": ("archive", "historical final Supply contract/memory verification"),
        "step_tau_idle_verify.py": ("archive", "historical tau-idle correctness verification"),
        "step_midnight_verify.py": ("archive", "historical midnight correctness verification"),
        "step_midnight_full.py": ("archive", "historical full-run verification"),
        "step1_logic_verify.py": ("archive", "historical Supply logic comparison"),
        "step1_fullmem_run.py": ("archive", "historical full-memory run"),
        "step1_compare_daily_vs_fullmem.py": ("archive", "historical daily/full-memory comparison"),
        "step_chunk_verify.py": ("archive", "historical chunking verification"),
        "step_chunk_full.py": ("archive", "historical chunked full run"),
        "supply_mem_probe.py": ("archive", "historical Supply memory probe"),
        "supply_block_mem_probe.py": ("archive", "historical block memory probe"),
    }
    result = {}
    for name, (classification, reason) in classifications.items():
        path = ROOT / "scripts/analysis" / name
        refs = subprocess.run(
            ["rg", "-l", name, "README.md", "CLAUDE.md", "docs", "tests", "src", "scripts"],
            cwd=ROOT, text=True, capture_output=True
        ).stdout.splitlines()
        refs = sorted(ref for ref in refs if ref != f"scripts/analysis/{name}")
        result[name] = {
            "classification": classification,
            "reason": reason,
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else None,
            "references_outside_self": refs,
            "deletion_condition": "Phase 10 or later, after replacement evidence is accepted" if classification == "archive" else None,
        }
    return result


def write_file_hashes() -> None:
    paths = [
        ROOT / "config.yaml",
        ROOT / "environment.yml",
        *sorted((ROOT / "src").rglob("*.py")),
        ROOT / "IntermediateDataForReproduce/segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.csv",
        ROOT / "IntermediateDataForReproduce/segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.gpkg",
        SCOPE / "segment_relation_edges_road_poi_order.csv",
        SCOPE / "order_pipeline/metadata.json",
        SCOPE / "order_pipeline/cluster_index.csv",
        SCOPE / "order_pipeline/od_tensor_10min.npz",
        SCOPE / "order_pipeline/orders_region_assigned.csv.gz",
        SCOPE / "supply/run_summary.json",
        SCOPE / "supply/supply_inservice_od.csv.gz",
        SCOPE / "supply/supply_available_floor.csv.gz",
        SCOPE / "supply/supply_fleet_lower_bound.csv.gz",
        *sorted((SCOPE / "tte").glob("*.parquet")),
        SCOPE / "tte/cluster_representative_nodes.csv",
    ]
    with (OUT / "file-hashes.sha256").open("w", encoding="utf-8") as handle:
        for path in paths:
            print(f"hashing {path.relative_to(ROOT)}", flush=True)
            handle.write(f"{sha256_file(path)}  {path.relative_to(ROOT)}\n")


def main() -> None:
    print("repository", flush=True)
    write_json("repository.json", repository_baseline())
    print("partition", flush=True)
    semantic = {"partition": partition_baseline()}
    print("demand", flush=True)
    semantic["demand"] = demand_baseline()
    print("supply", flush=True)
    semantic["supply"] = supply_baseline()
    print("tte", flush=True)
    semantic["tte"] = tte_baseline()
    write_json("semantic-baseline.json", semantic)
    write_json("supply-output-audit.json", supply_output_audit())
    write_json("analysis-script-audit.json", analysis_script_audit())
    write_file_hashes()
    print("complete", flush=True)


if __name__ == "__main__":
    main()
