# DAST integration

DAST has two independent roles in AIST, both keyed off one `OrgIntegration`
(`integration_type=DAST`): an **autonomous-scan gateway** consulted during a
normal pipeline run, and a **manual report import** path for reports produced
outside AIST's own orchestration — DAST is the first format wired into
client-ui for that path, but the import mechanism itself is generic to any
scan_type DefectDojo has a parser for.

```
aist/models.py
  OrgIntegration (type=DAST)
    config.gateway_url        ← non-secret, e.g. https://dast-gateway.internal
    secret                    ← integrator token, encrypted

aist/integrations/dast.py
  dast_env(project)           → {DAST_GATEWAY_URL, DAST_INTEGRATOR_TOKEN} | {}
    used by aist/pipeline_args.py to extend additional_environments for the
    analyzer container — the analyzer decides whether/how to call the gateway
  probe_dast_gateway(integration)
    → (ok, detail)            ← GET {gateway_url}/integrations/v1/ping,
                                 side-effect-free, no scan is started

aist/parser_overrides.py
  DAST_SCAN_TYPE = "DAST Autonomous Scan"
  DastReportParser registered for that scan_type — owns the source_commits
  routing hint via its own extract_source_commits(filename) method

aist/api/report_import.py, aist/tasks/report_import.py, aist/utils/report_import.py
  PipelineImportValidateAPI, PipelineImportAPI → generic manual import,
  fabricates a pipeline for any scan_type
```

## Role 1 — autonomous scan gateway (used during a pipeline run)

When a project's organization has an active DAST integration with both a
`gateway_url` and a token, `dast_env()` adds `DAST_GATEWAY_URL` /
`DAST_INTEGRATOR_TOKEN` to the DAST analyzer container's environment
(`aist/pipeline_args.py`). The analyzer is the only thing that calls the
gateway; the platform never does. If either the URL or the token is missing,
`dast_env()` returns an empty dict and the analyzer surfaces
`[INFO] DAST not configured` instead of attempting a request with partial
credentials.

## Role 2 — manual report import (any registered scan_type)

Some scans happen out of band (a separate scanning environment, a scheduled
run against a staging tier) and are not launched by an AIST pipeline at all.
For these, a user with edit access to the target project can upload a report
directly, and AIST fabricates a pipeline from it as if a normal run had
produced it — no analyzer container is invoked for this path. The mechanism
is not DAST-specific: `scan_type` is validated against
`dojo.tools.factory.get_choices_sorted()`, the same registry DefectDojo's own
import API uses, so any registered parser works through the same two
endpoints. client-ui currently only exposes DAST (`ImportPipelineDialog.tsx`
hardcodes `scan_type="DAST Autonomous Scan"`); adding a picker for other
formats is a frontend-only change.

**`POST /api/v2/aist/pipelines/import/validate/`** (`PipelineImportValidateAPI`)
— parses the upload with whatever parser is registered for `scan_type`
(`factory.get_parser(scan_type)`, then `parser.get_tests(scan_type, file)` —
the same call `DefaultImporter` itself makes before saving anything) and
returns a preview: finding count, severity breakdown, report name/version,
and a `detected_commit_hash` if the parser supplied one — entirely in memory
for this request. A malformed report fails here with the parser's own error
message, synchronously, via the registered parser's own validation.
`DastReportParser` is the one that supplies `detected_commit_hash`: its
`extract_source_commits(filename)` method reads the report's
`dast_run_metadata.source_commits` and returns it as a plain
`{repo_name: sha}` dict; the endpoint calls this (via
`getattr(parser, "extract_source_commits", None)`, generic to any parser that
chooses to implement it) and looks up the selected project's repo name in
that dict.

**`POST /api/v2/aist/pipelines/import/`** (`PipelineImportAPI`) — takes the
same file again plus the confirmed `project_id`, `scan_type`, and
`commit_hash` (pre-filled from `detected_commit_hash`, editable). Resolves a
**`GIT_HASH`** `AISTProjectVersion` for the commit
(`aist/utils/report_import.py:resolve_import_version` — reuses an existing
version row for the same commit through the database uniqueness constraint
and locked resolution), persists the file, creates an `AISTPipeline` in its
default `FINISHED` state, stores a preallocated `run_task_id`, and dispatches
`aist/tasks/report_import.py:import_report` to do the rest asynchronously;
the UI polls the returned `pipeline_id` until the terminal transition clears
`run_task_id`.

The Celery task locks the pipeline, transitions it to `UPLOADING_RESULTS`,
resolves the version, imports the report as a `Test` via
`aist/internal_upload.py:import_scan_via_default_importer` — the same helper
the SAST pipeline tail uses — records provenance on `pipeline.launch_data`
(`source: "manual_import"`, `scan_type`, `uploader_id`, `filename`, `sha256`),
and hands off to `postprocess_findings` / `finish_pipeline`, the exact tail
`run_sast_pipeline` uses, so a manually imported pipeline goes through
deduplication and lands on `FINISHED` / `FINISHED_WITH_WARNINGS` like any
other run. See [Pipeline execution](../product/pipeline-execution.md). The
uploaded file is deleted from storage in a `finally` block regardless of
success or failure.

