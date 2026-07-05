# Raw Order Trip-Time Distribution Report

## Data

- Input: `C:/Users/Administrator/Desktop/RoadNetCenteredPartition/data/raw/beijing_orders_2017-06_2017-08.csv`
- Scope: original order rows with parseable `departure_time` and `finish_time`, using positive order-level trip times only.
- Formula: `trip_time = (finish_time - departure_time).total_seconds() / 60`.
- Exclusions: no spatial clipping, no fifth-ring matching, no OD-slot aggregation, no imputation.

## Data Quality

- Raw rows: 90,105,866.
- Valid timestamp rows: 90,105,866; invalid timestamp rows: 0 (0.0000% of raw rows).
- Positive trip-time rows used for main statistics: 90,036,672 (99.9232% of raw rows).
- Non-positive trip-time rows excluded: 69,194 (0.0768% of valid timestamp rows).
- Trips inside the Stage 4 comparison band `[3, 80]` min: 88,787,256 (98.6123% of positive trips).
- Positive trips above 120 min: 111,720 (0.1241% of positive trips; outside the plotted range).

## Key Statistics

- Median trip time: **21.00 min**.
- 99th percentile trip time: **81.35 min**.
- Mean / std: 25.22 / 16.70 min.
- P1 / P25 / P75: 4.95 / 14.17 / 31.77 min.
- P90 / P95 / P99.9: 45.80 / 56.28 / 125.02 min.
- Min / max among positive trips: 0.02 / 5619.25 min.

## Distribution Insight

The raw order-level trip-time distribution is right-skewed: the median is 21.00 min, while the upper 1% starts at 81.35 min. The old processed-matrix result is not comparable as a primary statistic because it was computed after spatial matching, clipping, and OD-slot median aggregation.

## Artifacts

- Summary CSV: `C:/Users/Administrator/Desktop/RoadNetCenteredPartition/reports/tables/raw_order_trip_time_distribution_summary.csv`
- Histogram PDF: `C:/Users/Administrator/Desktop/RoadNetCenteredPartition/reports/figures/raw_order_trip_time_distribution.pdf`
- Histogram PNG: `C:/Users/Administrator/Desktop/RoadNetCenteredPartition/reports/figures/raw_order_trip_time_distribution.png`
