# Configuration migration workspace

The legacy `config.yaml` remains authoritative for existing entrypoints during
the staged refactor. New configuration fixtures in this directory exercise the
new rule that relative paths are resolved against the containing configuration
file, never against the current working directory.

Production dataset, zoning, and pipeline configurations will be introduced only
when their corresponding business stages migrate.
