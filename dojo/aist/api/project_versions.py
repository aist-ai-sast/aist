from __future__ import annotations

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.aist.models import AISTProjectVersion, VersionType
from dojo.aist.queries import get_authorized_aist_projects
from dojo.authorization.roles_permissions import Permissions


class AISTProjectVersionCreateSerializer(serializers.ModelSerializer):

    """
    Serializer for creating AISTProjectVersion instances via API.
    Performs the same validations as AISTProjectVersionForm:
    - For FILE_HASH requires `source_archive`
    - For GIT_HASH requires `version`
    - Ensures the combination (project, version) is unique
    """

    id = serializers.IntegerField(read_only=True)
    project = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = AISTProjectVersion
        fields = ("id", "project", "version_type", "version", "source_archive")
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

        if version_type == VersionType.GIT_HASH and not version:
            raise serializers.ValidationError(
                {"version": "This field is required for GIT_HASH versions."},
            )

        if version:
            exists = AISTProjectVersion.objects.filter(
                project=project, version=version,
            ).exists()
            if exists:
                raise serializers.ValidationError(
                    {"version": "This version already exists for this project."},
                )

        attrs["project"] = project
        return attrs

    def create(self, validated_data):
        # for FILE_HASH without explicit version the model will set sha256 in save()
        return AISTProjectVersion.objects.create(**validated_data)


class ProjectVersionCreateAPI(APIView):

    """API endpoint for creating AISTProjectVersion instances."""

    permission_classes = [IsAuthenticated]

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
    )
    def post(self, request, project_id):
        project = get_object_or_404(
            get_authorized_aist_projects(Permissions.Product_Edit, user=request.user),
            pk=project_id,
        )

        serializer = AISTProjectVersionCreateSerializer(
            data=request.data,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)
        version = serializer.save()

        out = AISTProjectVersionCreateSerializer(instance=version, context={"project": project})
        return Response(out.data, status=status.HTTP_201_CREATED)
