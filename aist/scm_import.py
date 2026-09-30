from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.models import DojoMeta, Product

from aist.api.projects import _create_initial_script
from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT
from aist.models import (
    AISTProject,
    AISTProjectScript,
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    RepositoryInfo,
    ScmType,
    VersionType,
)

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser

logger = logging.getLogger(__name__)


class ScmImportConflictError(Exception):

    """Raised for 409-worthy states (product/project already linked elsewhere)."""


@dataclass(frozen=True)
class ScmImportRequest:

    """
    Everything ``import_scm_project`` needs, resolved by the caller's
    provider-specific view/Celery task before the generic workflow runs.
    """

    request_user: AbstractBaseUser
    organization: Organization
    scm_type: ScmType
    scm_label: str
    binding_model: type
    org_integration: OrgIntegration
    repo_owner: str
    repo_name: str
    description: str
    inferred_base: str
    supported_languages: list[str]
    auto_analyze: bool
    # Default branch resolved by the caller's own VPN-aware fetch (e.g.
    # fetch_gitea_project_info), if any. Threading it through here lets us
    # seed the initial AISTProjectVersion directly — the post_save signal
    # (aist.celery_signals.create_default_master_version) that would
    # otherwise derive it has no VPN/proxy awareness and silently falls back
    # to "master" when the org's SCM integration is only reachable via VPN.
    default_branch: str = ""

    @property
    def repo_full(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}" if self.repo_owner else self.repo_name


def seed_imported_project(project: AISTProject, default_branch: str) -> AISTProjectVersion | None:
    """
    Give a newly imported project its initial script and default-branch version.

    Call inside the import transaction, only when the project was just created.
    Seeding the real default branch here — resolved by the caller's VPN-aware
    fetch — pre-empts create_default_master_version's own "master" fallback
    lookup, which has no VPN/proxy awareness. Returns the branch version, or
    ``None`` when the default branch is unknown.
    """
    _create_initial_script(project, DEFAULT_ENTRYPOINT_SCRIPT)
    if not default_branch:
        return None
    version, _ = AISTProjectVersion.objects.get_or_create(
        project=project,
        version=default_branch,
        version_type=VersionType.GIT_BRANCH,
        defaults={"script": AISTProjectScript.for_new_version(project)},
    )
    return version


def queue_import_auto_analyze(project: AISTProject, version: AISTProjectVersion | None) -> None:
    """
    Queue Claude analysis for the branch version this import created, after commit.

    Without such a version (default branch unknown, or the project already
    existed) nothing is queued: analysis always targets an explicit branch, and
    the user picks one later with Regenerate.
    """
    if version is None:
        logger.warning(
            "Project %s: import created no default-branch version; auto-analyze not queued. "
            "Use Regenerate with a chosen branch.",
            project.id,
        )
        return
    from aist.tasks.claude import analyze_project_after_import  # noqa: PLC0415

    project_id, version_id = project.id, version.id
    transaction.on_commit(lambda: analyze_project_after_import.delay(project_id, version_id))


def import_scm_project(req: ScmImportRequest) -> tuple[AISTProject, str]:
    """
    Shared "import a repo into AIST" workflow for every SCM binding
    (GitHub/GitLab/Gerrit/Gitea/...).

    Providers differ only in *how* they resolve the fields on
    ``ScmImportRequest`` — that part stays in each provider's own
    ``api/<provider>_integration.py`` view alongside its provider-specific
    Celery task. Everything after that (Product / RepositoryInfo / binding /
    AISTProject / DojoMeta creation, org-conflict checks) is identical across
    providers, so it lives here once.

    Returns ``(aist_project, repo_full)``. Raises ``ScmImportConflictError`` for
    409-worthy states — callers map that to a Response.
    """
    repo_full = req.repo_full
    product_type = req.organization.ensure_product_type()

    product, created_product = Product.objects.get_or_create(
        name=repo_full,
        defaults={"prod_type": product_type, "description": req.description or "Empty description. Admin, fix me"},
    )
    if not created_product:
        user_has_permission_or_403(req.request_user, product, Permissions.Product_Edit)
        if product.prod_type_id != product_type.id:
            msg = "Product already exists under another product type. Move it first or choose another organization."
            raise ScmImportConflictError(msg)

    DojoMeta.objects.update_or_create(
        product=product,
        name="scm-type",
        defaults={"value": req.scm_label},
    )

    repo_info, _ = RepositoryInfo.objects.get_or_create(
        type=req.scm_type,
        repo_owner=req.repo_owner,
        repo_name=req.repo_name,
        defaults={"base_url": req.inferred_base},
    )

    initial_version = None
    with transaction.atomic():
        aist_project, project_created = AISTProject.objects.get_or_create(
            product=product,
            defaults={
                "supported_languages": req.supported_languages,
                "compilable": False,
                "profile": {},
                "repository": repo_info,
            },
        )
        if not project_created:
            if aist_project.organization_id and aist_project.organization_id != req.organization.id:
                msg = "Project is already linked to another organization."
                raise ScmImportConflictError(msg)

        # Reassign the binding's credentials only once the org-conflict check
        # above has passed. Doing this earlier would let one org's import
        # silently repoint another org's existing RepositoryInfo/binding at
        # this caller's credentials — RepositoryInfo has no org scoping of
        # its own, so a (type, repo_owner, repo_name) collision across two
        # orgs would otherwise hijack the binding even on a rejected import.
        binding, _ = req.binding_model.objects.get_or_create(scm=repo_info)
        if binding.org_integration_id != req.org_integration.id:
            binding.org_integration = req.org_integration
            binding.save(update_fields=["org_integration"])

        if project_created:
            initial_version = seed_imported_project(aist_project, req.default_branch)

    if req.auto_analyze and aist_project.repository:
        queue_import_auto_analyze(aist_project, initial_version)

    return aist_project, repo_full
