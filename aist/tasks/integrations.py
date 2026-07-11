from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace

from celery import shared_task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GiteaRepoSummary:

    """One repository as shown in the Gitea project-listing modal."""

    id: int | None
    name: str
    full_name: str
    description: str
    web_url: str
    default_branch: str
    private: bool
    language: str = ""

    @classmethod
    def from_api(cls, data: dict) -> GiteaRepoSummary:
        return cls(
            id=data.get("id"),
            name=data.get("name", "") or "",
            full_name=data.get("full_name", "") or "",
            description=data.get("description", "") or "",
            web_url=data.get("html_url", "") or "",
            default_branch=data.get("default_branch", "") or "",
            private=bool(data.get("private")),
        )


@dataclass(frozen=True)
class GiteaProjectMetadata:

    """Metadata resolved for a single Gitea repo at import time."""

    path_with_namespace: str
    description: str
    web_url: str
    inferred_base: str
    default_branch: str
    langs_raw: dict = field(default_factory=dict)


@shared_task(name="aist.tasks.integrations.fetch_gitlab_projects", bind=True)
def fetch_gitlab_projects(self, integration_id: int, async_user=None) -> dict:
    """
    Fetch the list of GitLab projects accessible with the stored integration credentials.

    Routes through VPN when vpn_integration is configured.
    Must run in Celery worker (Docker socket access required for VPN sidecar).
    Returns {"ok": True, "projects": [...]} on success.
    """
    import gitlab  # noqa: PLC0415

    from aist.models import OrgIntegration  # noqa: PLC0415

    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_integration__vpn_secret")
        .get(pk=integration_id)
    )
    base_url = (integration.config or {}).get("base_url") or "https://gitlab.com"
    with integration.scoped_session(execution_id=f"gitlab-list-{integration_id}") as session:
        gl = gitlab.Gitlab(base_url, private_token=integration.secret or "", session=session)
        gl.auth()
        gl_projects = gl.projects.list(
            all=True,
            per_page=100,
            order_by="last_activity_at",
            sort="desc",
        )
        projects = []
        for p in gl_projects:
            language = ""
            with suppress(Exception):
                langs = p.languages()
                if isinstance(langs, dict) and langs:
                    language = max(langs, key=langs.get)
            projects.append({
                "id": p.id,
                "name": getattr(p, "name", "") or "",
                "path_with_namespace": getattr(p, "path_with_namespace", "") or "",
                "description": getattr(p, "description", "") or "",
                "web_url": getattr(p, "web_url", "") or "",
                "default_branch": getattr(p, "default_branch", "") or "",
                "visibility": getattr(p, "visibility", "") or "",
                "language": language,
            })
        return {"ok": True, "projects": projects}


@shared_task(name="aist.tasks.integrations.fetch_gitlab_project_info", bind=True)
def fetch_gitlab_project_info(self, integration_id: int, project_id: int, async_user=None) -> dict:
    """
    Fetch project metadata (namespace, languages, URLs) from GitLab for import.

    Routes through VPN when vpn_integration is configured.
    Must run in Celery worker (Docker socket access required for VPN sidecar).
    Returns {"ok": True, ...} on success or {"ok": False, "error": ..., "response_code": ...} on failure.
    """
    import gitlab  # noqa: PLC0415

    from aist.models import OrgIntegration  # noqa: PLC0415

    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_integration__vpn_secret")
        .get(pk=integration_id)
    )
    base_url = (integration.config or {}).get("base_url") or "https://gitlab.com"
    with integration.scoped_session(execution_id=f"gitlab-import-{integration_id}-{project_id}") as session:
        gl = gitlab.Gitlab(base_url, private_token=integration.secret or "", session=session)
        try:
            proj = gl.projects.get(project_id)
        except gitlab.exceptions.GitlabGetError as exc:
            logger.warning("GitLab project fetch failed for %s: %s", project_id, exc)
            return {"ok": False, "error": "GitLab project fetch failed", "response_code": exc.response_code}

        langs_raw: dict = {}
        try:
            langs_raw = proj.languages() or {}
        except Exception:
            logger.warning("Could not fetch languages for GitLab project %s", project_id)

        path_with_ns = getattr(proj, "path_with_namespace", "") or ""
        web_url = getattr(proj, "web_url", "") or base_url
        inferred_base = web_url.split("/" + path_with_ns)[0] if path_with_ns and path_with_ns in web_url else base_url

        return {
            "ok": True,
            "path_with_namespace": path_with_ns,
            "description": getattr(proj, "description", "") or "",
            "web_url": web_url,
            "inferred_base": inferred_base,
            "langs_raw": langs_raw,
        }


