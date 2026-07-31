# GitHub history audit v1 (regenerated 2026-07-31)

Method: `git rev-list --objects --all` (1,398 path entries, 637 blobs),
`git cat-file --batch-check` size enumeration, and content scans of every
blob for private identifiers, credentials, local paths, and proxy markers.

## 1. Blob sizes

- Blobs > 100 MiB: **0**
- Blobs > 50 MiB: **0**
- Blobs > 10 MiB: **0**
- Largest blob: 6,857,838 B (`artifacts/paper/figures/partition_and_mean_hourly_orders.pdf`)
- No GitHub 100 MiB limit risk from any blob.

## 2. Embedded private data (required purge)

`tmp.ipynb` (scratch notebook) contains **real private Beijing driver/order
identifiers in executed notebook outputs** (e.g. `562950053808583`,
`order_17593028586427`, `carpool_562950053809475_1`, trip dates/times from
`driver_chains.csv.gz`, `idle_windows.csv.gz`, `trip_segments.csv.gz`).

- Blobs containing private IDs:
  - `a77df15000100952e02f9991e5bca6c7c97c8c03`
  - `79bedf26cc043342e1dfb11e461bf8924438ae29`
- Path history: introduced at `e6fb93d` (`出五环结果`), present through
  `97de198`/`7b8c0ca`/`c66fc20`, removed from the tree at `d48af1a`
  (`RoadNet: remove final scratch notebook`).
- Action: remove `tmp.ipynb` from all history with `git filter-repo`
  (never `filter-branch`). Blocked on explicit authorization; no rewrite has
  been performed.

## 3. Local machine / user paths in history (recommended purge)

| Path | Content | Introduced | Removed |
| --- | --- | --- | --- |
| `.claude/settings.local.json` | `C:\Users\Administrator\miniconda3\...`, Bash rules, `additionalDirectories` with Windows user paths; no tokens found | `80deea6` | `10c86e6` (now gitignored, not in index) |
| `docs/refactor/pre-refactor-v1/pytest.txt` | `/home/dy/jupyter/workspace/RoadNetCenteredPartition/src/lib/...` | `ed510d9` | `eb89081` |
| `docs/refactor/pre-refactor-v1/repository.json` | `"executable": "/home/dy/miniconda3/envs/dydl/bin/python"` | `ed510d9` | `eb89081` |
| `regularized_zoning_experiments/EXPERIMENT_PLAN.md` | `C:\Users\Administrator\miniconda3\envs\bj_road_partition\python.exe ...` | `f7eaeca` | `5144b1f` |
| `regularized_zoning_experiments/EXPERIMENT_TRACKER.md` | same Windows user paths | `f7eaeca` | `5144b1f` |

## 4. Notebooks without private values

- `tests/Make_Beijing_TTE.ipynb` (introduced `97de198`, removed `b6f379e`):
  contains column-name references (`driver_id`, `order_id`) and
  `data\processed\...` path strings in code, but no embedded private values.
  Optional cleanup path for hygiene.

## 5. Never committed

- `data/raw/**`, `data/processed/**`, `data/interim/**`: **no paths or blobs
  in history**.
- Windows archive payloads (orders/drivers/coords): none.
- API keys, tokens, passwords, SSH/private keys: none found. The only
  `api_key`/`password` matches are in `tests/test_phase6a_boundaries.py`
  (forbidden-field name list) and in this audit's own scan text.
- Proxy configuration (e.g. `127.0.0.1:7890`): none found in history.

## 6. Current tracked worktree

- 125 tracked files, 919,469 bytes total; largest tracked file
  `src/roadnet_partition/downstream/supply.py` (39,866 B).
- No tracked file > 1 MiB; no private payloads; no `/home/dy`, `miniconda`,
  Windows user paths, or proxy configuration in tracked content.
- Two benign matches for the word `proxy`:
  `scripts/analysis/gap_distribution.py` (comments) and
  `tests/test_phase6a_boundaries.py` (forbidden-name list).
- `.claude/settings.local.json` is ignored (`.gitignore`) and not in the
  index; `artifacts/` files are deleted from the worktree (pending commit).

## 7. Verdict

History rewrite is **required before GitHub publication**: purge `tmp.ipynb`
(embedded private driver/order data). Recommended, lower priority: remove
`.claude/settings.local.json`, `docs/refactor/pre-refactor-v1/{pytest.txt,
repository.json}`, `regularized_zoning_experiments/EXPERIMENT_PLAN.md`,
`regularized_zoning_experiments/EXPERIMENT_TRACKER.md`, and optionally
`tests/Make_Beijing_TTE.ipynb`.

- Tool: `git filter-repo` only, with explicit authorization; `filter-branch`
  must not be used.
- No push may occur until the rewrite is completed and re-audited.
- Blob SHAs to target: `a77df15000100952e02f9991e5bca6c7c97c8c03`,
  `79bedf26cc043342e1dfb11e461bf8924438ae29` (tmp.ipynb), plus all historical
  blobs of the paths listed in sections 3–4.
