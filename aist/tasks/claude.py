from __future__ import annotations

import logging
import os
import shutil
import subprocess

from celery import shared_task
from django.conf import settings

from aist.integrations.claude import claude_subprocess_env
from aist.integrations.egress import resolve_source_vpn
from aist.models import AISTProject, VersionType
from aist.pipeline_args import PipelineArguments
from aist.utils.bridge_client_factory import BridgeError, build_bridge_client_from_settings, format_extra_args
from aist.utils.vpn import vpn_sidecar_context

logger = logging.getLogger("aist.tasks.claude")


def _clone_project_via_vpn(
    project,
    vpn_resolved,
    execution_id: str,
    clone_dir: str,
    project_id: int,
    branch: str,
) -> None:
    with vpn_sidecar_context(vpn_resolved, execution_id=execution_id) as (_vpn_container, vpn_proxy):
        clone_url = project.repository.clone_url
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if vpn_proxy:
            env["https_proxy"] = vpn_proxy
            env["http_proxy"] = vpn_proxy

        shutil.rmtree(clone_dir, ignore_errors=True)
        os.makedirs(clone_dir, exist_ok=True)  # noqa: PTH103
        logger.info("Cloning project %s branch %s to %s", project_id, branch, clone_dir)
        # ``--`` ends option parsing, so neither the branch nor the URL can act as a git option.
        subprocess.run(
            ["git", "clone", "--depth=1", "--branch", branch, "--", clone_url, clone_dir],  # noqa: S607
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )


@shared_task(bind=True)
def analyze_project_after_import(self, project_id: int, project_version_id: int, async_user_id=None) -> None:
    """
    Clone one branch of a project repository and run Claude analysis skills.

    Triggered after project import when ``auto_analyze=True`` and by the
    regenerate-analysis API. ``project_version_id`` names the GIT_BRANCH version
    the analysis is for: that branch is cloned, and the generated init script is
    bound to exactly that version.
    Uses existing integration infrastructure for auth (GitHub tokens, GitLab PATs)
    and VPN connectivity.

    Two skills are executed sequentially on the same clone:
    1. ``aist-init-script-generator`` — generates the init script for the version
    2. ``aist-project-profile-analyzer`` — generates path exclusions for the project profile
    """
    try:
        project = (
            AISTProject.objects
            .select_related("repository", "product__prod_type__aist_organization")
            .get(id=project_id)
        )
    except AISTProject.DoesNotExist:
        logger.error("Project %s not found for auto-analyze", project_id)
        return

    if not project.repository:
        logger.warning("Project %s has no repository; skipping auto-analyze", project_id)
        return

    version = project.versions.filter(pk=project_version_id).first()
    if version is None or version.version_type != VersionType.GIT_BRANCH:
        logger.error(
            "Project version %s is not a GIT_BRANCH version of project %s; skipping auto-analyze",
            project_version_id,
            project_id,
        )
        return

    # Resolve Claude credentials BEFORE cloning — no point pulling source
    # into a workspace that has zero chance of producing analysis output.
    # Empty dict means "no active CLAUDE_CODE OrgIntegration for this
    # project's org", and skip-with-warning matches the operator
    # expectation of "auto-analyze either runs to completion or no-ops".
    auth_env = claude_subprocess_env(project)
    if not auth_env:
        logger.warning(
            "Project %s has no active Claude integration; skipping auto-analyze",
            project_id,
        )
        return

    project_name = PipelineArguments.normalize_project_name(project)
    # One clone per version: the bridge reads it asynchronously, so a regenerate for
    # another branch must not replace the tree a running analysis is still reading.
    clone_dir = os.path.join(  # noqa: PTH118
        getattr(settings, "AIST_PROJECTS_BUILD_DIR", "/tmp/aist/projects"),  # noqa: S108
        project_name,
        f"claude-analysis-{version.id}",
    )
    execution_id = f"claude-analyze-{project_id}-{version.id}"

    # ── 1. Clone via integrations (auth + VPN) ──────────────────────────────
    try:
        vpn_resolved = resolve_source_vpn(project)
    except Exception:
        logger.exception("Failed to resolve VPN integration for project %s", project_id)
        vpn_resolved = None

    try:
        _clone_project_via_vpn(project, vpn_resolved, execution_id, clone_dir, project_id, version.version)
    except subprocess.TimeoutExpired:
        # Do NOT log the exception object: its str() includes the git argv,
        # and clone_url embeds credentials (PAT / Gerrit HTTP password).
        logger.error("Clone timed out for project %s", project_id)
        shutil.rmtree(clone_dir, ignore_errors=True)
        return
    except subprocess.CalledProcessError as exc:
        # Same reason as above — never log exc/argv (credential-bearing URL).
        logger.error("Clone failed for project %s (git exited %s)", project_id, exc.returncode)
        shutil.rmtree(clone_dir, ignore_errors=True)
        return
    except Exception:
        logger.exception("Clone failed for project %s", project_id)
        shutil.rmtree(clone_dir, ignore_errors=True)
        return

    # ── 2. Send to bridge (async — Claude runs in background) ──────────────
    # NOTE: clone_dir is NOT cleaned up here because bridge runs async (202).
    # Claude Code needs the clone dir to still exist when it starts working.
    # Cleanup happens inside the bridge after skill completion.
    bridge = build_bridge_client_from_settings(auth_env=auth_env)
    for skill_name, extra_args in (
        ("aist-init-script-generator", format_extra_args({"project_version_id": version.id})),
        ("aist-project-profile-analyzer", ""),
    ):
        try:
            bridge.analyze_async(
                skill_name=skill_name, project_id=str(project_id), source_path=clone_dir, extra_args=extra_args,
            )
        except BridgeError:
            logger.exception("Bridge request failed for %s (project %s)", skill_name, project_id)
        else:
            logger.info("Bridge accepted %s for project %s", skill_name, project_id)
