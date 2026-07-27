from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django_github_app.routing import GitHubRouter

from aist.execution.enqueue import LaunchPrincipal, enqueue_pipeline_launch
from aist.models import (
    AISTProjectVersion,
    PullRequest,
    RepositoryInfo,
    ScmGithubBinding,
    ScmType,
    VersionType,
)
from aist.utils.pipeline import has_unfinished_pipeline

gh = GitHubRouter()
logger = logging.getLogger("aist")


@gh.event("installation", action="created")
@gh.event("installation_repositories", action="added")
async def on_install_created_or_repos_added(event, gh, **_):
    event_type = getattr(event, "event", None)
    action = (event.data or {}).get("action")
    inst_id = ((event.data or {}).get("installation") or {}).get("id")
    logger.info("Received installation event: type=%s action=%s installation_id=%s", event_type, action, inst_id)

    if event_type == "installation":
        repos_added = [r["full_name"] for r in (event.data or {}).get("repositories", []) if r.get("full_name")]
    else:
        repos_added = [r["full_name"] for r in (event.data or {}).get("repositories_added", []) if r.get("full_name")]

    if not repos_added:
        logger.info("No repositories to update for installation=%s", inst_id)
        return

    for repo_full in repos_added:
        owner, name = repo_full.split("/", 1)

        repo_info = await sync_to_async(RepositoryInfo.objects.filter(
            type=ScmType.GITHUB,
            repo_owner=owner,
            repo_name=name,
        ).first)()
        if repo_info is None:
            logger.info("Repository %s is not imported in AIST yet; skipping binding update", repo_full)
            continue

        def _ensure_binding(scm=repo_info, installation_id=inst_id):
            binding, created = ScmGithubBinding.objects.get_or_create(
                scm=scm,
                defaults={"installation_id": installation_id},
            )
            updated = False
            if binding.installation_id != installation_id:
                binding.installation_id = installation_id
                binding.save(update_fields=["installation_id"])
                updated = True
            return created, updated

        created_bind, updated_bind = await sync_to_async(_ensure_binding)()
        if created_bind:
            logger.info("Created ScmGithubBinding for %s (installation_id=%s)", repo_full, inst_id)
        elif updated_bind:
            logger.info("Updated ScmGithubBinding for %s (installation_id=%s)", repo_full, inst_id)
        else:
            logger.info("Verified ScmGithubBinding for %s (installation_id=%s)", repo_full, inst_id)


@gh.event("pull_request", action="opened")
@gh.event("pull_request", action="synchronize")
async def on_pr_event(event, gh, **_):
    action = (event.data or {}).get("action")
    logger.info("Received pull_request event with action: %s", action)

    repo_payload = (event.data or {}).get("repository") or {}
    repo_full = repo_payload.get("full_name")
    if not repo_full:
        logger.error("Missing repository.full_name in pull_request payload.")
        return
    owner, name = repo_full.split("/", 1)

    pr = (event.data or {}).get("pull_request") or {}
    pr_number = pr.get("number")
    head = pr.get("head") or {}
    base = pr.get("base") or {}

    head_sha = (head.get("sha") or "").strip()
    head_ref = (head.get("ref") or "").strip()
    base_ref = (base.get("ref") or "").strip()
    is_from_fork = (head.get("repo") or {}).get("full_name") != repo_full

    if not head_sha or not pr_number:
        logger.error("Missing required PR fields for repo=%s", repo_full)
        return

    logger.info(
        "PR metadata: repo=%s pr_number=%s head_sha=%s head_ref=%s base_ref=%s from_fork=%s",
        repo_full,
        pr_number,
        head_sha[:7],
        head_ref,
        base_ref,
        is_from_fork,
    )

    repo_info = await sync_to_async(RepositoryInfo.objects.select_related(
        "project",
        "project__product__prod_type__aist_organization",
    ).filter(
        type=ScmType.GITHUB,
        repo_owner=owner,
        repo_name=name,
    ).first)()
    if repo_info is None:
        logger.error("RepositoryInfo not found for repository: %s", repo_full)
        return

    try:
        aist_project = repo_info.project
    except Exception:
        logger.error("AISTProject is not linked with repository: %s", repo_full)
        return

    pv, created = await sync_to_async(AISTProjectVersion.objects.get_or_create)(
        project=aist_project,
        version=head_sha,
        version_type=VersionType.GIT_HASH,
    )
    if created:
        logger.info("Created new AISTProjectVersion: %s", head_sha)

    if await sync_to_async(has_unfinished_pipeline)(pv):
        logger.warning("Pull request %s already has an unfinished pipeline. Skipping.", pr_number)
        return

    pr_ref, created_pr = await sync_to_async(PullRequest.objects.update_or_create)(
        project_version=pv,
        repository=repo_info,
        pr_number=pr_number,
        defaults={
            "base_ref": base_ref,
            "head_ref": head_ref,
            "is_from_fork": is_from_fork,
        },
    )
    if created_pr:
        logger.info("Created PullRequest record: #%s", pr_number)
    else:
        logger.info("Updated PullRequest record: #%s", pr_number)

    organization = aist_project.organization
    if organization is None:
        logger.error("AISTProject has no organization for repository: %s", repo_full)
        return
    enqueue_result = await sync_to_async(enqueue_pipeline_launch)(
        project=aist_project,
        principal=LaunchPrincipal.for_scm_webhook(organization=organization),
        raw_params={
            "pr_launch": True,
            "project_version": pv.as_dict(),
        },
        client_request_key=f"github:{repo_info.pk}:pr:{pr_number}:sha:{head_sha}",
        initial_launch_data={"pull_request_id": pr_ref.pk},
    )

    logger.info(
        "Launch request queued for PR #%s: request_id=%s created=%s",
        pr_number,
        enqueue_result.request.pk,
        enqueue_result.created,
    )
