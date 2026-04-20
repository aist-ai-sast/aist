from __future__ import annotations

import logging
import os
import shutil
import subprocess

import httpx
from celery import shared_task
from django.conf import settings

from aist.integrations.resolver import resolve_integration
from aist.models import AISTProject, OrgIntegrationType
from aist.pipeline_args import PipelineArguments
from aist.utils.vpn import vpn_sidecar_context

logger = logging.getLogger("aist.tasks.codex")


def _send_to_bridge(*, skill_name: str, project_id: int | str, source_path: str) -> bool:
    """
    Send an analyze request to the codex-bridge via Unix domain socket.

    Returns True on success (202 accepted), False on failure.
    """
    socket_path = getattr(settings, "AIST_LOCAL_TRIAGE_BRIDGE_SOCKET", "/tmp/aist/triage-bridge.sock")  # noqa: S108
    payload = {
        "skill_name": skill_name,
        "project_id": str(project_id),
        "source_path": source_path,
        "callback_url": "",  # analyze skills persist directly; no callback needed
    }
    try:
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, timeout=10) as client:
            resp = client.post("http://localhost/analyze", json=payload)
            resp.raise_for_status()
        logger.info("Bridge accepted %s for project %s", skill_name, project_id)
    except Exception:
        logger.exception("Bridge request failed for %s (project %s)", skill_name, project_id)
        return False
    return True


@shared_task(bind=True)
def analyze_project_after_import(self, project_id: int, async_user=None) -> None:
    """
    Clone a project repository and run Codex analysis skills.

    Triggered after project import when ``auto_analyze=True``.
    Uses existing integration infrastructure for auth (GitHub tokens, GitLab PATs)
    and VPN connectivity.

    Two skills are executed sequentially on the same clone:
    1. ``aist-init-script-generator`` — generates a project-specific init script
    2. ``aist-project-profile-analyzer`` — generates path exclusions for the project profile
    """
    try:
        project = (
            AISTProject.objects
            .select_related("repository", "product", "organization")
            .get(id=project_id)
        )
    except AISTProject.DoesNotExist:
        logger.error("Project %s not found for auto-analyze", project_id)
        return

    if not project.repository:
        logger.warning("Project %s has no repository; skipping auto-analyze", project_id)
        return

    project_name = PipelineArguments.normalize_project_name(project)
    clone_dir = os.path.join(  # noqa: PTH118
        getattr(settings, "AIST_PROJECTS_BUILD_DIR", "/tmp/aist/projects"),  # noqa: S108
        project_name,
        "codex-analysis",
    )
    execution_id = f"codex-analyze-{project_id}"

    # ── 1. Clone via integrations (auth + VPN) ──────────────────────────────
    try:
        vpn_resolved = resolve_integration(project, OrgIntegrationType.VPN)
    except Exception:
        logger.exception("Failed to resolve VPN integration for project %s", project_id)
        vpn_resolved = None

    try:
        with vpn_sidecar_context(vpn_resolved, execution_id=execution_id) as (_vpn_container, vpn_proxy):
            clone_url = project.repository.clone_url
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            if vpn_proxy:
                env["https_proxy"] = vpn_proxy
                env["http_proxy"] = vpn_proxy

            shutil.rmtree(clone_dir, ignore_errors=True)
            os.makedirs(clone_dir, exist_ok=True)  # noqa: PTH103
            logger.info("Cloning project %s to %s", project_id, clone_dir)
            subprocess.run(
                ["git", "clone", "--depth=1", clone_url, clone_dir],  # noqa: S607
                env=env,
                check=True,
                capture_output=True,
                timeout=300,
            )
    except Exception:
        logger.exception("Clone failed for project %s", project_id)
        shutil.rmtree(clone_dir, ignore_errors=True)
        return

    # ── 2. Send to bridge (async — Claude runs in background) ──────────────
    # NOTE: clone_dir is NOT cleaned up here because bridge runs async (202).
    # Claude Code needs the clone dir to still exist when it starts working.
    # Cleanup happens inside the bridge after skill completion.
    _send_to_bridge(
        skill_name="aist-init-script-generator",
        project_id=project_id,
        source_path=clone_dir,
    )
    _send_to_bridge(
        skill_name="aist-project-profile-analyzer",
        project_id=project_id,
        source_path=clone_dir,
    )
