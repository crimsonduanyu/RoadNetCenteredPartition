# BoundaryArtifactV1 — Figure Boundary Contract (AUD-007)

Remediation batch **R6.1**. This document is the audit record produced by Gate A
and the normative contract implemented by Gates B–E.

Section 1 describes the repository **as it was before R6.1**; it is retained as
the audit finding, not as a description of current code.

## 1. Problem statement

A publication figure asserts a study area. The best-partition figure drew the
partition and road network of whatever run the caller named, but overlaid a
boundary that was chosen independently of that run. A reader cannot detect the
mismatch from the image, so a wrong boundary silently produces a wrong figure.

### 1.1 Boundary selection before R6.1 (audited on `audit/full-repo-20260804`, HEAD `5625e88`)

Both figure entrypoints resolved every other input from the named run, and then
passed one hard-coded literal as the boundary:

| File | Line | Statement |
| --- | --- | --- |
| `scripts/figures/best_partition_maps.py` | 31 | `PROJECT_ROOT / "data/raw/beijing_fifth_ring_boundary.gpkg"` |
| `scripts/figures/best_partition_maps.py` | 39 | `PROJECT_ROOT / "data/raw/beijing_fifth_ring_boundary.gpkg"` |
| `scripts/figures/partition_order_panels.py` | 27 | `PROJECT_ROOT / "data/raw/beijing_fifth_ring_boundary.gpkg"` |

`render_partition_maps` and `render_partition_order_figure` then called
`gpd.read_file(boundary_path)` with no validation of format, layer, CRS,
geometry type, or relationship to the partition being drawn.

Neither entrypoint accepted a `--boundary` argument. The run manifest already
recorded the correct per-run boundary as the logical input
`inputs.files["preparation.boundary"]`, written by
`preparation.input_records`; both scripts ignored that record.

### 1.2 Reproduction (Gate A)

Two synthetic completed runs were built in a disposable temporary directory
outside the repository, one for each tracked dataset scope, each with a manifest
whose `preparation.boundary` record named its own real study-area boundary. The
real, unmodified CLI was then run against each, with `geopandas.read_file`
instrumented only to observe which paths it opened:

```
########## run scope = fourth ##########
--- geospatial files read by the real CLI ---
    /tmp/.../fourth/run/partition/clusters/segment_clusters_..._regularized_a.gpkg
    /tmp/.../fourth/run/preparation/road_edges_classified.gpkg
    .../data/raw/beijing_fifth_ring_boundary.gpkg     <-- wrong boundary

########## run scope = fifth ##########
    .../data/raw/beijing_fifth_ring_boundary.gpkg
```

The fourth-ring run rendered against the fifth-ring boundary and exited zero.
The two study areas differ substantially — fourth-ring extent 18.3 km wide,
fifth-ring 29.2 km — so the figure framed the partition against a study area it
does not belong to.

A second variant placed a correct and a wrong boundary, plus an
alphabetically-earlier decoy, in the run directory, and varied their creation
order. Every variant produced a byte-identical figure: the pre-R6.1 selection
did not scan directories or rank candidates at all, so the defect is a literal
hard-code, not filesystem-order sensitivity. Both failure modes are covered by
the regression tests so neither can be introduced later.

## 2. Contract

`BoundaryArtifactV1` is the only accepted description of a figure boundary. It
is enforced by `roadnet_partition.reporting.boundary_contract` and sequenced by
`roadnet_partition.reporting.boundary_resolver.resolve_figure_boundary`.

### 2.1 Source of truth

Exactly one of two explicit bindings supplies the boundary, and the caller
chooses which:

- **`--boundary <path>`** — the named file is the sole source of truth. No other
  location is consulted, before or after any failure.
- **`--boundary-from-manifest`** — the boundary recorded in the named run's
  manifest under the logical name `preparation.boundary` is used. The caller
  still names the run explicitly; nothing is discovered.

There is no default, no fallback between the two, and no third path. Supplying
neither is an error; supplying both is an error.

### 2.2 Path and file rules

- The path must exist, and must be a regular file.
- The path may not be a symbolic link.
- The suffix must be one of `.gpkg`, `.geojson`, `.json`, `.shp`. The suffix
  selects the driver, so an unsupported suffix is refused by name.
- Under a manifest binding, the recorded `size` and `sha256` are re-verified
  against the file on disk when the record carries them. A mismatch is refused;
  the figure is not drawn from a boundary that differs from the one the run was
  computed with.

### 2.3 Layer rules

- Single-layer formats (`.geojson`, `.json`, `.shp`) accept no layer argument.
- For `.gpkg`, layers are enumerated and only polygonal layers are eligible.
- Exactly one eligible layer and no `--boundary-layer`: that layer is used.
- More than one eligible layer and no `--boundary-layer`: refused, and the
  error lists every candidate layer name. The first layer is never chosen.
- `--boundary-layer` naming a layer that does not exist: refused, listing the
  available layers.
- `--boundary-layer` naming a non-polygonal layer: refused.

Layer eligibility is computed from the declared geometry type, so the result
does not depend on the order layers happen to appear in the container.

### 2.4 CRS rules

- The boundary must declare a CRS. A missing CRS is refused, never guessed.
- The partition must declare a CRS. A missing CRS is refused.
- When the two differ, the boundary is reprojected to the partition CRS with
  the repository's existing `project_gdf` policy, so the figure is drawn in one
  coordinate system. A geographic boundary is never mixed with projected metre
  coordinates.
- The scale bar continues to derive its length from the partition CRS through
  the existing `_scale_length_data`, which is unchanged.

### 2.5 Geometry rules

- Non-empty: a boundary with no features is refused.
- No null and no empty geometries.
- Every geometry must be `Polygon` or `MultiPolygon`. `Point`, `LineString`,
  and `GeometryCollection` are refused — this is what prevents a ring drawn as
  lines, a road segment layer, or a centroid from being used as a study area.
- Every polygon must be valid. Invalid polygons are refused rather than
  repaired, because silently repairing changes the drawn study area.
- Multiple polygon rows, polygons with holes, and disconnected multipolygons
  are all accepted; a study area legitimately takes those shapes.

### 2.6 Spatial consistency

The boundary and the partition are compared before anything is drawn:

- Both extents must be finite.
- The two must intersect.
- At most `CONTAINMENT_TOLERANCE` (2%) of the partition's total length may fall
  outside the boundary.

The tolerance exists because reprojection and clipping a network at the study
edge both move geometry slightly, so exact containment would reject correct
inputs. A boundary from a different study area falls far outside it. Partition
geometry is never modified to satisfy the check.

### 2.7 Ordering

`resolve_figure_boundary` performs the checks in a fixed order:

```
parse explicit input
  -> path / manifest ownership validation
  -> file record (size, sha256) validation
  -> read geospatial metadata (layer enumeration)
  -> layer selection validation
  -> CRS validation
  -> geometry type validation
  -> non-empty / validity validation
  -> spatial consistency against the partition
  -> canonical GeoDataFrame
```

Every failure raises `BoundaryContractError` **before** the figure entrypoint
creates its output directory, so a refused invocation leaves no PNG, no PDF, no
output directory, and no partially written file.

## 3. What this contract does not change

R6.1 changes only which boundary is loaded and whether it is accepted. The
rendering functions and every visual property are untouched: palettes and
cluster colour assignment, road and connector drawing, boundary line width and
style, north arrow, scale bar, legend and colourbar, figure size, DPI, extent
and padding, PNG/PDF output, and the existing no-title design.
