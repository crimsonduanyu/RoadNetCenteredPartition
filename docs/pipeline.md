# Pipeline

The fixed execution order is:

```text
partition → demand → supply → tte
```

Run the complete pipeline:

```bash
conda run --prefix ./.conda/dydl roadnet-partition run \
  --config configs/pipelines/full.yaml
```

Runs are owned directories under `outputs/runs/<run_id>/`. Resume or replace a
stage without writing the canonical scope directly:

```bash
conda run --prefix ./.conda/dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-dir outputs/runs/<run_id> \
  --from-stage supply --resume
```

Preparation reuse is content-addressed. Its identity combines the exact bytes
of the Preparation YAML with stable path/size/SHA-256 records for the resolved
dataset config and every declared raw input. Rewriting semantically equivalent
YAML with different bytes therefore invalidates the identity. An unchanged
identity and exact output inventory are reused byte-for-byte; a config, input,
manifest, or output mismatch invalidates Preparation and Partition → Demand →
Supply → TTE before any isolated worker starts. Preparation manifests written
before identity support are recomputed once on their first resume. This changes
resume eligibility and manifest metadata, not the formal stage artifact format
or canonical data contract.

Before creating a run directory or starting an isolated worker, the runner
collects one canonical runtime/Git provenance record. Its digests join the base
pipeline fingerprint and optional Preparation identity to form the effective
experiment fingerprint. Exact resume requires identical dependency/native
versions, Git commit, tracked diff bytes, and Git-visible untracked bytes/modes.
A mismatch invalidates every formal stage from Partition (and Preparation when
configured), even if `--from-stage` requests a later stage. Dirty execution
requires `--allow-dirty` and is byte-addressed rather than merely path-listed.

Run manifest schema 2 stores full records and structured mismatch reasons.
Schema-1 manifests remain readable, but missing historical runtime/Git
provenance cannot be reconstructed: resume performs a complete one-time
invalidation/recomputation, and publish/export reject incomplete provenance.

Standalone commands are available for `partition`, `demand`, `supply`, and
`tte`; each requires its split YAML configuration.

Preparation's relation graph travels between stages as `SafeGraphArtifactV1`
(`.graph.json.gz`), the only supported graph interchange format. Partition and
Evaluation refuse a `.gpickle`, `.pkl`, or `.pickle` input by name before they
create an output directory, and a manifest that declares one is refused before
resume, publish, or export touches anything — so a legacy run leaves no partial
output, marker, or bundle. Nothing converts a legacy artifact; re-run
Preparation. See `docs/data.md`.

Pipeline and standalone Supply use the same file adapter for Demand's
`orders_region_assigned.csv.gz`. Textual `order_id`/`driver_id` values are not
coerced to integers, so legacy numeric identifiers remain accepted as text and
leading zeros are retained. Invalid null or blank drivers fail before Supply
creates output files or starts block computation.

## Zoning evaluation diameter

`mean_network_diameter_m` and the related diameter fields now use exact weighted
all-pairs shortest paths, computed by streaming one NetworkX Dijkstra result per
source. An edge costs `max((length_u + length_v) / 2, 1e-9)` metres, preserving
the established segment-node length model and zero-length EPS fallback. Each
connected component is evaluated separately; a disconnected cluster reports the
maximum finite component diameter, and a singleton component reports `0.0`.
Negative, missing-record, NaN, or infinite segment lengths and directed graphs
are rejected. A graph node absent from the supplied length mapping retains the
legacy zero/EPS fallback; an empty partition keeps the existing NaN aggregate.

Evaluation CSVs record diameter metric version 2, the exact algorithm, and the
weight semantics. Earlier evaluation files used a double-sweep approximation,
may underestimate cyclic graphs, and must not be mixed numerically with new
exact results. Exact evaluation is deterministic across hash and insertion order
but costs one weighted shortest-path traversal per source (roughly all-pairs
Dijkstra); it is intended for evaluation/decision workloads, not inner-loop
clustering optimization.

Validation writes run-owned evidence unless `--report` is explicitly provided:

```bash
conda run --prefix ./.conda/dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden /path/to/external/golden
```

Publishing does not rerun algorithms. It validates and transactionally swaps a
complete scope into `data/processed/<scope>/`:

```bash
conda run --prefix ./.conda/dydl roadnet-partition publish \
  --run outputs/runs/<run_id> \
  --scope fifth_ring --overwrite \
  --baseline-decision configs/policies/fifth_ring_linux_canonical_v1.yaml \
  --dry-run
```

Dataset, stage, pipeline, manifest, and publish scopes are directory identifiers,
not paths. A scope is one NFC-normalized component beginning with a Unicode
letter or number and containing only Unicode letters/numbers plus `.`, `_`, and
`-`. Empty/whitespace values, separators, traversal, Windows drive/UNC forms,
and trailing dots are rejected rather than rewritten. Publish additionally
requires the resolved target and staging directories to be direct children of
the exact `<project_root>/data/processed` ownership root before disk probing or
copying begins.

Generated diagnostics belong under `outputs/reports/`; validation campaigns
belong under `outputs/validation/`. Reproduction packages are destructive-swap
targets and therefore belong only under the controlled external sibling root
`<project-directory-name>-releases/`, never inside the project. Diagnostics and
validation products are disposable and Git-ignored.
