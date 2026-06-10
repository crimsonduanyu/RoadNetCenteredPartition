from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
r"""
python -u scripts\reconstruct_driver_chains.py --orders-path data\processed\fifth_ring\order_pipeline\orders_region_assigned.csv.gz --max-gap 60 --carpool-merge-gap-s 0 --slot-duration 15 --output-dir data\processed\fifth_ring\supply

"""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "src" / "lib" / "supply.py"


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


def test_chain_splitting_by_gap_only_and_crosses_midnight() -> None:
    """Chains split only by the gap threshold (midnight break removed):
    - s1->s2 gap exactly 60min -> same chain (boundary inclusive).
    - s2->s3 gap 61min -> new chain.
    - s4->s5 gap 11min ACROSS MIDNIGHT -> same chain (would previously split on date).
    chain_id no longer encodes a date; chain_seq is a continuous per-driver count.
    """
    module = load_module()

    def seg(sid, start, end, o, d):
        return {"segment_id": sid, "driver_id": 1, "trip_start": pd.Timestamp(start),
                "trip_end": pd.Timestamp(end), "origin_cluster_id": o,
                "destination_cluster_id": d, "service_type": "exclusive", "order_ids": [int(sid[1:])]}

    segments = pd.DataFrame([
        seg("s1", "2017-06-01 08:00:00", "2017-06-01 08:10:00", 1, 2),
        seg("s2", "2017-06-01 09:10:00", "2017-06-01 09:20:00", 2, 3),   # gap 60 -> same
        seg("s3", "2017-06-01 10:21:00", "2017-06-01 10:30:00", 3, 4),   # gap 61 -> new
        seg("s4", "2017-06-01 23:50:00", "2017-06-01 23:59:00", 4, 5),   # gap >60 -> new
        seg("s5", "2017-06-02 00:10:00", "2017-06-02 00:20:00", 5, 6),   # gap 11 across midnight -> same
    ])

    chain_segments, chains = module.reconstruct_driver_chains(segments, max_gap_minutes=60)

    assert chain_segments["chain_seq"].tolist() == [1, 1, 2, 3, 3]
    assert chains["segment_ids"].tolist() == [["s1", "s2"], ["s3"], ["s4", "s5"]]
    # chain_id is driver_id + per-driver sequence (no date), and the cross-midnight
    # pair shares one chain_id.
    assert chain_segments["chain_id"].tolist() == ["1_1", "1_1", "1_2", "1_3", "1_3"]
    assert "date_" not in chains.columns


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


