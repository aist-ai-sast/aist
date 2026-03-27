# AIST — Shared Agent Rules

Universal workflow for all AI agents (Claude Code, Codex, etc.).

## General rules

- Ask before deleting files.
- Prefer minimal diffs — change only what is necessary.
- Follow existing code style enforced by ruff.
- Do not change global settings or access files outside the project.
- Find the CI plan in `.github/workflows/`.
- Add or update tests for every code change, even if not asked.
  Tests must reflect real user scenarios, not synthetic smoke tests.
- Dev environment: ARM macOS. Prod: Ubuntu amd64.
- `vendor/` files are read-only — never modify.
- Use already developed and popular solutions. Do not invent from scratch.
- Solutions must be secure, efficient, flexible, and reusable.
- Import statements at top of file, never inside function scope.
- Solutions must be free of deadlocks and race conditions.

## Architecture

AIST is a SAST aggregation and triage platform:

- `aist/` — core platform: models, REST API (`aist/api/`), async Celery tasks (`aist/tasks/`),
  deduplication (`aist/dedupe/`), SCM integrations, work items, UI views
- `sast-combinator/sast-pipeline/` — analyzer orchestrator : runs SAST tools in isolated
  Docker containers; results written to shared volume, then loaded into platform from files (git submodule)
- `sast-combinator/context_extractor_service/` — MCP server with Tree-sitter AST analysis (git submodule);
  called by external AI triage agents; resolves `pipeline_id` → project path via platform API
- `client-ui/` — React enterprise frontend
- `vendor/` — upstream base platform, read-only (git submodule)

Data flow:
```
sast-pipeline (Docker analyzers) → shared volume → platform ingestion →
deduplication + context enrichment → n8n AI triage →
AI agents → MCP server → verdict → platform DB
```

Key models: `Organization`, `AISTProject`, `AISTProjectVersion`, `AISTPipeline`,
`ProcessedFinding`, `AISTAIFindingResponse` — all in `aist/models.py`.

## Security rules

**Organization isolation — highest priority.**

Every QuerySet touching org-owned data MUST be scoped. Required pattern in `get_queryset()`:

```python
def get_queryset(self):
    qs = Model.objects.all()
    if self.request.user.is_superuser:
        return qs
    return qs.filter(project__organization=self.request.user.aist_organization)
```

- Never use `.all()` without org filter on org-owned models.
- Org hierarchy: User → OrgMembership → Organization → AISTProject → AISTPipeline → Finding.
  Cross-org access is absolutely prohibited even through nested lookups.
- In `context_extractor_service`: all file access must validate path against project root.
  No `..` traversal, no absolute paths outside allowed directory.
  Before adding any file-reading code, check the existing path guard in `mcp_server.py`.
- Docker in `sast-pipeline`: no `privileged: true`, no `network_mode: host` without
  explicit documented justification.

**Security checklist — verify before finalizing any change:**

- [ ] New QuerySets in `aist/` have org filter + superuser bypass in `get_queryset()`.
- [ ] No cross-org access possible through nested object lookups.
- [ ] No `request.data` accessed directly in views — must go through serializer.
- [ ] No `.raw()` or `cursor.execute()` with f-strings or string concatenation.
- [ ] File paths in `context_extractor_service` validated against project root.
- [ ] No hardcoded tokens or passwords outside test fixtures.
- [ ] Docker configs: no privileged mode, no host network without justification.

## REST API patterns

- AIST REST API lives in `aist/api/`. Each domain has its own file.
  Always check a neighboring file before writing a new ViewSet.
- Always override `get_queryset()` — never rely on class-level queryset alone.
- Serializers validate ALL input. Views only call service/model methods.
- Superuser bypass goes in `get_queryset()`, not in `perform_create`/`perform_update`.
- `sast-pipeline` REST client: reuse `DefectDojoClient` session (has retry/backoff).
  Never add bare `requests.get()` calls.

## sast-combinator rules

