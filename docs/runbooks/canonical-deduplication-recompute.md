# Recompute canonical duplicates

Use this runbook to evaluate canonical deduplication against findings that were
already imported. Start with a dry run and narrow the scope before applying
changes.

## Preview changes

Run the command inside the supported Django container:

```bash
python3 manage.py recompute_aist_duplicates \
  --dry-run \
  --explain-json \
  --product-id 12 \
  --since 2026-01-01
```

Review the proposed duplicate roots and candidate changes. If the scope is too
broad, filter by one pipeline, product, date, or result limit.

`--explain-json` emits one JSON object per finding with the verdict, selected
root, score contributions, location strength, fallback reason, number of
database candidates, and decision duration. Retain these rows for historical
replay evaluation and performance percentiles.

## Apply reviewed changes

```bash
python3 manage.py recompute_aist_duplicates \
  --apply \
  --pipeline-id 5ae48a36
```

Available scope controls are:

- `--pipeline-id`;
- `--product-id`;
- `--since YYYY-MM-DD`;
- `--limit`.

Use `--clear-existing-aist-duplicate-tags` only when the reviewed operation is
intended to replace existing AIST canonical tags. It broadens the mutation and
should not be combined with an unreviewed full-product apply.

After applying, verify the affected finding groups and retain the command scope
with the operational change record.
