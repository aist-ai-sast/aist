from __future__ import annotations

from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.models import AISTProject, Organization
from aist.queries import get_authorized_aist_projects
from aist.utils.pipeline_imports import _load_analyzers_config


class AISTProjectSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_id = serializers.IntegerField(source="product.id", read_only=True)

    class Meta:
        model = AISTProject
        fields = [
            "id",
            "product_id",
            "product_name",
            "supported_languages",
            "compilable",
            "created",
            "updated",
            "repository",
        ]


class DefaultAnalyzersRequestSerializer(serializers.Serializer):
    project = serializers.IntegerField(required=False)
    time_class_level = serializers.CharField(required=False)
    languages = serializers.ListField(child=serializers.CharField(), required=False)


class ProjectUpdateRequestSerializer(serializers.Serializer):
    script_path = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)
    supported_languages = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    compilable = serializers.BooleanField(required=False, default=False)
    profile = serializers.JSONField(required=False, default=dict)
    organization = serializers.PrimaryKeyRelatedField(queryset=Organization.objects.all(), required=False, allow_null=True)

    def to_internal_value(self, data):
        mutable = data.copy()
        raw_languages = mutable.get("supported_languages")
        if isinstance(raw_languages, str):
            parsed_languages = [token.strip() for token in raw_languages.split(",") if token.strip()]
            if hasattr(mutable, "setlist"):
                mutable.setlist("supported_languages", parsed_languages)
            else:
                mutable["supported_languages"] = parsed_languages
        return super().to_internal_value(mutable)

    def validate_profile(self, value):
        if value is None or not value:
            return {}
        if not isinstance(value, dict):
            msg = 'Profile must be a JSON object (e.g. {"paths": {"exclude": []}}).'
            raise serializers.ValidationError(msg)
        return value


class AISTProjectListAPI(AuthorizedQuerySetMixin, generics.ListAPIView):

    """List all current AISTProjects."""

    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="List all AISTProjects",
        description="Returns all existing AISTProject records with their metadata.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return (
            self.get_authorized_queryset()
            .select_related("product")
            .order_by("created")
        )


class AISTProjectDetailAPI(AuthorizedQuerySetMixin, generics.RetrieveDestroyAPIView):
    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={204: OpenApiResponse(description="AIST project deleted"), 404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Delete AIST project",
        description="Deletes the specified AISTProject by id.",
    )
    def delete(self, request, project_id: int, *args, **kwargs) -> Response:
        p = self.get_authorized_object(
            permission=Permissions.Product_Edit,
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
        project = self.get_authorized_object(
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
    script_path = payload["script_path"]
    compilable = bool(payload.get("compilable"))
    supported_languages_raw = payload.get("supported_languages") or []
    profile = payload.get("profile") or {}
    organization = payload.get("organization")

    cfg = _load_analyzers_config()
    if not cfg:
        return None, {"__all__": "config not loaded"}
    languages = cfg.convert_languages(supported_languages_raw)

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


class AISTProjectMetaAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(responses={200: OpenApiResponse(description="Project meta")})
    def get(self, request, project_id: int):
        project = self.get_authorized_object(
            id=project_id,
        )
        return Response(project_meta_payload(project))


class AISTDefaultAnalyzersAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        request=DefaultAnalyzersRequestSerializer,
        responses={200: OpenApiResponse(description="Default analyzers")},
    )
    def post(self, request):
        serializer = DefaultAnalyzersRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_id = serializer.validated_data.get("project")
        time_class = serializer.validated_data.get("time_class_level") or "slow"
        langs = serializer.validated_data.get("languages", [])

        project = self.get_authorized_queryset().filter(id=project_id).first()
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


class AISTProjectUpdateAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        request=ProjectUpdateRequestSerializer,
        responses={200: OpenApiResponse(description="Project updated")},
    )
    def post(self, request, project_id: int):
        project = self.get_authorized_object(
            permission=Permissions.Product_Edit,
            id=project_id,
        )
        user_has_permission_or_403(request.user, project.product, Permissions.Product_Edit)
        serializer = ProjectUpdateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload, errors = update_project_from_payload(project=project, payload=serializer.validated_data)
        if errors:
            return Response({"ok": False, "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True, "project": payload})
