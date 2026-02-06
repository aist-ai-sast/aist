from __future__ import annotations

import json

from django.shortcuts import get_object_or_404
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.models import AISTProject, Organization
from aist.queries import get_authorized_aist_projects
from aist.utils.pipeline_imports import _load_analyzers_config


class AISTProjectSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = AISTProject
        fields = ["id", "product_name", "supported_languages", "compilable", "created", "updated", "repository"]


class DefaultAnalyzersRequestSerializer(serializers.Serializer):
    project = serializers.IntegerField(required=False)
    time_class_level = serializers.CharField(required=False)
    languages = serializers.ListField(child=serializers.CharField(), required=False)


class ProjectUpdateRequestSerializer(serializers.Serializer):
    script_path = serializers.CharField(required=True)
    supported_languages = serializers.CharField(required=False)
    compilable = serializers.BooleanField(required=False)
    profile = serializers.JSONField(required=False)
    organization = serializers.IntegerField(required=False, allow_null=True)


class AISTProjectListAPI(generics.ListAPIView):

    """List all current AISTProjects."""

    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List all AISTProjects",
        description="Returns all existing AISTProject records with their metadata.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return (
            get_authorized_aist_projects(Permissions.Product_View, user=self.request.user)
            .select_related("product")
            .order_by("created")
        )


class AISTProjectDetailAPI(generics.RetrieveDestroyAPIView):
    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"

    @extend_schema(
        responses={204: OpenApiResponse(description="AIST project deleted"), 404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Delete AIST project",
        description="Deletes the specified AISTProject by id.",
    )
    def delete(self, request, project_id: int, *args, **kwargs) -> Response:
        p = get_object_or_404(
            get_authorized_aist_projects(Permissions.Product_Edit, user=request.user),
            id=project_id,
        )
        user_has_permission_or_403(request.user, p.product, Permissions.Product_Edit)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        responses={404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Get AIST project",
        description="Get the specified AISTProject by id.",
    )
    def get(self, request, project_id: int, *args, **kwargs) -> Response:
        project = get_object_or_404(
            get_authorized_aist_projects(Permissions.Product_View, user=request.user),
            id=project_id,
        )
        serializer = AISTProjectSerializer(project)
        return Response(serializer.data, status=status.HTTP_200_OK)


def project_meta_payload(project: AISTProject) -> dict:
    versions = [{"id": str(v.id), "label": str(v)} for v in project.versions.all()]
    return {
        "supported_languages": project.supported_languages or [],
        "versions": versions,
    }


def _get_list(payload, key: str) -> list[str]:
    if hasattr(payload, "getlist"):
        return payload.getlist(key)
    value = payload.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def default_analyzers_payload(*, project: AISTProject | None, project_id: str | None, langs: list[str], time_class: str):
    cfg = _load_analyzers_config()
    if not cfg:
        return None, "config not loaded"

    filtered = cfg.get_filtered_analyzers(
        analyzers_to_run=None,
        max_time_class=time_class,
        non_compile_project=not project.compilable if project else False,
        target_languages=langs,
        show_only_parent=True,
    )
    defaults = cfg.get_names(filtered)
    return {
        "defaults": defaults,
        "signature": f"{project.id if project else (project_id or '')}::{time_class}::{','.join(sorted(set(langs or [])))}",
    }, None


def update_project_from_payload(*, project: AISTProject, payload: dict):
    script_path = (payload.get("script_path") or "").strip()
    compilable = payload.get("compilable") in {"on", True, "true", "1"}
    supported_languages_raw = payload.get("supported_languages")
    profile_raw = payload.get("profile")
    organization_raw = payload.get("organization")

    errors: dict[str, str] = {}

    if not script_path:
        errors["script_path"] = "Script path is required."

    cfg = _load_analyzers_config()
    if not cfg:
        return None, {"__all__": "config not loaded"}

    if isinstance(supported_languages_raw, list):
        languages = cfg.convert_languages(supported_languages_raw)
    elif supported_languages_raw:
        languages = cfg.convert_languages(
            [x.strip() for x in str(supported_languages_raw).split(",") if x.strip()],
        )
    else:
        languages = []

    profile: dict | list | None
    if profile_raw in {None, ""}:
        profile = {}
    elif isinstance(profile_raw, dict):
        profile = profile_raw
    else:
        try:
            profile = json.loads(profile_raw)
        except Exception:
            errors["profile"] = "Profile must be a valid JSON value."
            profile = None

    if profile is not None and not isinstance(profile, dict):
        errors["profile"] = 'Profile must be a JSON object (e.g. {"paths": {"exclude": []}}).'

    organization = None
    if organization_raw not in {None, ""}:
        try:
            org_id = int(organization_raw)
            organization = Organization.objects.get(id=org_id)
        except (ValueError, Organization.DoesNotExist):
            errors["organization"] = "Selected organization does not exist."

    if errors:
        return None, errors

    project.script_path = script_path
    project.compilable = compilable
    project.supported_languages = languages
    project.profile = profile or {}
    project.organization = organization
    project.save(
        update_fields=[
            "script_path",
            "compilable",
            "supported_languages",
            "profile",
            "organization",
            "updated",
        ],
    )

    return {
        "id": project.id,
        "product_name": getattr(project.product, "name", str(project.id)),
        "script_path": project.script_path,
        "compilable": project.compilable,
        "supported_languages": project.supported_languages,
        "profile": project.profile,
        "organization_id": project.organization_id,
        "organization_name": getattr(project.organization, "name", None),
    }, None


class AISTProjectMetaAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Project meta")})
    def get(self, request, project_id: int):
        project = get_object_or_404(
            get_authorized_aist_projects(Permissions.Product_View, user=request.user),
            id=project_id,
        )
        return Response(project_meta_payload(project))


class AISTDefaultAnalyzersAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=DefaultAnalyzersRequestSerializer,
        responses={200: OpenApiResponse(description="Default analyzers")},
    )
    def post(self, request):
        project_id = request.data.get("project")
        time_class = request.data.get("time_class_level") or "slow"
        langs = _get_list(request.data, "languages")

        project = (
            get_authorized_aist_projects(Permissions.Product_View, user=request.user)
            .filter(id=project_id)
            .first()
        )
        if not project:
            return Response({"detail": "Project not found"}, status=status.HTTP_404_NOT_FOUND)

        payload, error = default_analyzers_payload(
            project=project,
            project_id=str(project_id) if project_id is not None else None,
            langs=langs,
            time_class=time_class,
        )
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class AISTProjectUpdateAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ProjectUpdateRequestSerializer,
        responses={200: OpenApiResponse(description="Project updated")},
    )
    def post(self, request, project_id: int):
        project = get_object_or_404(
            get_authorized_aist_projects(Permissions.Product_Edit, user=request.user),
            id=project_id,
        )
        user_has_permission_or_403(request.user, project.product, Permissions.Product_Edit)
        payload, errors = update_project_from_payload(project=project, payload=request.data)
        if errors:
            return Response({"ok": False, "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True, "project": payload})
