# Production split configuration

`configs/pipelines/full.yaml` is the recommended fixed four-stage entrypoint.
All relative paths resolve against the file containing them, never the current
working directory.

Normal production inputs live under `data/`. Partition initialization and its
canonical expected contract are explicitly versioned under
`artifacts/golden/beijing-fifth-ring-v1/`. Demand, Supply, and TTE receive
same-run upstream outputs through fixed runtime bindings; their configured
`data/processed/` paths are standalone fallbacks only.

The pre-refactor unified configuration is preserved byte-for-byte at
`configs/legacy/config.pre-refactor.yaml` for historical audit only. It has no
active runtime reader.
