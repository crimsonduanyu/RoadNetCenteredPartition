# GitHub history audit v1

Date: 2026-07-30

## Scope and method

The current tracked tree and every reachable Git object were inspected. The
history inventory was generated with:

```bash
git rev-list --objects --all
git log --all --stat
git log --all --name-only
git cat-file --batch-check
```

At the audit point the repository contained 137 reachable commits and 1,386
object/path inventory entries. Unique text blobs were scanned for credentials,
private keys, API tokens, email addresses, private IP addresses, local hostnames,
Linux/Windows user paths and local Conda installations. Blob paths and sizes were
also checked for raw/processed data, payload archives, notebooks and files above
GitHub's size limits.

## Current tracked tree

- No tracked file exceeds 10 MiB or 50 MiB.
- The largest tracked file is
  `artifacts/paper/figures/partition_and_mean_hourly_orders.pdf` at 6,857,838
  bytes.
- No raw order rows, driver records, coordinate payload, Parquet matrix,
  GeoPackage canonical payload, SQLite database, log, Golden payload, or Windows
  baseline payload is tracked.
- No credential, private key, API token, private IP, private hostname, personal
  email, `/home/<user>/`, Windows user path, or local Conda path remains in the
  tracked working tree. `test@example.com` is a synthetic Git-test fixture.
- `tests/Make_Beijing_TTE.ipynb` was removed from the current tree because it
  contained executed private-data-derived output and Windows kernel paths.

The current tree passes the privacy scan, but this does not make the complete
repository history safe to publish.

## Git object size result

No historical blob exceeds 50 MiB or GitHub's 100 MiB hard limit. No historical
blob path under `data/raw/`, `data/interim/`, `data/processed/`, a Golden payload,
or the Windows baseline payload was found. Large private datasets therefore do
not appear to have been committed as ordinary Git blobs.

## Sensitive historical blobs

The following reachable blobs contain private-data-derived notebook output or
local-machine information. First/last refer to path history, including removal
commits where applicable.

| Path | Blob SHA(s) | Finding | First commit | Last commit |
|---|---|---|---|---|
| `.claude/settings.local.json` | `41b35ddb3dfe7ea16d269b2f643b392f41652b46`, `6f46992438cdafca3adcc72e71cda1063fa18d6c`, `71a46b38db021447f0f9eea58a013465a4b115c8` | Windows user directory, local Conda executable, local agent permissions | `80deea6036c3ccfd747975d4d1773c696f45ed8d` | `10c86e64a002f9b0bd90edc7d63003cf81d5339e` |
| `tests/Make_Beijing_TTE.ipynb` | `28599acbfde7673c8965179759fcddf9ddf408e0` | 24 executed outputs, about 975k output characters, formal TTE/private derived-data references, Windows kernel path | `97de198ede6c9638272d4603fc8fbceb10bed1c2` | `97de198ede6c9638272d4603fc8fbceb10bed1c2` |
| `tmp.ipynb` | `60421f61e7d8b2d343710777aa06eddf6b746375`, `79855f731c1bcf91e6a736e49c9c07cbad9d5588`, `79bedf26cc043342e1dfb11e461bf8924438ae29`, `a77df15000100952e02f9991e5bca6c7c97c8c03` | Executed output and order/driver/coordinate or Supply intermediate references | `e6fb93dca987949fee7a3c672f4ad5eae641eecf` | `d48af1ab8818ee491fe099b178907e37bc9b2dfe` |
| `reports/raw_order_trip_time_distribution_report.md` | `e45a5c447d6196fea66b3301c2cf1bee9fa04f27` | Windows user/Desktop project paths | `97de198ede6c9638272d4603fc8fbceb10bed1c2` | `b83013c29af8dce9d4cbe25cd31d7a5b82fda10b` |
| `docs/refactor/pre-refactor-v1/pytest.txt` | `714905640daf040ada523ecb7904cd81a71477ad` | `/home/<user>/` path | `ed510d9bc6408f10db6e1fd8d5c0d68adab837bd` | `eb89081926fc6daea4e9798dbe13dcc6704ef087` |
| `docs/refactor/pre-refactor-v1/repository.json` | `b3ee1c1c9b86075f109806fb497121fa0c518ebb` | `/home/<user>/` and local Conda executable path | `ed510d9bc6408f10db6e1fd8d5c0d68adab837bd` | `eb89081926fc6daea4e9798dbe13dcc6704ef087` |
| `regularized_zoning_experiments/EXPERIMENT_PLAN.md` | `27cb35dfc66c41f12a6a7929f87534e691540806` | Local Windows Conda command path | `f7eaeca574400e8de7c6dd7ec4ac824e561f0de9` | `5144b1fc30a4b11cd541773bde2a1e94d19e15fd` |
| `regularized_zoning_experiments/EXPERIMENT_TRACKER.md` | `5f8406b18f8bd1ae73a286716586cfe61d4403a0` | Local Windows Conda command path | `f7eaeca574400e8de7c6dd7ec4ac824e561f0de9` | `5144b1fc30a4b11cd541773bde2a1e94d19e15fd` |

No actual API credential, access token, private key, password value, private IP,
or private server address was found, so there is no identified credential to
revoke. The notebook outputs and machine-specific paths are nevertheless not
appropriate for a public immutable history.

## Required action before GitHub publication

History rewrite is required. This audit does not perform it. Obtain separate
authorization, make a protected backup, and use `git filter-repo` to remove the
paths listed above from all branches and tags. Do not use `filter-branch`.
After rewriting, rerun this audit, expire/repack unreachable objects as
appropriate, coordinate replacement of any existing remote refs, and verify a
fresh clone before publication.

Because sensitive historical blobs remain reachable, this repository is **not
GitHub-ready** at the end of this audit even though the current working tree is
cleanable and contains no identified private payload.
