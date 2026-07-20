# VPN Integration — How AIST Uses the VPN Sidecar

## Purpose

Some clients host Jira, GitLab, YouTrack, and other systems on an internal
corporate network that is not reachable from the internet. AIST spins up a
temporary VPN container for the duration of a single operation, routes the
relevant traffic through it, and removes it immediately after. There is no
permanent VPN connection on the server.

For the technical deep-dive into the sidecar itself (Dockerfile, entrypoint,
troubleshooting): `sast-combinator/vpn-sidecar/README.md`.

---

## Architecture inside AIST

```
aist/models.py
  OrgIntegration (type=VPN)
    └── OrgIntegrationVPNSecret     ← encrypted credentials

  OrgIntegration (type=GITLAB / JIRA / …)
    └── vpn_integration → OrgIntegration (type=VPN)   ← "this integration
                                                          requires VPN"
  WorkItemProvider
    └── vpn_integration → OrgIntegration (type=VPN)   ← same for work-item
                                                          providers

aist/integrations/resolver.py
  resolve_integration(project, OrgIntegrationType.VPN)
    → ResolvedIntegration | None    ← None = no VPN, code runs without tunnel

aist/utils/vpn.py
  vpn_sidecar_context(resolved, execution_id=...)
    → context manager: (container_name, proxy_url)   ← both None when no VPN
```

---

## Which traffic goes through VPN and which does not

| Operation | Via VPN | Notes |
|-----------|---------|-------|
| Validate GitLab / Jira / YouTrack integration | Yes | `proxy_url` passed to `requests.Session` |
| Work-item sync (issue status updates) | Yes | `vpn_sidecar_context` in `aist/work_items/sync.py` |
| SAST pipeline builder (git clone, SCM API) | Yes | Builder runs with `--network container:aist-vpn-<id>` |
| SAST result ingestion into the platform | No | Internal Celery → Django call, no external system involved |
| AI triage, MCP server | No | Do not call client systems |
| Deduplication | No | Operates on data already in the DB |
| n8n webhooks | No | n8n initiates requests to the platform, not the other way around |
| UI file blob / findings-list snippet | Yes | Via a **warm per-VPN egress** proxy (see below), not the ephemeral sidecar |
| Django admin, REST API (other) | No | Incoming traffic; VPN does not apply |

**Rule of thumb:** VPN is used only when AIST makes an outbound HTTPS request to
a client-controlled system. If that system is on the public internet, VPN is not
needed and no container is started.

---

## How VPN is linked to other integrations

The link is stored in the `vpn_integration` FK on `OrgIntegration` and
`WorkItemProvider`. Callers resolve and apply it as follows:

```python
# aist/tasks/pipeline.py
vpn_resolved = resolve_integration(pipeline.project, OrgIntegrationType.VPN)
with vpn_sidecar_context(vpn_resolved, execution_id=pipeline_id) as (vpn_container, _proxy):
    ...

# aist/api/org_integrations.py (validate endpoint)
vpn = integration.vpn_integration
if vpn and vpn.vpn_secret.ovpn_content:
    with vpn_sidecar_context(...) as (_, proxy_url):
        session.proxies = {"https": proxy_url}
        ...

# aist/work_items/sync.py
with vpn_sidecar_context(vpn_resolved, ...) as (_, proxy_url):
    backend = get_backend(provider, proxy_url=proxy_url)
    ...
```

If `resolve_integration` returns `None`, or `ovpn_content` is empty,
`vpn_sidecar_context` immediately returns `(None, None)` without starting any
container. The calling code does not need to change.

---

## Warm per-VPN egress (UI file blobs)

The blob endpoint (`aist/api/files.py`) runs in the **web** process, which has no
Docker socket and cannot absorb the ~30 s OpenVPN cold start on the request path.
So instead of the ephemeral one-container-per-execution model, interactive file
fetches use a **long-lived egress sidecar, one per VPN integration**, started
ahead of time from a Celery worker and reused across requests.

Concern owner: `aist/integrations/egress.py` (naming, VPN resolution, lifecycle).
Low-level container mechanics stay in `aist/utils/vpn.py`; the blob endpoint only
resolves a proxy URL and connects.

- **Name / discovery:** deterministic `aist-vpn-egress-<vpn_integration_id>`;
  proxy is `http://aist-vpn-egress-<vpn_integration_id>:1080`. The web process
  derives this from the *authorized* project version — never from user input —
  so no external registry is needed and one org can never reach another org's
  tunnel. Docker is the source of truth (liveness = does the connect succeed).
- **Keyed by `vpn_integration_id`, not org:** one org may own several VPN
  integrations for distinct internal networks. A VPN integration belongs to
  exactly one org, so per-VPN keying still guarantees org isolation. A
  defense-in-depth cross-org guard in `vpn_integration_for_project_version`
  ignores any SCM binding that points at another org's VPN.
