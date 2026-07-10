# Gerrit integration

Import projects from a Gerrit server and use them like GitLab/GitHub sources
(clone for pipelines and Claude analysis, raw-file viewing, default-branch
resolution). Gerrit is wired as an SCM provider through the same
`RepositoryInfo` binding mechanism as GitLab/GitHub — see `ScmGerritBinding` in
`aist/models.py`.

## Credentials

Gerrit uses **HTTP credentials** (not OAuth / not a GitHub App):

- **Username** — the Gerrit account's HTTP username. Stored in
  `OrgIntegration.config["username"]` (required; enforced by the serializer).
- **HTTP password** — generated in Gerrit under *Settings → HTTP Credentials*.
  Stored encrypted in `OrgIntegration.secret`.
- **Base URL** — the Gerrit server root, e.g. `https://gerrit.example.com`.
  Stored in `OrgIntegration.config["base_url"]`.

Authenticated REST and clone use the `/a/` path prefix
(`https://user:password@host/a/<project>`). Manage these credentials on the
`/integrations` page (React `OrgIntegrationsPage`) — type **Gerrit**.

## Project names

Gerrit projects are slash-paths (e.g. `platform/build/soong`) with no
owner/repo split. On import the path is split by the **last** `/` into
`RepositoryInfo.repo_owner` / `repo_name`, so `repo_full` reconstructs the full
path. Single-segment projects (e.g. `All-Projects`) get an empty `repo_owner`;
the binding falls back to `repo_name` so the clone URL has no stray slash.

## Languages

Gerrit's REST API exposes no language statistics, so imported projects start
with **empty** `supported_languages`. Assign languages afterwards via the
project edit form; they drive analyzer selection at pipeline time.

## VPN

Listing and metadata fetch run in a Celery worker through
`OrgIntegration.scoped_session(...)`, which routes traffic through the VPN
sidecar **only when** a `vpn_integration` is attached — otherwise a direct
session is used. Gerrit therefore works both behind a VPN and without one, the
same way as self-hosted GitLab.

## Import flow

1. `/integrations` — create a **Gerrit** `OrgIntegration` (base URL + username +
   HTTP password).
2. Projects page → **Import from Gerrit** → pick the organization → **List
   projects** (calls `gerrit_projects_list` → `fetch_gerrit_projects`) → select
   projects → **Import selected** (`import_project_from_gerrit` →
   `fetch_gerrit_project_info`).
