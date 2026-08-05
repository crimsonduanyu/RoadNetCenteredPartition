# Full Repository Remediation Plan

Source audit: `docs/audit/full-repository-audit.md` at HEAD `5956ddf883bc355ef09c354acec3cb9c3b985ac1`.

This plan groups work by root cause, not by file. It does not implement any fix. Each batch is intended to be independently reviewable and revertible; where a compatibility transition is necessary, compatibility and removal are separate batches.

## Priority and dependency map

```text
R1.1 destructive export boundary ─┬─> R1.2 scope/publish boundary
                                 └─> resume canonical publish/export

R2.1 preparation identity ─────────> R2.2 complete runtime provenance

R3.1 cross-stage ID schema ─────────> canonical full-pipeline acceptance

R4.1 exact deterministic diameter     (independent)
R5.1 safe graph interchange ────────> R5.2 legacy pickle removal
R6.1 run-derived figure boundary      (independent)
```

Operational gate: freeze `export-reproduction --overwrite` and canonical publication work until R1.1 and R1.2 pass. Do not accept new canonical experiment results until R2.1 and R3.1 pass. Other batches can proceed independently.

## Root cause R1 — Destructive filesystem operations lack one explicit ownership boundary

Addresses AUD-001.

### Batch R1.1 — Constrain reproduction export before any write

- **Dependencies:** none; first batch.
- **Modification scope:** destination policy in `releases/reproduction.py`; owned-root support in `io/paths.py` only if the policy cannot be expressed safely at the caller; focused release/path tests and CLI error text.
- **Required behavior:** resolve the destination once, reject symlinked paths, and require it to be a child of one documented release root. If product requirements permit arbitrary export locations, reject destination relationships that are ancestor/equal/descendant to project root, run root and protected data roots, and pass an exact allowed parent to the swap helper.
- **Tests:** every ancestor of project/run/data; exact protected paths; protected descendants; absolute external path under the explicitly allowed release root; symlink components; existing/non-existing destinations; overwrite false/true; assertion that failure creates no staging path and preserves marker files.
- **Risk:** too-strict policy could reject a legitimate external release directory. Mitigate with one explicit `--output-root`/documented root policy, not scattered exceptions.
- **Acceptance criteria:** the synthetic AUD-001 reproduction is rejected before `mkdir`; no test can cause the swap helper to target outside its allowed parent; all existing rollback tests pass.
- **Rollback:** revert caller policy and owned-root helper change together; no artifact-format migration is involved.

### Batch R1.2 — Validate scope and publish containment before staging

- **Dependencies:** R1.1 establishes the shared destructive-path invariant.
- **Modification scope:** dataset and pipeline scope validation, publish target/staging construction, and config/publish tests.
- **Required behavior:** reuse the existing single-component output-identifier validator for dataset and pipeline scopes; after resolution require target and staging to be direct children of `<project_root>/data/processed`; run all checks before free-space probing, directory creation or inventory copy.
- **Tests:** empty, `.`, `..`, slash/backslash traversal, absolute POSIX/Windows-looking values, Unicode/safe identifiers, manifest/CLI scope mismatch, and “no external staging directory created” assertions.
- **Risk:** previously accepted exotic scope names will become invalid. This is intentional; publish scope is a directory identifier, not a path.
- **Acceptance criteria:** only safe one-segment scopes load; target containment is asserted twice (configuration boundary and destructive call); a crafted scope cannot write outside `data/processed` even when the final swap is mocked.
- **Rollback:** revert as one validation batch; no data migration.

## Root cause R2 — Experiment identity is split across incomplete fingerprints

Addresses AUD-002 and AUD-006.

### Batch R2.1 — Make preparation reuse depend on config and inputs

