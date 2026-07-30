# Artifact cleanup proposal v1

Date: 2026-07-30
Scope: `artifacts/`, `reports/`, `outputs/`, `releases/`
Action taken: audit only; no artifact was deleted or moved.

## Classification

The audit covers 444 files after adding the corrected two-panel PNG/PDF.

| Classification | Files | Scope / rationale |
|---|---:|---|
| paper-final | 8 | `reports/paper/figures/*.{png,pdf}`, including the corrected two-panel figure |
| paper-source | 4 | figure manifest, checksums, raw-order report and raw-order summary table |
| canonical | 0 | Current canonical payload is under `data/processed`, outside this audit scope |
| golden | 11 | Entire `artifacts/golden/beijing-fifth-ring-v1/` tree |
| historical-baseline | 94 | Entire private Windows archive `artifacts/baselines/fifth-ring-windows-v1/` |
| required-validation | 159 | Phase 5, pre-refactor and Phase 9 validation evidence; Phase 9 source/acceptance run is protected |
| reproducible-output | 114 | Non-duplicate Fourth/Fifth Ring figures, graphs and tables |
| duplicate | 34 | 32 exact duplicate aliases under Fourth/Fifth Ring outputs plus two superseded raw-order paper figure copies |
| obsolete | 17 | Retired one-off memory/probe/diagnostic outputs under `outputs/analysis/` |
| unknown | 3 | `reports/figures/tte_trip_time_distribution.{png,pdf}` and `reports/tables/tte_raw_distribution_summary.csv` |

Directory footprint before the two small audit metadata files was approximately 18.55 GB. The largest protected groups are the Windows historical baseline (7.59 GB) and required validation evidence (10.37 GB).

## Duplicate evidence

Exact SHA-256 groups under `outputs/fourth_ring/` and `outputs/fifth_ring/` yield 32 redundant copies if one representative per group is retained. They include:

- 21 identical connector-zoom PNGs;
- three identical Fourth Ring relation-graph PNGs;
- duplicated default/algorithm-specific cluster summaries, road-name diagnostics and evaluation tables;
- duplicated default/algorithm-specific cluster PNG aliases;
- duplicated default/`road_only` graph pickle aliases.

The old `reports/figures/raw_order_trip_time_distribution.png` is pixel-identical to the recovered paper PNG. Its PDF is visually equivalent but has different PDF metadata/backend bytes. Because these are paper images, neither is a direct-delete candidate.

## Safe deletion candidates for a later approved cleanup

Only the following 12 retired, unreferenced one-off JSON results are considered direct candidates (15,938 bytes total):

- `outputs/analysis/block_probe_k4.json`
- `outputs/analysis/block_probe_k8.json`
- `outputs/analysis/block_probe_k16.json`
- `outputs/analysis/step1_logic_verify_report.json`
- `outputs/analysis/step_chunk_full_report.json`
- `outputs/analysis/step_chunk_verify_report.json`
- `outputs/analysis/step_finalize_full_report.json`
- `outputs/analysis/step_midnight_full_report.json`
- `outputs/analysis/step_midnight_verify_report.json`
- `outputs/analysis/step_tau_idle_verify_report.json`
- `outputs/analysis/supply_block_mem_probe_summary.json`
- `outputs/analysis/supply_mem_probe_report.json`

Their producing scripts were retired and the retained refactor documentation records their conclusions. This proposal does not authorize deletion.

## Manual decision required

- All 32 duplicate Fourth/Fifth Ring output aliases: confirm external notebooks or paper source do not reference the alias names before pruning.
- All PNG/PDF files, including duplicate or unknown figures: determine paper usage first.
- `outputs/analysis/gap_*` PNGs and their JSON source reports: preserve together until their diagnostic value is decided.
- The three `unknown` TTE report artifacts: establish producer, paper citation and source data before classification changes.
- Phase 5 and pre-refactor validation trees: retention may be shortened only after the refactor evidence policy is agreed.
- Golden, Windows archive, current canonical data and the Phase 9 acceptance run: retain; never direct-delete.
- `releases/` is absent/empty; there is nothing to clean.
