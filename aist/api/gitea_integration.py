from __future__ import annotations

from django.shortcuts import get_object_or_404
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.schema import AISTApiTag
from aist.models import OrgIntegration, OrgIntegrationType, ScmGiteaBinding, ScmType
from aist.queries import get_authorized_aist_organizations
from aist.scm_import import ScmImportConflict, ScmImportRequest, import_scm_project
from aist.tasks.integrations import fetch_gitea_project_info
from aist.utils.pipeline_imports import _load_analyzers_config  # same helper as GitLab/GH flows use


class OptionalIntField(serializers.IntegerField):
    def to_internal_value(self, data):
        if data in {None, ""}:
            return None
        return super().to_internal_value(data)


class ImportGiteaRequestSerializer(serializers.Serializer):
    # Gitea project is "owner/repo" (no arbitrary nested subgroups, unlike GitLab).
    repo_full_name = serializers.CharField(required=True, allow_blank=False, max_length=255)
    organization_id = OptionalIntField(required=True, allow_null=False)
    auto_analyze = serializers.BooleanField(default=False, required=False)


class ImportGiteaResponseSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    aist_project_id = serializers.IntegerField()
    repository_id = serializers.IntegerField()
    repo_full = serializers.CharField()


class ImportProjectFromGiteaAPI(APIView):

    """
    Create Product + RepositoryInfo(GITEA) + ScmGiteaBinding + AISTProject
    from a Gitea "owner/repo" full name.

    Gitea-specific concerns only (request shape, integration resolution,
    metadata fetch via the Gitea Celery task); the generic Product/AISTProject
    creation workflow shared by every SCM provider lives in
    ``aist.scm_import.import_scm_project``.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ImportGiteaRequestSerializer,
        responses={201: OpenApiResponse(ImportGiteaResponseSerializer)},
        tags=[AISTApiTag.GITEA.value],
        summary="Import project from Gitea",
        description="Creates Product and AISTProject from a Gitea 'owner/repo' full name.",
    )
    def post(self, request, *args, **kwargs):
        serializer = ImportGiteaRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        repo_full_name = serializer.validated_data["repo_full_name"].strip().strip("/")
        if not repo_full_name or "/" not in repo_full_name:
            return Response({"detail": "repo_full_name must be 'owner/repo'."}, status=status.HTTP_400_BAD_REQUEST)

        organization_id = serializer.validated_data.get("organization_id")
        organization = get_object_or_404(
            get_authorized_aist_organizations(Permissions.Product_Type_Add_Product, user=request.user),
            pk=organization_id,
        )

        integration = (
            OrgIntegration.objects.filter(
                organization=organization,
                integration_type=OrgIntegrationType.GITEA,
                is_active=True,
            )
            .order_by("pk")
            .first()
        )
        if not integration:
            return Response(
                {"detail": "No active Gitea integration found for this organization."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fetch project metadata via Celery worker (has Docker socket for VPN sidecar).
        try:
            proj_data = fetch_gitea_project_info.delay(integration.pk, repo_full_name).get(timeout=60)
        except Exception:
            return Response({"detail": "Gitea fetch timed out or failed."}, status=status.HTTP_502_BAD_GATEWAY)

        if not proj_data.get("ok"):
            if proj_data.get("response_code") == 404:
                return Response({"detail": "Gitea project not found"}, status=status.HTTP_404_NOT_FOUND)
            return Response({"detail": proj_data.get("error", "Gitea API error")}, status=status.HTTP_502_BAD_GATEWAY)

        path_with_ns = proj_data["path_with_namespace"]
        if "/" not in path_with_ns:
            return Response({"detail": "Unexpected repo full name"}, status=status.HTTP_400_BAD_REQUEST)
        owner_ns, repo_name = path_with_ns.rsplit("/", 1)

        cfg = _load_analyzers_config()
        if not cfg:
            return Response({"detail": "Analyzers config not loaded"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        langs = cfg.convert_languages(proj_data["langs_raw"])

        import_request = ScmImportRequest(
            request_user=request.user,
            organization=organization,
            scm_type=ScmType.GITEA,
            scm_label="gitea",
            binding_model=ScmGiteaBinding,
            org_integration=integration,
            repo_owner=owner_ns,
            repo_name=repo_name,
            description=proj_data["description"],
            inferred_base=proj_data["inferred_base"],
            supported_languages=langs,
            auto_analyze=serializer.validated_data.get("auto_analyze", False),
            default_branch=proj_data.get("default_branch") or "",
        )
        try:
            aist_project, repo_full = import_scm_project(import_request)
        except ScmImportConflict as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

        out = ImportGiteaResponseSerializer(
            {
                "product_id": aist_project.product_id,
                "product_name": aist_project.product.name,
                "aist_project_id": aist_project.id,
                "repository_id": aist_project.repository_id,
                "repo_full": repo_full,
            },
        )
        return Response(out.data, status=status.HTTP_201_CREATED)
