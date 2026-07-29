# Deferred validation gates

## Gate before Phase 9 acceptance

The current `dydl` environment does not contain `python-louvain`,
`python-igraph`/`leidenalg`, or `pymetis`, although `environment.yml` declares
them. Phase 4 validated the Louvain, Leiden and METIS adapters with fixed
deterministic fixtures, including argument, node/edge order, weight and seed
handling, but did not execute the real third-party implementations.

Before Phase 9 full-pipeline acceptance, create or synchronize an environment
that matches `environment.yml` and run real baseline smoke tests for Louvain,
Leiden and METIS. This gate does not block Phase 5A and Phase 5A must not install,
upgrade or otherwise change these dependencies.
