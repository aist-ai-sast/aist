# GitLab integration

GitLab has two independent roles in AIST: an **SCM source-control
integration** (import and clone repositories for pipelines) and a **work-item
provider** (sync GitLab Issues to findings). They use separate credentials and
separate models — configuring one does not configure the other.

## SCM integration

Import projects from GitLab (cloud or self-managed) and use them like
GitHub/Gerrit/Gitea sources (clone for pipelines and Claude analysis, raw-file
viewing, default-branch resolution). GitLab is wired as an SCM provider
through the same `RepositoryInfo` binding mechanism as the others — see
`ScmGitlabBinding` in `aist/models.py`.

### Credentials

GitLab uses a **personal access token** (not OAuth / not a GitLab App):

- **Token** — stored encrypted in `OrgIntegration.secret`. Sent as the
  `PRIVATE-TOKEN` header for API calls, and as `oauth2:<token>` in the HTTPS
  clone URL.
- **Base URL** — the GitLab server root, stored in
  `OrgIntegration.config["base_url"]`. Optional: defaults to
  `https://gitlab.com` when left blank, so no config is needed for
  gitlab.com-hosted projects; set it explicitly for a self-managed instance.

Manage these credentials on the `/integrations` page (React
`OrgIntegrationsPage`) — type **GitLab**. Validation (`/validate` endpoint)
authenticates via `python-gitlab`'s `gl.auth()` through the same VPN-aware
session as normal use.

### Project names

GitLab projects are `path_with_namespace` (e.g. `group/subgroup/name`) — an
arbitrary depth of subgroups is possible, unlike Gitea/GitHub's flat
`owner/repo`. Import splits on the **last** `/` into `repo_owner` (everything
before it, including any subgroups) and `repo_name`, so `repo_full`
reconstructs the full path.

### Languages

GitLab exposes per-project language byte-counts (`langs_raw`, a dict of
language name → count) — the same shape as Gitea's `.languages()` endpoint, so
the shared `AnalyzersConfigHelper.convert_languages()` helper is reused
unchanged.

### VPN

Listing and metadata fetch run in a Celery worker through
`OrgIntegration.scoped_session(...)`, which routes traffic through the VPN
sidecar **only when** a `vpn_integration` is attached — otherwise a direct
session is used. GitLab therefore works both behind a VPN and without one,
the same way as self-hosted Gerrit/Gitea. The default-branch lookup during
import specifically resolves through this VPN-aware session so a
VPN-only-reachable instance does not silently fall back to a wrong "master"
guess.

### Import flow

1. `/integrations` — create a **GitLab** `OrgIntegration` (personal access
   token, optional base URL for self-managed instances).
2. Projects page → **Import from GitLab** → pick the organization → **List
   projects** (calls `gitlab_projects_list` → `fetch_gitlab_projects`) →
   select a project → **Import selected**
   (`ImportProjectFromGitlabAPI` → `fetch_gitlab_project_info`), which creates
   `Product` + `RepositoryInfo(GITLAB)` + `ScmGitlabBinding` + `AISTProject`
   and seeds the initial version from GitLab's real default branch.

## Work-item provider (GitLab Issues)

Implemented in `aist/work_items/backends/gitlab.py` (`GitlabIssuesBackend`,
registered as `"GITLAB"`). Uses its own, separate credentials from the SCM
integration above — a `WorkItemProvider` row, not an `OrgIntegration`.

### Required fields

- `WorkItemProvider.api_token` — a GitLab personal access token or project
  access token.
- `provider_config["project_id"]` — the GitLab numeric project ID, or a
  `"namespace/project"` path.

### Optional fields

- `WorkItemProvider.base_url` — override for a self-managed instance (e.g.
  `https://gitlab.company.com`); leave blank for gitlab.com.

### Status mapping

GitLab issue state maps to AIST's normalized status category:

| GitLab state | Category |
|---|---|
| `opened` | Open |
| `closed` | Done |
| `closed` with `closed_as == "unresolved"` (newer GitLab versions) | Cancelled |

### Setup

Create a work-item provider of type **GitLab** on the organization's
work-item providers page, supplying the access token and project
identifier. See [work-item links](../product/work-item-links.md) for the
sync lifecycle shared by all providers.
