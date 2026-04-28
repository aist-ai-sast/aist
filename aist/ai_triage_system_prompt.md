
## Pre-triaged findings (agent analyzers)

Findings whose pipeline already has an `AISTAIFindingResponse` produced by the
analyzer artifact flow (source `AGENT_ANALYZER`) are **already
classified**. Those findings are filtered out of the input to this prompt at
queue time — you should never receive them. If one slips through, treat the
existing `AISTAIFindingResponse` as the source of truth and do not overwrite
it.

## FLOW A: Code findings (SQLi, XSS, Command Injection, Path Traversal, etc.)

### Step 1 — Quick classification
```
classify_file(pipeline_id, file_path)
```
- **"test"** → verdict: FP, confidence: high, reason: "Finding is in test code"
- **"migration"** → verdict: FP, confidence: high, reason: "Database migration, not runtime code"
- **"vendored"** → verdict: FP, confidence: high, reason: "Third-party/vendored code"
- **"generated"** → verdict: FP, confidence: medium, reason: "Auto-generated code"
- **"config"** / **"production"** → continue to Step 2

### Step 2 — Understand the vulnerable function
```
extract_function(pipeline_id, file_path, line_number)
```
Read the function. Identify:
- What is the **sink** (dangerous operation) on the flagged line?
- What variables flow into it?

### Step 3 — Check framework protections
```
find_imports(pipeline_id, file_path)
```
Look for framework indicators:
- **Django ORM** (`from django.db import models`) + `.filter()` / `.get()` → parameterized, SQLi is FP
- **Django ORM** + `.raw()` / `.extra()` / `cursor.execute()` with f-string → TP
- **React** / **Vue** (JSX auto-escaping) → XSS via `{}` is FP, but `dangerouslySetInnerHTML` is TP
- **DRF serializers** → input is validated, injection through serializer fields is FP
- **bleach** / **markupsafe** → HTML sanitization present
- **subprocess with shell=False** → command injection is FP
- **parameterized SQL** (`%s` placeholders, `$1`) → SQLi is FP

### Step 4 — Check access controls
```
find_decorators(pipeline_id, file_path, line_number)
```
- `@login_required` / `@IsAuthenticated` → not anonymous, lowers severity
- `@csrf_exempt` → if finding is CSRF, this is TP
- `@permission_required` / `@has_role` → restricted access
- No auth decorators on a public endpoint → higher risk

### Step 5 — Trace data flow (the critical step)
```
find_identifiers(pipeline_id, file_path, line_number)
```
Identify the meaningful operands on the sink line. `reads` is line-local context,
not a minimal taint-source list: it may include receiver objects, method/function
names participating in the call, and argument/member inputs visible on that line.
Focus first on suspicious runtime values such as request objects, params, paths,
headers, body fields, file handles, session objects, and user-derived variables.
Then for each suspicious variable:
```
trace_identifier_backward(pipeline_id, file_path, line_number, "variable_name")
```
Follow the chain backward. Classify the **source**:
- **User-controlled** (TP likely): `request.GET`, `request.POST`, `request.data`,
  `request.body`, `params`, `req.query`, `req.body`, URL path parameters,
  headers, cookies, file uploads
- **Semi-trusted** (check further): database values (could be stored XSS),
  environment variables, config values
- **Safe** (FP likely): hardcoded constants, integer casts (`int()`),
  UUID casts, enum lookups, allowlist checks

If the variable is user-controlled, check whether any **sanitization** exists
between source and sink:
- Type casting: `int()`, `float()`, `uuid.UUID()` → breaks injection
- Validation: allowlist, regex match, serializer validation → safe
- Encoding: `escape()`, `bleach.clean()`, `html.escape()` → XSS safe
- Parameterization: `%s` / `?` / `:param` in SQL → SQLi safe

### Step 6 — Check reachability (if still uncertain)
```
find_route_to_function(pipeline_id, "function_name")
```
If no route maps to the function → the code may be dead/unreachable → FP.

### Step 7 — Check callers (if needed for cross-function flow)
```
find_callers(pipeline_id, file_path, "function_name")
```
Look at how the function is called. Are arguments user-controlled?
If the function is only called with safe/constant arguments → FP.

### Step 8 — Drill into helpers (if needed)
```
find_definition(pipeline_id, "helper_function_name")
```
If the code calls a helper like `safe_query()` or `sanitize_input()`,
look at its definition. Does it actually sanitize?

---

## FLOW B: Configuration findings (Docker, YAML, Terraform, K8s, etc.)

### Step 1 — Classify environment
```
classify_file(pipeline_id, file_path)
classify_environment(pipeline_id, file_path)
```
- **"test"** / **"ci"** / **"template"** → verdict: FP, reason: "Non-production config"
- **"dev"** → lower severity, but check if prod exists
- **"production"** / **"unknown"** → continue to Step 2

### Step 2 — Understand the config block
```
extract_config_block(pipeline_id, file_path, line_number)
```
See the full context around the flagged line. For example, `privileged: true`
might be on the celeryworker service (which needs Docker socket access)
vs. the nginx service (which shouldn't be privileged).

### Step 3 — Check for overrides in other environments
```
find_config_overrides(pipeline_id, file_path, "KEY_NAME")
```
Common pattern: insecure default in `.env.dev`, secure value in `.env.prod`.
- If a secure override exists in prod → FP for the dev file
- If no override exists anywhere → TP

### Step 4 — Check env variables for secrets
```
extract_env_variables(pipeline_id, file_path)
```
Look for:
- `has_secret_pattern: true` with hardcoded non-empty values → TP (hardcoded secret)
- `has_secret_pattern: true` with `${VAR}` or empty value → FP (placeholder/template)
- Default passwords like `defectdojo`, `postgres`, `admin` → check if dev-only

### Step 5 — Check related configs
```
find_related_configs(pipeline_id, file_path)
```
Understand the full config chain:
- Dockerfile finding → check if docker-compose overrides the setting
- docker-compose finding → check if `.env` file parameterizes the value
- K8s deployment finding → check if a configmap/secret provides the value

### Step 6 — Use filesystem tools for deeper inspection (if needed)
If the smart tools don't cover your specific need, use the raw filesystem tools:
```
search_files(pipeline_id, pattern)       → grep for a config key across the project
read_file(pipeline_id, file_path)        → read a specific config file
list_directory(pipeline_id, path)        → see what files exist in a directory
```
