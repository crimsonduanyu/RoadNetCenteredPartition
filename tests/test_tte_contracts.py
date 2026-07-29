from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.downstream import tte, tte_contracts
from roadnet_partition.pipeline.results import RunContext, StageStatus


def write_contract_fixture(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    clusters = ["10", "30", "isolated"]
    columns = pd.Index(tte.build_od_columns(clusters), name="OD")
    index = pd.date_range("2020-01-01", periods=2, freq="10min")
    raw = pd.DataFrame(np.nan, index=index, columns=columns, dtype="float64")
    raw.loc[index[0], "10->10"] = 5.0
    raw.loc[index[0], "10->30"] = 10.0
    raw.loc[index[0], "30->10"] = 10.0
    count = raw.notna().astype("int32")
    imputed = raw.astype("float32")
    imputed.loc[index[1], "10->30"] = 12.0
    hops = pd.DataFrame(-1, index=index, columns=columns, dtype="int16")
    hops[raw.notna()] = 0
    hops.loc[index[1], "10->30"] = 1
    support = pd.DataFrame(-1, index=index, columns=columns, dtype="int32")
    support.loc[index[1], "10->30"] = 1
    raw.to_parquet(output_dir / "TTE_raw.parquet")
    count.to_parquet(output_dir / "TTE_count.parquet")
    support.to_parquet(output_dir / "TTE_support.parquet")
    hops.to_parquet(output_dir / "TTE_hops.parquet")
    imputed.to_parquet(output_dir / "TTE_imputed.parquet")
    distance = pd.DataFrame(
        [[0.0, 5.0, np.inf], [5.0, 0.0, np.inf], [np.inf, np.inf, 0.0]],
        index=clusters,
        columns=clusters,
        dtype="float64",
    )
    distance.to_parquet(output_dir / "cluster_network_distance.parquet")
    pd.DataFrame({
        "cluster_id": clusters,
        "rep_osmid": [101, 303, 999],
        "dist_to_centroid_m": [1.0, 2.0, 3.0],
    }).to_csv(output_dir / "cluster_representative_nodes.csv", index=False)


def test_tte_contract_validates_axes_masks_and_isolated_cluster(tmp_path: Path) -> None:
    write_contract_fixture(tmp_path)

    result = tte_contracts.validate_tte_outputs(
        tmp_path,
        expected_cluster_ids=["10", "30", "isolated"],
        expected_time_index=pd.date_range("2020-01-01", periods=2, freq="10min"),
        raw_range=(3, 80),
        max_hops=1,
        batch_size=1,
    )

    assert result["shape"] == [2, 9]
    assert result["observed_cells"] == 3
    assert result["inferred_cells"] == 1
    assert result["missing_cells"] == 14
    assert result["diagonal_observed_cells"] == 1
    assert result["distance_unreachable_pairs"] == 4


def test_tte_contract_rejects_support_mask_mismatch(tmp_path: Path) -> None:
    write_contract_fixture(tmp_path)
    support = pd.read_parquet(tmp_path / "TTE_support.parquet")
    support.loc[support.index[1], "10->30"] = -1
    support.to_parquet(tmp_path / "TTE_support.parquet")

    with pytest.raises(ValueError, match="support/inferred"):
        tte_contracts.validate_tte_outputs(tmp_path, batch_size=1)


def test_run_tte_uses_owned_stage_directory_and_resolves_inputs(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(values):
        calls.append(values)
        output = Path(values["stage4_tte"]["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        for name in [
            "cluster_network_distance.parquet", "cluster_representative_nodes.csv",
            "TTE_raw.parquet", "TTE_count.parquet", "TTE_support.parquet",
            "TTE_hops.parquet", "TTE_imputed.parquet",
        ]:
            (output / name).write_text("x", encoding="utf-8")
        return {
            "num_clusters": 3, "num_od_columns": 9, "num_slots": 2,
            "num_observed_cells": 3, "num_inferred_cells": 1,
        }

    monkeypatch.setattr(tte, "run_from_config", fake_run)
    config = ResolvedStageConfig(
        tmp_path / "configs/tte.yaml",
        {
            "stage4_tte": {
                "inputs": {
                    "orders_path": "../inputs/orders.csv.gz",
                    "cluster_index_path": "../inputs/cluster_index.csv",
                },
                "output_dir": "escape",
                "distance": {
                    "graphml_path": "../inputs/graph.graphml",
                    "classified_edges_path": "../inputs/edges.gpkg",
                    "partition_gpkg": "../inputs/partition.gpkg",
                },
                "time": {
                    "freq": "10min",
                    "start_time": "2020-01-01 00:00:00",
                    "end_time": "2020-01-01 00:10:00",
                },
                "trip_time": {"min_minutes": 3, "max_minutes": 80},
            }
        },
        "fingerprint",
    )
    context = RunContext("run", tmp_path / "run", tmp_path).for_stage("tte")

    result = tte.run_tte(config, context)

    assert result.status is StageStatus.COMPLETE
    stage = calls[0]["stage4_tte"]
    assert stage["output_dir"] == str(context.stage_dir)
    assert stage["inputs"]["orders_path"] == str((tmp_path / "configs/../inputs/orders.csv.gz").resolve())
    assert stage["distance"]["partition_gpkg"] == str((tmp_path / "configs/../inputs/partition.gpkg").resolve())
    assert all(path.parent == context.stage_dir for path in result.outputs.values())
    assert result.contract == {}
    assert not (context.stage_dir / "_SUCCESS").exists()
