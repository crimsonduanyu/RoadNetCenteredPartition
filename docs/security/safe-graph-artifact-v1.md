# SafeGraphArtifactV1 — Graph Exchange Contract (AUD-005)

Remediation batch **R5.1**, amended by **R5.2**. This document is the audit
record produced by R5.1 Gate A and the normative contract implemented by its
Gates B–E.

Sections 1 and 1.1 describe the repository **as it was before R5.1**; they are
retained as the audit finding, not as a description of current code. Section 4
was rewritten by R5.2, which deleted the temporary legacy-pickle conversion
path entirely: the current version cannot read, convert, or execute a graph
pickle by any route.

## 1. Problem statement

The segment relation graph is exchanged between pipeline stages as a Python
pickle (`*.gpickle`). Every public consumer calls `pickle.load` on a path that
is supplied by configuration, CLI argument, or a downloaded reproduction
bundle. `pickle.load` reconstructs arbitrary objects and therefore executes
attacker-controlled code **before** any type or attribute validation can run.

### 1.1 Current pickle call sites (audited on `audit/full-repo-20260804`, HEAD `d4f5cc9`)

| Role | File | Line | Statement |
| --- | --- | --- | --- |
| Producer | `src/roadnet_partition/pipeline/preparation.py` | 50 | `"graph": "segment_relation_graph_road_poi_order.gpickle"` |
| Producer | `src/roadnet_partition/pipeline/preparation.py` | 477 | `pickle.dump(graph, handle)` |
| Consumer | `src/roadnet_partition/zoning/partition.py` | 87 | `graph = pickle.load(handle)` |
| Consumer | `src/roadnet_partition/zoning/evaluate.py` | 40 | `graph = pickle.load(handle)` |
| Consumer | `src/roadnet_partition/reporting/best_partition_map.py` | 43 | `graph = pickle.load(handle)` |

Script entrypoints that resolve `.gpickle` inputs for those consumers:

- `scripts/figures/best_partition_maps.py` (lines 30, 38)
- `scripts/figures/partition_order_panels.py` (line 27)

Configuration defaults that point at the pickle artifact:

- `configs/zoning/regularized.yaml` (`inputs.graph`)
- `configs/legacy/config.pre-refactor.yaml` (legacy path block)

Test modules also read/write pickles, but only fixtures they created inside
`tmp_path`; those are controlled inputs and are out of scope for the
"public path" requirement.

### 1.2 Safe reproduction (Gate A)

A disposable temporary directory was created outside the repository. Inside it,
a pickle was written whose `__reduce__` hook calls a function that only writes a
harmless text marker **into that same temporary directory**. The artifact was
then handed to each of the three public loaders in turn.

Observed result:

```
[partition]           marker_before_load=False marker_after_load=True loader_error=AttributeError: 'str' object has no attribute 'nodes'
[evaluate]            marker_before_load=False marker_after_load=True loader_error=AttributeError: 'str' object has no attribute 'nodes'
[best_partition_map]  marker_before_load=False marker_after_load=True loader_error=AttributeError: 'str' object has no attribute 'nodes'
```

The marker exists after every load even though each loader subsequently rejects
the object. This proves the side effect happens **inside `pickle.load`**, i.e.
strictly before the graph-shape checks (`graph.nodes` access / relabelling) that
follow it. Defensive checks placed after deserialization cannot mitigate the
issue; the deserializer itself must be replaced.

The disposable directory and all markers were deleted immediately after the
reproduction. No malicious artifact is committed to this repository.

## 2. Actual graph contract

Determined empirically by building a graph through
`preparation._build_relation_graph` with the fixtures from
`tests/test_preparation_relation_graph.py`, and by grepping every attribute
access in `src/`.

### 2.1 Container

| Property | Value |
| --- | --- |
| Class | `networkx.Graph` |
| Directed | `False` |
| Multigraph | `False` |
| Graph-level attributes | none (`{}`) |
| Self-loops | impossible — `graphs.relations.canonical_pair` rejects `a == b` |
| Parallel edges | impossible — edges are canonical unordered pairs |
| Node identifier type | `str` (segment id, e.g. `"s1"`) |

### 2.2 Node attributes

| Attribute | Type | Nullable |
| --- | --- | --- |
| `u` | int | no |
| `v` | int | no |
| `highway` | str | no |
| `name` | str | yes (`None` / NaN when absent) |
| `osmid` | int | yes |
| `length` | float | no |
| `bearing` | float | no |
| `segment_role` | str | no |

### 2.3 Edge attributes

| Attribute | Type | Nullable |
| --- | --- | --- |
| `relation_types` | str (pipe-joined, sorted) | no |
| `has_direct`, `has_continuity`, `has_connector` | bool | no |
| `same_osmid`, `same_name`, `same_highway` | bool | no |
| `direct_weight`, `continuity_weight`, `connector_weight` | float | no |
| `poi_weight`, `order_weight`, `base_weight`, `weight` | float | no |
| `continuity_score`, `poi_similarity`, `order_similarity` | float | no |
| `angle_diff` | float | yes (`None`) |
| `connector_count` | int | no |
| `connector_ids`, `connector_highways` | str (pipe-joined, sorted) | no |

### 2.4 What downstream code actually consumes

- Edge weights: `weight`, `continuity_weight`, `connector_weight`
  (`zoning/objective.py`, `zoning/leiden.py`, `zoning/search.py`,
  `zoning/skater.py`).
- Node attribute: `length` (`zoning/adaptive.py`, `zoning/metis.py`).
- Topology only: `zoning/evaluate.py`, `zoning/metrics.py`,
  `zoning/adaptive.py`, `zoning/skater.py`, `zoning/search.py`.

