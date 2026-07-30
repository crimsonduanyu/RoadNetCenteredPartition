from __future__ import annotations

import gzip

import numpy as np
import pandas as pd
import pytest

from roadnet_partition.downstream import tte as mod


# Three roughly colinear clusters: B sits between A and C, so B is a valid
# corridor (detour) candidate for the A->C trip.
CLUSTER_INDEX = pd.DataFrame(
    {
        "cluster_id": ["A", "B", "C"],
        "centroid_lon": [116.000, 116.000, 116.000],
        "centroid_lat": [39.900, 39.945, 39.990],
    }
)


def make_pruner() -> mod.SpatialPruner:
    return mod.SpatialPruner.from_cluster_index(CLUSTER_INDEX)


# --------------------------------------------------------------------------
# SpatialPruner
# --------------------------------------------------------------------------
def test_distance_matrix_symmetric_zero_diagonal() -> None:
    pruner = make_pruner()
    m = pruner.dist_matrix
    assert np.allclose(m, m.T)
    assert np.allclose(np.diag(m), 0.0)
    # A->C should be ~ the sum of the two colinear legs.
    d_ac = pruner.get_distance("A", "C")
    d_ab = pruner.get_distance("A", "B")
    d_bc = pruner.get_distance("B", "C")
    assert d_ac > 0
    assert abs((d_ab + d_bc) - d_ac) < 0.05 * d_ac


def test_get_candidates_corridor_excludes_endpoints() -> None:
    pruner = make_pruner()
    candidates = pruner.get_candidates("A", "C", detour_ratio=1.3)
    assert "B" in candidates
    assert "A" not in candidates and "C" not in candidates
    # A tight ratio of ~1.0 still admits the colinear midpoint.
    assert "B" in pruner.get_candidates("A", "C", detour_ratio=1.01)


def test_get_candidates_unknown_id_returns_empty() -> None:
    pruner = make_pruner()
    assert pruner.get_candidates("A", "ZZZ") == []
    assert np.isnan(pruner.get_distance("A", "ZZZ"))


# --------------------------------------------------------------------------
# from_distance_matrix: precomputed (e.g. network) distance source
# --------------------------------------------------------------------------
def _network_pruner() -> mod.SpatialPruner:
    # Colinear A-B-C with B the midpoint corridor; distances in km (pruner unit).
    matrix = pd.DataFrame(
        [[0.0, 5.0, 10.0], [5.0, 0.0, 5.0], [10.0, 5.0, 0.0]],
        index=["A", "B", "C"],
        columns=["A", "B", "C"],
    )
    return mod.SpatialPruner.from_distance_matrix(matrix)


def test_from_distance_matrix_reads_matrix_and_candidates() -> None:
    pruner = _network_pruner()
    assert pruner.get_distance("A", "C") == 10.0
    assert pruner.get_distance("A", "B") == 5.0
    # B is on the corridor: 5000 + 5000 <= 1.3 * 10000
    assert "B" in pruner.get_candidates("A", "C", detour_ratio=1.3)
    assert "A" not in pruner.get_candidates("A", "C")
    # rejects a non-square / mismatched-id matrix
    bad = pd.DataFrame([[0.0, 1.0]], index=["A"], columns=["A", "B"])
    with pytest.raises(ValueError):
        mod.SpatialPruner.from_distance_matrix(bad)


def test_imputation_invariants_under_network_pruner() -> None:
    """Switching the distance source to a precomputed matrix must not break the
    'only fill NaN / never overwrite observed / diagonal not imputed' invariants."""
    pruner = _network_pruner()
    df = _build_observed_frame()
    df["A->C"] = np.nan
    df.loc[df.index[0], "A->C"] = 999.0
    df["A->A"] = np.nan  # diagonal stays missing (dist 0 -> no candidates)

    imputed, _, _ = mod.run_imputation_pipeline(df, pruner, use_validation=True)

    assert (imputed["A->B"] == 10.0).all()
    assert (imputed["B->C"] == 10.0).all()
    assert imputed.loc[df.index[0], "A->C"] == 999.0      # observed not overwritten
    assert imputed["A->C"].iloc[1:].notna().any()         # some NaNs filled
    assert imputed["A->A"].isna().all()                   # diagonal never imputed


