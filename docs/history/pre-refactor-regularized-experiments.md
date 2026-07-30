# Pre-refactor regularized experiments

The retired `regularized_zoning_experiments/` directory contained three thin
CLIs, three YAML configurations, a plan/tracker and ignored generated runs.
The search, objective, evaluation and selection implementations had already
moved into `roadnet_partition.zoning` before retirement.

## Recorded experiments

- V1 Fourth Ring: 48 candidates from three initializations and a
  `lambda_c × lambda_r` grid. All regularized results remained connected, but
  none passed the original strict success predicates because continuity cut
  increased while connector cut and demand balance improved.
- V1 selected `regularized_louvain_lc1p0_lr4p0` by its historical balanced
  score, with 137 clusters. This is analysis history, not the current canonical
  product.
- V2 introduced an explicit target of 100 clusters and kept merge/split off.
- Fifth Ring V2 fixed `lambda_c=lambda_r=alpha_cont=alpha_conn=1`, target 100,
  five passes and merge/split disabled. Those effective canonical values are
  retained in `configs/zoning/regularized.yaml`.

The accepted Fifth Ring result is preserved by the Golden asset, the Phase 9
source run and the published Linux canonical scope. Historical Windows data is
preserved separately in its private archive. No unique payload remained only
under the ignored experiment run directory when Phase 10 deleted it.

Use the public Partition command for current controlled execution:

```bash
conda run -n dydl roadnet-partition partition \
  --config configs/zoning/regularized.yaml
```
