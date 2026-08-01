"""Low-invasive, disableable stage timing for the Demand stage (Batch 2A).

Gated by ``ROADNET_DEMAND_TIMING`` (off by default -> zero production cost and no
output change). When enabled, records per-call wall-clock durations per phase
(cumulative / count / mean / P50 / P95 / max), per-chunk row counts and per-chunk
phase durations (to detect degradation across chunks), the spatial-index rebuild
count (whether the STRtree is built fresh per call vs reused), the SQLite staging
file peak size, and process read/write bytes via /proc/self/io. It never records
order-level information; the summary is printed to stderr.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import os
import sys
import time
from typing import Iterator

_ENV_VAR = "ROADNET_DEMAND_TIMING"
_TRUTHY = {"1", "true", "yes", "on"}


def _quantile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


class StageTimer:
    """Accumulates per-call wall-clock durations per phase. A no-op when disabled."""

    def __init__(self, name: str, enabled: bool) -> None:
        self.name = name
        self.enabled = enabled
        self._calls: dict[str, list[tuple[int, float]]] = {}  # phase -> [(chunk, secs)]
        self._counters: dict[str, int] = {}
        self._chunks: list[tuple[str, int, int]] = []  # (kind, chunk_index, rows)
        self._metrics: dict[str, int | float | str] = {}
        self._sqlite_peak_bytes = 0
        self._wall_start = time.perf_counter() if enabled else None
        self._wall_end = None
        self._io_start = self._read_io() if enabled else None
        self._io_end = None

    @property
    def chunk(self) -> int:
        return self._chunks[-1][1] if self._chunks else -1

    def set_chunk(self, index: int, rows: int, kind: str = "order") -> None:
        if self.enabled:
            self._chunks.append((kind, index, rows))

    def count(self, label: str, n: int = 1) -> None:
        if self.enabled:
            self._counters[label] = self._counters.get(label, 0) + n

    def metric(self, label: str, value: int | float | str) -> None:
        if self.enabled:
            self._metrics[label] = value

    def record(self, label: str, seconds: float, chunk: int | None = None) -> None:
        if self.enabled:
            self._calls.setdefault(label, []).append((self.chunk if chunk is None else chunk, seconds))

    def record_sqlite_size(self, path) -> None:
        if not self.enabled:
            return
        try:
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size > self._sqlite_peak_bytes:
                self._sqlite_peak_bytes = size
        except OSError:
            pass

    @contextlib.contextmanager
    def phase(self, label: str, chunk: int | None = None) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        yield
        secs = time.perf_counter() - start
        self.record(label, secs, chunk=chunk)

    def _read_io(self):
        try:
            with open("/proc/self/io", encoding="utf-8") as handle:
                vals = {}
                for line in handle:
                    key, _, val = line.partition(":")
                    vals[key.strip()] = int(val)
                return vals
        except OSError:
            return None

    def finalize_io(self) -> None:
        if self.enabled:
            self._io_end = self._read_io()
            self._wall_end = time.perf_counter()

    def to_profile(self) -> dict:
        """Serializable profile dict (no order/driver/coordinate/row-level data)."""
        phases = []
        for label, calls in self._calls.items():
            secs = [s for _, s in calls]
            ss = sorted(secs)
            phases.append({"phase": label, "calls": len(secs), "total_s": sum(secs),
                           "mean_s": sum(secs) / len(secs) if secs else 0.0,
                           "p50_s": _quantile(ss, 0.5), "p95_s": _quantile(ss, 0.95),
                           "max_s": max(secs) if secs else 0.0,
                           "chunk_seconds": [{"index": i, "seconds": s} for i, s in calls]})
        phases.sort(key=lambda r: -r["total_s"])
        phase_sum = sum(sum(seconds for _, seconds in calls) for calls in self._calls.values())
        stage_wall = None if self._wall_start is None or self._wall_end is None else self._wall_end - self._wall_start
        unclassified = None if stage_wall is None else stage_wall - phase_sum
        return {
            "name": self.name, "phases": phases, "counters": dict(self._counters),
            "metrics": dict(self._metrics),
            "stage_wall_s": stage_wall,
            "phase_sum_s": phase_sum,
            "unclassified_s": unclassified,
            "unclassified_pct": None if stage_wall in (None, 0) else 100.0 * unclassified / stage_wall,
            "chunks": [{"kind": kind, "index": i, "rows": r} for kind, i, r in self._chunks],
            "sqlite_peak_bytes": self._sqlite_peak_bytes,
            "proc_io": {"start": self._io_start, "end": self._io_end},
        }

    def write_profile(self, path) -> None:
        """Write the profile JSON to a run-owned path (no PII)."""
        if not self.enabled:
            return
        import json
        from pathlib import Path
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_profile(), indent=2), encoding="utf-8")

    def report(self) -> None:
        if not self.enabled or not self._calls:
            return
        print(f"[stage-timing:{self.name}]", file=sys.stderr)
        # Per-phase stats sorted by cumulative time (rank by measured wall time).
        rows = []
        for label, calls in self._calls.items():
            secs = [s for _, s in calls]
            total = sum(secs)
            ss = sorted(secs)
            rows.append((label, len(secs), total, total / len(secs) if secs else 0.0,
                         _quantile(ss, 0.5), _quantile(ss, 0.95), max(secs)))
        rows.sort(key=lambda r: -r[2])
        print(f"  {'phase':24s} {'calls':>7s} {'total':>10s} {'mean':>10s} {'p50':>10s} {'p95':>10s} {'max':>10s}", file=sys.stderr)
        for label, n, total, mean, p50, p95, mx in rows:
            print(f"  {label:24s} {n:7d} {total:9.3f}s {mean:9.4f}s {p50:9.4f}s {p95:9.4f}s {mx:9.4f}s", file=sys.stderr)
        if self._wall_start is not None and self._wall_end is not None:
            phase_sum = sum(total for _, _, total, _, _, _, _ in rows)
            stage_wall = self._wall_end - self._wall_start
            unclassified = stage_wall - phase_sum
            print(
                f"  stage_wall={stage_wall:.3f}s phase_sum={phase_sum:.3f}s "
                f"unclassified={unclassified:.3f}s ({100.0 * unclassified / stage_wall:.2f}%)",
                file=sys.stderr,
            )
        if self._counters:
            print(f"  counters: {dict(self._counters)}", file=sys.stderr)
        if self._chunks:
            print(f"  chunks: {len(self._chunks)} chunks, rows per chunk: {[r for _, _, r in self._chunks[:10]]}{'...' if len(self._chunks) > 10 else ''}", file=sys.stderr)
            # Per-chunk degradation for the spatial/sqlite phases.
            for label in (
                "csv_parse", "point_construction", "spatial_index_build", "nearest_query", "sqlite_append",
                "export_join_fetch", "export_frame_build", "export_datetime_format", "export_other_format",
                "export_csv_serialize", "export_gzip_compress_write",
            ):
                if label in self._calls:
                    by_chunk = {}
                    for c, s in self._calls[label]:
                        by_chunk.setdefault(c, []).append(s)
                    series = [sum(v) for c, v in sorted(by_chunk.items())]
                    if len(series) > 1:
                        print(f"  per-chunk {label}: first={series[0]*1e3:.2f}ms last={series[-1]*1e3:.2f}ms max={max(series)*1e3:.2f}ms", file=sys.stderr)
        print(f"  sqlite_peak_bytes: {self._sqlite_peak_bytes:,}", file=sys.stderr)
        if self._io_start and self._io_end:
            rb = self._io_end.get("read_bytes", 0) - self._io_start.get("read_bytes", 0)
            wb = self._io_end.get("write_bytes", 0) - self._io_start.get("write_bytes", 0)
            print(f"  proc_io: read_bytes={rb:,} write_bytes={wb:,}", file=sys.stderr)


class _TimedGzipFile(gzip.GzipFile):
    def __init__(self, *args, **kwargs):
        self.write_seconds = 0.0
        self.uncompressed_bytes = 0
        super().__init__(*args, **kwargs)

    def write(self, data):
        start = time.perf_counter()
        try:
            return super().write(data)
        finally:
            self.write_seconds += time.perf_counter() - start
            self.uncompressed_bytes += len(data)


@contextlib.contextmanager
def open_timed_gzip_text(path, timer: StageTimer):
    """Open the existing gzip text stream, exposing compression-only timing."""
    if not timer.enabled:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            yield handle, None
        return

    with timer.phase("export_flush_close"):
        raw = _TimedGzipFile(filename=path, mode="wb")
        handle = io.TextIOWrapper(raw, encoding="utf-8", newline="")
    try:
        yield handle, raw
    finally:
        before = raw.write_seconds
        start = time.perf_counter()
        try:
            handle.flush()
        finally:
            handle.close()
        elapsed = time.perf_counter() - start
        timer.record("export_gzip_compress_write", raw.write_seconds - before)
        timer.record("export_flush_close", max(0.0, elapsed - (raw.write_seconds - before)))


_ACTIVE: StageTimer | None = None


def _is_enabled() -> bool:
    return os.environ.get(_ENV_VAR, "").strip().lower() in _TRUTHY


def reset(name: str = "demand") -> StageTimer:
    global _ACTIVE
    _ACTIVE = StageTimer(name, _is_enabled())
    return _ACTIVE


def collect_sqlite_evidence(connection) -> dict:
    """Read-only SQLite evidence for the Demand staging DB. Captures PRAGMA
    settings (no modifications) and EXPLAIN QUERY PLAN for the three main
    queries. Profiling only; never changes a SQLite setting, schema, or index."""
    if not _is_enabled():
        return {}
    evidence = {"pragmas": {}, "explain": {}, "sqlite_version": "", "index_build_times_s": {}}
    try:
        import sqlite3
        evidence["sqlite_version"] = sqlite3.sqlite_version
        for pragma in ("journal_mode", "synchronous", "temp_store", "cache_size", "page_size", "mmap_size", "automatic_index"):
            try:
                row = connection.execute(f"PRAGMA {pragma}").fetchone()
                evidence["pragmas"][pragma] = None if row is None else row[0]
            except Exception as exc:
                evidence["pragmas"][pragma] = f"error: {exc}"
        queries = {
            "service_label_ordered_select": "SELECT stage_id, driver_id, departure_time_ns, finish_time_ns FROM staged_orders ORDER BY driver_id, departure_time_ns, finish_time_ns, stage_id",
            "cluster_od_groupby": "SELECT o.slot_start_ns, o.origin_cluster_id, o.destination_cluster_id, l.service_type, COUNT(*) AS c FROM staged_orders o JOIN service_labels l ON o.stage_id = l.stage_id GROUP BY o.slot_start_ns, o.origin_cluster_id, o.destination_cluster_id, l.service_type",
            "assigned_orders_export": "SELECT o.stage_id, o.driver_id, o.departure_time_ns, o.finish_time_ns, o.origin_cluster_id, o.destination_cluster_id, l.service_type FROM staged_orders o JOIN service_labels l ON o.stage_id = l.stage_id ORDER BY o.stage_id",
        }
        for name, sql in queries.items():
            try:
                plan = connection.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
                evidence["explain"][name] = [list(row) for row in plan]
            except Exception as exc:
                evidence["explain"][name] = f"error: {exc}"
    except Exception as exc:
        evidence["error"] = str(exc)
    return evidence


def get_active_timer() -> StageTimer:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = StageTimer("demand", _is_enabled())
    return _ACTIVE
