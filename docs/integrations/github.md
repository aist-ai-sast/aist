# GitHub integration

GitHub has two independent roles in AIST: an **SCM source** for repository
import and pipeline clones (via a GitHub App installation), and a **work-item
provider** for GitHub Issues (via a PAT or the same App installation). They
use different auth mechanisms and are documented separately below.

## SCM: repository import

### Credentials — GitHub App, not a stored token

Unlike Gerrit/Gitea (HTTP username+password / PAT), GitHub SCM access uses a
**GitHub App installation**, not a credential stored on `OrgIntegration`:

- The `OrgIntegration` row for `integration_type=GITHUB` carries no secret.
  `_validate_integration`'s GITHUB branch (`aist/api/org_integrations.py`)
  literally returns *"GitHub uses App-level auth; no credential stored."*
- Per-repository auth lives on `ScmGithubBinding.installation_id`
  (`aist/models.py`), which points at a `django_github_app.models.Installation`
  row. Access tokens are minted per request from the installation
  (`installation.aget_access_token`), never stored long-term.
- Optional **GitHub Enterprise Server** support: `OrgIntegration.config["base_api_url"]`
  holds the GHES API base; `ScmGithubBinding._base_api_url()` reads it.
- The App itself is configured once, server-side, via `settings.GITHUB_APP["NAME"]`
  (used to build the `https://github.com/apps/<name>/installations/new` install URL).

### Project names

GitHub repos are `owner/repo` (no nested subgroups, like Gitea) — `RepositoryInfo.repo_owner`
/ `repo_name` split on the single `/`.

### Languages

Fetched via `GET /repos/{owner}/{repo}/languages` at import or link time
(`_afetch_repository_details` in `aist/api/github_integration.py`), converted
through the same `AnalyzersConfigHelper.convert_languages()` helper used by
GitLab and Gitea.

### Import flow

Both flows below share one GitHub App install redirect and a signed, single-use
state token (`GithubConnectState`) — the difference is only in what the
callback does with the resulting `installation_id`.

1. **Bring in new repositories**: Projects page → **Connect GitHub** → redirect
   to `https://github.com/apps/<app>/installations/new?state=...` → user
   installs the app / picks repos on GitHub → GitHub redirects back to the
   connect callback (`GithubConnectCallbackAPI`), which resolves or creates the
   `Installation` row and caches it against the organization. Then: **list
   repositories** (`GithubImportRepositoriesAPI`) → select repos → **import**
   (`GithubImportExecuteAPI` → `_import_github_repository`), which creates the
   Product/AISTProject/RepositoryInfo/`ScmGithubBinding`, seeds the initial
   version from the repository's real default branch, and records languages
   for analyzer selection.
2. **Attach an existing AIST project to a repository**: project edit page →
   **Connect GitHub** (`GithubProjectConnectStartAPI`, same install redirect,
   scoped to that project) → **list repositories for the installation**
   (`GithubProjectRepositoriesAPI`) → **link one repository**
   (`GithubProjectLinkRepositoryAPI`), which upserts the binding and
   `RepositoryInfo` and refreshes languages/description.

## Work-item provider: GitHub Issues

### Credentials

`GithubIssuesBackend` (`aist/work_items/backends/github.py`) tries two auth
paths in order:

1. `WorkItemProvider.api_token` (a PAT or fine-grained token), sent as
   `Authorization: Bearer <token>`.
2. If no token is set, it falls back to the organization's existing GitHub App
   installation — the first `ScmGithubBinding` found for any project in the
   org — so a project already imported via the App needs zero extra
   configuration to also sync GitHub Issues.

`base_url` is optional: when blank it defaults to `https://api.github.com`, or
to the GHES `base_api_url` recorded on a `ScmGithubBinding`'s org integration
if one exists — the same GHES support as the SCM side.

`provider_config` needs no `repo_owner`/`repo_name` in the normal case — the
backend parses `owner/repo` straight from each link's GitHub issue URL
(`external_url`). Set `provider_config = {"repo_owner": ..., "repo_name": ...}`
only as a fallback for links where that parse fails.

### Status mapping

GitHub issue `state` (`open`/`closed`) maps to `WorkItemStatusCategory`
(`OPEN`/`DONE`). A closed issue with `state_reason == "not_planned"` maps to
`CANCELLED` instead of `DONE`.

## VPN

Two independent code paths, two different levels of VPN support:

- **SCM clone/API** (`ScmGithubBinding`, GitHub App installation tokens) does
  not thread a proxy today. A GitHub Enterprise instance reachable only over
  VPN is not yet covered by clone or metadata calls — see the "GitHub
  limitation" note in [VPN integration](vpn.md). GitHub Cloud needs no VPN and
  works normally.
- **Work-item sync** (`GithubIssuesBackend`) goes through the standard
  `WorkItemBackend.scoped_context()` VPN path like every other provider: if
  `WorkItemProvider.vpn_integration` is set and active, requests route through
  the resolved VPN sidecar's proxy; otherwise they go direct.

## Known limitations

- GHES behind a VPN is unsupported for the SCM clone/API path (see above);
  issue sync for the same GHES host works if a VPN integration is attached to
  the work-item provider, since that path does support proxying.
- verify in code: whether `provider_config['repo_owner'/'repo_name']` can be
  set from the client-ui `OrgIntegrationsPage`/work-item provider form, or
  only via direct API calls — no UI reference was found while writing this.