# --------------------------------------------------------------------------
# Transitive time math
# --------------------------------------------------------------------------
def test_calculate_transitive_time_interpolates() -> None:
    time_values = np.array([0.0, 10.0, 20.0, 30.0])
    tte_ok = np.array([10.0, 10.0, 10.0, 10.0])
    tte_kd = np.array([5.0, 7.0, 9.0, 11.0])
    # arrival = t + 10 -> [10, 20, 30, 40]; interp of k->D at those instants is
    # [7, 9, 11, NaN] (40 is past the last sample), so T_od = ok + that.
    result = mod.calculate_transitive_time(time_values, tte_ok, tte_kd)
    assert np.allclose(result[:3], [17.0, 19.0, 21.0])
    assert np.isnan(result[3])


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_validate_estimates_rejects_overspeed() -> None:
    cfg = mod.DEFAULT_CONFIG
    dist_km = 10.0
    # index 2 is implausibly fast (10 km in 1 min -> 600 km/h) and must be NaN'd.
    estimates = np.array([20.0, 20.0, 1.0, 20.0, 20.0, 20.0])
    out = mod.validate_estimates(estimates, dist_km, cfg)
    assert np.isnan(out[2])
    assert out[0] == 20.0 and out[5] == 20.0


# --------------------------------------------------------------------------
# Imputation pipeline: only fills NaN, never overwrites observed values
# --------------------------------------------------------------------------
def _build_observed_frame(periods: int = 6) -> pd.DataFrame:
    index = pd.date_range("2020-01-01", periods=periods, freq="10min")
    columns = mod.build_od_columns(["A", "B", "C"])
    df = pd.DataFrame(index=index, columns=columns, dtype=float)
    df["A->B"] = 10.0
    df["B->C"] = 10.0
    return df


def test_imputation_fills_only_missing_cells() -> None:
    pruner = make_pruner()
    df = _build_observed_frame()
    # A->C entirely missing except a sentinel observed value at slot 0.
    df["A->C"] = np.nan
    df.loc[df.index[0], "A->C"] = 999.0

    imputed, _, _ = mod.run_imputation_pipeline(df, pruner, use_validation=True)

    # Observed legs are untouched.
    assert (imputed["A->B"] == 10.0).all()
    assert (imputed["B->C"] == 10.0).all()
    # The sentinel observed value must never be overwritten.
    assert imputed.loc[df.index[0], "A->C"] == 999.0
    # Some previously-missing A->C cells get filled near 20 (=10+10).
    filled = imputed["A->C"].iloc[1:]
    assert filled.notna().any()
    assert np.nanmax(np.abs(filled.dropna().to_numpy() - 20.0)) < 1.0
    # Overall NaN count does not increase.
    assert imputed.isna().sum().sum() <= df.isna().sum().sum()


# --------------------------------------------------------------------------
# build_tte_raw  (value + count)
# --------------------------------------------------------------------------
def _make_build_orders() -> pd.DataFrame:
    return pd.DataFrame(
        {
            mod.DEPARTURE_COL: [
                "2020-01-01 00:00:00",
                "2020-01-01 00:01:00",  # same 10-min slot as the first
                "2020-01-01 00:15:00",  # falls in the 00:10 slot
                "2020-01-01 00:00:00",  # too-short trip, dropped
            ],
            mod.FINISH_COL: [
                "2020-01-01 00:10:00",  # 10 min
                "2020-01-01 00:21:00",  # 20 min  -> median(10,20)=15 in slot 00:00
                "2020-01-01 00:55:00",  # 40 min  -> slot 00:10
                "2020-01-01 00:01:00",  # 1 min, below min_minutes
            ],
            mod.ORIGIN_COL: ["A", "A", "A", "A"],
            mod.DESTINATION_COL: ["B", "B", "B", "B"],
        }
    )


