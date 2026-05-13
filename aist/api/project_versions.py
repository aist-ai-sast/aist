from __future__ import annotations

from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import AISTProjectScript, AISTProjectVersion, VersionType
from aist.queries import get_authorized_aist_projects

SCRIPT_SOURCE_VERSION = "version"
SCRIPT_SOURCE_PROJECT_REVISION = "project_revision"
SCRIPT_SOURCE_SHARED_DEFAULT = "shared_default"


def _resolve_script_for_new_version(project) -> AISTProjectScript:
    """
    Return the script to use for a new version when no script_id is specified.

    Resolution order:
    1. Latest project-scoped script revision (set at project creation or via API)
    2. Project-scoped copy of the shared default (created on demand)
    """
    latest_revision = project.script_revisions.order_by("-created_at").first()
    if latest_revision:
        return latest_revision
    global_default = AISTProjectScript.get_shared_default()
    script, _ = AISTProjectScript.get_or_create_for_project(
        content=global_default.content,
        project=project,
    )
    return script


class AISTProjectVersionCreateSerializer(serializers.ModelSerializer):

    """
    Serializer for creating AISTProjectVersion instances via API.
    Performs the same validations as AISTProjectVersionForm:
    - For FILE_HASH requires `source_archive`
    - For GIT_BRANCH/GIT_HASH requires `version`
    - Ensures the combination (project, version) is unique
    """

    id = serializers.IntegerField(read_only=True)
    project = serializers.PrimaryKeyRelatedField(read_only=True)
    script_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = AISTProjectVersion
        fields = ("id", "project", "version_type", "version", "source_archive", "script_id")
        extra_kwargs = {
            "version": {"required": False, "allow_blank": True},
            "source_archive": {"required": False},
        }

    def validate(self, attrs):
        project = self.context.get("project")
        if project is None:
            raise serializers.ValidationError({"project": "Project is required."})

        version_type = attrs.get("version_type")
        version = attrs.get("version") or ""
        source_archive = attrs.get("source_archive")

        if version_type == VersionType.FILE_HASH and not source_archive:
            raise serializers.ValidationError(
                {"source_archive": "This field is required for FILE_HASH versions."},
            )

        if version_type in {VersionType.GIT_BRANCH, VersionType.GIT_HASH} and not version:
            raise serializers.ValidationError(
                {"version": "This field is required for GIT_BRANCH/GIT_HASH versions."},
            )

        if version:
            exists = AISTProjectVersion.objects.filter(
                project=project,
                version=version,
                version_type=version_type,
            ).exists()
            if exists:
                raise serializers.ValidationError(
                    {"version": "This version already exists for this project."},
                )

        attrs["project"] = project
        return attrs

    def create(self, validated_data):
        # Ensure every version always has a project-scoped script.
        if "script" not in validated_data or validated_data.get("script") is None:
            validated_data["script"] = _resolve_script_for_new_version(validated_data["project"])
        # for FILE_HASH without explicit version the model will set sha256 in save()
        return AISTProjectVersion.objects.create(**validated_data)


class AISTProjectVersionScriptUpdateSerializer(serializers.Serializer):
    script_id = serializers.IntegerField(allow_null=True)

    def validate_script_id(self, value):
        if value is None:
            return None
        project = self.context["project"]
        # Only project-scoped scripts are allowed; shared singleton must never be
        # set directly on a version (org isolation + historicity contract).
        allowed = AISTProjectScript.objects.filter(
            project=project,
            is_shared=False,
            pk=value,
        )
        if not allowed.exists():
            msg = (
                "Script not found or not accessible for this project. "
                "Only project-scoped scripts may be assigned to a version."
            )
            raise serializers.ValidationError(msg)
        return value


