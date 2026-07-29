# TTE package boundary audit v1

Phase 5C keeps one implementation in
`roadnet_partition.downstream.tte`, one output validator in
`roadnet_partition.downstream.tte_contracts`, and the existing static
network-distance implementation in `roadnet_partition.graphs.distance`.
This audit does not change TTE behavior or data.

## Import direction

- The new package contains no `lib`, `src`, or legacy-stage import and does not
  mutate `sys.path`.
- `downstream.tte` imports `graphs.distance`; `graphs.distance` does not import
  `downstream.tte`, so that edge is one-way.
- `downstream.tte` does not import Demand, Supply, or the public CLI.
- `downstream.tte_contracts` does not import the pipeline layer or public CLI.
- `src/stages/stage4_tte.py` has exactly one project import:
  `roadnet_partition.downstream.tte`. It has no path injection.
- Both TTE modules import from outside the repository after editable install.

These rules are executable in `tests/test_package_import_boundaries.py`; the
existing all-package import test also exercises runtime cycle detection.

## Serialization and module paths

The TTE implementation, contract, distance module, Stage 4 wrapper, and legacy
bridge contain no `pickle`, `joblib`, `cloudpickle`, or `dill` import, no
dynamic import call, and no custom pickle state/reducer hook. TTE persists only
the documented Parquet matrices and representative-node CSV; it does not
serialize `SpatialPruner` or another custom Python object.

A tracked-source search found no production serializer or dynamic module lookup
that names `lib.tte_dataset`, `roadnet_partition.downstream.tte`, or
`SpatialPruner`. Migration tests load source files by path only to compare the
old and new entry points. Other references are ordinary source imports, tests,
documentation, configuration comments, and notebook code. The temporary
`src/lib/tte_dataset.py` bridge re-exports the authoritative objects without
copying implementation, but no TTE pickle compatibility requirement was
identified.

The static test rejects future serializer imports, dynamic import calls, or
custom reducer/state hooks in this TTE boundary. Repository graph pickles are
unrelated zoning artifacts and remain outside this audit.
