# --- add near other imports in api.py ---
import gitlab
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.aist.models import AISTProject, Organization, RepositoryInfo, ScmGitlabBinding, ScmType
from dojo.aist.utils.pipeline_imports import _load_analyzers_config  # same helper as GH flow uses
from dojo.authorization.authorization import (
    user_has_global_permission_or_403,
    user_has_permission_or_403,
)
from dojo.authorization.roles_permissions import Permissions
from dojo.models import DojoMeta, Product, Product_Type


class OptionalIntField(serializers.IntegerField):
    def to_internal_value(self, data):
        if data in {None, ""}:
            return None
        return super().to_internal_value(data)


class ImportGitlabRequestSerializer(serializers.Serializer):
    # GitLab numeric project id
    project_id = serializers.IntegerField(required=True)
    # Personal/Group/Project Access Token with read_api (and read_repository if cloning is needed)
    gitlab_api_token = serializers.CharField(write_only=True, trim_whitespace=True)
    # Optional for self-hosted GitLab like https://gitlab.company.tld
    base_url = serializers.URLField(required=False, default="https://gitlab.com")
    organization_id = OptionalIntField(required=False, allow_null=True)


class ImportGitlabResponseSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    product_name = serializers.CharField()
    aist_project_id = serializers.IntegerField()
    repository_id = serializers.IntegerField()
    repo_full = serializers.CharField()


class UpdateGitlabTokenRequestSerializer(serializers.Serializer):
    gitlab_api_token = serializers.CharField(write_only=True, trim_whitespace=True)


class ImportProjectFromGitlabAPI(APIView):

    """
    Create Product + RepositoryInfo(GITLAB) + ScmGitlabBinding + AISTProject
    from a GitLab project id.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ImportGitlabRequestSerializer,
        responses={201: OpenApiResponse(ImportGitlabResponseSerializer)},
        tags=["aist"],
        summary="Import project from GitLab",
        description="Creates Product and AISTProject from GitLab project id (MVP).",
    )
    def post(self, request, *args, **kwargs):
        serializer = ImportGitlabRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_id = serializer.validated_data["project_id"]
        token = serializer.validated_data["gitlab_api_token"].strip()
        base_url = serializer.validated_data.get("base_url") or "https://gitlab.com"

        gl = gitlab.Gitlab(base_url, private_token=token)

        try:
            proj = gl.projects.get(project_id)
        except gitlab.exceptions.GitlabGetError as exc:
            if exc.response_code == 404:
                return Response({"detail": "GitLab project not found"}, status=status.HTTP_404_NOT_FOUND)
            msg = f"GitLab API error: {exc}"
            return Response({"detail": msg}, status=status.HTTP_502_BAD_GATEWAY)

        # path_with_namespace like "group/subgroup/name"
        path_with_ns = getattr(proj, "path_with_namespace", None) or ""
        if "/" not in path_with_ns:
            return Response({"detail": "Unexpected path_with_namespace"}, status=status.HTTP_400_BAD_REQUEST)

        owner_ns, repo_name = path_with_ns.rsplit("/", 1)
        description = getattr(proj, "description", None) or "Empty description. Admin, fix me"
        web_url = (getattr(proj, "web_url", None) or base_url).rstrip("/")
        # base host like https://gitlab.com or self-hosted origin
        inferred_base = web_url.split("/" + path_with_ns)[0]

        # 2) Fetch languages (dict {lang: percent})
        try:
            langs_raw = proj.languages() or {}
        except gitlab.exceptions.GitlabGetError as exc:
            msg = f"GitLab API error: {exc}"
            return Response({"detail": msg}, status=status.HTTP_502_BAD_GATEWAY)

        cfg = _load_analyzers_config()
        if not cfg:
            return Response({"detail": "Analyzers config not loaded"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        langs = cfg.convert_languages(langs_raw)

        # 3) Create Product Type and Product
        product_type, created_product_type = Product_Type.objects.get_or_create(name="Gitlab Imported")
        if created_product_type:
            user_has_global_permission_or_403(request.user, Permissions.Product_Type_Add)
        user_has_permission_or_403(request.user, product_type, Permissions.Product_Type_Add_Product)
        product, _created = Product.objects.get_or_create(
            name=path_with_ns,
            defaults={"prod_type": product_type, "description": description},
        )
        if not _created:
            user_has_permission_or_403(request.user, product, Permissions.Product_Edit)

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

        binding, _created = ScmGitlabBinding.objects.get_or_create(scm=repo_info)

        if token and binding.personal_access_token != token:
            binding.personal_access_token = token
            binding.save(update_fields=["personal_access_token"])

        organization_id = serializer.validated_data.get("organization_id")
        organization = None
        if organization_id is not None:
            organization = get_object_or_404(Organization, pk=organization_id)

        aist_project, _ = AISTProject.objects.get_or_create(
            product=product,
            defaults={
                "supported_languages": langs,
                "script_path": "input_projects/default_imported_project_no_built.sh",
                "compilable": False,
                "profile": {},
                "repository": repo_info,
                "organization": organization,
            },
        )

        out = ImportGitlabResponseSerializer({
            "product_id": product.id,
            "product_name": product.name,
            "aist_project_id": aist_project.id,
            "repository_id": repo_info.id,
            "repo_full": f"{owner_ns}/{repo_name}",
        })
        return Response(out.data, status=status.HTTP_201_CREATED)


class ProjectGitlabTokenUpdateAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=UpdateGitlabTokenRequestSerializer,
        responses={200: OpenApiResponse(description="Token updated")},
        tags=["aist"],
        summary="Update GitLab token for project",
    )
    def post(self, request, project_id: int, *args, **kwargs):
        serializer = UpdateGitlabTokenRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["gitlab_api_token"].strip()
        if not token:
            return Response({"detail": "GitLab token is required"}, status=status.HTTP_400_BAD_REQUEST)

        project = get_object_or_404(AISTProject.objects.select_related("repository"), id=project_id)
        repo = project.repository
        if not repo or repo.type != ScmType.GITLAB:
            return Response({"detail": "Project repository is not GitLab"}, status=status.HTTP_400_BAD_REQUEST)

        binding, _created = ScmGitlabBinding.objects.get_or_create(scm=repo)
        binding.personal_access_token = token
        binding.save(update_fields=["personal_access_token"])

        return Response({"ok": True})