def _gerrit_rest(integration, session):
    """Build a pygerrit2 REST client for an integration over the given session."""
    import pygerrit2  # noqa: PLC0415

    base_url = ((integration.config or {}).get("base_url") or "").rstrip("/")
    username = ((integration.config or {}).get("username") or "").strip()
    password = (integration.secret or "").strip()
    auth = pygerrit2.HTTPBasicAuth(username, password) if username and password else None
    rest = pygerrit2.GerritRestAPI(url=base_url, auth=auth)
    rest.session = session
    return rest, base_url


@shared_task(name="aist.tasks.integrations.fetch_gerrit_projects", bind=True)
def fetch_gerrit_projects(self, integration_id: int, async_user=None) -> dict:
    """
    Fetch the list of Gerrit projects accessible with the stored integration credentials.

    Routes through VPN when vpn_integration is configured (scoped_session), otherwise a
    direct session. Must run in Celery worker (Docker socket access required for VPN sidecar).
    Returns {"ok": True, "projects": [...]} on success.
    """
    from aist.models import OrgIntegration  # noqa: PLC0415

    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_integration__vpn_secret")
        .get(pk=integration_id)
    )
    with integration.scoped_session(execution_id=f"gerrit-list-{integration_id}") as session:
        rest, base_url = _gerrit_rest(integration, session)
        # ?d includes the project description; ?tree/HEAD not requested to keep it light.
        data = rest.get("/projects/?d")
        projects = []
        for name, info in (data or {}).items():
            if (info or {}).get("state") == "HIDDEN":
                continue
            projects.append({
                "name": name,
                "project_path": name,
                "description": (info or {}).get("description", "") or "",
                "web_url": f"{base_url}/admin/repos/{name}",
                "default_branch": "",
                "state": (info or {}).get("state", "") or "",
            })
        return {"ok": True, "projects": projects}


@shared_task(name="aist.tasks.integrations.fetch_gerrit_project_info", bind=True)
def fetch_gerrit_project_info(self, integration_id: int, project_path: str, async_user=None) -> dict:
    """
    Fetch project metadata (description, default branch, URLs) from Gerrit for import.

    Routes through VPN when vpn_integration is configured. Must run in Celery worker.
    Returns {"ok": True, ...} on success or {"ok": False, "response_code": ...} on failure.
    """
    from urllib.parse import quote  # noqa: PLC0415

    from aist.models import OrgIntegration  # noqa: PLC0415

    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_integration__vpn_secret")
        .get(pk=integration_id)
    )
    with integration.scoped_session(execution_id=f"gerrit-import-{integration_id}") as session:
        rest, base_url = _gerrit_rest(integration, session)
        proj_id = quote(project_path, safe="")
        try:
            info = rest.get(f"/projects/{proj_id}")
        except Exception as exc:  # pygerrit2 raises requests.HTTPError
            code = getattr(getattr(exc, "response", None), "status_code", None)
            return {"ok": False, "response_code": code, "error": "Gerrit project fetch failed"}

        default_branch = ""
        try:
            head = rest.get(f"/projects/{proj_id}/HEAD")
            default_branch = str(head or "").removeprefix("refs/heads/").strip()
        except Exception:
            logger.warning("Could not fetch HEAD for Gerrit project %s", project_path)

        return {
            "ok": True,
            "project_path": project_path,
            "description": (info or {}).get("description", "") or "",
            "web_url": f"{base_url}/admin/repos/{project_path}",
            "inferred_base": base_url,
            "default_branch": default_branch,
        }


def _gitea_headers(integration) -> dict:
    """Gitea's documented auth header — not Bearer, not Basic."""
    token = (integration.secret or "").strip()
    return {"Authorization": f"token {token}"} if token else {}