def _reference_value(orders, keep, time_range, columns, freq, lo, hi, aggregation):
    """Old-style single-agg value path, parameterized by the SAME aggregation
    operator as the code under test (never a hard-coded 'median')."""
    df = orders.copy()
    df[mod.DEPARTURE_COL] = pd.to_datetime(df[mod.DEPARTURE_COL])
    df[mod.FINISH_COL] = pd.to_datetime(df[mod.FINISH_COL])
    df["trip_time"] = (df[mod.FINISH_COL] - df[mod.DEPARTURE_COL]).dt.total_seconds() / 60.0
    df = df[(df["trip_time"] >= lo) & (df["trip_time"] <= hi)]
    df[mod.ORIGIN_COL] = df[mod.ORIGIN_COL].astype(str)
    df[mod.DESTINATION_COL] = df[mod.DESTINATION_COL].astype(str)
    keep_set = set(keep)
    df = df[df[mod.ORIGIN_COL].isin(keep_set) & df[mod.DESTINATION_COL].isin(keep_set)]
    df["slot"] = df[mod.DEPARTURE_COL].dt.floor(freq)
    grouped = df.groupby(["slot", mod.ORIGIN_COL, mod.DESTINATION_COL])["trip_time"].agg(aggregation).reset_index()
    grouped["OD"] = grouped[mod.ORIGIN_COL] + "->" + grouped[mod.DESTINATION_COL]
    return grouped.pivot(index="slot", columns="OD", values="trip_time").reindex(index=time_range, columns=columns)


def test_build_tte_raw_filters_and_aggregates() -> None:
    orders = _make_build_orders()
    keep = ["A", "B"]
    columns = mod.build_od_columns(keep)
    time_range = pd.date_range("2020-01-01 00:00:00", "2020-01-01 00:50:00", freq="10min")
    aggregation = "median"  # single source of truth for both sides of the regression

    value, count = mod.build_tte_raw(orders, keep, time_range, columns, "10min", 3, 80, aggregation)

    # --- value: structure + aggregation ---
    assert list(value.columns) == columns
    assert value.index.equals(time_range)
    assert value.loc[time_range[0], "A->B"] == 15.0  # median of 10 and 20
    assert value.loc[time_range[0], "A->A"] != value.loc[time_range[0], "A->A"]  # NaN (unobserved)

    # --- regression: named-agg refactor == old-style single agg, same operator ---
    reference = _reference_value(orders, keep, time_range, columns, "10min", 3, 80, aggregation)
    pd.testing.assert_frame_equal(value, reference, check_dtype=False)

    # --- count: structure mirrors value, integer, non-negative, correct support ---
    assert list(count.columns) == columns
    assert count.index.equals(value.index)
    assert pd.api.types.is_integer_dtype(count.to_numpy().dtype)
    assert (count.to_numpy() >= 0).all()
    assert count.loc[time_range[0], "A->B"] == 2          # 10 + 20 min, both in slot 00:00
    assert count.loc[time_range[1], "A->B"] == 1          # 40 min in slot 00:10
    assert count.loc[time_range[0], "A->A"] == 0          # unobserved diagonal
    assert count.loc[time_range[2], "A->B"] == 0          # no trips in slot 00:20


def test_count_matches_observed_nonnan() -> None:
    """Core invariant on the raw (pre-imputation) matrix: count>=1 iff observed."""
    orders = _make_build_orders()
    keep = ["A", "B"]
    columns = mod.build_od_columns(keep)
    time_range = pd.date_range("2020-01-01 00:00:00", "2020-01-01 00:50:00", freq="10min")
    value, count = mod.build_tte_raw(orders, keep, time_range, columns, "10min", 3, 80, "median")
    observed = count.to_numpy() >= 1
    non_nan = value.notna().to_numpy()
    assert (observed == non_nan).all()


