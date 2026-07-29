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

AIST is a SAST aggregation and triage platform. **`docs/README.md` is the canonical
architecture, data-flow, and security reference — read it before any change that crosses
a service boundary.** Do not re-derive architecture facts from memory or from this file;
if something here looks stale relative to `docs/`, `docs/` wins and this file should be
corrected.

Quick file-location index (paths, not responsibilities — see `docs/architecture/` for
responsibility groupings):

- `aist/` — core platform: models, REST API (`aist/api/`), async Celery tasks (`aist/tasks/`),
  deduplication (`aist/dedupe/`), SCM integrations, work items, UI views
- `sast-combinator/sast-pipeline/` — SAST and standalone-provider execution package
  (git submodule); see [pipeline execution runtime](docs/architecture/sast-pipeline-runtime.md)
- `sast-combinator/context_extractor_service/` — MCP server with Tree-sitter AST analysis
  (git submodule); resolves `pipeline_id` → project path via platform API
- `sast-combinator/vpn-sidecar/` — ephemeral VPN container; see
  [VPN integration](docs/integrations/vpn.md) and
  [VPN-routed operations](docs/data-flows/vpn-routed-operations.md)
- `client-ui/` — React enterprise frontend
- `vendor/` — upstream base platform, read-only (git submodule)

Data flow, runtime deployment, and per-domain component ownership:
[platform building blocks](docs/architecture/platform-building-blocks.md),
[runtime deployment](docs/architecture/runtime-deployment.md).
Key models (`Organization`, `AISTProject`, `AISTProjectVersion`, `AISTPipeline`,
`ProcessedFinding`, `AISTAIFindingResponse`) live in `aist/models.py`. The
[platform building blocks](docs/architecture/platform-building-blocks.md) explain
their runtime owners without duplicating a source-symbol inventory.

## Security rules

**Organization isolation — highest priority.**

Every API endpoint that touches an org-owned resource MUST declare a
`ResourcePolicy` and inherit from `AISTAPIView` or `AISTAuthzMixin`. Resolve
objects through the central scoped queryset:

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

- Never fetch an org-owned API object by raw model manager and identifier.
- `aist/authz/policy.py` is the only API-layer mapping from named `Action` values
  to DefectDojo `Permissions`. Direct `Permissions.*` use in `aist/api/` is
  forbidden and enforced recursively by `test_authz_lint.py`.
- For an additional resource inside one endpoint, use
  `self.authorized_queryset(resource=..., action=...)` or the central
  `queryset_for_action(...)` helper. Do not recreate role filtering in a serializer.
- Org hierarchy: User → OrgMembership → Organization → ProductType → Product → AISTProject → AISTPipeline → Finding.
  Cross-org access is absolutely prohibited even through nested lookups. For full members,
  project overrides can only narrow the organization role. Restricted members receive no
  project access from the baseline membership; each explicit project grant defines the role
  for that project and may therefore be Writer or Maintainer. The full model is in
  [tenant isolation and access](docs/security/tenant-isolation-and-access.md).
- In `context_extractor_service`: all file access must validate path against project root.
  No `..` traversal, no absolute paths outside allowed directory.
  Before adding any file-reading code, check the existing path guard in `mcp_server.py`.
- Docker in `sast-pipeline`: no `privileged: true`, no `network_mode: host` without
  explicit documented justification.
- `PUBLIC` and `INTERNAL_SERVICE` are explicit exceptional policies, not substitutes
  for tenant scoping.

**Security checklist — verify before finalizing any change:**

- [ ] Every org-owned API object is resolved through `ResourcePolicy` and a registered getter.
- [ ] No direct `Permissions.*` reference exists under `aist/api/`.
- [ ] No cross-org access possible through nested object lookups.
- [ ] No `request.data` accessed directly in views — must go through serializer.
- [ ] No `.raw()` or `cursor.execute()` with f-strings or string concatenation.
- [ ] File paths in `context_extractor_service` validated against project root.
- [ ] No hardcoded tokens or passwords outside test fixtures.
- [ ] Docker configs: no privileged mode, no host network without justification.
- [ ] Touching API tokens? Preserve the token's organization binding and intersect it with
      the owner's current role, project restrictions, expiry, and method scope. See
      [access control and roles](docs/security/access-control-and-roles.md#aist-personal-access-tokens).
- [ ] Touching local-file serving? Resolve the requested path, verify that it remains under
      the authorized project root, and cover traversal and symlink cases with tests. Report
      any suspected vulnerability privately according to [SECURITY.md](SECURITY.md); do not
      describe an exploitable, unpatched path in public documentation.

## REST API patterns

- AIST REST API lives in `aist/api/`. Each domain has its own file.
  Always check a neighboring file before writing a new ViewSet.
- Declare `authz = ResourcePolicy(...)` on every tenant endpoint. Generic views
  return `authorized_queryset_for_request()` from `get_queryset()`; plain views
  use `resolve()` and `authorized_queryset()`.
- Serializers validate ALL input. Views only call service/model methods.
- Superuser and token behavior belongs in the registered query getter, not in a view
  or serializer branch.
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
- **API regression** → check: `ResourcePolicy`, named `Action`, registered scoped
  getter, serializer field list, and neighboring endpoint behavior.
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
| `/security-architecture-maintainer` | Update architecture documentation and threat models from an affected diff | `.codex/skills/security-architecture-maintainer/SKILL.md` |