class ProjectVersionCreateAPI(AuthorizedQuerySetMixin, APIView):

    """API endpoint for creating AISTProjectVersion instances."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        methods=["post"],
        request=AISTProjectVersionCreateSerializer,
        responses={
            201: OpenApiResponse(
                AISTProjectVersionCreateSerializer,
                description="Project version created successfully",
            ),
            400: OpenApiResponse(
                description="Validation failed",
            ),
            404: OpenApiResponse(
                description="Project not found",
            ),
        },
        tags=[AISTApiTag.PROJECTS.value],
    )
    def post(self, request, project_id):
        project = self.get_authorized_object(permission=Permissions.Product_Edit, pk=project_id)

        serializer = AISTProjectVersionCreateSerializer(
            data=request.data,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)
        version = serializer.save()

        out = AISTProjectVersionCreateSerializer(instance=version, context={"project": project})
        return Response(out.data, status=status.HTTP_201_CREATED)


def _resolve_version_script(version, project) -> tuple[AISTProjectScript, str]:
    """
    Return the script for a project version with inheritance fallback.

    Resolution order:
    1. version.script (source="version")
    2. latest project-scoped revision (source="project_revision")
    3. shared default singleton (source="shared_default")
    """
    if version.script_id:
        return version.script, SCRIPT_SOURCE_VERSION
    latest_revision = project.script_revisions.order_by("-created_at").first()
    if latest_revision:
        return latest_revision, SCRIPT_SOURCE_PROJECT_REVISION
    return AISTProjectScript.get_shared_default(), SCRIPT_SOURCE_SHARED_DEFAULT


def _serialize_version_script(script: AISTProjectScript, source: str) -> dict:
    return {
        "id": script.id,
        "content": script.content,
        "sha256": script.sha256,
        "is_shared": script.is_shared,
        "created_at": script.created_at.isoformat() if script.created_at else None,
        "created_by_id": script.created_by_id,
        "created_by_username": script.created_by.username if script.created_by else None,
        "inherited": source != SCRIPT_SOURCE_VERSION,
        "source": source,
    }


class ProjectVersionScriptUpdateAPI(AuthorizedQuerySetMixin, APIView):

    """GET/PATCH endpoint for a project version's entrypoint script."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        methods=["get"],
        responses={
            200: OpenApiResponse(description="Script for the version (own or inherited)"),
            404: OpenApiResponse(description="Project or version not found"),
        },
        tags=[AISTApiTag.PROJECTS.value],
        summary="Get version script",
        description=(
            "Returns the script used for a specific project version. "
            "If the version has no own script, falls back to the latest project-scoped "
            "revision; if none exists, returns the shared default. The response "
            "includes `inherited` and `source` flags so the UI can label the script."
        ),
    )
    def get(self, request, project_id, version_id):
        project = self.get_authorized_object(id=project_id)

        try:
            version = AISTProjectVersion.objects.select_related("script").get(
                pk=version_id, project=project,
            )
        except AISTProjectVersion.DoesNotExist:
            return Response({"detail": "Version not found."}, status=status.HTTP_404_NOT_FOUND)

        script, source = _resolve_version_script(version, project)
        return Response(_serialize_version_script(script, source), status=status.HTTP_200_OK)

    @extend_schema(
        methods=["patch"],
        request=AISTProjectVersionScriptUpdateSerializer,
        responses={
            200: OpenApiResponse(description="Script updated"),
            400: OpenApiResponse(description="Validation failed"),
            404: OpenApiResponse(description="Project or version not found"),
        },
        tags=[AISTApiTag.PROJECTS.value],
        summary="Set version script override",
        description=(
            "Set or clear the script override for a specific project version. "
            "Pass script_id=null to clear the override (falls back to project active_script)."
        ),
    )
    def patch(self, request, project_id, version_id):
        project = self.get_authorized_object(permission=Permissions.Product_Edit, pk=project_id)

        try:
            version = AISTProjectVersion.objects.get(pk=version_id, project=project)
        except AISTProjectVersion.DoesNotExist:
            return Response({"detail": "Version not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = AISTProjectVersionScriptUpdateSerializer(
            data=request.data,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)

        script_id = serializer.validated_data["script_id"]
        version.script_id = script_id
        version.save(update_fields=["script", "updated"])

        return Response({"script_id": version.script_id}, status=status.HTTP_200_OK)
