# Contributing

Contributions are welcome through GitHub issues and pull requests.

## Development setup

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

Use focused commits. Pull requests should describe the affected pipeline stage,
commands run, data classification, and any changed artifact checksums.

## Repository boundaries

- Put reusable code under `src/roadnet_partition/` and analysis entrypoints
  under `scripts/`.
- Put generated content under `outputs/`; do not commit it.
- Do not modify canonical data except through the publish transaction.
- Do not commit raw orders, driver identifiers, precise trip coordinates,
  private derived matrices, credentials, or local agent/editor settings.
- Update schemas, public documentation, and tests when changing a public
  contract.

Run `git diff --check` and the relevant tests before opening a pull request.