- **Dependencies:** none; required before canonical experiment acceptance.
- **Modification scope:** pipeline fingerprint construction, preparation manifest/reuse predicate, invalidation reason reporting, and resume tests.
- **Required behavior:** calculate current preparation config and input file records before reuse; require exact stable equality to the stored records; include their fingerprint in the parent pipeline identity. A mismatch invalidates Preparation and all dependent stages instead of silently returning paths.
- **Tests:** same-path YAML content mutation; each input content mutation; missing/added output; unchanged exact reuse; partial/interrupted preparation; downstream invalidation ordering.
- **Risk:** old manifests may lack the new identity and cease to resume. Prefer explicit one-time recomputation over guessing compatibility.
- **Acceptance criteria:** the AUD-002 reproduction cannot print `preparation: reused`; the manifest always explains the identity mismatch; unchanged runs retain reuse behavior.
- **Rollback:** revert fingerprint schema and tests together; do not attempt to downgrade newly written manifests silently.

### Batch R2.2 — Record complete runtime and dirty-tree provenance

- **Dependencies:** R2.1, so one manifest-schema review covers the final experiment identity rather than being revised twice.
- **Modification scope:** runtime dependency collection, dirty Git fingerprinting, manifest validation/versioning, reproducibility documentation and lifecycle tests.
- **Required behavior:** record all direct result-affecting distributions and relevant native/geospatial versions. For allowed dirty runs, hash bytes of all untracked regular files (reject symlinks/special files), or forbid untracked content for publish/export.
- **Tests:** installed/missing distribution records; deterministic ordering; same untracked name with changed bytes; binary and empty files; symlink/special-file rejection; clean-tree stability.
- **Risk:** manifests become larger and version names may differ between Conda and Python distributions. Maintain one explicit mapping table and tolerate unavailable packages as `null` with reason.
- **Acceptance criteria:** every direct dependency in `environment.yml` that can change results is represented; changing same-name untracked bytes changes provenance; manifest validation remains deterministic.
- **Rollback:** keep schema reader backward-compatible for one version; revert writer expansion separately if needed.

## Root cause R3 — No single cross-stage schema owns shared identifiers

Addresses AUD-003.

### Batch R3.1 — Adopt the Demand checkpoint ID schema in Supply

- **Dependencies:** none; required before canonical full-pipeline acceptance.
- **Modification scope:** Supply file adapter and only downstream operations that assume integer identifiers; shared schema/contract tests; public data-contract text if needed.
- **Required behavior:** load `order_id` and `driver_id` as strings following `order_checkpoints.py`; permit null `order_id`, reject null/blank `driver_id`, and preserve leading zeros. Do not change numeric cluster/time fields.
- **Tests:** real Demand-format assigned artifact with alphanumeric IDs, leading zeros, null order ID, invalid driver ID, repeated drivers and both service types; compare Supply aggregates to numeric-fixture baselines.
- **Risk:** integer-specific hashing/block assignment may produce different block placement. Block placement is internal; validate aggregate equivalence and deterministic string hashing/order.
- **Acceptance criteria:** Demand output is accepted by the actual Supply reader without coercing identifier values; invalid driver IDs fail at the contract boundary; full tiny pipeline passes.
- **Rollback:** one adapter/schema commit; no serialized public Supply format change should be necessary.

## Root cause R4 — An approximation is exposed and consumed as an exact metric

Addresses AUD-004.

### Batch R4.1 — Define exact, deterministic network diameter semantics

- **Dependencies:** independent.
- **Modification scope:** network diameter implementation, metric documentation and focused metric/evaluation tests.
- **Required behavior:** for each connected component, compute exact maximum weighted shortest-path distance using the existing node-length edge weight. If production scale makes exact calculation unacceptable, introduce an explicitly named deterministic approximation and prohibit it from exact pass/fail criteria.
- **Tests:** single node, path/tree, disconnected cluster, weighted cyclic counterexamples, zero/missing lengths under current EPS rule, multiple hash seeds, and comparison to NetworkX all-pairs ground truth.
- **Risk:** exact all-pairs work can be expensive. Benchmark representative cluster sizes before selecting exact evaluation or documented approximation; do not silently preserve the current semantics.
- **Acceptance criteria:** cyclic counterexample returns 2 under every hash seed; decision rows are stable; metric name/documentation matches the algorithm.
- **Rollback:** revert metric and its tests as one isolated analytics batch; no pipeline artifact migration.

