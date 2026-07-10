# Plan: Add Gerrit as an SCM integration

**Date:** 2026-07-10
**Context:** The platform integrates SCM sources (GitLab, GitHub) through a duck-typed
"binding" model on `RepositoryInfo` — there is **no abstract base class**. Each provider
adds a `ScmType`, an `OrgIntegrationType`, a `Scm<Provider>Binding` model implementing 6
methods (`host`, `build_clone_url`, `build_blob_url`, `build_raw_url`, `get_auth_headers`,
`get_project_info`), and a `get_binding()` mapping entry. Everything downstream (pipeline
clone `pipeline_args.py:223`, Claude clone `tasks/claude.py:119`, raw-file read
`api/files.py:135`, default-branch resolution `celery_signals.py:42`) dispatches generically
through `RepositoryInfo.get_binding()`/`clone_url` and needs **no change** once the binding
exists. Import is a server-rendered Django flow (`templates/aist/projects.html` modal →
`views/integrations.py` listing → `api/*_integration.py` create), credentials are managed in
`OrgIntegration` (secret encrypted) via the React `OrgIntegrationsPage.tsx`. The change adds a
Gerrit provider paralleling the GitLab path. Risk areas: (a) Gerrit's REST XSSI `)]}'` prefix
and `/a/` auth prefix; (b) Gerrit project names are slash-paths with no owner/repo split; (c)
Gerrit REST exposes no language stats; (d) a new PyPI dependency (`pygerrit2`) requires a
Docker image rebuild; (e) org isolation must hold on the new import endpoint and listing view.

**Design decisions already made:**
- **Project name:** split by last `/` → `repo_owner` = everything before, `repo_name` = last
  segment. The `_repo_part_validator` regex (`models.py:29`) already allows `/`, so
  `repo_full` (`owner/name`) reconstructs the full Gerrit path losslessly — clone/raw URLs are
  built from `repo_full`; **no new model field needed**.
- **Auth:** HTTP user in `OrgIntegration.config["username"]`, HTTP password in
  `OrgIntegration.secret` (encrypted). Authenticated REST/clone via the `/a/` path prefix.
  Library: `pygerrit2` (handles XSSI prefix + HTTP-basic auth).
- **Languages:** Gerrit has no language API → `supported_languages` is left empty at import;
  user assigns languages via the **existing** project-edit form (`forms.py:149`). (Auto-detect
  from clone is a possible follow-up — see Open questions.)

**Estimated tasks:** 12

---

## Tasks

### Task 1: Add Gerrit enum values

**Test first:** In `aist/test/test_org_integration_types.py` (and a new assert in a scm test),
assert `ScmType.GERRIT == "GERRIT"` and `OrgIntegrationType.GERRIT == "GERRIT"` exist.
Expected failure: `AttributeError: GERRIT`.

**Implementation:** Add `GERRIT = "GERRIT", "Gerrit"` to `ScmType` (`aist/models.py:43`) and
`GERRIT = "GERRIT", "Gerrit"` to `OrgIntegrationType` (`aist/models.py:350`). Add the
`ScmType.GERRIT: "gerrit_binding"` entry to `RepositoryInfo.get_binding()` (`:60`).

**Verify:** `python manage.py test aist.test.test_org_integration_types`. Passes.

**Commit:** `Add GERRIT to ScmType and OrgIntegrationType enums`

---

### Task 2: Add `ScmGerritBinding` model implementing the binding contract

**Test first:** New `aist/test/test_gerrit_binding.py` mirroring `test_gitlab_binding.py`.
Create `RepositoryInfo(type=GERRIT, repo_owner="platform/build", repo_name="soong",
base_url="https://gerrit.example.com")`, an `OrgIntegration(GERRIT, secret="httppass",
config={"username":"svc"})`, and `ScmGerritBinding(scm=repo, org_integration=...)`. Assert:
- `build_clone_url(repo)` == `https://svc:httppass@gerrit.example.com/a/platform/build/soong`
- `build_raw_url(repo, "main", "src/a.c")` hits the Gerrit content endpoint for the full path
- `get_auth_headers()` returns HTTP-basic `Authorization` header
- `build_clone_url` returns `None` when username or secret missing