def test_in_service_slots_span_midnight() -> None:
    """Fix-1 removed: a trip crossing midnight emits every slot it overlaps,
    including the cross-midnight tail slots (no natural-day clipping)."""
    module = load_module()
    trips = pd.DataFrame(
        [
            {
                "driver_id": 1,
                "trip_start": pd.Timestamp("2017-06-01 23:50:00"),
                "trip_end": pd.Timestamp("2017-06-02 00:30:00"),
                "origin_cluster_id": 1,
                "destination_cluster_id": 2,
            }
        ]
    )

    _, driver_slots = module.compute_in_service_od(trips, slot_duration_min=15, return_driver_slots=True)

    slots = set(driver_slots["slot_start"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    assert slots == {"2017-06-01 23:45:00", "2017-06-02 00:00:00", "2017-06-02 00:15:00"}


def test_tau_idle_decoupled_from_chain_formation() -> None:
    """A 45-min inter-trip gap: chain formation (max_gap=60) keeps both trips in one
    chain regardless of tau_idle; the idle window is kept at tau_idle=60 but dropped
    at tau_idle=30. Confirms the idle cap is decoupled from chain formation."""
    module = load_module()
    segments = pd.DataFrame(
        [
            {"segment_id": "a", "driver_id": 1, "trip_start": pd.Timestamp("2017-06-01 08:00:00"),
             "trip_end": pd.Timestamp("2017-06-01 08:20:00"), "origin_cluster_id": 1, "destination_cluster_id": 2},
            {"segment_id": "b", "driver_id": 1, "trip_start": pd.Timestamp("2017-06-01 09:05:00"),
             "trip_end": pd.Timestamp("2017-06-01 09:20:00"), "origin_cluster_id": 2, "destination_cluster_id": 3},
        ]
    )
    chain_segments, chains = module.reconstruct_driver_chains(segments, max_gap_minutes=60)
    assert chains["chain_id"].nunique() == 1  # 45min < 60min -> one chain, both thresholds

    idle60 = module.extract_idle_windows(chain_segments, tau_idle_minutes=60)
    idle30 = module.extract_idle_windows(chain_segments, tau_idle_minutes=30)
    assert len(idle60) == 1   # 45min gap kept under tau=60
    assert len(idle30) == 0   # 45min gap excluded under tau=30 (tighter)


def test_driver_chunked_equals_whole_frame(tmp_path) -> None:
    """The driver-chunked skeleton must be lossless: summing per-block nunique over
    disjoint driver blocks equals the whole-frame compute, cell for cell."""
    module = load_module()
    orders = pd.DataFrame(
        [
            make_order(1, 11, "2017-06-01 08:00:00", "2017-06-01 08:20:00", 1, 2, "exclusive"),
            make_order(2, 11, "2017-06-01 08:25:00", "2017-06-01 08:40:00", 2, 3, "exclusive"),
            make_order(3, 22, "2017-06-01 08:05:00", "2017-06-01 08:35:00", 1, 4, "carpool"),
            make_order(4, 22, "2017-06-01 08:15:00", "2017-06-01 08:50:00", 5, 6, "carpool"),
            make_order(5, 33, "2017-06-01 23:50:00", "2017-06-02 00:30:00", 2, 1, "exclusive"),
            make_order(6, 44, "2017-06-01 09:00:00", "2017-06-01 09:20:00", 3, 2, "exclusive"),
            make_order(7, 55, "2017-06-01 09:10:00", "2017-06-01 09:18:00", 4, 4, "carpool"),
            make_order(8, 66, "2017-06-01 10:00:00", "2017-06-01 10:40:00", 1, 3, "exclusive"),
        ]
    )
    orders_path = tmp_path / "orders.csv.gz"
    orders.to_csv(orders_path, index=False, compression="gzip")
    out_dir = tmp_path / "chunked"

    module.run_chunked_pipeline(
        orders_path=orders_path, output_dir=out_dir,
        max_gap_minutes=60, carpool_merge_gap_s=0, slot_duration_min=15, n_blocks=8,
    )
    whole = module.process_orders_frame(orders, 60, 0, 15)

    def norm(df, keys):
        d = df.copy()
        d["slot_start"] = pd.to_datetime(d["slot_start"])
        return d.sort_values(keys).reset_index(drop=True)

    c_in = norm(pd.read_csv(out_dir / "supply_inservice_od.csv.gz"),
                ["slot_start", "origin_cluster_id", "destination_cluster_id"])
    w_in = norm(whole["supply_inservice_od"], ["slot_start", "origin_cluster_id", "destination_cluster_id"])
    merged = c_in.merge(w_in, on=["slot_start", "origin_cluster_id", "destination_cluster_id"],
                        how="outer", suffixes=("_c", "_w"))
    assert (merged["vehicles_in_service_c"].fillna(0) == merged["vehicles_in_service_w"].fillna(0)).all()

    # available / fleet match on the keys present in the (densified) whole-frame output.
    c_av = norm(pd.read_csv(out_dir / "supply_available_floor.csv.gz"), ["slot_start", "cluster_id"])
    w_av = norm(whole["supply_available_floor"], ["slot_start", "cluster_id"])
    av = w_av.merge(c_av, on=["slot_start", "cluster_id"], how="left", suffixes=("_w", "_c"))
    assert (av["available_vehicles_w"] == av["available_vehicles_c"]).all()


def test_fleet_lower_bound_in_service_counts_origin_only() -> None:
    """Fix-2: an in-service driver is attributed to the origin cluster only."""
    module = load_module()
    idle_driver_slots = pd.DataFrame(columns=["slot_start", "driver_id", "cluster_id"])
    trip_driver_slots = pd.DataFrame(
        [
            {
                "slot_start": pd.Timestamp("2017-06-01 08:00:00"),
                "driver_id": 7,
                "origin_cluster_id": 10,
                "destination_cluster_id": 20,
            }
        ]
    )

    fleet = module.compute_fleet_lower_bound(idle_driver_slots, trip_driver_slots)

    by_cluster = dict(zip(fleet["cluster_id"], fleet["fleet_lower_bound_cluster"]))
    assert by_cluster.get(10) == 1
    assert 20 not in by_cluster  # destination cluster must not count the driver