# --------------------------------------------------------------------------
# End-to-end run_from_config smoke test on a tiny fixture
# --------------------------------------------------------------------------
def test_run_from_config_smoke(tmp_path: Path) -> None:
    # Build a tiny assigned-orders file with observed A->B and B->C legs.
    index = pd.date_range("2020-01-01 00:00:00", periods=6, freq="10min")
    rows = []
    for t in index:
        # Round trips so every cluster appears as both origin and destination.
        rows.append((t, t + pd.Timedelta(minutes=10), "A", "B"))
        rows.append((t, t + pd.Timedelta(minutes=10), "B", "A"))
        rows.append((t, t + pd.Timedelta(minutes=10), "B", "C"))
        rows.append((t, t + pd.Timedelta(minutes=10), "C", "B"))
        rows.append((t, t + pd.Timedelta(minutes=20), "A", "C"))
        rows.append((t, t + pd.Timedelta(minutes=20), "C", "A"))
    orders = pd.DataFrame(rows, columns=[mod.DEPARTURE_COL, mod.FINISH_COL, mod.ORIGIN_COL, mod.DESTINATION_COL])
    orders[mod.DEPARTURE_COL] = orders[mod.DEPARTURE_COL].dt.strftime("%Y-%m-%d %H:%M:%S")
    orders[mod.FINISH_COL] = orders[mod.FINISH_COL].dt.strftime("%Y-%m-%d %H:%M:%S")

    orders_path = tmp_path / "orders_region_assigned.csv.gz"
    with gzip.open(orders_path, "wt", encoding="utf-8", newline="") as handle:
        orders.to_csv(handle, index=False)
    cluster_index_path = tmp_path / "cluster_index.csv"
    CLUSTER_INDEX.to_csv(cluster_index_path, index=False)

    out_dir = tmp_path / "tte"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Pre-place the cached network-distance matrix (metres) so build_or_load loads
    # it instead of needing a real OSM graphml. Colinear A-B-C corridor.
    dist_m = pd.DataFrame(
        [[0.0, 5000.0, 10000.0], [5000.0, 0.0, 5000.0], [10000.0, 5000.0, 0.0]],
        index=["A", "B", "C"],
        columns=["A", "B", "C"],
    )
    matrix_path = out_dir / "cluster_network_distance.parquet"
    dist_m.to_parquet(matrix_path)

    config = {
        "crs": {"projected": "EPSG:32650", "geographic": "EPSG:4326"},
        "stage4_tte": {
            "inputs": {"orders_path": str(orders_path), "cluster_index_path": str(cluster_index_path)},
            "output_dir": str(out_dir),
            "distance": {"matrix_filename": "cluster_network_distance.parquet", "recompute": False},
            "time": {"freq": "10min", "start_time": "2020-01-01 00:00:00", "end_time": "2020-01-01 00:50:00"},
            "trip_time": {"min_minutes": 3, "max_minutes": 80, "aggregation": "median"},
            "keep_place": {"min_origin_orders": 1, "min_dest_orders": 1},
            "imputation": {
                "method": "transitive",
                "max_hops": 3,
                "source_min_count": 1,   # tiny fixture has count==1 legs; keep them eligible
                "detour_ratio": 1.3,
                "speed_limit_kmh": [5, 120],
                "min_dist_km": 0.01,
                "window": 6,
                "outlier_std_threshold": 3,
                "use_validation": True,
            },
        }
    }

    summary = mod.run_from_config(config)

    raw = pd.read_parquet(out_dir / "TTE_raw.parquet")
    imputed = pd.read_parquet(out_dir / "TTE_imputed.parquet")
    count = pd.read_parquet(out_dir / "TTE_count.parquet")
    assert raw.shape == imputed.shape
    assert len(raw.index) == 6
    assert summary["num_clusters"] == 3
    # Observed legs are present in the raw matrix.
    assert raw["A->B"].notna().any()
    # Imputation does not increase missingness.
    assert summary["imputed_nan_ratio"] <= summary["raw_nan_ratio"]

    # --- count matrix: aligned with raw, integer, correct support ---
    assert count.shape == raw.shape
    assert list(count.columns) == list(raw.columns)
    assert count.index.equals(raw.index)
    assert pd.api.types.is_integer_dtype(count.to_numpy().dtype)
    assert ((count.to_numpy() >= 1) == raw.notna().to_numpy()).all()   # count>=1 iff observed
    assert ((count.to_numpy() == 0) == raw.isna().to_numpy()).all()    # count==0 iff missing
    assert summary["num_observed_cells"] == int((count.to_numpy() > 0).sum())

    # --- provenance matrices written, aligned with raw ---
    hops = pd.read_parquet(out_dir / "TTE_hops.parquet")
    support = pd.read_parquet(out_dir / "TTE_support.parquet")
    assert hops.shape == raw.shape and support.shape == raw.shape
    assert list(hops.columns) == list(raw.columns) and hops.index.equals(raw.index)
    # hops: observed -> 0, inferred -> >=1, unfilled -> -1
    assert ((hops.to_numpy() == 0) == raw.notna().to_numpy()).all()
    inferred = hops.to_numpy() >= 1
    assert (inferred == (raw.isna().to_numpy() & imputed.notna().to_numpy())).all()
    # support meaningful (>=1) exactly on inferred cells, -1 elsewhere
    assert ((support.to_numpy() >= 1) == inferred).all()

    # --- regression: imputation matches re-running with the same source/params ---
    pruner = mod.SpatialPruner.from_distance_matrix(dist_m / 1000.0)  # same km matrix run_from_config used
    imp_config = mod._imputation_config(config["stage4_tte"])
    exp_value, _, _ = mod.run_imputation_pipeline(
        raw, pruner, count_df=count, use_validation=True, config=imp_config, max_hops=3, k=1
    )
    pd.testing.assert_frame_equal(imputed, exp_value, check_dtype=False)