Expected failure: `ImportError: cannot import name 'ScmGerritBinding'`.

**Implementation:** Add `ScmGerritBinding` to `aist/models.py` after `ScmGitlabBinding`
(~`:260`). OneToOne `scm` (`related_name="gerrit_binding"`), FK `org_integration`
(`limit_choices_to={"integration_type":"GERRIT"}`, `related_name="+"`). Helpers `_username()`
(from `org_integration.config`) and `_password()` (from `org_integration.secret`). Implement
the 6 methods; build URLs from `scm.repo_full` with the `/a/` prefix; Gerrit raw endpoint:
`GET {base}/a/projects/{url-quoted repo_full}/branches/{ref}/files/{url-quoted path}/content`
(base64) — mirror GitLab's `build_raw_url` quoting. `get_project_info` uses `pygerrit2`
`GerritRestAPI` to `GET /projects/{repo_full}` and `/projects/{repo_full}/HEAD` for
`default_branch`, returning a dict with `default_branch`. Keep imports at top of file.

**Verify:** `python manage.py test aist.test.test_gerrit_binding`. Passes.

**Commit:** `Add ScmGerritBinding implementing SCM binding contract`

---

### Task 3: Migration for Gerrit choices + `ScmGerritBinding` table

**Test first:** `python manage.py makemigrations --check --dry-run` must report the migration
is present after generation (CI guard); add the new file to `max_migration.txt`.

**Implementation:** Run `makemigrations aist` **inside Docker** → `0033_gerrit_integration.py`
(alters `ScmType`/`OrgIntegrationType` choices + creates `ScmGerritBinding`). Update
`aist/migrations/max_migration.txt` to `0033_gerrit_integration`.

**Verify:** `python manage.py migrate aist` in Docker; `makemigrations --check` clean.

**Commit:** `Add migration 0033 for Gerrit binding and enum choices`

---

### Task 4: Add `pygerrit2` dependency

**Test first:** N/A (dependency). Guarded by Task 2/5 tests importing `pygerrit2` at module top.

**Implementation:** Add `pygerrit2==<pinned>` to root `requirements.txt` (not the read-only
`vendor/` file). Rebuild the backend Docker image.

**Verify:** `python -c "import pygerrit2"` inside the rebuilt image.

**Commit:** `Add pygerrit2 dependency for Gerrit integration`

---

### Task 5: `fetch_gerrit_projects` Celery task

**Test first:** In `aist/test/test_gerrit_integration_api.py` (new), patch `pygerrit2` and
assert `fetch_gerrit_projects(integration_id)` returns
`{"ok": True, "projects": [{"name": ..., "web_url": ..., "default_branch": ...}, ...]}` and
that it opens `integration.scoped_session(...)`. Expected failure: task does not exist.

**Implementation:** Add `fetch_gerrit_projects` to `aist/tasks/integrations.py`, paralleling
`fetch_gitlab_projects` (`:11`): `select_related` vpn, read
`config["base_url"]` + `config["username"]` + `secret`, open
`integration.scoped_session(execution_id=f"gerrit-list-{id}")`, call Gerrit
`GET /a/projects/?d` via `pygerrit2.GerritRestAPI(auth=HTTPBasicAuth(...), session=...)`,
normalize to the project dicts (no `language` — Gerrit has none). Import `pygerrit2` at top.

**Verify:** `python manage.py test aist.test.test_gerrit_integration_api` (task tests). Passes.

**Commit:** `Add fetch_gerrit_projects Celery task`

---

### Task 6: `fetch_gerrit_project_info` Celery task

**Test first:** In the same test file, patch `pygerrit2` and assert
`fetch_gerrit_project_info(integration_id, "platform/build/soong")` returns
`{"ok": True, "project_path": "platform/build/soong", "description": ..., "web_url": ...,
"default_branch": ...}` and `{"ok": False, "response_code": 404}` on a not-found. Expected
failure: task does not exist.

