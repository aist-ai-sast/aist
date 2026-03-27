# aist-api-review

Review new or modified REST API endpoints in `aist/api/` for correctness, security, and
consistency with existing patterns.

## Inputs

- `files` (optional): comma-separated list of changed files to review. If omitted, use
  `git diff --name-only HEAD` to detect changed files.

## What to review

For each changed ViewSet or APIView in `aist/api/`:

### 1. Organization isolation (critical)

Check `get_queryset()`:
- Does it filter by organization for non-superusers?
- Is the superuser bypass present?

Required pattern:
```python
def get_queryset(self):
    qs = ModelName.objects.all()
    if self.request.user.is_superuser:
        return qs
    return qs.filter(project__organization=self.request.user.aist_organization)
```

If `get_queryset()` is missing or does not filter — flag as **CRITICAL**.

Check that no nested object lookup bypasses the org scope
(e.g., fetching a `Finding` by `id` alone without verifying it belongs to the user's org).

### 2. Permission classes

- Does the ViewSet declare `permission_classes`?
- Do they match the pattern of adjacent endpoints in the same file?
- Read-only endpoints should use at minimum `IsAuthenticated`.
- Write endpoints should require appropriate write permissions.

### 3. Serializer usage

- Is there a serializer defined for the ViewSet?
- Is `request.data` ever accessed directly in view methods? If yes — flag as **ERROR**.
  All input must go through a serializer.
- Does the serializer validate required fields explicitly?

### 4. Superuser visibility

- Can a superuser see all data across organizations?
- Is the bypass in `get_queryset()` and NOT in `perform_create` / `perform_update`
  (which should still set org from the request user for create operations)?

### 5. Consistency

- Does the new endpoint follow field naming conventions from neighboring endpoints?
- Does it return the same error format (DRF default or custom handler in `exception_handler.py`)?
- Are pagination classes consistent with other endpoints?

### 6. sast-pipeline REST client (if changed)

If `sast-combinator/sast-pipeline/pipeline/defect_dojo/client.py` or `sast_client.py` changed:
- Are all HTTP calls going through the session from `DefectDojoClient`?
- Is retry/backoff preserved?
- No hardcoded tokens in code (use environment variables)?

## Output format

For each file reviewed, output:

```
## <file_path>

### CRITICAL (must fix before merge)
- <issue description with line reference>

### ERROR (should fix)
- <issue description>

### WARNING (consider fixing)
- <issue description>

### OK
- Organization isolation: ✓
- Permission classes: ✓
- Serializer usage: ✓
- Superuser bypass: ✓
```

If no issues found, output `All checks passed.`

## How to trigger

```
/aist-api-review
/aist-api-review files=aist/api/findings.py,aist/api/pipelines.py
```
