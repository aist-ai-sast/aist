"""Validate and asynchronously import reports handled by registered parsers."""
from __future__ import annotations

from collections import Counter
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from dojo.tools import factory
from dojo.utils import is_scan_file_too_large
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy, queryset_for_action
from aist.integrations.dast_report import (
    DastReportValidationError,
    validate_exported_dast_report_bytes,
)
from aist.models import AISTProject, DastProjectBinding, PipelineExecutionType
from aist.parser_overrides import DAST_SCAN_TYPE
from aist.services.dast_run_metadata import reported_dast_run_preview
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
    project_action: Action

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["project_id"].queryset = queryset_for_action(
                resource=AISTProject,
                action=self.project_action,
                user=request.user,
            )
        return fields


class _DastBindingFieldMixin:
    project_action: Action

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["binding_id"].queryset = queryset_for_action(
                resource=DastProjectBinding,
                action=self.project_action,
                user=request.user,
            )
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        is_dast = attrs.get("scan_type") == DAST_SCAN_TYPE
        binding = attrs.get("binding")
        project = attrs.get("project")
        commit_hash = attrs.get("commit_hash")
        if is_dast:
            if binding is None:
                raise serializers.ValidationError({"binding_id": "An explicit DAST project binding is required."})
            if binding.project_id != project.pk:
                raise serializers.ValidationError({"binding_id": "DAST binding must belong to the selected project."})
            if not binding.enabled:
                raise serializers.ValidationError({"binding_id": "DAST binding must be enabled."})
            # Only a target that declares a repository trigger needs one: its result is pinned to a
            # commit, which has to come from a repository linked to this project. A target with no
            # such requirement reports no source revision at all and its findings attach to the
            # version standing for the target itself, so demanding a repository would lock every
            # perimeter import out of a project that legitimately has none.
            if binding.requires_source_repository and project.repository_id is None:
                raise serializers.ValidationError(
                    {"project_id": "This DAST target is source-bound, so the project needs a linked repository."},
                )
            if commit_hash:
                raise serializers.ValidationError({"commit_hash": "DAST source commits come only from the report."})
        else:
            if binding is not None:
                raise serializers.ValidationError({"binding_id": "Bindings are valid only for DAST reports."})
            if "commit_hash" in self.fields and not commit_hash:
                raise serializers.ValidationError({"commit_hash": "This field is required."})
        return attrs


def _validate_file_size(value) -> None:
    if value.size > settings.PIPELINE_IMPORT_MAX_SIZE_BYTES or is_scan_file_too_large(value):
        msg = f"File exceeds the maximum allowed size of {settings.PIPELINE_IMPORT_MAX_SIZE_BYTES} bytes."
        raise serializers.ValidationError(msg)


class PipelineImportValidateRequestSerializer(
    _DastBindingFieldMixin,
    _ProjectFieldMixin,
    _ScanTypeFieldMixin,
    serializers.Serializer,
):
    project_action = Action.PRODUCT_READ

    file = serializers.FileField()
    project_id = serializers.PrimaryKeyRelatedField(
        source="project",
        queryset=AISTProject.objects.none(),
    )
    binding_id = serializers.PrimaryKeyRelatedField(
        source="binding",
        queryset=DastProjectBinding.objects.none(),
        required=False,
        allow_null=True,
    )

    def validate_file(self, value):
        _validate_file_size(value)
        return value


class PipelineImportPreviewSerializer(serializers.Serializer):
    findings_count = serializers.IntegerField()
    severity_breakdown = serializers.DictField(child=serializers.IntegerField())
    name = serializers.CharField(allow_null=True)
    version = serializers.CharField(allow_null=True)
    actual_source_commit = serializers.CharField(allow_null=True)
    # Coverage and token usage the DAST report carries, so the operator sees what the run
    # covered before committing the import. Null for every non-DAST scan type.
    dast_run = serializers.JSONField(allow_null=True, required=False)


class PipelineImportRequestSerializer(
    _DastBindingFieldMixin,
    _ProjectFieldMixin,
    _ScanTypeFieldMixin,
    serializers.Serializer,
):
    project_action = Action.PROJECT_OPERATE

    file = serializers.FileField()
    project_id = serializers.PrimaryKeyRelatedField(
        source="project",
        queryset=AISTProject.objects.none(),
    )
    binding_id = serializers.PrimaryKeyRelatedField(
        source="binding",
        queryset=DastProjectBinding.objects.none(),
        required=False,
        allow_null=True,
    )
    commit_hash = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
    )  # matches AISTProjectVersion.version's own max_length

    def validate_file(self, value):
        _validate_file_size(value)
        return value