# ==========================================================================
# Multi-hop TDSP + edge-level k-gate + provenance (hops / support)
# ==========================================================================
def _km_matrix(ids, pos_km) -> pd.DataFrame:
    n = len(ids)
    m = np.zeros((n, n))
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            m[i, j] = abs(pos_km[a] - pos_km[b])
    return pd.DataFrame(m, index=ids, columns=ids)


def _chain_setup(count_overrides=None, periods=6):
    """Colinear A-B-C-D at 0/5/10/15 km; observed consecutive legs (10 min, count 5)."""
    ids = ["A", "B", "C", "D"]
    pruner = mod.SpatialPruner.from_distance_matrix(_km_matrix(ids, {"A": 0, "B": 5, "C": 10, "D": 15}))
    index = pd.date_range("2020-01-01", periods=periods, freq="10min")
    cols = mod.build_od_columns(ids)
    value = pd.DataFrame(np.nan, index=index, columns=cols, dtype=float)
    count = pd.DataFrame(0, index=index, columns=cols, dtype=int)
    for a, b in [("A", "B"), ("B", "C"), ("C", "D")]:
        value[f"{a}->{b}"] = 10.0
        count[f"{a}->{b}"] = 5
    if count_overrides:
        for col, c in count_overrides.items():
            count[col] = c
    return pruner, value, count, index, cols


def _col(cols, name):
    return cols.index(name)


def test_multihop_two_hop_fill_and_cap_and_provenance() -> None:
    pruner, value, count, index, cols = _chain_setup()

    # max_hops=1: A->D needs a 2-hop path (B->D / A->C are themselves hop-1), so it stays NaN.
    v1, h1, _ = mod.run_imputation_pipeline(value, pruner, count_df=count, config=mod.DEFAULT_CONFIG.copy(), max_hops=1, k=1)
    assert v1["A->D"].isna().all()

    # max_hops=2: A->D fills at hop 2 with value 30 = 10 (A->B) + 20 (B->D), support 5.
    v2, h2, s2 = mod.run_imputation_pipeline(value, pruner, count_df=count, config=mod.DEFAULT_CONFIG.copy(), max_hops=2, k=1)
    j = _col(cols, "A->D")
    assert abs(v2.loc[index[0], "A->D"] - 30.0) < 1e-4
    assert h2[0, j] == 2          # filled at hop 2
    assert s2[0, j] == 5.0        # weakest support along chosen chain
    # B->D / A->C themselves are hop-1
    assert h2[0, _col(cols, "B->D")] == 1
    assert h2[0, _col(cols, "A->C")] == 1