`zoning/metrics.exact_network_diameter` derives edge distances from the
*node* `length` attribute, not from edge attributes, so preserving node ids,
node `length`, and topology is sufficient to keep the R4.1 exact diameter and
all connectivity metrics bit-identical.

The schema above is closed: every field is a JSON scalar (`str`, `int`,
`float`, `bool`) or `null`. No Python object, set, tuple, ndarray, or
timestamp appears in the graph, so a JSON-based encoding is lossless.

## 3. Selected format: `SafeGraphArtifactV1`

| Aspect | Decision |
| --- | --- |
| Container | gzip-compressed UTF-8 JSON |
| File extension | `.graph.json.gz` |
| Logical artifact name | `segment_relation_graph_road_poi_order` (unchanged) |
| New dependency | none (`json` + `gzip` from the standard library) |
| Schema identifier | `SafeGraphArtifactV1` |

### 3.1 Determinism

The writer produces byte-identical output for equal graphs:

- nodes sorted by identifier, edges sorted by canonical `(min, max)` pair;
- attribute keys sorted, `json.dumps(..., sort_keys=True)`;
- fixed gzip compression level and `mtime=0` in the gzip header;
- non-finite floats are rejected rather than emitted as `NaN`/`Infinity`;
- written to a temporary file in the destination directory and atomically
  renamed into place.

### 3.2 Integrity

Each artifact carries a **semantic digest**: a SHA-256 over the canonical
node/edge/attribute projection, independent of file framing. The manifest
additionally records the file SHA-256 and size. A reader mismatch on either the
declared schema, the embedded semantic digest, or the manifest file hash is a
hard failure.

### 3.3 Reader safety rules

The reader is validate-before-construct and must never:

- call `pickle.load`, `eval`, `exec`, `yaml.load`, or `marshal`;
- pass `object_hook` / `object_pairs_hook` that instantiates project classes;
- fall back to pickle when JSON parsing fails;
- import modules named in the payload.

It must reject, before returning any graph object: wrong magic/schema version,
unknown or missing attribute keys, wrong attribute types, non-`str` node ids,
edges that reference unknown nodes, self-loops, duplicate edges, and semantic
digest mismatch. Consumers must fail before writing any output file.

## 4. Legacy boundary — no compatibility path (R5.2)

R5.1 shipped a temporary, dual-gated conversion path for pre-migration
pickles. **Batch R5.2 removed it.** The current version has no way to read,
convert, or execute a legacy graph pickle, and this section is normative for
the current version.

- `SafeGraphArtifactV1` is the only supported graph interchange format. No
  module in `src/` or `scripts/` imports `pickle`, `marshal`, or `shelve`, and
  none calls `pickle.load` / `pickle.loads`. This is asserted by static scans
  in `tests/test_package_import_boundaries.py`, which have no skip list.
- A legacy artifact is refused **by name**, before the file is opened:
  `reject_legacy_graph_path` fires on a `.gpickle`, `.pkl`, or `.pickle`
  filename as the first statement of the reader's payload read. Opening a
  pickle is itself the dangerous act, so refusal cannot wait for content
  inspection. A pickle payload smuggled under a `.graph.json.gz` name is
  refused a second time by the magic-byte check, which raises instead of
  deserializing.
- Refusal happens before any side effect: no output directory, CSV, figure,
  completion marker, or manifest entry is produced for a run whose graph input
  is legacy.
- There is **no** migration command, opt-in flag, environment variable, or
  private helper that re-enables pickle. `migrate-legacy-graph` is not a
  registered subcommand and `--allow-trusted-legacy-graph-pickle` is not a
  known option on any subcommand; both are asserted by tests.
- An operator holding only a pre-migration `.gpickle` must re-run Preparation
  to produce a `SafeGraphArtifactV1` artifact. If the original bytes must be
  read for a historical audit, use a checkout of a release that predates R5.2
  in an isolated environment; the current version will not do it.
- Preparation never writes a pickle, and never silently produces a `.pkl`
  companion.
- Publication and reproduction bundles must contain zero pickle artifacts.
  This is enforced in code, not only by convention: `_validate_staging` and
  `_validate_release` both call `executable_serialization_files` and refuse a
  `.gpickle`, `.pkl`, or `.pickle` file by name before the bundle is sealed.
- A manifest that declares an executable graph serialization is refused on the
  same terms as the file itself — by declaration, without reading the payload.

## 5. Measured cost

Synthetic graphs matching the real contract and density (3.86 edges/node),
one process per size, warm-up write+read then 5 timed runs, on the project
environment. `deterministic`, `order-independent`, and `round-trip digest`
were true at every size.

| Nodes | Edges | Bytes | Write median / p95 (s) | Read+validate median / p95 (s) | Peak RSS (MiB) |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 3,862 | 241,390 | 0.108 / 0.111 | 0.163 / 0.170 | 68.4 |
| 10,000 | 38,622 | 2,403,614 | 1.233 / 1.297 | 1.976 / 2.005 | 312.9 |
| 59,096 | 228,243 | 14,200,577 | 7.688 / 7.751 | 12.197 / 12.410 | 1,557.4 |
| 100,000 | 386,224 | 24,030,786 | 13.198 / 13.435 | 20.865 / 21.385 | 2,595.5 |

Cost is linear in graph size (~240 bytes/node across all four sizes). At the
production size (59,096 nodes) Preparation pays ~7.7 s to write once and each
consumer ~12.2 s to read *and fully validate* — a one-off cost against stage
runtimes measured in minutes. The read figure includes schema validation and
digest recomputation, work the pickle path did not do at all.

## 6. Non-goals for this batch

Batch R5.1 does not implement R5.2, R6.1, or figure boundary selection, and
does not change graph structure, graph attributes, the Partition algorithm, or
any formal computation result.