class PipelineImportResponseSerializer(serializers.Serializer):
    pipeline_id = serializers.CharField()
    run_task_id = serializers.CharField()


def _validated_manual_dast_report(uploaded_file, binding: DastProjectBinding):
    uploaded_file.seek(0)
    raw = uploaded_file.read(settings.PIPELINE_IMPORT_MAX_SIZE_BYTES + 1)
    uploaded_file.seek(0)
    if len(raw) > settings.PIPELINE_IMPORT_MAX_SIZE_BYTES:
        msg = "DAST report exceeds its size limit."
        raise DastReportValidationError(msg)
    report = validate_exported_dast_report_bytes(
        raw,
        target_id=binding.target.provider_id,
        allowed_repository_keys=frozenset(binding.target.repository_keys),
        maximum_report_bytes=settings.PIPELINE_IMPORT_MAX_SIZE_BYTES,
    )
    if binding.requires_source_repository and report.source_commit_for(binding.source_repo_key) is None:
        msg = (
            f"This report carries no source revision for '{binding.source_repo_key}', which is the "
            f"repository this binding is bound to."
        )
        raise DastReportValidationError(msg)
    return report


class PipelineImportValidateAPI(AISTAPIView):

    """Parse an upload and return its preview."""

    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_pipeline_import"
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PRODUCT_READ)
    token_read_only = True

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

        scan_type = serializer.validated_data["scan_type"]
        uploaded_file = serializer.validated_data["file"]
        validated_report = None
        try:
            binding = serializer.validated_data.get("binding")
            if scan_type == DAST_SCAN_TYPE:
                validated_report = _validated_manual_dast_report(uploaded_file, binding)
                parser_input = validated_report.open_report()
                actual_source_commit = validated_report.source_commit_for(binding.source_repo_key)
            else:
                parser_input = uploaded_file
                parser_input.seek(0)
                actual_source_commit = None
            parser = factory.get_parser(scan_type)
            tests = parser.get_tests(scan_type, parser_input)
            test = tests[0] if tests else None
            findings = test.findings if test else []
        except (DastReportValidationError, TypeError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        out = PipelineImportPreviewSerializer({
            "findings_count": len(findings),
            "severity_breakdown": dict(Counter(f.severity for f in findings)),
            "name": getattr(test, "name", None) if test else None,
            "version": getattr(test, "version", None) if test else None,
            "actual_source_commit": actual_source_commit,
            # Derived after validation, from the already-validated report: it cannot fail, so it
            # stays out of the try clause guarding the parse.
            "dast_run": None if validated_report is None else reported_dast_run_preview(validated_report.run_metadata),
        })
        return Response(out.data, status=status.HTTP_200_OK)


class PipelineImportAPI(AISTAPIView):

    """Persist an already-validated report upload and fabricate a pipeline from it (async)."""

    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "aist_pipeline_import"
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=PipelineImportRequestSerializer,
        responses={
            202: OpenApiResponse(PipelineImportResponseSerializer, description="Import accepted, running async"),
            400: OpenApiResponse(description="Invalid report, upload, source metadata, or target project"),
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
        uploaded_file = serializer.validated_data["file"]
        binding = serializer.validated_data.get("binding")
        commit_hash = serializer.validated_data.get("commit_hash", "")
        if scan_type == DAST_SCAN_TYPE:
            try:
                validated_report = _validated_manual_dast_report(uploaded_file, binding)
            except DastReportValidationError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        else:
            try:
                resolve_import_version(project, commit_hash)
            except ReportImportError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        storage_name, sha256 = store_uploaded_report(uploaded_file)

        task_id = uuid4().hex
        with transaction.atomic():
            pipeline = create_pipeline_object(
                project,
                None,
                None,
                execution_type=PipelineExecutionType.MANUAL_IMPORT,
            )
            pipeline.run_task_id = task_id
            if scan_type == DAST_SCAN_TYPE:
                pipeline.launch_data = {
                    "source": "manual_import",
                    "scan_type": scan_type,
                    "uploader_id": request.user.id,
                    "filename": uploaded_file.name,
                    "sha256": sha256,
                    "dast_binding_id": binding.pk,
                    "provider_run_id": validated_report.run_id,
                    "provider_correlation_id": validated_report.correlation_id,
                }
            pipeline.save(update_fields=["run_task_id", "launch_data"])

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
                    binding.pk if binding is not None else None,
                ),
                task_id=task_id,
            )
        except Exception:
            discard_uploaded_report(storage_name)
            pipeline.delete()
            raise

        out = PipelineImportResponseSerializer({"pipeline_id": pipeline.id, "run_task_id": task_id})
        return Response(out.data, status=status.HTTP_202_ACCEPTED)