**Implementation:** Add `fetch_gerrit_project_info` to `aist/tasks/integrations.py`
paralleling `fetch_gitlab_project_info` (`:59`). Uses `scoped_session`; resolves default
branch via `/projects/{path}/HEAD`; `inferred_base` = `config["base_url"]`. No `langs_raw`
(empty). Import `pygerrit2` at top.

**Verify:** `python manage.py test aist.test.test_gerrit_integration_api`. Passes.

**Commit:** `Add fetch_gerrit_project_info Celery task`

---

### Task 7: `ImportProjectFromGerritAPI` endpoint

**Test first:** In `aist/test/test_gerrit_integration_api.py`, mirror the GitLab API tests
(happy path 201, 404, requires-organization 400, requires-active-integration 404, product-type
conflict 409). Happy path: `project_path="platform/build/soong"` →
`repo_owner="platform/build"`, `repo_name="soong"`, `ScmType.GERRIT`, binding bound to the
integration, `AISTProject.organization` set, empty `supported_languages`. Expected failure:
endpoint/URL name does not exist.

**Implementation:** New `aist/api/gerrit_integration.py` paralleling `gitlab_integration.py`.
`permission_classes = [IsAuthenticated]`. Request serializer takes `project_path` (str),
`organization_id`, `auto_analyze`. Resolve org via
`get_authorized_aist_organizations(Permissions.Product_Type_Add_Product, ...)` (**org
isolation**). Resolve the active `OrgIntegrationType.GERRIT` integration. Call
`fetch_gerrit_project_info.delay(...)`. Split `project_path` by last `/` →
`owner_ns, repo_name`. Create `Product` (name = full path) + `DojoMeta("scm-type"="gerrit")` +
`RepositoryInfo(GERRIT, base_url=inferred_base)` + `ScmGerritBinding` bound to integration +
`AISTProject(supported_languages=[], repository=..., organization=...)` +
`_create_initial_script`. Reuse `analyze_project_after_import` when `auto_analyze`.

**Verify:** `python manage.py test aist.test.test_gerrit_integration_api`. All pass.

**Commit:** `Add ImportProjectFromGerritAPI endpoint`

---

### Task 8: Route the import endpoint + API tag

