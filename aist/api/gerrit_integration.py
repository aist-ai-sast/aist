from __future__ import annotations

from django.db import transaction
from dojo.authorization.authorization import (
    user_has_permission_or_403,
)
from dojo.authorization.roles_permissions import Permissions
from dojo.models import DojoMeta, Product
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from aist.api.projects import _create_initial_script
from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmGerritBinding,
    ScmType,
    VersionType,
)
from aist.tasks.integrations import fetch_gerrit_project_info


class OptionalIntField(serializers.IntegerField):
    def to_internal_value(self, data):
        if data in {None, ""}:
            return None
        return super().to_internal_value(data)


class ImportGerritRequestSerializer(serializers.Serializer):
    # Gerrit project is a slash-path, e.g. "platform/build/soong".
    project_path = serializers.CharField(required=True, allow_blank=False, max_length=255)
    organization_id = OptionalIntField(required=True, allow_null=False)
    auto_analyze = serializers.BooleanField(default=False, required=False)


class ImportGerritResponseSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    aist_project_id = serializers.IntegerField()
    repository_id = serializers.IntegerField()
    repo_full = serializers.CharField()


class ImportProjectFromGerritAPI(AISTAPIView):

    """
    Create Product + RepositoryInfo(GERRIT) + ScmGerritBinding + AISTProject
    from a Gerrit project path.
    """

    authz = ResourcePolicy(resource=Organization, read=Action.PROJECT_CREATE, write=Action.PROJECT_CREATE)

    @extend_schema(
        request=ImportGerritRequestSerializer,
        responses={201: OpenApiResponse(ImportGerritResponseSerializer)},
        tags=[AISTApiTag.GERRIT.value],
        summary="Import project from Gerrit",
        description="Creates Product and AISTProject from a Gerrit project path.",
    )
    def post(self, request, *args, **kwargs):
        serializer = ImportGerritRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_path = serializer.validated_data["project_path"].strip().strip("/")
        if not project_path:
            return Response({"detail": "project_path is required."}, status=status.HTTP_400_BAD_REQUEST)

        organization_id = serializer.validated_data.get("organization_id")
        organization = self.resolve(pk=organization_id)

        integration = (
            OrgIntegration.objects.filter(
                organization=organization,
                integration_type=OrgIntegrationType.GERRIT,
                is_active=True,
            )
            .order_by("pk")
            .first()
        )
        if not integration:
            return Response(
                {"detail": "No active Gerrit integration found for this organization."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fetch project metadata via Celery worker (has Docker socket for VPN sidecar).
        try:
            proj_data = fetch_gerrit_project_info.delay(integration.pk, project_path).get(timeout=60)
        except Exception:
            return Response({"detail": "Gerrit fetch timed out or failed."}, status=status.HTTP_502_BAD_GATEWAY)

        if not proj_data.get("ok"):
            if proj_data.get("response_code") == 404:
                return Response({"detail": "Gerrit project not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": proj_data.get("error", "Gerrit API error")}, status=status.HTTP_502_BAD_GATEWAY)

        # Split by last "/": multi-segment → owner/name; single-segment → empty owner.
        if "/" in project_path:
            owner_ns, repo_name = project_path.rsplit("/", 1)
        else:
            owner_ns, repo_name = "", project_path
        description = proj_data.get("description") or "Empty description. Admin, fix me"
        inferred_base = proj_data["inferred_base"]

        product_type = organization.ensure_product_type()

        product, created_product = Product.objects.get_or_create(
            name=project_path,
            defaults={"prod_type": product_type, "description": description},
        )
        if not created_product:
            user_has_permission_or_403(request.user, product, Permissions.Product_Edit)
            if product.prod_type_id != product_type.id:
                msg = "Product already exists under another product type. Move it first or choose another organization."
                return Response({"detail": msg}, status=status.HTTP_409_CONFLICT)

        DojoMeta.objects.update_or_create(
            product=product,
            name="scm-type",
            defaults={"value": "gerrit"},
        )

        repo_info, _ = RepositoryInfo.objects.get_or_create(
            type=ScmType.GERRIT,
            repo_owner=owner_ns,
            repo_name=repo_name,
            defaults={"base_url": inferred_base},
        )

        binding, _ = ScmGerritBinding.objects.get_or_create(scm=repo_info)
        if binding.org_integration_id != integration.id:
            binding.org_integration = integration
            binding.save(update_fields=["org_integration"])

        with transaction.atomic():
            aist_project, project_created = AISTProject.objects.get_or_create(
                product=product,
                defaults={
                    # Gerrit exposes no language stats; user assigns languages via the
                    # project edit form after import.
                    "supported_languages": [],
                    "compilable": False,
                    "profile": {},
                    "repository": repo_info,
                },
            )
            if project_created:
                _create_initial_script(aist_project, DEFAULT_ENTRYPOINT_SCRIPT)
                default_branch = proj_data.get("default_branch") or ""
                if default_branch:
                    # Seed the initial version with the real default branch now,
                    # while it's still committed inside this transaction — this
                    # pre-empts create_default_master_version's own "master"
                    # fallback lookup (which has no VPN/proxy awareness and
                    # would silently fall back when Gerrit is only reachable
                    # via VPN). fetch_gerrit_project_info already resolved this
                    # correctly through the VPN-aware scoped_session above.
                    AISTProjectVersion.objects.get_or_create(
                        project=aist_project,
                        version=default_branch,
                        defaults={"version_type": VersionType.GIT_BRANCH},
                    )
            elif aist_project.organization_id and aist_project.organization_id != organization.id:
                msg = "Project is already linked to another organization."
                return Response({"detail": msg}, status=status.HTTP_409_CONFLICT)

        if serializer.validated_data.get("auto_analyze") and aist_project.repository:
            from aist.tasks.claude import analyze_project_after_import  # noqa: PLC0415
            analyze_project_after_import.delay(aist_project.id)

        out = ImportGerritResponseSerializer(
            {
                "product_id": product.id,
                "product_name": product.name,
                "aist_project_id": aist_project.id,
                "repository_id": repo_info.id,
                "repo_full": project_path,
            },
        )
        return Response(out.data, status=status.HTTP_201_CREATED)
