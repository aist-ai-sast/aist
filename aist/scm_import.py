from __future__ import annotations

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
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    RepositoryInfo,
    ScmType,
    VersionType,
)

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


class ScmImportConflict(Exception):

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

    Returns ``(aist_project, repo_full)``. Raises ``ScmImportConflict`` for
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
            raise ScmImportConflict(msg)

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

    with transaction.atomic():
        aist_project, project_created = AISTProject.objects.get_or_create(
            product=product,
            defaults={
                "supported_languages": req.supported_languages,
                "compilable": False,
                "profile": {},
                "repository": repo_info,
                "organization": req.organization,
            },
        )
        if not project_created:
            if aist_project.organization_id and aist_project.organization_id != req.organization.id:
                msg = "Project is already linked to another organization."
                raise ScmImportConflict(msg)
            if aist_project.organization_id is None:
                aist_project.organization = req.organization
                aist_project.save(update_fields=["organization"])

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
            _create_initial_script(aist_project, DEFAULT_ENTRYPOINT_SCRIPT)
            if req.default_branch:
                # Seed the initial version with the real default branch now,
                # while it's still committed inside this transaction — this
                # pre-empts create_default_master_version's own "master"
                # fallback lookup (it only runs if no version exists yet).
                AISTProjectVersion.objects.get_or_create(
                    project=aist_project,
                    version=req.default_branch,
                    defaults={"version_type": VersionType.GIT_BRANCH},
                )

    if req.auto_analyze and aist_project.repository:
        from aist.tasks.claude import analyze_project_after_import  # noqa: PLC0415
        analyze_project_after_import.delay(aist_project.id)

    return aist_project, repo_full
