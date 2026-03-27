# aist-security-check

Audit changed files for security issues before commit. Covers organization boundary
violations, path traversal, missing authentication, and dangerous query patterns.

## Inputs

- `files` (optional): comma-separated list of files to audit. If omitted, detect via
  `git diff --name-only HEAD` and `git diff --name-only --cached`.

## Checks

Run each check for every changed file. Skip checks that are not applicable to the file type.

---

### Check 1 — Organization isolation (`aist/` files only)

**What to find:** QuerySets returned without org filter.

Grep for:
- `.objects.all()` not followed by `.filter(` on the same or next line
- `.objects.filter(` that does NOT include `organization` anywhere in the chain
- `get_queryset` method that returns early without org filter for non-superusers

**Flag as CRITICAL if:**
- A ViewSet's `get_queryset()` can return objects from another org for a regular user.
- A `get_object()` or custom action fetches by primary key without verifying org ownership.

**Correct pattern:**
```python
def get_queryset(self):
    qs = Model.objects.all()
    if self.request.user.is_superuser:
        return qs
    return qs.filter(project__organization=self.request.user.aist_organization)
```

---

### Check 2 — Path traversal (`context_extractor_service/` files only)

**What to find:** File path construction without validation.

Grep for:
- `os.path.join(` where one argument could come from user input
- `open(` where the path is not validated against an allowed root
- `..` in any path-related variable or string

**Flag as CRITICAL if:**
- A file path built from `pipeline_id`, `file_path`, or any MCP tool argument is used
  in `open()` / `os.path.*` without first passing through the project root guard.

**Check the existing guard in `mcp_server.py`** — every new file-reading function must
call the same validation before opening any file.

---

### Check 3 — Missing authentication

**What to find:** Views or endpoints without authentication.

In `aist/api/` files:
- ViewSets missing `permission_classes`
- `permission_classes = []` or `permission_classes = [AllowAny]` on non-public endpoints

In `context_extractor_service/mcp_server.py`:
- New routes added without the auth middleware check

**Flag as ERROR if:**
- An endpoint that accesses org-scoped data has no authentication guard.

---

### Check 4 — Dangerous query patterns (`aist/` and `sast-pipeline/`)

**What to find:** SQL injection risk.

Grep for:
- `.raw(f"` or `.raw("` with string concatenation
- `cursor.execute(f"` or `cursor.execute("` with variables concatenated (not parameterized)
- `extra(where=[f"` pattern

**Flag as CRITICAL if:** User-controlled data flows into a raw SQL call.

---

### Check 5 — Direct `request.data` access in views

**What to find:** View logic bypassing serializer validation.

Grep for `request.data[` or `request.data.get(` inside view methods (not in serializers).

**Flag as ERROR if:** Found in a view method — input must be validated by a serializer first.

---

### Check 6 — Docker security (`sast-pipeline/Dockerfiles/` only)

For each changed Dockerfile:
- [ ] No `USER root` left at the end (switch to non-root before CMD)
- [ ] No `--privileged` in RUN commands
- [ ] No hardcoded secrets or tokens in ENV instructions
- [ ] Base image is pinned (not `latest`)

---

### Check 7 — Hardcoded credentials

Grep changed files for:
- `password =`, `token =`, `secret =`, `api_key =` assigned to a string literal
- Hex strings ≥ 20 chars assigned to a variable

**Flag as ERROR if:** Found outside of test fixtures.

---

## Output format

```
## Security Audit: <commit or file list>

### CRITICAL
- [<file>:<line>] <description>

### ERROR
- [<file>:<line>] <description>

### WARNING
- [<file>:<line>] <description>

### Passed
- Org isolation: ✓ / ✗
- Path traversal: ✓ / ✗
- Authentication: ✓ / ✗
- Raw SQL: ✓ / ✗
- Serializer usage: ✓ / ✗
- Docker security: ✓ / N/A
- Hardcoded credentials: ✓ / ✗
```

If all checks pass: output `No security issues found.`

## How to trigger

```
/aist-security-check
/aist-security-check files=aist/api/findings.py
```