def test_k_gate_thin_leg_not_used() -> None:
    # B->C is a thin observation (count 1 < k=3): it must not serve as a leg, so
    # A->C (whose only corridor is via B) cannot be filled; B->C itself is kept.
    pruner, value, count, index, cols = _chain_setup(count_overrides={"B->C": 1})
    v, h, _ = mod.run_imputation_pipeline(value, pruner, count_df=count, config=mod.DEFAULT_CONFIG.copy(), max_hops=3, k=3)
    assert v["A->C"].isna().all()                       # thin B->C leg unusable
    assert (v["B->C"] == 10.0).all()                    # thin observation preserved
    assert (h[:, _col(cols, "B->C")] == 0).all()        # B->C stays observed (hop 0)


def test_thin_observed_cells_protected() -> None:
    # Several observed legs are thin (count 1 < k=3). After max_hops rounds they
    # must equal raw exactly (neither NaN'd nor overwritten) and stay hops==0.
    pruner, value, count, index, cols = _chain_setup(count_overrides={"A->B": 1, "C->D": 1})
    v, h, _ = mod.run_imputation_pipeline(value, pruner, count_df=count, config=mod.DEFAULT_CONFIG.copy(), max_hops=3, k=3)
    for thin_col in ["A->B", "C->D"]:
        assert (v[thin_col] == 10.0).all()              # preserved verbatim
        assert (h[:, _col(cols, thin_col)] == 0).all()  # still observed, not inferred


def test_early_stop_no_extra_hops() -> None:
    # Everything fillable at hop 1 (A->C via B), nothing needs hop 2+. Running with
    # max_hops=3 must produce only hop-1 fills (round 2 fills nothing -> early stop).
    ids = ["A", "B", "C"]
    pruner = mod.SpatialPruner.from_distance_matrix(_km_matrix(ids, {"A": 0, "B": 5, "C": 10}))
    index = pd.date_range("2020-01-01", periods=6, freq="10min")
    cols = mod.build_od_columns(ids)
    value = pd.DataFrame(np.nan, index=index, columns=cols, dtype=float)
    count = pd.DataFrame(0, index=index, columns=cols, dtype=int)
    for a, b in [("A", "B"), ("B", "C")]:
        value[f"{a}->{b}"] = 10.0
        count[f"{a}->{b}"] = 5
    v, h, _ = mod.run_imputation_pipeline(value, pruner, count_df=count, config=mod.DEFAULT_CONFIG.copy(), max_hops=3, k=1)
    assert v["A->C"].notna().any()          # filled
    assert (h == 2).sum() == 0 and (h == 3).sum() == 0   # no hop-2/3 fills -> converged at hop 1


def test_provenance_records_selected_path_support() -> None:
    # Two corridors for A->C: via B1 (est 20, support 8) and via B2 (est 15, support 2).
    # argmin selects B2 (smaller estimate), so recorded support must be 2, not 8.
    ids = ["A", "B1", "B2", "C"]
    m = pd.DataFrame(
        [[0, 5, 5, 10], [5, 0, 9, 5], [5, 9, 0, 5], [10, 5, 5, 0]],
        index=ids, columns=ids, dtype=float,
    )
    pruner = mod.SpatialPruner.from_distance_matrix(m)
    index = pd.date_range("2020-01-01", periods=6, freq="10min")
    cols = mod.build_od_columns(ids)
    value = pd.DataFrame(np.nan, index=index, columns=cols, dtype=float)
    count = pd.DataFrame(0, index=index, columns=cols, dtype=int)
    obs = {("A", "B1"): (10.0, 8), ("B1", "C"): (10.0, 8), ("A", "B2"): (10.0, 2), ("B2", "C"): (5.0, 9)}
    for (a, b), (val, c) in obs.items():
        value[f"{a}->{b}"] = val
        count[f"{a}->{b}"] = c
    v, h, s = mod.run_imputation_pipeline(value, pruner, count_df=count, config=mod.DEFAULT_CONFIG.copy(), max_hops=1, k=1)
    j = _col(cols, "A->C")
    assert abs(v.loc[index[0], "A->C"] - 15.0) < 1e-4   # via B2 (the min estimate)
    assert h[0, j] == 1
    assert s[0, j] == 2.0                                 # support of the SELECTED path, not 8