## Root cause R5 — Internal executable serialization is treated as a portable artifact

Addresses AUD-005.

### Batch R5.1 — Add one safe graph interchange format and prefer it everywhere public

- **Dependencies:** independent, but coordinate with R2.2 so format/library versions enter provenance.
- **Modification scope:** preparation graph writer, partition/evaluation/reporting graph readers, run manifest output records, configs and format round-trip tests.
- **Required behavior:** choose one existing-dependency format (GraphML or schema-validated node-link data), define supported node/edge attribute types, and make all new runs and shared exports use it. Do not add a new serialization dependency.
- **Tests:** attribute-preserving round trip; malformed/missing schema; unexpected object-like values; large synthetic graph smoke test; all three consumers.
- **Risk:** GraphML type coercion or JSON size may change identifiers/attributes. Establish a small explicit schema and compare semantic graph equality.
- **Acceptance criteria:** normal public CLIs no longer invoke `pickle.load`; a malformed artifact fails before object construction; new run manifests identify the safe format.
- **Rollback:** reader can temporarily retain legacy fallback; new safe artifacts remain readable.

### Batch R5.2 — Retire trusted-only pickle compatibility

- **Dependencies:** R5.1 released and documented; existing canonical artifacts converted or regenerated.
- **Modification scope:** legacy fallback, compatibility flag and migration notice/tests.
- **Required behavior:** during transition, require an explicit trusted-only flag and warning for pickle. Remove the fallback after the documented compatibility window.
- **Tests:** default rejection, explicit legacy opt-in, and final removal test.
- **Risk:** old local runs stop rendering. Provide a one-shot offline converter that is itself explicitly trusted-input-only, then remove it with the fallback.
- **Acceptance criteria:** no public/default path deserializes pickle; shareable export inventory contains no executable graph artifact.
- **Rollback:** restore only the explicit opt-in reader, never silent auto-detection.

## Root cause R6 — Reporting scripts infer dataset identity from repository defaults

Addresses AUD-007.

### Batch R6.1 — Resolve figure boundary from the selected run

- **Dependencies:** independent; may reuse manifest additions from R2.1 if scheduled later.
- **Modification scope:** figure CLI run resolver, preparation/run manifest boundary record, publication-figure documentation and CLI tests.
- **Required behavior:** select the boundary recorded for the named run and verify its hash/CRS before rendering. If only Fifth Ring is supported, reject any other scope explicitly before creating files.
- **Tests:** fifth- and fourth-ring synthetic run manifests, missing/tampered boundary, CRS mismatch, unsupported scope and no-partial-output behavior.
- **Risk:** older manifests may not record the boundary. Support an explicit `--boundary` override for legacy runs only if needed; do not silently fall back to Fifth Ring.
- **Acceptance criteria:** fourth-ring input can never render with the Fifth Ring boundary; help/documentation states the source and fallback policy; rendering helpers remain unchanged.
- **Rollback:** revert the CLI resolver batch; no production algorithm or data format changes.

## Final acceptance gate

Before lifting the operational freeze:

1. R1.1 and R1.2 security regressions pass in a disposable tree, including “no write before validation.”
2. The full existing pytest suite passes with new regression tests.
3. A clean environment can install the package and both CLI help commands pass.
4. One synthetic end-to-end run exercises Demand → Supply string IDs and preparation resume invalidation.
5. Manifest/Markdown contract changes are documented and backward-compatibility behavior is explicit.

R4-R6 do not block unrelated development, but any evaluation, shared graph artifact or non-Fifth-Ring figure relying on those paths must remain non-canonical until its corresponding batch is accepted.