- **Separate pool from the pipeline sidecar:** the analyzer keeps its own
  ephemeral `aist-vpn-<execution_id>`; UI fetches never share a container or lock
  with it, so browsing blobs can never block an analysis.
- **Cold path:** if the tunnel is not up, the blob endpoint answers
  `202 {"status":"warming"}` and enqueues `prewarm_egress`; the UI retries. First
  view of an idle VPN pays the ~30 s once, server-wide (shared by all users);
  subsequent fetches are fast until it idles out.
- **Pre-warm:** `POST /projects_version/<id>/files/prewarm` (idempotent,
  org-scoped) lets the UI warm the tunnel when opening the findings list / code
  view, before clicking a file.
- **Reaping:** `aist.tasks.egress.reap_egress` (celery-beat, every 5 min) stops
  containers idle longer than `AIST_EGRESS_IDLE_TTL` (default 900 s) and evicts
  the least-recently-used above `AIST_EGRESS_MAX_WARM` (default 10). Idle is
  measured worker-side with no shared state, from the mtime of the sidecar's
  tinyproxy connect log.
- **tinyproxy connect log:** the warm sidecar logs one CONNECT line per fetch to
  `/tmp/tinyproxy-access.log` (used only for the mtime idle signal — content is
  never read). This file holds **one org's** internal SCM hostnames, is readable
  only via `docker exec`/inspect (already a high-privilege boundary, same as VPN
  credentials), and is destroyed when the container is reaped (`docker rm`). It is
  not written to `docker logs`.
- **Settings:** `AIST_EGRESS_IDLE_TTL`, `AIST_EGRESS_MAX_WARM`,
  `AIST_EGRESS_WEB_SERVICE` (default `uwsgi`, resolved for the tinyproxy Allow
  list), `AIST_EGRESS_ALLOWED_IPS` (optional explicit Allow override).
- **GitHub limitation:** the GitHub App client (`ScmGithubBinding`) does not
  thread a proxy, so a GitHub-Enterprise instance reachable only through a VPN is
  not yet covered by the blob path. GitHub Cloud needs no VPN and works.

## Configuration

### 1. Create a VPN integration

```
POST /api/v2/aist/org-integrations/
{ "integration_type": "VPN", "name": "Corporate VPN", ... }
```

Then upload credentials:
```
PATCH /api/v2/aist/org-integrations/<id>/vpn-secret/
{ "ovpn_content": "<full .ovpn file content>" }
```

If the `.ovpn` file contains inline blocks (`<ca>`, `<cert>`, `<key>`,
`<tls-auth>`, `<tls-crypt>`), they are automatically extracted into separate
encrypted fields on save (`_split_ovpn_pem_blocks` in
`aist/api/org_integrations.py`). There is no need to supply them separately.

If the `.ovpn` has no inline cert blocks, pass them as separate fields:
`ca_cert`, `client_cert`, `client_key`, `tls_auth_key`.

### 2. Link VPN to another integration

```
PATCH /api/v2/aist/org-integrations/<gitlab_id>/
{ "vpn_integration": <vpn_integration_id> }
```

For a work-item provider:
```
PATCH /api/v2/aist/work-item-providers/<id>/
{ "vpn_integration": <vpn_integration_id> }
```

The VPN integration must belong to the same organization as the target
integration.

### 3. Verify

```
POST /api/v2/aist/org-integrations/<gitlab_id>/validate/
```

In the celeryworker logs you should see:
```
vpn=starting sidecar=aist-vpn-validate-<id> ...
vpn=up sidecar=... proxy=http://aist-vpn-...:1080
```

---

## Organization isolation

- A VPN integration belongs to one organization. It is impossible to link an
  integration from one organization to a VPN from another (enforced in the
  serializer).
- All VPN-related ViewSet `get_queryset()` methods apply an org filter.
- `ovpn_content`, certificates, and passwords are stored in `EncryptedCharField`
  — plaintext is never written to the database.

---

## Key files

| File | Role |
|------|------|
| `aist/models.py` | `OrgIntegrationVPNSecret`; `vpn_integration` FK on `OrgIntegration` and `WorkItemProvider` |
| `aist/utils/vpn.py` | `vpn_sidecar_context()` — start/stop sidecar |
| `aist/integrations/resolver.py` | `resolve_integration()` — find the active VPN integration for a project |
| `aist/api/org_integrations.py` | REST API, `.ovpn` parsing, `validate` endpoint |
| `aist/tasks/pipeline.py` | Starts VPN before the SAST pipeline |
| `aist/work_items/sync.py` | Starts VPN before work-item sync |
| `sast-combinator/vpn-sidecar/` | Sidecar Docker image (OpenVPN + tinyproxy) |
