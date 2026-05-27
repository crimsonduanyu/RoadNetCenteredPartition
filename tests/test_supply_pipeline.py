from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
r"""
python -u scripts\reconstruct_driver_chains.py --orders-path data\processed\fifth_ring\order_pipeline\orders_region_assigned.csv.gz --max-gap 60 --carpool-merge-gap-s 0 --slot-duration 15 --output-dir data\processed\fifth_ring\supply

"""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "reconstruct_driver_chains.py"


def load_module():
    spec = importlib.util.spec_from_file_location("supply_pipeline", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_order(
    order_id: int,
    driver_id: int,
    departure_time: str,
    finish_time: str,
    origin_cluster_id: int,
    destination_cluster_id: int,
    service_type: str = "carpool",
) -> dict:
    return {
        "order_id": order_id,
        "driver_id": driver_id,
        "departure_time": pd.Timestamp(departure_time),
        "finish_time": pd.Timestamp(finish_time),
        "origin_cluster_id": origin_cluster_id,
        "destination_cluster_id": destination_cluster_id,
        "service_type": service_type,
    }


def test_carpool_interval_merging_overlapping_non_overlapping_and_single() -> None:
    module = load_module()
    orders = pd.DataFrame(
        [
            make_order(1, 10, "2017-06-01 08:00:00", "2017-06-01 08:20:00", 1, 2),
            make_order(2, 10, "2017-06-01 08:10:00", "2017-06-01 08:30:00", 3, 4),
            make_order(3, 10, "2017-06-01 08:30:00", "2017-06-01 08:45:00", 5, 6),
            make_order(4, 20, "2017-06-01 09:00:00", "2017-06-01 09:15:00", 7, 8),
        ]
    )

    groups = module.resolve_carpool_trip_groups(orders)

    assert len(groups) == 3
    first = groups.loc[groups["driver_id"].eq(10)].iloc[0]
    second = groups.loc[groups["driver_id"].eq(10)].iloc[1]
    single = groups.loc[groups["driver_id"].eq(20)].iloc[0]
    assert first["trip_start"] == pd.Timestamp("2017-06-01 08:00:00")
    assert first["trip_end"] == pd.Timestamp("2017-06-01 08:30:00")
    assert first["origin_cluster_id"] == 1
    assert first["destination_cluster_id"] == 4
    assert first["order_ids"] == [1, 2]
    assert second["order_ids"] == [3]
    assert single["order_ids"] == [4]


def test_chain_splitting_at_gap_boundary_and_date_boundary() -> None:
    module = load_module()
    segments = pd.DataFrame(
        [
            {
                "segment_id": "s1",
                "driver_id": 1,
                "trip_start": pd.Timestamp("2017-06-01 08:00:00"),
                "trip_end": pd.Timestamp("2017-06-01 08:10:00"),
                "origin_cluster_id": 1,
                "destination_cluster_id": 2,
                "service_type": "exclusive",
                "order_ids": [1],
            },
            {
                "segment_id": "s2",
                "driver_id": 1,
                "trip_start": pd.Timestamp("2017-06-01 09:10:00"),
                "trip_end": pd.Timestamp("2017-06-01 09:20:00"),
                "origin_cluster_id": 2,
                "destination_cluster_id": 3,
                "service_type": "exclusive",
                "order_ids": [2],
            },
            {
                "segment_id": "s3",
                "driver_id": 1,
                "trip_start": pd.Timestamp("2017-06-01 10:21:00"),
                "trip_end": pd.Timestamp("2017-06-01 10:30:00"),
                "origin_cluster_id": 3,
                "destination_cluster_id": 4,
                "service_type": "exclusive",
                "order_ids": [3],
            },
            {
                "segment_id": "s4",
                "driver_id": 1,
                "trip_start": pd.Timestamp("2017-06-02 00:05:00"),
                "trip_end": pd.Timestamp("2017-06-02 00:15:00"),
                "origin_cluster_id": 4,
                "destination_cluster_id": 5,
                "service_type": "exclusive",
                "order_ids": [4],
            },
        ]
    )

    chain_segments, chains = module.reconstruct_driver_chains(segments, max_gap_minutes=60)

    assert chain_segments["chain_seq"].tolist() == [1, 1, 2, 1]
    assert chains["segment_ids"].tolist() == [["s1", "s2"], ["s3"], ["s4"]]


def test_slot_overlap_logic_inside_straddling_and_outside() -> None:
    module = load_module()
    trips = pd.DataFrame(
        [
            {
                "driver_id": 1,
                "trip_start": pd.Timestamp("2017-06-01 08:01:00"),
                "trip_end": pd.Timestamp("2017-06-01 08:14:00"),
                "origin_cluster_id": 1,
                "destination_cluster_id": 2,
            },
            {
                "driver_id": 2,
                "trip_start": pd.Timestamp("2017-06-01 08:14:00"),
                "trip_end": pd.Timestamp("2017-06-01 08:16:00"),
                "origin_cluster_id": 1,
                "destination_cluster_id": 3,
            },
            {
                "driver_id": 3,
                "trip_start": pd.Timestamp("2017-06-01 08:15:00"),
                "trip_end": pd.Timestamp("2017-06-01 08:30:00"),
                "origin_cluster_id": 4,
                "destination_cluster_id": 5,
            },
        ]
    )

    in_service, driver_slots = module.compute_in_service_od(trips, slot_duration_min=15, return_driver_slots=True)

    slots_by_driver = {
        driver_id: set(values["slot_start"].dt.strftime("%H:%M:%S"))
        for driver_id, values in driver_slots.groupby("driver_id")
    }
    assert slots_by_driver[1] == {"08:00:00"}
    assert slots_by_driver[2] == {"08:00:00", "08:15:00"}
    assert slots_by_driver[3] == {"08:15:00"}
    assert int(in_service["vehicles_in_service"].sum()) == 4


def test_fleet_lower_bound_deduplicates_driver_idle_and_in_service_same_slot() -> None:
    module = load_module()
    idle_driver_slots = pd.DataFrame(
        [
            {
                "slot_start": pd.Timestamp("2017-06-01 08:00:00"),
                "driver_id": 1,
                "cluster_id": 2,
            }
        ]
    )
    trip_driver_slots = pd.DataFrame(
        [
            {
                "slot_start": pd.Timestamp("2017-06-01 08:00:00"),
                "driver_id": 1,
                "origin_cluster_id": 1,
                "destination_cluster_id": 2,
            },
            {
                "slot_start": pd.Timestamp("2017-06-01 08:00:00"),
                "driver_id": 2,
                "origin_cluster_id": 2,
                "destination_cluster_id": 3,
            },
        ]
    )

    fleet = module.compute_fleet_lower_bound(idle_driver_slots, trip_driver_slots)

    cluster_2 = fleet.loc[fleet["cluster_id"].eq(2)].iloc[0]
    assert int(cluster_2["fleet_lower_bound_cluster"]) == 2
    assert int(cluster_2["global_fleet_lower_bound"]) == 2


def test_daily_pipeline_writes_parts_merges_and_respects_date_range(tmp_path) -> None:
    module = load_module()
    orders_path = tmp_path / "orders.csv.gz"
    output_dir = tmp_path / "supply"
    pd.DataFrame(
        [
            make_order(1, 1, "2017-06-01 23:50:00", "2017-06-02 00:20:00", 1, 2, "exclusive"),
            make_order(2, 1, "2017-06-02 00:30:00", "2017-06-02 00:45:00", 2, 3, "exclusive"),
            make_order(3, 2, "2017-06-02 08:00:00", "2017-06-02 08:20:00", 4, 5, "carpool"),
            make_order(4, 2, "2017-06-02 08:10:00", "2017-06-02 08:30:00", 6, 7, "carpool"),
        ]
    ).to_csv(orders_path, index=False, compression="gzip")

    summary = module.run_pipeline(
        orders_path=orders_path,
        output_dir=output_dir,
        max_gap_minutes=60,
        carpool_merge_gap_s=0,
        slot_duration_min=15,
        io_chunk_rows=2,
        keep_daily_parts=True,
    )

    assert summary["days_processed"] == 2
    assert (output_dir / "trip_segments.csv.gz").exists()
    assert (output_dir / "_daily_parts" / "date=2017-06-01" / "trip_segments.csv.gz").exists()
    assert (output_dir / "_daily_parts" / "date=2017-06-02" / "trip_segments.csv.gz").exists()
    trips = pd.read_csv(output_dir / "trip_segments.csv.gz")
    chains = pd.read_csv(output_dir / "driver_chains.csv.gz")
    in_service = pd.read_csv(output_dir / "supply_in_service_od.csv.gz")
    assert len(trips) == 3
    assert set(chains["date_"].astype(str)) == {"2017-06-01", "2017-06-02"}
    assert "2017-06-02 00:15:00" in set(in_service["slot_start"].astype(str))

    filtered_output = tmp_path / "supply_filtered"
    filtered = module.run_pipeline(
        orders_path=orders_path,
        output_dir=filtered_output,
        max_gap_minutes=60,
        carpool_merge_gap_s=0,
        slot_duration_min=15,
        io_chunk_rows=2,
        start_date="2017-06-02",
        end_date="2017-06-02",
    )

    filtered_chains = pd.read_csv(filtered_output / "driver_chains.csv.gz")
    assert filtered["days_processed"] == 1
    assert set(filtered_chains["date_"].astype(str)) == {"2017-06-02"}
