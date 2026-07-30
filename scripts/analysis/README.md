# Analysis-only tools

These scripts are optional, read-oriented diagnostics and paper/report helpers.
They are not pipeline stages, do not publish data and are never imported by the
`roadnet_partition` package.

- `compare_tte_outputs.py`: compare two explicit TTE result directories.
- `diagnose_demand_spatial_differences.py`: diagnose cross-platform nearest
  segment assignment differences and write an explicit report path.
- `gap_crossday_crosstab.py`: analyze Supply gap/cross-day frequencies.
- `gap_distribution.py`: analyze Supply inter-trip gap distributions.
- `tte_distribution_report.py`: build the documented trip-time distribution
  report and figures.

Run them from the repository root with `conda run -n dydl python ...`. Their
outputs belong under `outputs/reports/` or `outputs/validation/`; none may write a
published `data/processed/<scope>/` tree.
