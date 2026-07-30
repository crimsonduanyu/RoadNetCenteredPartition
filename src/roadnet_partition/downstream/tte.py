"""Stage 4 - trip-time (TTE) dataset construction.

Builds a ``(time slot x OD)`` wide matrix of median trip times from the Stage 2
assigned-orders export and fills gaps with a transitive (corridor) imputation.

Migrated from the KoopmanTTE ``functions_dp.py`` / ``Make_*_TTE`` notebooks and
adapted to the road-centered partition: the spatial unit is a **cluster**
(road-network zone) rather than a regular grid, and all cluster ids are handled
as strings to support arbitrary partition labels.

Inputs (both Stage 2 ``order_pipeline`` products):

- ``orders_region_assigned.csv.gz`` - one row per assigned order with
  ``departure_time``, ``finish_time``, ``origin_cluster_id``,
  ``destination_cluster_id`` (see ``order_dataset.export_assigned_orders``).
- ``cluster_index.csv`` - per-cluster ``centroid_lon`` / ``centroid_lat`` in
  EPSG:4326 (see ``order_dataset.build_cluster_index``), used to build the
  Haversine distance matrix for corridor pruning.

Outputs (under ``data/processed/<scope>/tte/``):

- ``TTE_raw.parquet``     - observed median trip time, time x OD wide matrix.
- ``TTE_imputed.parquet`` - same matrix after transitive imputation.

The computation is pure (functions may do I/O but there are no module-level side
effects); the public CLI supplies a resolved split configuration.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
from typing import Any, Iterable

from roadnet_partition.io import environment as _environment  # noqa: F401
import numpy as np
import pandas as pd

from roadnet_partition.graphs import distance as network_distance
from roadnet_partition.graphs.distance import (
    project_path,
    sort_cluster_ids,
)
from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.io.paths import assert_owned_path, resolve_path
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus

try:  # tqdm is a convenience only; degrade gracefully if unavailable.
    from tqdm import tqdm
except Exception:  # pragma: no cover - exercised only without tqdm installed
    def tqdm(iterable: Iterable[Any], **_: Any) -> Iterable[Any]:
        return iterable


# Column names emitted by Stage 2 ``export_assigned_orders`` (fixed contract).
DEPARTURE_COL = "departure_time"
FINISH_COL = "finish_time"
ORIGIN_COL = "origin_cluster_id"
DESTINATION_COL = "destination_cluster_id"

# Default imputation / validation parameters (mirror KoopmanTTE DEFAULT_CONFIG).
DEFAULT_CONFIG: dict[str, Any] = {
    "detour_ratio": 1.3,          # corridor (ellipse) detour tolerance eta
    "speed_limit": (5, 120),      # implied-speed sanity bounds, km/h
    "min_dist": 0.01,             # minimum O-D physical distance, km
    "window": 6,                  # rolling-outlier window (in slots)
    "outlier_std_threshold": 3,   # rolling-outlier rejection threshold (n * std)
}


def active_scope_name(config: dict[str, Any]) -> str:
    study_area = config["study_area"]
    return str(study_area.get("active", study_area.get("boundary_source", "fifth_ring")))


# ==========================================================================
# Spatial pruner: cluster-to-cluster distance matrix + detour-ratio candidates
# ==========================================================================
class SpatialPruner:
    """Hold a full cluster-to-cluster distance matrix and expose the
    detour-ratio candidate set ``{k : d(O,k) + d(k,D) <= eta * d(O,D)}``.

    The distance matrix can come from centroid Haversine (``from_cluster_index``)
    or from precomputed road-network shortest paths (``from_distance_matrix``);
    the candidate logic is identical either way (it only reads the matrix)."""

    def __init__(self, coords: dict[str, dict[str, float]]):
        # coords: {cluster_id: {'lat': .., 'lon': ..}}; ids forced to str.
        self.grid_coords = {str(cid): value for cid, value in coords.items()}
        self.ids = list(self.grid_coords.keys())
        self.id_to_idx = {cid: i for i, cid in enumerate(self.ids)}
        self.idx_to_id = {i: cid for i, cid in enumerate(self.ids)}
        self.dist_matrix = self._build_distance_matrix()
        print(f"SpatialPruner ready: {len(self.ids)} clusters.")

    @classmethod
    def from_meta_df(cls, meta_df: pd.DataFrame) -> "SpatialPruner":
        """Build from a KoopmanTTE-style OD ``meta_df`` with O_/D_ columns."""
        return cls(cls._extract_unique_coords(meta_df))

    @classmethod
    def from_cluster_index(
        cls,
        cluster_index: pd.DataFrame,
        id_col: str = "cluster_id",
        lat_col: str = "centroid_lat",
        lon_col: str = "centroid_lon",
    ) -> "SpatialPruner":
        """Build directly from Stage 2's ``cluster_index.csv`` centroids."""
        missing = {id_col, lat_col, lon_col} - set(cluster_index.columns)
        if missing:
            raise ValueError(f"cluster_index missing columns: {sorted(missing)}")
        coords: dict[str, dict[str, float]] = {}
        for row in cluster_index.itertuples(index=False):
            cid = str(getattr(row, id_col))
            coords[cid] = {"lat": float(getattr(row, lat_col)), "lon": float(getattr(row, lon_col))}
        return cls(coords)

    @classmethod
    def from_distance_matrix(cls, matrix: pd.DataFrame) -> "SpatialPruner":
        """Build from a precomputed cluster-to-cluster distance matrix.

        ``matrix`` is square with identical str cluster-id index/columns. Values
        must be in the same unit the consumers expect, i.e. **kilometres** (to
        match ``validate_estimates`` speed and ``min_dist`` checks); callers
        holding a metres matrix should divide by 1000 first. No Haversine is
        computed; the candidate logic reads this matrix as-is.
        """
        ids = [str(cid) for cid in matrix.index]
        if [str(c) for c in matrix.columns] != ids:
            raise ValueError("Distance matrix must have identical index and column cluster ids.")
        obj = cls.__new__(cls)
        obj.grid_coords = None
        obj.ids = ids
        obj.id_to_idx = {cid: i for i, cid in enumerate(ids)}
        obj.idx_to_id = {i: cid for i, cid in enumerate(ids)}
        obj.dist_matrix = matrix.to_numpy(dtype=float)
        print(f"SpatialPruner ready: {len(ids)} clusters (precomputed network distance).")
        return obj

    @staticmethod
    def _extract_unique_coords(df: pd.DataFrame) -> dict[str, dict[str, float]]:
        o_part = df[["O_Objectid", "O_y", "O_x"]].rename(columns={"O_Objectid": "id", "O_y": "lat", "O_x": "lon"})
        d_part = df[["D_Objectid", "D_y", "D_x"]].rename(columns={"D_Objectid": "id", "D_y": "lat", "D_x": "lon"})
        all_nodes = pd.concat([o_part, d_part]).drop_duplicates(subset="id")
        all_nodes["id"] = all_nodes["id"].astype(str)
        return all_nodes.set_index("id")[["lat", "lon"]].to_dict("index")

    @staticmethod
    def _haversine_matrix(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        """Vectorized Haversine distance matrix (km) via broadcasting."""
        R = 6371.0
        lat1 = np.radians(lats[:, np.newaxis])
        lat2 = np.radians(lats[np.newaxis, :])
        lon1 = np.radians(lons[:, np.newaxis])
        lon2 = np.radians(lons[np.newaxis, :])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
        return R * c

    def _build_distance_matrix(self) -> np.ndarray:
        lats = np.array([self.grid_coords[i]["lat"] for i in self.ids])
        lons = np.array([self.grid_coords[i]["lon"] for i in self.ids])
        return self._haversine_matrix(lats, lons)

    def get_candidates(self, o_id: str, d_id: str, detour_ratio: float = 1.3) -> list[str]:
        """Intermediate clusters k satisfying d(O,k)+d(k,D) <= ratio*d(O,D)."""
        o_id, d_id = str(o_id), str(d_id)
        if o_id not in self.id_to_idx or d_id not in self.id_to_idx:
            return []
        i_o = self.id_to_idx[o_id]
        i_d = self.id_to_idx[d_id]
        dist_od = self.dist_matrix[i_o, i_d]
        if dist_od < 0.01:  # coincident / near-coincident endpoints
            return []
        total_dist = self.dist_matrix[i_o, :] + self.dist_matrix[:, i_d]
        mask = total_dist <= (detour_ratio * dist_od)
        candidates = []
        for idx in np.where(mask)[0]:
            if idx == i_o or idx == i_d:
                continue
            candidates.append(self.idx_to_id[idx])
        return candidates

    def get_distance(self, o_id: str, d_id: str) -> float:
        """Physical distance (km) between two clusters."""
        o_id, d_id = str(o_id), str(d_id)
        if o_id not in self.id_to_idx or d_id not in self.id_to_idx:
            return np.nan
        return self.dist_matrix[self.id_to_idx[o_id], self.id_to_idx[d_id]]


# ==========================================================================
# Transitive imputation core
# ==========================================================================
def parse_columns(columns: Iterable[str]) -> dict[tuple[str, str], str]:
    """Map an ``'O->D'`` column name to its ``(O, D)`` string id pair."""
    col_map: dict[tuple[str, str], str] = {}
    for col in columns:
        if "->" in col:
            o, d = col.split("->")
            col_map[(o, d)] = col
    return col_map


def get_transitive_data(df_TTE: pd.DataFrame, o_id: str, k: str, d_id: str):
    """Fetch the O->k and k->D series, or (None, None) if a leg is absent."""
    col_ok = f"{o_id}->{k}"
    col_kd = f"{k}->{d_id}"
    if col_ok not in df_TTE.columns or col_kd not in df_TTE.columns:
        return None, None
    return df_TTE[col_ok].values, df_TTE[col_kd].values


def calculate_transitive_time(time_values: np.ndarray, tte_ok: np.ndarray, tte_kd: np.ndarray):
    """T_od(t) = T_ok(t) + T_kd(t + T_ok(t)) via linear interpolation on k->D."""
    arrival_times = time_values + tte_ok
    valid_mask_kd = ~np.isnan(tte_kd)
    if not np.any(valid_mask_kd):
        return None
    xp = time_values[valid_mask_kd]
    fp = tte_kd[valid_mask_kd]
    pred_kd = np.interp(arrival_times, xp, fp, left=np.nan, right=np.nan)
    return tte_ok + pred_kd


def vectorize_transitive_impute(
    target_col: str,
    source_value: pd.DataFrame,
    support_src: pd.DataFrame,
    pruner: SpatialPruner,
    slot_min: float,
    config: dict[str, Any] | None = None,
):
    """Best transitive estimate for one OD column from the current source matrices.

    Legs (``T_ok`` / ``T_kd``) are read from ``source_value`` (already gated:
    thin observed sources are NaN). For each corridor candidate ``k`` the
    estimate is ``calculate_transitive_time`` (value, continuous interpolation);
    the path support is ``min(support of O->k at t, support of k->D at the
    arrival slot)`` where the arrival slot is the nearest index to ``t+T_ok``.

    Returns ``(estimate_min, support_min)``: the per-timestamp minimum estimate
    over candidates and the support of *that selected* path (aligned via argmin),
    or NaN where no candidate produced a value.
    """
    if config is None:
        config = DEFAULT_CONFIG
    o_id, d_id = target_col.split("->")
    n = len(source_value)
    candidates_k = pruner.get_candidates(o_id, d_id, detour_ratio=config["detour_ratio"])
    if not candidates_k:
        nan = np.full(n, np.nan)
        return nan, nan

    timestamps = source_value.index
    time_values = np.asarray((timestamps - timestamps[0]).total_seconds(), dtype=float) / 60.0
    est = np.full((n, len(candidates_k)), np.nan)
    sup = np.full((n, len(candidates_k)), np.nan)
    columns = source_value.columns

    for idx, k in enumerate(candidates_k):
        col_ok = f"{o_id}->{k}"
        col_kd = f"{k}->{d_id}"
        if col_ok not in columns or col_kd not in columns:
            continue
        tte_ok = source_value[col_ok].to_numpy()
        tte_kd = source_value[col_kd].to_numpy()
        estimate = calculate_transitive_time(time_values, tte_ok, tte_kd)
        if estimate is None:
            continue
        est[:, idx] = estimate
        # Path support = min(support of leg O->k at t, support of leg k->D at arrival slot).
        sup_ok = support_src[col_ok].to_numpy()
        sup_kd = support_src[col_kd].to_numpy()
        arrival = time_values + tte_ok
        with np.errstate(invalid="ignore"):
            arr_idx = np.rint(np.where(np.isfinite(arrival), arrival, 0.0) / slot_min)
        arr_idx = np.clip(arr_idx.astype(np.int64), 0, n - 1)
        cand_sup = np.minimum(sup_ok, sup_kd[arr_idx])
        sup[:, idx] = np.where(np.isnan(estimate), np.nan, cand_sup)

    all_nan = np.all(np.isnan(est), axis=1)
    estimate_min = np.full(n, np.nan)
    support_min = np.full(n, np.nan)
    if not np.all(all_nan):
        filled_est = np.where(np.isnan(est), np.inf, est)
        idxmin = np.argmin(filled_est, axis=1)
        rows = np.arange(n)
        estimate_min = np.where(all_nan, np.nan, est[rows, idxmin])
        support_min = np.where(all_nan, np.nan, sup[rows, idxmin])
    return estimate_min, support_min


def validate_estimates(imputed_values: np.ndarray, dist_km: float, config: dict[str, Any] | None = None) -> np.ndarray:
    """Reject physically implausible (speed) and statistically outlying estimates."""
    if config is None:
        config = DEFAULT_CONFIG
    valid_estimates = imputed_values.copy()

    safe_values = valid_estimates.copy()
    safe_values[safe_values == 0] = np.nan
    implied_speed = dist_km / (safe_values / 60.0)  # km/h
    speed_limit = config["speed_limit"]
    mask_speed = (implied_speed > speed_limit[1]) | (implied_speed < speed_limit[0])
    valid_estimates[mask_speed] = np.nan

    s = pd.Series(valid_estimates)
    window = config["window"]
    rolling_median = s.rolling(window=window, center=True, min_periods=1).median()
    rolling_std = s.rolling(window=window, center=True, min_periods=1).std()
    threshold = config["outlier_std_threshold"]
    outlier_mask = np.abs(s - rolling_median) > threshold * rolling_std.fillna(100)
    valid_estimates[outlier_mask.to_numpy()] = np.nan
    return valid_estimates


def run_imputation_pipeline(
    df_TTE: pd.DataFrame,
    pruner: SpatialPruner,
    count_df: pd.DataFrame | None = None,
    use_validation: bool = True,
    config: dict[str, Any] | None = None,
    max_hops: int = 1,
    k: int = 1,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Multi-hop transitive (min-plus) imputation with edge-level observation
    gate and provenance.

    Returns ``(value, hops, support)``:

    - ``value``  : filled matrix (float32). Only originally-NaN cells are written;
      observed cells (including thin ones) are never overwritten.
    - ``hops``   : int16, 0 = observed, ``r`` = filled in round ``r``, -1 = unfilled.
    - ``support``: float32 internal; for filled cells = the weakest support (min
      observation count along the chosen path); +inf where not a usable source.

    Source gate (``count_df`` given): an **observed** cell may serve as a leg only
    if ``count >= k`` (thin observed cells, ``1 <= count < k``, are masked out of
    the source but kept verbatim in ``value``); **inferred** cells may serve as
    legs with no count gate. Each round reads a *frozen snapshot* of the previous
    round's value/support (so hop depth == round). Stops at ``max_hops`` or when a
    round fills nothing (early stop).
    """
    if config is None:
        config = DEFAULT_CONFIG.copy()

    index = df_TTE.index
    columns = df_TTE.columns
    n, m = df_TTE.shape
    slot_min = (index[1] - index[0]).total_seconds() / 60.0 if n > 1 else 1.0
    col_to_j = {c: j for j, c in enumerate(columns)}

    value_cur = df_TTE.astype(np.float32).copy()
    observed_mask = df_TTE.notna().to_numpy()

    hops = np.full((n, m), -1, dtype=np.int16)
    hops[observed_mask] = 0
    support = np.full((n, m), np.inf, dtype=np.float32)  # +inf = not a usable source
    if count_df is not None:
        count = count_df.to_numpy()
        eligible_obs = observed_mask & (count >= k)
        support[eligible_obs] = count[eligible_obs].astype(np.float32)
        thin = observed_mask & (count >= 1) & (count < k)  # masked from source, kept in value
    else:
        support[observed_mask] = np.inf  # no gate: all observed are sources
        thin = np.zeros((n, m), dtype=bool)
    thin_df = pd.DataFrame(thin, index=index, columns=columns)

    for r in range(1, max_hops + 1):
        # Frozen round-start snapshots (legs read previous round only).
        source_value = value_cur.mask(thin_df)
        support_src = pd.DataFrame(support.copy(), index=index, columns=columns)
        nan_cols = value_cur.columns[value_cur.isna().any()].tolist()
        print(f"hop {r}/{max_hops}: {len(nan_cols)} OD columns still with NaN...")

        filled = 0
        for target_col in tqdm(nan_cols, desc=f"hop {r}"):
            estimate_min, support_min = vectorize_transitive_impute(
                target_col, source_value, support_src, pruner, slot_min, config
            )
            if np.all(np.isnan(estimate_min)):
                continue
            if use_validation:
                o_id, d_id = target_col.split("->")
                dist_km = pruner.get_distance(o_id, d_id)
                if np.isnan(dist_km) or dist_km < config["min_dist"]:
                    continue
                validated = validate_estimates(estimate_min, dist_km, config)
                support_min = np.where(np.isnan(validated), np.nan, support_min)
                estimate_min = validated

            j = col_to_j[target_col]
            col_vals = value_cur[target_col].to_numpy()
            mask_fill = np.isnan(col_vals) & (~np.isnan(estimate_min))
            if not mask_fill.any():
                continue
            col_vals = col_vals.copy()
            col_vals[mask_fill] = estimate_min[mask_fill].astype(np.float32)
            value_cur[target_col] = col_vals
            hops[mask_fill, j] = r
            support[mask_fill, j] = support_min[mask_fill].astype(np.float32)
            filled += int(mask_fill.sum())

        print(f"  hop {r}: filled {filled:,} cells.")
        if filled == 0:
            break

    return value_cur, hops, support


# ==========================================================================
# Raw TTE matrix construction (observed median trip time)
# ==========================================================================
def compute_keep_clusters(orders: pd.DataFrame, min_origin: int, min_dest: int) -> list[str]:
    """Clusters that appear as origin (>= min_origin) and destination (>= min_dest)."""
    o_counts = orders[ORIGIN_COL].astype(str).value_counts()
    d_counts = orders[DESTINATION_COL].astype(str).value_counts()
    origin_ok = set(o_counts[o_counts >= min_origin].index)
    dest_ok = set(d_counts[d_counts >= min_dest].index)
    return sort_cluster_ids(origin_ok & dest_ok)


def build_od_columns(keep_clusters: list[str]) -> list[str]:
    """Full ordered list of ``'O->D'`` column names over the kept clusters."""
    return [f"{o}->{d}" for o in keep_clusters for d in keep_clusters]


def build_tte_raw(
    orders: pd.DataFrame,
    keep_clusters: list[str],
    time_range: pd.DatetimeIndex,
    columns: list[str],
    freq: str,
    min_minutes: float,
    max_minutes: float,
    aggregation: str = "median",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Observed (time slot x OD) trip-time matrix plus its observation counts.

    trip_time = finish_time - departure_time (minutes); rows outside
    ``[min_minutes, max_minutes]`` or with an endpoint outside ``keep_clusters``
    are dropped; departures are floored to ``freq`` slots and aggregated.

    Returns ``(value_df, count_df)`` sharing the exact same time index, column
    order and structure:

    - ``value_df``: aggregated (default median) trip time, float, NaN where no
      observation supports the cell.
    - ``count_df``: number of post-filter trips supporting each cell, int32,
      ``0`` where there is no observation. Both come from a single groupby so
      ``count_df`` is exactly the support of ``value_df`` (count>=1 iff value
      not NaN). ``count`` (non-null tally) is used rather than ``size`` so this
      invariant holds even if a NaN trip_time ever slips into the groupby. The
      count is materialized only; it never feeds the imputation.
    """
    df = orders.copy()
    df[DEPARTURE_COL] = pd.to_datetime(df[DEPARTURE_COL])
    df[FINISH_COL] = pd.to_datetime(df[FINISH_COL])
    df["trip_time"] = (df[FINISH_COL] - df[DEPARTURE_COL]).dt.total_seconds() / 60.0
    df = df[(df["trip_time"] >= min_minutes) & (df["trip_time"] <= max_minutes)]

    df[ORIGIN_COL] = df[ORIGIN_COL].astype(str)
    df[DESTINATION_COL] = df[DESTINATION_COL].astype(str)
    keep_set = set(keep_clusters)
    df = df[df[ORIGIN_COL].isin(keep_set) & df[DESTINATION_COL].isin(keep_set)]

    if df.empty:
        value_df = pd.DataFrame(index=time_range, columns=columns, dtype=float)
        count_df = pd.DataFrame(0, index=time_range, columns=columns, dtype="int32")
        return value_df, count_df

    df["slot"] = df[DEPARTURE_COL].dt.floor(freq)
    grouped = (
        df.groupby(["slot", ORIGIN_COL, DESTINATION_COL])
        .agg(value=("trip_time", aggregation), count=("trip_time", "count"))
        .reset_index()
    )
    grouped["OD"] = grouped[ORIGIN_COL] + "->" + grouped[DESTINATION_COL]

    value_df = grouped.pivot(index="slot", columns="OD", values="value").reindex(
        index=time_range, columns=columns
    )
    count_df = (
        grouped.pivot(index="slot", columns="OD", values="count")
        .reindex(index=time_range, columns=columns)
        .fillna(0)
        .astype("int32")
    )
    return value_df, count_df


# ==========================================================================
# Config-driven orchestration
# ==========================================================================
def _imputation_config(stage_config: dict[str, Any]) -> dict[str, Any]:
    imp = stage_config.get("imputation", {})
    speed = imp.get("speed_limit_kmh", list(DEFAULT_CONFIG["speed_limit"]))
    return {
        "detour_ratio": float(imp.get("detour_ratio", DEFAULT_CONFIG["detour_ratio"])),
        "speed_limit": (float(speed[0]), float(speed[1])),
        "min_dist": float(imp.get("min_dist_km", DEFAULT_CONFIG["min_dist"])),
        "window": int(imp.get("window", DEFAULT_CONFIG["window"])),
        "outlier_std_threshold": float(imp.get("outlier_std_threshold", DEFAULT_CONFIG["outlier_std_threshold"])),
    }


def _nan_ratio(df: pd.DataFrame) -> float:
    if df.size == 0:
        return float("nan")
    return float(df.isna().to_numpy().mean())


def resolve_output_dir(config: dict[str, Any]) -> Path:
    stage_config = config["stage4_tte"]
    configured = stage_config.get("output_dir")
    if configured:
        return Path(project_path(configured))
    return Path(project_path(f"data/processed/{active_scope_name(config)}/tte"))


def run_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Build and write ``TTE_raw.parquet`` / ``TTE_imputed.parquet`` from config."""
    if "stage4_tte" not in config:
        raise ValueError("configuration must contain a stage4_tte section.")

    stage_config = config["stage4_tte"]
    inputs = stage_config["inputs"]
    orders_path = Path(project_path(inputs["orders_path"]))
    output_dir = resolve_output_dir(config)
    output_dir.mkdir(parents=True, exist_ok=True)

    time_cfg = stage_config["time"]
    freq = str(time_cfg["freq"])
    time_range = pd.date_range(start=time_cfg["start_time"], end=time_cfg["end_time"], freq=freq)

    trip_cfg = stage_config["trip_time"]
    min_minutes = float(trip_cfg["min_minutes"])
    max_minutes = float(trip_cfg["max_minutes"])
    aggregation = str(trip_cfg.get("aggregation", "median"))

    keep_cfg = stage_config.get("keep_place", {})
    min_origin = int(keep_cfg.get("min_origin_orders", 1))
    min_dest = int(keep_cfg.get("min_dest_orders", 1))

    print(f"Loading assigned orders from {orders_path}...")
    orders = pd.read_csv(
        orders_path,
        usecols=[DEPARTURE_COL, FINISH_COL, ORIGIN_COL, DESTINATION_COL],
        dtype={ORIGIN_COL: str, DESTINATION_COL: str},
    )
    print(f"  {len(orders):,} assigned orders.")

    keep_clusters = compute_keep_clusters(orders, min_origin, min_dest)
    columns = build_od_columns(keep_clusters)
    print(f"  kept {len(keep_clusters)} clusters -> {len(columns):,} OD columns.")

    # Distance source = precomputed cluster road-network shortest paths (built/cached
    # on first run). Replaces centroid Haversine; candidate logic is unchanged.
    distance_matrix_m = network_distance.build_or_load(config)
    # SpatialPruner works in km (matches the former Haversine convention and the
    # validate_estimates speed / min_dist checks); the cached matrix is in metres.
    pruner = SpatialPruner.from_distance_matrix(distance_matrix_m / 1000.0)

    df_raw, df_count = build_tte_raw(orders, keep_clusters, time_range, columns, freq, min_minutes, max_minutes, aggregation)
    raw_path = output_dir / "TTE_raw.parquet"
    df_raw.to_parquet(raw_path)
    raw_nan = _nan_ratio(df_raw)
    print(f"Wrote {raw_path} (shape={df_raw.shape}, NaN ratio={raw_nan:.4f}).")

    # Per-cell observation counts, aligned 1:1 with TTE_raw (count>=1 iff observed).
    # Materialized only here; never consumed by the imputation below.
    count_filename = str(stage_config.get("outputs", {}).get("count_filename", "TTE_count.parquet"))
    count_path = output_dir / count_filename
    df_count.to_parquet(count_path)
    num_observed_cells = int((df_count.to_numpy() > 0).sum())
    print(f"Wrote {count_path} (shape={df_count.shape}, observed cells={num_observed_cells:,}).")

    imp_section = stage_config.get("imputation", {})
    imp_config = _imputation_config(stage_config)
    use_validation = bool(imp_section.get("use_validation", True))
    max_hops = int(imp_section.get("max_hops", 3))
    k = int(imp_section.get("source_min_count", 3))
    df_imputed, hops_arr, support_arr = run_imputation_pipeline(
        df_raw, pruner, count_df=df_count, use_validation=use_validation,
        config=imp_config, max_hops=max_hops, k=k,
    )
    imputed_path = output_dir / "TTE_imputed.parquet"
    df_imputed.to_parquet(imputed_path)
    imputed_nan = _nan_ratio(df_imputed)
    print(f"Wrote {imputed_path} (shape={df_imputed.shape}, NaN ratio={imputed_nan:.4f}).")

    # Provenance, aligned 1:1 with TTE_raw. hops: 0=observed, r=filled at hop r, -1=unfilled.
    # support: weakest path support (min observation count) for inferred cells, -1 elsewhere.
    out_cfg = stage_config.get("outputs", {})
    hops_path = output_dir / str(out_cfg.get("hops_filename", "TTE_hops.parquet"))
    support_path = output_dir / str(out_cfg.get("support_filename", "TTE_support.parquet"))
    inferred_mask = hops_arr >= 1
    out_support = np.where(inferred_mask, support_arr, -1.0).astype(np.int32)
    pd.DataFrame(hops_arr, index=df_raw.index, columns=df_raw.columns).to_parquet(hops_path)
    pd.DataFrame(out_support, index=df_raw.index, columns=df_raw.columns).to_parquet(support_path)
    hop_counts = {f"hop{h}_cells": int((hops_arr == h).sum()) for h in range(1, max_hops + 1)}
    print(f"Wrote {hops_path} / {support_path}; inferred cells={int(inferred_mask.sum()):,}, by hop={hop_counts}.")

    return {
        "output_dir": str(output_dir),
        "num_clusters": len(keep_clusters),
        "num_od_columns": len(columns),
        "num_slots": int(len(time_range)),
        "raw_nan_ratio": raw_nan,
        "imputed_nan_ratio": imputed_nan,
        "count_path": str(count_path),
        "num_observed_cells": num_observed_cells,
        "max_hops": max_hops,
        "source_min_count": k,
        "num_inferred_cells": int(inferred_mask.sum()),
        **hop_counts,
    }


def run_tte(config: ResolvedStageConfig, context: RunContext) -> StageResult:
    """Run TTE inside its owned stage directory without updating a manifest."""
    if context.stage_dir is None or context.stage_name != "tte":
        raise ValueError("run_tte requires context.for_stage('tte')")
    values = deepcopy(dict(config.values))
    if "stage4_tte" not in values:
        raise ValueError("configuration must contain a stage4_tte section")
    stage = deepcopy(dict(values["stage4_tte"]))
    inputs = deepcopy(dict(stage["inputs"]))
    base_dir = config.source_path.parent
    for key in (
        "orders_path", "cluster_index_path", "network_distance_path",
        "representative_nodes_path",
    ):
        if key in inputs:
            inputs[key] = str(resolve_path(inputs[key], base_dir=base_dir))
    distance = deepcopy(dict(stage.get("distance", {})))
    for key in ("graphml_path", "classified_edges_path", "partition_gpkg"):
        if key in distance:
            distance[key] = str(resolve_path(distance[key], base_dir=base_dir))
    output_dir = context.stage_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stage["inputs"] = inputs
    stage["distance"] = distance
    stage["output_dir"] = str(output_dir)
    values["stage4_tte"] = stage

    outputs_cfg = stage.get("outputs", {})
    distance_cfg = stage.get("distance", {})
    names = {
        "network_distance": str(distance_cfg.get("matrix_filename", "cluster_network_distance.parquet")),
        "representative_nodes": str(distance_cfg.get("representatives_filename", "cluster_representative_nodes.csv")),
        "tte_raw": "TTE_raw.parquet",
        "tte_count": str(outputs_cfg.get("count_filename", "TTE_count.parquet")),
        "tte_support": str(outputs_cfg.get("support_filename", "TTE_support.parquet")),
        "tte_hops": str(outputs_cfg.get("hops_filename", "TTE_hops.parquet")),
        "tte_imputed": "TTE_imputed.parquet",
    }
    outputs = {
        name: assert_owned_path(output_dir / filename, output_dir)
        for name, filename in names.items()
    }
    fallback_keys = {
        "network_distance": "network_distance_path",
        "representative_nodes": "representative_nodes_path",
    }
    provided_fallbacks = [key for key in fallback_keys.values() if inputs.get(key)]
    if provided_fallbacks and len(provided_fallbacks) != len(fallback_keys):
        raise ValueError(
            "stage4_tte.inputs network_distance_path and representative_nodes_path "
            "must be provided together"
        )
    if provided_fallbacks and bool(distance_cfg.get("recompute", False)):
        raise ValueError("precomputed TTE distance fallbacks require distance.recompute=false")
    for output_name, input_key in fallback_keys.items():
        source_value = inputs.get(input_key)
        if not source_value:
            continue
        source = Path(source_value)
        if not source.is_file():
            raise FileNotFoundError(f"Configured TTE fallback does not exist: {input_key}={source}")
        destination = outputs[output_name]
        if source.resolve() != destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    summary = run_from_config(values)
    missing = [path.name for path in outputs.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"TTE completed without required outputs: {missing}")
    return StageResult(
        stage="tte",
        status=StageStatus.COMPLETE,
        outputs=outputs,
        metrics={
            "clusters": int(summary["num_clusters"]),
            "od_columns": int(summary["num_od_columns"]),
            "slots": int(summary["num_slots"]),
            "observed_cells": int(summary["num_observed_cells"]),
            "inferred_cells": int(summary["num_inferred_cells"]),
        },
    )
