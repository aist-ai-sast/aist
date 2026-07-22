"""Validate and asynchronously import reports handled by registered parsers."""
from __future__ import annotations

from collections import Counter
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from dojo.authorization.roles_permissions import Permissions
from dojo.tools import factory
from dojo.utils import is_scan_file_too_large
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import AISTProject
from aist.queries import get_authorized_aist_projects
from aist.tasks.report_import import import_report
from aist.utils.pipeline import create_pipeline_object
from aist.utils.report_import import (
    ReportImportError,
    discard_uploaded_report,
    resolve_import_version,
    store_uploaded_report,
)


class _ScanTypeFieldMixin:

    """Shared, always-current ``scan_type`` gate for both import serializers below."""

    def get_fields(self):
        fields = super().get_fields()
        fields["scan_type"] = serializers.ChoiceField(choices=factory.get_choices_sorted())
        return fields


class _ProjectFieldMixin:
    project_permission: str

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["project_id"].queryset = get_authorized_aist_projects(
                self.project_permission,
                user=request.user,
            )
        return fields


def _validate_file_size(value) -> None:
    if value.size > settings.PIPELINE_IMPORT_MAX_SIZE_BYTES or is_scan_file_too_large(value):
        msg = f"File exceeds the maximum allowed size of {settings.PIPELINE_IMPORT_MAX_SIZE_BYTES} bytes."
        raise serializers.ValidationError(msg)


class PipelineImportValidateRequestSerializer(_ProjectFieldMixin, _ScanTypeFieldMixin, serializers.Serializer):
    project_permission = Permissions.Product_View

    file = serializers.FileField()
    project_id = serializers.PrimaryKeyRelatedField(
        source="project",
        queryset=AISTProject.objects.none(),
    )

    def validate_file(self, value):
        _validate_file_size(value)
        return value


class PipelineImportPreviewSerializer(serializers.Serializer):
    findings_count = serializers.IntegerField()
    severity_breakdown = serializers.DictField(child=serializers.IntegerField())
    name = serializers.CharField(allow_null=True)
    version = serializers.CharField(allow_null=True)
    detected_commit_hash = serializers.CharField(allow_null=True)


class PipelineImportRequestSerializer(_ProjectFieldMixin, _ScanTypeFieldMixin, serializers.Serializer):
    project_permission = Permissions.Product_Edit

    file = serializers.FileField()
    project_id = serializers.PrimaryKeyRelatedField(
        source="project",
        queryset=AISTProject.objects.none(),
    )
    commit_hash = serializers.CharField(max_length=64)  # matches AISTProjectVersion.version's own max_length

    def validate_file(self, value):
        _validate_file_size(value)
        return value


class PipelineImportResponseSerializer(serializers.Serializer):
    pipeline_id = serializers.CharField()
    run_task_id = serializers.CharField()


class PipelineImportValidateAPI(AuthorizedQuerySetMixin, APIView):

    """Parse an upload and return its preview."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_pipeline_import"
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        request=PipelineImportValidateRequestSerializer,
        responses={
            200: OpenApiResponse(PipelineImportPreviewSerializer, description="Report parsed"),
            400: OpenApiResponse(description="Invalid report, upload, or target project"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Validate and preview a report before importing it",
        description=(
            "Parses the uploaded report using whatever parser is registered for scan_type "
            "(the same call DefaultImporter itself makes) and returns a preview, entirely "
            "in memory for this request."
        ),
    )
    def post(self, request, *args, **kwargs) -> Response:
        serializer = PipelineImportValidateRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        project = serializer.validated_data["project"]

        scan_type = serializer.validated_data["scan_type"]
        uploaded_file = serializer.validated_data["file"]
        uploaded_file.seek(0)

        parser = factory.get_parser(scan_type)
        try:
            tests = parser.get_tests(scan_type, uploaded_file)
            test = tests[0] if tests else None
            findings = test.findings if test else []
            repository = project.repository
            extract_source_commits = getattr(parser, "extract_source_commits", None)
            source_commits = extract_source_commits(uploaded_file) if extract_source_commits else {}
            detected_commit_hash = source_commits.get(repository.repo_name) if repository else None
        except (TypeError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        out = PipelineImportPreviewSerializer({
            "findings_count": len(findings),
            "severity_breakdown": dict(Counter(f.severity for f in findings)),
            "name": getattr(test, "name", None) if test else None,
            "version": getattr(test, "version", None) if test else None,
            "detected_commit_hash": detected_commit_hash,
        })
        return Response(out.data, status=status.HTTP_200_OK)


class PipelineImportAPI(AuthorizedQuerySetMixin, APIView):

    """Persist an already-validated report upload and fabricate a pipeline from it (async)."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_pipeline_import"
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_Edit,
    )

    @extend_schema(
        request=PipelineImportRequestSerializer,
        responses={
            202: OpenApiResponse(PipelineImportResponseSerializer, description="Import accepted, running async"),
            400: OpenApiResponse(description="Invalid report, upload, commit_hash, or target project"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Import a report",
        description=(
            "Uploads a report for a registered scan_type and fabricates a FINISHED "
            "AISTPipeline from it, as if a normal run had produced it. Runs "
            "asynchronously; poll the returned pipeline_id for status."
        ),
    )
    def post(self, request, *args, **kwargs) -> Response:
        serializer = PipelineImportRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        project = serializer.validated_data["project"]

        scan_type = serializer.validated_data["scan_type"]
        commit_hash = serializer.validated_data["commit_hash"]
        try:
            resolve_import_version(project, commit_hash)
        except ReportImportError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        uploaded_file = serializer.validated_data["file"]
        storage_name, sha256 = store_uploaded_report(uploaded_file)

        task_id = uuid4().hex
        with transaction.atomic():
            pipeline = create_pipeline_object(project, None, None)
            pipeline.run_task_id = task_id
            pipeline.save(update_fields=["run_task_id"])

        try:
            import_report.apply_async(
                args=(
                    pipeline.id,
                    storage_name,
                    project.id,
                    request.user.id,
                    scan_type,
                    commit_hash,
                    uploaded_file.name,
                    sha256,
                ),
                task_id=task_id,
            )
        except Exception:
            discard_uploaded_report(storage_name)
            pipeline.delete()
            raise

        out = PipelineImportResponseSerializer({"pipeline_id": pipeline.id, "run_task_id": task_id})
        return Response(out.data, status=status.HTTP_202_ACCEPTED)