- `context_extractor` changes: every new or modified MCP tool needs isolated tests in
  `sast-combinator/context_extractor_service/ansible/files/tests/`.
  Tests MUST NOT depend on live `/tmp/aist/projects/` paths — fixtures only.
- New MCP tools must use the `@log_tool` decorator from `mcp_server.py`.
- After adding a new MCP tool, update `aist/ai_triage_system_prompt.md`.
- sast-pipeline analyzer addition requires all four: Dockerfile + `analyze.sh` +
  entry in `pipeline/config/analyzers.yaml` + test.

## Regression fix patterns

### `aist/` (core platform)
- **Model change** → check `aist/migrations/`, run `makemigrations` inside Docker.
- **API regression** → check: org filter in `get_queryset()`, serializer field list,
  `permission_classes` consistent with neighboring endpoints in the same file.
- **Celery task regression** → verify task is idempotent; handles `None` pipeline gracefully.
- **Deduplication regression** → `aist/dedupe/` — finding hash fields drive dedup;
  if a model field changed, hash computation likely needs updating.

### `context_extractor_service` (MCP server)
- **Tool returns wrong result** → reproduce with isolated fixture; fix in `context_extractor/`
  module, NOT in the `mcp_server.py` handler. Use `/mcp-regression-fix` skill.
- **Tool hangs** → add `pytest.mark.xfail(strict=True)` with pipeline_id + file + line.
- **Parser fails on new construct** → check `ts_utils.py` language/node-type mapping first.
- Fix must address the root cause and cover the full family of similar inputs — not just
  the specific failing case.

### `sast-pipeline` (analyzer orchestrator)
- **Analyzer produces no output** → check `analyze.sh` exit codes and output path.
- **Builder container not found** → check container lifecycle in `project_builder.py`.
- **Upload fails** → verify `scan_type` in `analyzers.yaml` matches exact platform importer name.

### `client-ui/` (React frontend)
- **Permission regression** → check `PermissionGate` wraps the control.
- **Data not showing** → check API call parameters match updated serializer fields.

## Frontend rules

- Frontend shows data from backend — no complex logic in UI.
- Use `PermissionGate` for controls requiring Write permissions.
- All elements must match the style of existing elements of the same type.
- Design must align with enterprise-level standards.

## Tests

- Full suite: `run-rest-framework-tests.zsh --clean` and `run-client-ui-tests.zsh --clean`.
  WARNING: long-running, use with caution.
- Do not run tests/npm/npx locally — all environment is in Docker containers.

## Skills

To invoke a skill, read its SKILL.md and follow the instructions exactly.

| Command | Description | File |
|---|---|---|
| `/aist-finding-triage` | Classify findings TP/FP, produce AISTAIFindingResponse | `.codex/skills/aist-finding-triage/SKILL.md` |
| `/context-extractor-mcp-audit` | Replay findings through MCP, produce isolated tests | `.codex/skills/context-extractor-mcp-audit/SKILL.md` |
| `/aist-api-review` | Deep review of REST endpoints for org isolation and patterns | `.codex/skills/aist-api-review/SKILL.md` |
| `/aist-security-check` | Deep audit of changed files for security violations | `.codex/skills/aist-security-check/SKILL.md` |
| `/mcp-tool-add` | Add a new MCP tool with correct patterns, test, system prompt update | `.codex/skills/mcp-tool-add/SKILL.md` |
| `/sast-analyzer-add` | Add a new SAST analyzer: Dockerfile, analyze.sh, config, test | `.codex/skills/sast-analyzer-add/SKILL.md` |
| `/mcp-regression-fix` | Fix failing MCP regression tests with generic root-cause fix | `.codex/skills/mcp-regression-fix/SKILL.md` |
| `/aist-plan` | Create atomic implementation plan before starting a feature | `.codex/skills/aist-plan/SKILL.md` |
| `/aist-debug` | Systematic root-cause debugging for aist/, Celery, dedup, pipeline | `.codex/skills/aist-debug/SKILL.md` |
| `/aist-jira-description` | Generate grouped Jira ticket descriptions from pipeline findings | `.codex/skills/aist-jira-description/SKILL.md` |
