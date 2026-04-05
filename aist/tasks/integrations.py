from __future__ import annotations

import logging
from contextlib import suppress

from celery import shared_task

logger = logging.getLogger(__name__)


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
            return {"ok": False, "error": str(exc), "response_code": exc.response_code}

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
