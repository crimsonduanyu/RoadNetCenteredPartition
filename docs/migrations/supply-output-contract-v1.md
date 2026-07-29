# Supply output contract v1

This records filenames and repository evidence before old Supply names are
eventually retired. It does not delete files or create duplicate compatibility
outputs.

## Current formal publish allowlist

| File | Current writer | Current repository readers/tests | Publish decision |
|---|---|---|---|
| `supply_inservice_od.csv.gz` | `src/lib/supply.py:run_chunked_pipeline` | `src/lib/supply.py`, `tests/test_supply_pipeline.py`, `scripts/analysis/step_finalize_full.py` | include |
| `supply_available_floor.csv.gz` | `src/lib/supply.py:run_chunked_pipeline` | `src/lib/supply.py`, `tests/test_supply_pipeline.py`, `scripts/analysis/step_finalize_full.py` | include |
| `supply_fleet_lower_bound.csv.gz` | `src/lib/supply.py:run_chunked_pipeline` | Supply analysis scripts and historical notebook | include |
| `run_summary.json` | `src/lib/supply.py:run_chunked_pipeline` | baseline audit and analysis scripts | include |
| `config_used.json` | `src/lib/supply.py:run_pipeline` | provenance inspection; no production parser found | include |

`run.log` is a run diagnostic and is not a published Supply data product.
`supply_demand_merged.csv.gz` is written only when the optional legacy merge
flag is enabled; Stage 3 does not enable it and it is not in the formal
allowlist.

## Historical legacy and intermediate files

| File | Current writer | Repository references | Classification / publish decision |
|---|---|---|---|
| `supply_in_service_od.csv.gz` | none | old `step_*` analyses and `tmp.ipynb` | legacy name; exclude |
| `supply_available_by_cluster.csv.gz` | none | old `step_*` analyses and `tmp.ipynb` | legacy name; exclude |
| `trip_segments.csv.gz` | none | `tmp.ipynb` | historical intermediate; exclude |
| `driver_chains.csv.gz` | none | `tmp.ipynb` | historical intermediate; exclude |
| `idle_windows.csv.gz` | none | `tmp.ipynb` | historical intermediate; exclude |
| `run_summary.partial.json` | none | no live reader found | historical partial status; exclude |

The current chunked implementation deliberately does not persist trip segments,
driver chains, or idle windows because their object columns were a principal
memory/storage cost. A failed current run does not write a partial summary, and
a historical partial summary must never satisfy a completed Supply contract.

Historical scripts remain evidence of earlier Supply corrections; Phase 5B
does not rewrite them or reintroduce their old filenames into the formal API.
Repository-external consumers of every current or historical filename remain
unknown and must be confirmed manually by the user before final deletion or
publication decisions. Long-term compatibility does not require generating two
sets of filenames.
