---
name: aist-project-profile-analyzer
description: Analyze a cloned project repository to generate path exclusion patterns for the AIST project profile, reducing false positives by excluding non-production code from SAST analysis.
---

# AIST Skill: Generate Project Profile Exclusions

## Purpose

Analyze a project repository and generate **path exclusion patterns** for SAST analysis.
The goal is to identify directories and file patterns that should be excluded from scanning
(tests, documentation, vendored dependencies, generated code, etc.) to reduce noise and
false positives.

---

## Inputs

* `project_id` **(required)**
  AIST project ID. Used to persist the exclusion list in the platform database.

* `source_path` **(required)**
  Absolute path to the cloned repository to analyze.

---

## Rules

1. Use `docker compose` only when querying or persisting project data. Do not address containers directly.
2. Analyze only the repository at `source_path`. Do not access other paths.
3. The skill must persist results in the project database.
4. Write all reasoning in English.
5. Do not include paths that contain production source code.
6. Prefer broader patterns over many narrow ones (e.g. `test/` over `test/unit/`, `test/integration/`).

---

## Workflow

### Phase 1 — Analyze directory structure

List the top-level and second-level directory tree of `source_path`.

Identify directories that typically contain non-production code:
* `test/`, `tests/`, `__tests__/`, `spec/`, `testing/`
* `docs/`, `documentation/`, `doc/`
* `vendor/`, `node_modules/`, `third_party/`, `external/`
* `examples/`, `samples/`, `demo/`, `playground/`
* `benchmarks/`, `bench/`, `perf/`
* `.github/`, `.gitlab/`, `.circleci/`
* `scripts/`, `tools/` (if they contain only build tooling)
* `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`
* `dist/`, `build/`, `out/`, `target/`
* `fixtures/`, `testdata/`, `test-fixtures/`
* `e2e/`, `cypress/`, `playwright/`
* `storybook/`, `.storybook/`
* `mock/`, `mocks/`, `__mocks__/`

### Phase 2 — Analyze project configuration files

Read and extract exclusion hints from:
* `.gitignore` — what the project itself excludes from version control
* `.dockerignore` — what is excluded from production Docker images
* CI configs (`.github/workflows/*.yml`, `.gitlab-ci.yml`, `Jenkinsfile`) — paths excluded from CI
* `tsconfig.json` / `tsconfig.*.json` — `exclude` field
* `pyproject.toml` — `tool.pytest.ini_options.testpaths`, `tool.ruff.exclude`
* `package.json` — `jest.testPathIgnorePatterns`, `jest.roots`

### Phase 3 — Determine exclusion patterns

Based on phases 1-2, produce a list of relative path patterns to exclude from SAST.

Rules for pattern selection:
* Only exclude paths that exist in the repository
* Use directory paths with trailing `/` (e.g. `tests/`)
* Prefer parent directories over individual files
* Do not exclude paths that contain production source code mixed with tests
* When in doubt, do NOT exclude — false negatives are worse than false positives

### Phase 4 — Persist to database

Update `AISTProject.profile.paths.exclude` via `docker compose exec`:

```
docker compose exec uwsgi python manage.py shell -c "
from aist.models import AISTProject
project = AISTProject.objects.get(id=<project_id>)
profile = project.profile or {}
paths = profile.get('paths', {})
paths['exclude'] = <generated_exclusion_list>
profile['paths'] = paths
project.profile = profile
project.save(update_fields=['profile'])
print(f'Updated profile.paths.exclude for project {project.id}: {paths[\"exclude\"]}')
"
```

---

## Output contract

Primary outcome: persisted `AISTProject.profile.paths.exclude` in the database.

Assistant response: concise summary of what directories were analyzed, what patterns were
chosen and why, and confirmation of database update.

---

## Constraints

* DO NOT exclude directories that contain production source code
* DO NOT guess — only exclude paths that actually exist in the repository
* DO NOT modify any other profile fields (severity, ai_triage, etc.)
* DO NOT add patterns for files/directories not present in the repository
* DO NOT over-exclude — when in doubt, keep the path in scope

---

## Success criteria

* `AISTProject.profile.paths.exclude` contains a curated list of path patterns
* All patterns correspond to directories that actually exist in the repository
* No production code directories are excluded
* Exclusions reduce SAST noise without missing real vulnerabilities

---

## Example

```id="aist-skill-example"
Use this skill:

project_id=42
source_path="/tmp/aist/projects/my-project/codex-analysis"
```

Result:

* Analyzed directory structure and config files
* Generated exclusions: `["tests/", "docs/", "node_modules/", "examples/", ".github/", "__pycache__/"]`
* Persisted to `AISTProject.profile.paths.exclude`