A finding produced this way is an ordinary `dojo.Finding` row — see
[Finding review](../product/finding-review.md) for how the finding-detail
view adapts for it. The DAST exporter appends the curated report URL as the
final line of the finding's native `references` field. The client renders
`http` and `https` reference lines as links and keeps reference labels as text.

## Configuration

### 1. Create the DAST integration

```
POST /api/v2/aist/org-integrations/
{ "integration_type": "DAST", "name": "DAST gateway", "config": {"gateway_url": "https://dast-gateway.internal"}, "secret": "<integrator token>" }
```

`gateway_url` is required (validated server-side — `_validate_dast_attrs` in
`aist/api/org_integrations.py`). In client-ui this is the **DAST**
integration type on the Organization → Integrations page: a Gateway URL field
plus an "Integrator Token" secret field, reusing the same generic
create/update/validate flow every other integration type uses.

### 2. Verify

```
POST /api/v2/aist/org-integrations/<id>/validate/
```

Calls `probe_dast_gateway`: `GET {gateway_url}/integrations/v1/ping` with
`Authorization: Bearer <token>`, timeout 10s. Returns reachable/token-accepted,
token-rejected (401/403), or unreachable (connection error/timeout) — the
detail string is built only from status codes and exception type names, never
from request/response content, so it cannot leak the token.

### 3. Import a report manually

Requires project edit permission. See the Role 2 flow above; the UI path is
Pipelines page → Import. Directly:

```
POST /api/v2/aist/pipelines/import/validate/   (multipart/form-data)
file: <report>
project_id: <AISTProject id>
scan_type: DAST Autonomous Scan

POST /api/v2/aist/pipelines/import/            (multipart/form-data)
file: <same report>
project_id: <AISTProject id>
scan_type: DAST Autonomous Scan
commit_hash: <sha>
```

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `AIST_PIPELINE_IMPORT_MAX_SIZE_BYTES` (`PIPELINE_IMPORT_MAX_SIZE_BYTES`) | 15 MB | Reject an oversized upload before it is read into memory. |
| `DD_AIST_PIPELINE_IMPORT_THROTTLE_RATE` (`AIST_PIPELINE_IMPORT_THROTTLE_RATE`) | 20/hour | Per-user throttle scope `aist_pipeline_import`, shared by both import endpoints — one confirmed import fans out to `DefaultImporter` + Celery and writes a Test/Finding/AISTProjectVersion plus a storage file per call. |

## Security notes

- **Authorization:** both import endpoints require authentication and resolve
  the target project through the same organization-scoped authorized-queryset
  pattern as every other AIST endpoint (`get_authorized_aist_projects`;
  `Permissions.Product_View` for validate, `Permissions.Product_Edit` for
  confirm) — see
  [tenant isolation and access](../security/tenant-isolation-and-access.md).
  There is no DAST-specific bypass of that model.
- **Per-format validation is the registered parser's job, not a bespoke
  schema:** `scan_type` is gated to what `dojo.tools.factory` actually has
  registered; content validation happens inside `parser.get_tests(...)`
  itself. This mirrors DefectDojo's own import API exactly — AIST does not
  duplicate per-field rules for any format.
- **No SSRF:** report-supplied endpoint URLs are never fetched by the
  backend. A generic hardening pass in
  `aist/internal_upload.py:import_scan_via_default_importer` — applied to
  *every* import, any scan_type, not just DAST — strips any endpoint whose
  scheme isn't `http`/`https` after import, since `Endpoint.clean()` has no
  scheme allowlist for any format.
- **Rate limited** to bound how many imports one user can trigger (see
  Settings above).
- **No secret in the report path:** the DAST gateway integrator token is
  unrelated to the manual-import upload — importing a report requires no
  gateway credential at all, only project edit access.
- **Storage hygiene:** only the confirm endpoint ever persists the upload,
  and only because the Celery task needs to read it from a separate process;
  it is deleted once the task finishes, success or failure. The validate
  endpoint never touches storage at all.

## Key files

| File | Role |
|------|------|
| `aist/models.py` | `OrgIntegrationType.DAST` |
| `aist/integrations/dast.py` | `dast_env()` (analyzer env injection), `probe_dast_gateway()` (validate) |
| `aist/api/org_integrations.py` | Generic `OrgIntegration` CRUD + validate, DAST-specific config validation |
| `aist/api/report_import.py` | `PipelineImportValidateAPI`, `PipelineImportAPI` — generic validate/confirm endpoints |
| `aist/utils/report_import.py` | `resolve_import_version()` — GIT_HASH version resolution from a commit hash |
| `aist/tasks/report_import.py` | `import_report` Celery task — resolve version, import, provenance, hand off to dedup/finish |
| `aist/internal_upload.py` | `import_scan_via_default_importer()` — shared import call, generic endpoint-scheme hardening |
| `aist/parser_overrides.py` | `DAST_SCAN_TYPE`, `DastReportParser` — owns the `source_commits` routing hint |
| `client-ui/src/pages/OrgIntegrationsPage.tsx` | DAST integration type UI (Gateway URL, Integrator Token) |
| `client-ui/src/components/ImportPipelineDialog.tsx` | Upload → backend-computed preview → commit SHA → confirm → progress UI |
