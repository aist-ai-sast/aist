# Gitea integration

Import projects from a Gitea server and use them like GitLab/GitHub/Gerrit sources
(clone for pipelines and Claude analysis, raw-file viewing, default-branch
resolution). Gitea is wired as an SCM provider through the same
`RepositoryInfo` binding mechanism as the others — see `ScmGiteaBinding` in
`aist/models.py`.

## Credentials

Gitea uses a **personal access token** (Settings → Applications → Generate New Token):

- **Token** — stored encrypted in `OrgIntegration.secret`. Sent as
  `Authorization: token <PAT>` — Gitea's documented header format (not Bearer,
  not Basic).
- **Base URL** — the Gitea server root, e.g. `https://gitea.example.com`.
  Stored in `OrgIntegration.config["base_url"]` (required — Gitea is always
  self-hosted, there is no public default the way `gitlab.com` is for GitLab).

No dedicated Gitea Python SDK is vendored: the two options on PyPI at the time
this was written were `giteapy` (auto-generated, last released 2020) and
`python-gitea` (actively released, but the maintainer's own README says the
project and its API are still in progress, and it's `aiohttp`-first rather
than `requests`-based). Both are a worse bet than `requests` directly against
Gitea's stable, documented `/api/v1` REST surface — the same call already made
for the client-side README you're reading now. If a mature `requests`-based
Gitea SDK becomes available later, revisit this.

## Project names

Gitea projects are `owner/repo` (no arbitrary nested subgroups, unlike
GitLab) — same shape as GitHub. Import splits on the last `/` into
`RepositoryInfo.repo_owner` / `repo_name`.

## Languages

Gitea exposes per-repo language byte-counts via
`GET /api/v1/repos/{owner}/{repo}/languages` — same shape as GitLab's
`.languages()` (dict of language name → count), so the existing
`AnalyzersConfigHelper.convert_languages()` helper (which only reads dict
keys) works unchanged.

## VPN

Listing and metadata fetch run in a Celery worker through
`OrgIntegration.scoped_session(...)`, which routes traffic through the VPN
sidecar **only when** a `vpn_integration` is attached — otherwise a direct
session is used. Same pattern as GitLab/Gerrit.

## Import flow

1. `/integrations` — create a **Gitea** `OrgIntegration` (base URL + personal
   access token), optionally attaching a VPN integration for self-hosted
   servers reachable only over VPN.
2. Projects page → **Import from Gitea** → pick the organization → **List
   projects** (calls `gitea_projects_list` → `fetch_gitea_projects`) → select
   repos → **Import selected** (`import_project_from_gitea` →
   `fetch_gitea_project_info` → the shared `aist.scm_import.import_scm_project`
   workflow, also used by the Gitea binding's sibling providers).
