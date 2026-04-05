from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from dojo.authorization.authorization import (
    user_has_permission_or_403,
)
from dojo.authorization.roles_permissions import Permissions
from dojo.models import DojoMeta, Product
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.projects import _create_initial_script
from aist.api.schema import AISTApiTag
from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT
from aist.models import AISTProject, OrgIntegration, OrgIntegrationType, RepositoryInfo, ScmGitlabBinding, ScmType
from aist.queries import get_authorized_aist_organizations
from aist.tasks.integrations import fetch_gitlab_project_info
from aist.utils.pipeline_imports import _load_analyzers_config  # same helper as GH flow uses


class OptionalIntField(serializers.IntegerField):
    def to_internal_value(self, data):
        if data in {None, ""}:
            return None
        return super().to_internal_value(data)


class ImportGitlabRequestSerializer(serializers.Serializer):
    # GitLab numeric project id
    project_id = serializers.IntegerField(required=True)
    organization_id = OptionalIntField(required=True, allow_null=False)


class ImportGitlabResponseSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    aist_project_id = serializers.IntegerField()
    repository_id = serializers.IntegerField()
    repo_full = serializers.CharField()


class ImportProjectFromGitlabAPI(APIView):

    """
    Create Product + RepositoryInfo(GITLAB) + ScmGitlabBinding + AISTProject
    from a GitLab project id.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ImportGitlabRequestSerializer,
        responses={201: OpenApiResponse(ImportGitlabResponseSerializer)},
        tags=[AISTApiTag.GITLAB.value],
        summary="Import project from GitLab",
        description="Creates Product and AISTProject from GitLab project id (MVP).",
    )
    def post(self, request, *args, **kwargs):
        serializer = ImportGitlabRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_id = serializer.validated_data["project_id"]

        organization_id = serializer.validated_data.get("organization_id")
        organization = get_object_or_404(
            get_authorized_aist_organizations(Permissions.Product_Type_Add_Product, user=request.user),
            pk=organization_id,
        )

        integration = (
            OrgIntegration.objects.filter(
                organization=organization,
                integration_type=OrgIntegrationType.GITLAB,
                is_active=True,
            )
            .order_by("pk")
            .first()
        )
        if not integration:
            return Response(
                {"detail": "No active GitLab integration found for this organization."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fetch project metadata via Celery worker (has Docker socket for VPN sidecar).
        try:
            proj_data = fetch_gitlab_project_info.delay(integration.pk, project_id).get(timeout=60)
        except Exception:
            return Response({"detail": "GitLab fetch timed out or failed."}, status=status.HTTP_502_BAD_GATEWAY)

        if not proj_data.get("ok"):
            if proj_data.get("response_code") == 404:
                return Response({"detail": "GitLab project not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": proj_data.get("error", "GitLab API error")}, status=status.HTTP_502_BAD_GATEWAY)

        # path_with_namespace like "group/subgroup/name"
        path_with_ns = proj_data["path_with_namespace"]
        if "/" not in path_with_ns:
            return Response({"detail": "Unexpected path_with_namespace"}, status=status.HTTP_400_BAD_REQUEST)

        owner_ns, repo_name = path_with_ns.rsplit("/", 1)
        description = proj_data["description"] or "Empty description. Admin, fix me"
        inferred_base = proj_data["inferred_base"]
        langs_raw = proj_data["langs_raw"]

        cfg = _load_analyzers_config()
        if not cfg:
            return Response({"detail": "Analyzers config not loaded"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        langs = cfg.convert_languages(langs_raw)

        product_type = organization.ensure_product_type()

        # 3) Create Product in resolved Product Type
        product, created_product = Product.objects.get_or_create(
            name=path_with_ns,
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
            defaults={"value": "gitlab"},
        )

        # 4) Create/Update RepositoryInfo (GITLAB)
        repo_info, _ = RepositoryInfo.objects.get_or_create(
            type=ScmType.GITLAB,
            repo_owner=owner_ns,
            repo_name=repo_name,
            defaults={"base_url": inferred_base},
        )

        binding, _ = ScmGitlabBinding.objects.get_or_create(scm=repo_info)

        if binding.org_integration_id != integration.id:
            binding.org_integration = integration
            binding.save(update_fields=["org_integration"])

        with transaction.atomic():
            aist_project, project_created = AISTProject.objects.get_or_create(
                product=product,
                defaults={
                    "supported_languages": langs,
                    "compilable": False,
                    "profile": {},
                    "repository": repo_info,
                    "organization": organization,
                },
            )
            if project_created:
                _create_initial_script(aist_project, DEFAULT_ENTRYPOINT_SCRIPT)
            else:
                if aist_project.organization_id and aist_project.organization_id != organization.id:
                    msg = "Project is already linked to another organization."
                    return Response({"detail": msg}, status=status.HTTP_409_CONFLICT)
                if aist_project.organization_id is None:
                    aist_project.organization = organization
                    aist_project.save(update_fields=["organization"])

        out = ImportGitlabResponseSerializer(
            {
                "product_id": product.id,
                "product_name": product.name,
                "aist_project_id": aist_project.id,
                "repository_id": repo_info.id,
                "repo_full": f"{owner_ns}/{repo_name}",
            },
        )
        return Response(out.data, status=status.HTTP_201_CREATED)
