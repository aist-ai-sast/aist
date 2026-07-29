---
name: aist-api-review
description: Deep review of new or modified REST endpoints in aist/api for organization isolation, serializer-driven validation, permissions, and consistency with neighboring AIST API patterns.
---

# aist-api-review

Review new or modified REST API endpoints in `aist/api/` for correctness, security, and
consistency with existing patterns.

## Inputs

- `files` (optional): comma-separated list of changed files to review. If omitted, use
  `git diff --name-only HEAD` to detect changed files.

## What to review

For each changed ViewSet or APIView in `aist/api/`:

### 1. Organization isolation (critical)

Check the central authorization contract:
- Does the endpoint inherit from `AISTAPIView` or `AISTAuthzMixin`?
- Does it declare a `ResourcePolicy` for the object it resolves?
- Do object lookups use `resolve()` and additional resource lookups use
  `authorized_queryset(resource=..., action=...)`?
- Is each resource registered in `RESOURCE_GETTERS`?

Required pattern:
```python
class ExampleAPI(AISTAPIView):
    authz = ResourcePolicy(
        resource=AISTProject,
        read=Action.PRODUCT_READ,
        write=Action.PROJECT_OPERATE,
    )

    def get(self, request, project_id):
        project = self.resolve(pk=project_id)
```

If an org-owned object can be resolved outside that layer — flag as **CRITICAL**.

Check that no nested object lookup bypasses the org scope
(e.g., fetching a `Finding` by `id` alone without verifying it belongs to the user's org).

### 2. Named actions

- Does the policy use the correct reader/writer/maintainer action tier?
- Are direct `Permissions.*` references absent from `aist/api/`?
- Is `ACTION_PERMISSIONS` used only in `aist/authz/policy.py`?

### 3. Serializer usage

- Is there a serializer defined for the ViewSet?
- Is `request.data` ever accessed directly in view methods? If yes — flag as **ERROR**.
  All input must go through a serializer.
- Does the serializer validate required fields explicitly?

### 4. Superuser and token visibility

- Can a superuser see all data across organizations?
- Is bypass/token restriction implemented by the registered query getter rather
  than a view or serializer branch?

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