@shared_task(name="aist.tasks.integrations.fetch_gitea_projects", bind=True)
def fetch_gitea_projects(self, integration_id: int, async_user=None) -> dict:
    """
    Fetch the repositories accessible to the authenticated Gitea account.

    Routes through VPN when vpn_integration is configured (scoped_session), otherwise a
    direct session. Must run in Celery worker (Docker socket access required for VPN sidecar).
    Returns {"ok": True, "projects": [...]} on success.
    """
    from aist.models import OrgIntegration  # noqa: PLC0415

    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_integration__vpn_secret")
        .get(pk=integration_id)
    )
    base_url = ((integration.config or {}).get("base_url") or "").rstrip("/")
    headers = _gitea_headers(integration)
    summaries: list[GiteaRepoSummary] = []
    with integration.scoped_session(execution_id=f"gitea-list-{integration_id}") as session:
        limit = 50
        max_pages = 200  # safety bound (10k repos) — a real hit is logged below, never silent
        page = 1
        while page <= max_pages:
            # /api/v1/user/repos requires the "read:user" scope; /api/v1/repos/search
            # only needs "read:repository" — matching what a repo-scoped PAT actually
            # grants and what fetch_gitea_project_info/get_project_info already use.
            resp = session.get(
                f"{base_url}/api/v1/repos/search",
                headers=headers,
                params={"limit": limit, "page": page},
                timeout=30,
            )
            resp.raise_for_status()
            batch = (resp.json() or {}).get("data") or []
            for repo in batch:
                summary = GiteaRepoSummary.from_api(repo)
                language = ""
                with suppress(Exception):
                    langs_resp = session.get(
                        f"{base_url}/api/v1/repos/{summary.full_name}/languages",
                        headers=headers,
                        timeout=15,
                    )
                    langs_resp.raise_for_status()
                    langs = langs_resp.json() or {}
                    if isinstance(langs, dict) and langs:
                        language = max(langs, key=langs.get)
                summaries.append(replace(summary, language=language))
            if len(batch) < limit:
                break
            page += 1
        else:
            logger.warning(
                "Gitea project listing for integration=%s hit the %s-page safety cap",
                integration_id, max_pages,
            )
        return {"ok": True, "projects": [asdict(s) for s in summaries]}


@shared_task(name="aist.tasks.integrations.fetch_gitea_project_info", bind=True)
def fetch_gitea_project_info(self, integration_id: int, repo_full_name: str, async_user=None) -> dict:
    """
    Fetch project metadata (description, default branch, languages, URLs) from Gitea for import.

    Routes through VPN when vpn_integration is configured. Must run in Celery worker.
    Returns {"ok": True, ...} on success or {"ok": False, "response_code": ...} on failure.
    """
    from aist.models import OrgIntegration  # noqa: PLC0415

    integration = (
        OrgIntegration.objects
        .select_related("vpn_integration", "vpn_integration__vpn_secret")
        .get(pk=integration_id)
    )
    base_url = ((integration.config or {}).get("base_url") or "").rstrip("/")
    headers = _gitea_headers(integration)
    with integration.scoped_session(execution_id=f"gitea-import-{integration_id}") as session:
        resp = session.get(f"{base_url}/api/v1/repos/{repo_full_name}", headers=headers, timeout=15)
        if resp.status_code == 404:
            return {"ok": False, "response_code": 404, "error": "Gitea project not found"}
        try:
            resp.raise_for_status()
        except Exception:
            logger.warning("Gitea project fetch failed for %s (status=%s)", repo_full_name, resp.status_code)
            return {"ok": False, "response_code": resp.status_code, "error": "Gitea project fetch failed"}
        data = resp.json() or {}

        langs_raw: dict = {}
        try:
            langs_resp = session.get(
                f"{base_url}/api/v1/repos/{repo_full_name}/languages",
                headers=headers,
                timeout=15,
            )
            langs_resp.raise_for_status()
            langs_raw = langs_resp.json() or {}
        except Exception:
            logger.warning("Could not fetch languages for Gitea repo %s", repo_full_name)

        path_with_ns = data.get("full_name") or repo_full_name
        web_url = data.get("html_url") or base_url
        inferred_base = web_url.split("/" + path_with_ns)[0] if path_with_ns and path_with_ns in web_url else base_url

        metadata = GiteaProjectMetadata(
            path_with_namespace=path_with_ns,
            description=data.get("description", "") or "",
            web_url=web_url,
            inferred_base=inferred_base,
            default_branch=data.get("default_branch", "") or "",
            langs_raw=langs_raw,
        )
        return {"ok": True, **asdict(metadata)}