**Test first:** Assert `reverse("aist_api:import_project_from_gerrit")` resolves (add to the
API test's `_url()`); the happy-path test from Task 7 exercises the route.

**Implementation:** Add `AISTApiTag.GERRIT = "gerrit"` to `aist/api/schema.py`. Add the URL to
`aist/api_urls.py` next to the GitLab import route (`:210`), importing the new view.

**Verify:** `python manage.py test aist.test.test_gerrit_integration_api`. Passes.

**Commit:** `Route Gerrit import endpoint and add API tag`

---

### Task 9: `gerrit_projects_list` server view + route + `_validate_gerrit_attrs`

**Test first:** (a) A view test: authenticated POST with `organization_id` returns the task's
project list; missing org → 400; unauthorized org → 404 (**org isolation**). (b) In
`test_org_integrations_api.py`, assert creating a `GERRIT` integration without
`config["username"]` raises a 400 validation error.

**Implementation:** Add `gerrit_projects_list` to `aist/views/integrations.py` paralleling
`gitlab_projects_list` (org-authorization check via `get_authorized_aist_organizations`,
`fetch_gerrit_projects.delay(...).get(...)`). Add its route to `aist/urls.py` next to
`projects/gitlab/list/` (`:64`). Add `_validate_gerrit_attrs` to `OrgIntegrationSerializer`
(`api/org_integrations.py:262`) requiring `config["username"]` for GERRIT.

**Verify:** `python manage.py test aist.test.test_gerrit_integration_api
aist.test.test_org_integrations_api`. Passes.

**Commit:** `Add Gerrit project-listing view and integration validation`

---

### Task 10: Gerrit import modal in the Django template

**Test first:** Manual/UI (no unit test for the template). Optionally extend an existing
projects-view render test to assert the `gerrit-import-open` button id is present.

**Implementation:** In `aist/templates/aist/projects.html` add a third "Import from Gerrit"
button (near `:29`/`:35`) and a `gerritImportModal` (mirror the GitLab modal at `:307-411`):
org select → list projects via `projects/gerrit/list/` → checkbox table → "Import selected"
POSTing `project_path` to `import_project_from_gerrit`. Mirror the GitLab inline JS block
(`:1001+`). Since Gerrit projects are slash-paths, the row key is `project_path` (not numeric
id).

**Verify:** Load the projects page in the dev stack; import a project from a test Gerrit.

**Commit:** `Add Gerrit import modal to projects page`

---

### Task 11: Gerrit in the React OrgIntegrations credential UI

**Test first:** Extend `client-ui/e2e/integrations.spec.ts` to create a GERRIT integration
with base URL + username + secret and assert it saves and renders the badge.

**Implementation:** In `client-ui/src/pages/OrgIntegrationsPage.tsx`: add `"GERRIT"` to the
`IntegrationType` union (`:42`), the type list (`:45`), label map (`:54`), and color map
(`:68`); add a `type === "GERRIT"` config block (`:364` area) with a **Gerrit URL** (`base_url`)
field and a **Username** (`config.username`) field. Secret uses the existing secret input.

**Verify:** `run-client-ui-tests.zsh` (Gerrit e2e case) in Docker.

**Commit:** `Add Gerrit to OrgIntegrations credential UI`

---

### Task 12: End-to-end smoke + docs

**Test first:** A `TestCase` that creates a GERRIT `OrgIntegration`, mocks `pygerrit2`, calls
the import endpoint, and asserts a runnable `AISTProject` with a Gerrit `clone_url` of the form
`https://user:pass@host/a/<full/path>` (integration-level assertion tying the layers together).

**Implementation:** No new production code expected; wire any gaps found. Add a short "Gerrit"
section to the integrations docs (mirroring `VPN.md` style) covering credentials
(username + HTTP password), base URL, and the `/a/` prefix.

**Verify:** `python manage.py test aist.test.test_gerrit_integration_api`. All pass.

**Commit:** `Add Gerrit end-to-end smoke test and docs`

---

## Security checklist (per repo rules)

- [x] New QuerySets are org-scoped: import endpoint (Task 7) and listing view (Task 9) both
  gate on `get_authorized_aist_organizations(...)`; no `.all()` on org-owned models.
- [x] No cross-org access through the integration lookup — integration is filtered by the
  authorized `organization`.
- [x] `request.data` only via serializer (Task 7 uses `ImportGerritRequestSerializer`).
- [x] No `.raw()`/`cursor.execute()`; all Gerrit calls go through `pygerrit2` + `scoped_session`.
- [x] Secret (HTTP password) stored in `OrgIntegration.secret` (`EncryptedCharField`), never in
  `config`; masked in error details (mirror the GitLab masking test).
- [x] No hardcoded credentials outside test fixtures.
- [ ] `aist-api-reviewer` + `aist-security-checker` to run after Tasks 7 & 9 (auto-triggered).

## Open questions — RESOLVED (2026-07-10)

1. **Languages — RESOLVED.** Import Gerrit projects with **empty** `supported_languages`; user
   assigns languages via the existing project-edit form (`forms.py:149`). No auto-detect step in
   this scope. (There is currently no clone-based language detection anywhere in the code;
   auto-detect would be a separate `sast-pipeline` effort if wanted later.)
2. **`pygerrit2` — RESOLVED (default).** Use `pygerrit2` (Task 4): standard library, handles the
   XSSI `)]}'` prefix + HTTP-basic auth, matches the "use popular solutions" repo rule. New
   dependency + Docker image rebuild accepted.
3. **Auth — RESOLVED (default).** MVP assumes authenticated access via `/a/` with an HTTP
   password; `config["username"]` is required (Task 9 validation). No anonymous-clone fallback in
   this scope; `build_clone_url` returns `None` when credentials are missing.
4. **VPN optional — RESOLVED.** Gerrit may run with or without VPN. `scoped_session` already
   handles both — it routes through the VPN sidecar only when `vpn_integration` is set on the
   integration, otherwise a direct session. **No plan change required**; same mechanism as
   self-hosted GitLab.
