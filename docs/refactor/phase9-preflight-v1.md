# Phase 9 preflight v1

Recorded at `2026-07-29T17:59:29Z` before the formal Phase 9 pipeline run.
The machine-readable, ignored record is
`outputs/refactor-validation/phase9/preflight/preflight.json`. No formal data
or Golden payload was modified while producing this inventory.

## Frozen source state

- Git commit: `9706e94b5530b36ff639d2b75600a825114439f8`
- tracked worktree: clean
- pipeline config fingerprint: `02fce2f7318c690806ed93672a7e26d06bcfa116b8e0a709ac0cc213f551c97d`
- Partition config fingerprint: `fc46b025656f54bf9ea012f01b4babac64927f3b4f0c26bc61e773c0c9dcc05d`
- Demand config fingerprint: `1349138962a5eb6d53d203539bf83dcfddfaf9ef06d47b73c353e30bb1d39a67`
- Supply config fingerprint: `b38df21528ffc184772834e0c45bcd90576850c47b1da8baeda7310635c8f35d`
- TTE config fingerprint: `ea567241a1fb5ca83ca22b9b19c1fc38729305333572ecf84455a35828174034`

The current `data/processed/fifth_ring/` inventory contains 91 files totaling
7,588,535,926 bytes. Its stable path/size/content inventory hash is
`0815dc61d0e8ac950229372a4b0f7fae76adae8c410a1535328c112c8fad9540`.
The ignored JSON contains each relative path, size and SHA-256, but no private
row-level content.

Golden v1 metadata was frozen as:

- `manifest.json`: `a545e9c316899be271399ab2c3002c07f05925d6a322abcd9cf29f7f86327962`
- `checksums.sha256`: `22bccddd31ec007015f8f1476acc26c1d5698910fb995b14809d9be34e3219db`

## Runtime and capacity

- Linux `7.0.0-28-generic`, x86-64
- Python `3.12.13`
- Ryzen 5 5600G, 6 cores / 12 threads
- memory: 60,554,400 kB total; 55,471,616 kB available
- swap: 8,388,604 kB total; 7,886,440 kB free
- disk: 488,420,507,648 bytes total; 260,106,989,568 bytes free
- GeoPandas `1.1.3`, Shapely `2.1.2`, GEOS `3.14.1`
- pyproj `3.7.2`, PROJ `9.7.1`
- pandas `3.0.3`, NumPy `2.4.6`, SciPy `1.18.0`
- NetworkX `3.6.1`, PyArrow `24.0.0`

## Clustering runtime gate

`environment.yml` declares Python 3.11, `python-igraph`, `leidenalg`,
`pymetis`, and pip package `python-louvain`; the active environment uses Python
3.12.13. Before installation all four clustering packages were absent. A pip
dry-run showed only the requested packages and their direct `igraph`/
`texttable` support packages, with no unrelated scientific upgrades.

Installed versions are `python-louvain 0.16`, `python-igraph 1.0.0`,
`igraph 1.0.0`, `leidenalg 0.12.0`, and `pymetis 2025.2.2`. The project runners
for Louvain, Leiden and METIS were then called directly on a fixed connected
graph with seed 42. All returned two complete connected clusters and repeated
deterministically. The real-runtime smoke test passed without adapters or
monkeypatching.

## Baseline references

- Phase 0: `docs/refactor/pre-refactor-v1/semantic-baseline.json`
- Phase 5A: `docs/refactor/demand-full-validation-v1.md`
- Phase 5B: `docs/refactor/supply-full-validation-v1.md`
- Phase 5C: `docs/refactor/tte-full-validation-v1.md`

The historical Windows Demand baseline remains distinct from the Linux Phase
5A baseline. This preflight does not approve Linux Demand as canonical and does
not authorize a real publish.
