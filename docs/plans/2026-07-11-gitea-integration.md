# Plan: Add Gitea as an SCM integration

**Date:** 2026-07-11
**Context:** A Gerrit integration was added the day before (`docs/plans/2026-07-10-gerrit-integration.md`)
and a user configuring it against a real internal server (`10.2.40.158:3000`) hit a
"Failed to fetch projects (timeout or task error)" import failure. Live reproduction inside
the actual `aist-celeryworker-1` container (not guesswork) showed the server was answering as
**Gitea** ("Gitea: Git with a cup of tea" on `/`, Gitea's own `/api/v1/version` error shape),
not Gerrit — Gerrit's REST endpoints (`/a/projects/`, `/a/accounts/self`) all 404 there. Gitea
and Gerrit are unrelated products (GitHub-style hosting vs. a Gerrit-style change/review
workflow) with completely different REST APIs. This plan adds Gitea as a first-class provider,
following the same binding-on-`RepositoryInfo` mechanism as GitHub/GitLab/Gerrit.

**Design decisions:**
- **Project name:** `owner/repo`, split on the last `/` — same shape as GitHub, no arbitrary
  nested subgroups (unlike GitLab).
- **Auth:** personal access token in `OrgIntegration.secret`, sent as
  `Authorization: token <PAT>` (Gitea's documented header — not Bearer, not Basic).
  `config["base_url"]` is required (Gitea is always self-hosted).
- **HTTP client:** plain `requests`, not a dedicated Gitea SDK. Checked PyPI for real
  candidates: `giteapy` (auto-generated, frozen since 2020) and `python-gitea` (actively
  released, but its own README says the project/API are still in progress, and it's
  `aiohttp`-first rather than `requests`-based, which doesn't reuse the `requests.Session`
  object the existing VPN-proxy `scoped_session` pattern hands to every other binding).
  Decision confirmed with the user rather than assumed.
- **Shared import workflow:** the Product/RepositoryInfo/binding/AISTProject/DojoMeta creation
  logic was duplicated near-verbatim across `api/github_integration.py`,
  `api/gitlab_integration.py`, and `api/gerrit_integration.py`. Per explicit user feedback
  ("the import of project should be encapsulated in scm, don't move it to integration and
  make huge code, don't mix the responsibilities"), that shared workflow was extracted into
  `aist/scm_import.py` (`import_scm_project` + `ScmImportRequest` dataclass +
  `ScmImportConflict` exception) and `api/gitea_integration.py` only implements the
  Gitea-specific parts (request shape, integration resolution, metadata fetch via the Gitea
  Celery task). **GitHub/GitLab/Gerrit's existing views were intentionally left untouched** —
  refactoring working, tested code to adopt the shared helper wasn't requested; it's a
  reasonable future cleanup if wanted.
- **Typed data:** per user request, Gitea's task-layer JSON parsing uses dataclasses
  (`GiteaRepoSummary`, `GiteaProjectMetadata` in `aist/tasks/integrations.py`) rather than
  passing raw dicts around, converted to plain dicts only at the Celery-task return boundary
  (task results must stay JSON-serializable).
- **Languages:** Gitea's `/api/v1/repos/{owner}/{repo}/languages` returns byte-counts per
  language — same dict shape as GitLab's `.languages()`. `AnalyzersConfigHelper.convert_languages()`
  only reads dict keys, so it works unchanged.

## Files touched

- `aist/models.py` — `ScmType.GITEA`, `OrgIntegrationType.GITEA`, `get_binding()` entry,
  `ScmGiteaBinding` (clone/blob/raw URLs, auth header, `get_project_info`).
- `aist/migrations/0034_gitea_integration.py` — enum choices + `ScmGiteaBinding` table
  (verified against `makemigrations --check --dry-run` — zero drift).
- `aist/scm_import.py` (new) — shared `import_scm_project` workflow + `ScmImportRequest`.
- `aist/tasks/integrations.py` — `_gitea_headers`, `fetch_gitea_projects` (paginated
  `/api/v1/user/repos`), `fetch_gitea_project_info`, `GiteaRepoSummary`/`GiteaProjectMetadata`
  dataclasses.
- `aist/api/gitea_integration.py` — `ImportProjectFromGiteaAPI` (thin; delegates to
  `import_scm_project`).
- `aist/api/org_integrations.py` — `_validate_gitea_attrs` (requires `base_url`),
  `_validate_integration` GITEA branch (`GET /api/v1/user` through `scoped_session`).
- `aist/api/schema.py` — `AISTApiTag.GITEA`.
- `aist/views/integrations.py`, `aist/views/__init__.py`, `aist/urls.py`, `aist/api_urls.py` —
  `gitea_projects_list` view + route, `import_project_from_gitea` route.
- `aist/templates/aist/projects.html` — Import-from-Gitea button/modal/JS, mirroring the
  Gerrit block but keyed by `full_name` instead of a single path.
- `client-ui/src/lib/providerIcons.ts`, `client-ui/src/pages/OrgIntegrationsPage.tsx` —
  Gitea icon/badge/config fields; VPN-integration selector generalized to
  `SELF_HOSTED_SCM_TYPES` (GitLab/Gerrit/Gitea) instead of GitLab-only.
- `aist/integrations/GITEA.md` — user-facing integration doc.
- Tests: `aist/test/test_gitea_binding.py`, `aist/test/test_gitea_integration_api.py`,
  `aist/test/test_org_integration_types.py`, `aist/test/test_org_integrations_api.py`,
  `client-ui/e2e/integrations.spec.ts`.

## Verification

- `manage.py makemigrations aist --check --dry-run` — no drift.
- `manage.py check` — no new warnings.
- Real imports of every new/changed Python module inside the running `aist-uwsgi-1` container
  (not just `ast.parse`).
- Template rendered via `render_to_string` inside the container — new `{% url %}` tags resolve.
- `tsc --noEmit` inside a `node:20-bookworm` container — zero new errors (pre-existing errors
  in unrelated files untouched by this change).
- Full Django test suite for the new/changed test modules run inside `aist-uwsgi-1` — 135 tests,
  6 pre-existing failures unrelated to this change (a middleware quirk specific to that
  always-on container swallows a handful of view-level HTTP responses into a generic 404 page;
  confirmed identical on unmodified git HEAD, and confirmed the view logic itself is correct by
  calling it directly via `RequestFactory`, bypassing the middleware).
- `aist-migration-validator`, `aist-security-checker`, `aist-api-reviewer` subagent review.
  Migration and API review came back clean. Security review found one real HIGH-severity issue
  in the new `aist/scm_import.py` helper (not inherited from Gerrit/GitLab): the binding's
  `org_integration` was reassigned outside the transaction and *before* the org-conflict check,
  so a `(type, repo_owner, repo_name)` collision across two orgs could silently repoint one
  org's binding at another org's credentials even on a rejected import. Fixed by moving that
  reassignment inside `transaction.atomic()`, strictly after the conflict check. Also flagged
  two lower-severity findings that already exist identically in the shipped GitLab/Gerrit code
  (unescaped HTML when rendering repo name/description into the project-listing table; raw
  exception text — including the internal `base_url` — forwarded to the API client on a failed
  metadata fetch instead of a generic message). Per explicit user decision, fixed across all
  three providers: added a shared `escapeHtml()` JS helper used by the GitLab/Gerrit/Gitea
  project-listing table renderers in `projects.html`, and replaced the raw `str(exc)` forwarded
  by `fetch_gitlab_project_info`/`fetch_gitea_project_info` with a generic message (logged
  server-side via `logger.warning`) — `fetch_gerrit_project_info` already used a generic message.
  GitHub's project-listing table has the same unescaped-HTML pattern but was left untouched —
  out of the explicitly agreed scope (Gitea+Gerrit+GitLab only).
