from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from dojo.aist.api.pipelines import PipelineResponseSerializer
from dojo.aist.models import (
    AISTLaunchConfigAction,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    AISTStatus,
)
from dojo.aist.pipeline_args import PipelineArguments
from dojo.aist.tasks import run_sast_pipeline
from dojo.aist.utils.pipeline import create_pipeline_object, has_unfinished_pipeline


class LaunchConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISTProjectLaunchConfig
        fields = ["id", "project", "name", "description", "params", "is_default", "created", "updated"]
        read_only_fields = ["id", "project", "created", "updated"]


class LaunchConfigCreateRequestSerializer(serializers.Serializer):
    name = serializers.CharField(required=True, max_length=128)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_default = serializers.BooleanField(required=False, default=False)
    params = serializers.JSONField(required=True)


class LaunchConfigStartRequestSerializer(serializers.Serializer):

    """All runtime options must live in `params` (PipelineArguments-like dict)."""

    params = serializers.JSONField(required=False, default=dict)

    def validate_params(self, value):
        if not isinstance(value, dict):
            msg = "params must be a JSON object"
            raise serializers.ValidationError(msg)
        return value


class LaunchConfigActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISTLaunchConfigAction
        fields = [
            "id",
            "launch_config",
            "trigger_status",
            "action_type",
            "config",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "launch_config", "created", "updated"]


class LaunchConfigDashboardSerializer(serializers.ModelSerializer):
    actions = LaunchConfigActionSerializer(many=True, read_only=True)
    project_name = serializers.CharField(source="project.product.name", read_only=True)
    product_name = serializers.CharField(source="project.product.name", read_only=True)
    organization_id = serializers.IntegerField(source="project.organization_id", read_only=True)
    organization_name = serializers.CharField(source="project.organization.name", read_only=True)

    class Meta:
        model = AISTProjectLaunchConfig
        fields = [
            "id",
            "project",
            "project_name",
            "product_name",
            "organization_id",
            "organization_name",
            "name",
            "description",
            "params",
            "is_default",
            "created",
            "updated",
            "actions",
        ]
        read_only_fields = fields


class BaseActionCreateSerializer(serializers.Serializer):
    trigger_status = serializers.ChoiceField(choices=AISTStatus.choices)
    action_type = serializers.ChoiceField(choices=AISTLaunchConfigAction.ActionType.choices)
    config = serializers.JSONField(required=False, default=dict)
    secret_config = serializers.JSONField(required=False, default=dict, write_only=True)

    def validate_config(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            msg = "config must be a JSON object"
            raise serializers.ValidationError(msg)
        return value

    def validate_secret_config(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            msg = "secret_config must be a JSON object"
            raise serializers.ValidationError(msg)
        return value


class SlackActionCreateSerializer(BaseActionCreateSerializer):
    def validate(self, attrs):
        if attrs.get("action_type") != AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK:
            raise serializers.ValidationError({"action_type": "action_type must be PUSH_TO_SLACK"})

        config = attrs.get("config") or {}
        channels = config.get("channels") or []
        if isinstance(channels, str):
            channels = [channels]
        if not channels:
            raise serializers.ValidationError({"config": {"channels": "channels is required"}})

        title = config.get("title") or ""
        description = config.get("description") or ""

        attrs["config"] = {
            "channels": channels,
            "title": title,
            "description": description,
        }

        secret_config = attrs.get("secret_config") or {}
        slack_token = secret_config.get("slack_token") or ""
        attrs["secret_config"] = {"slack_token": slack_token} if slack_token else {}
        return attrs


class EmailActionCreateSerializer(BaseActionCreateSerializer):
    def validate(self, attrs):
        if attrs.get("action_type") != AISTLaunchConfigAction.ActionType.SEND_EMAIL:
            raise serializers.ValidationError({"action_type": "action_type must be SEND_EMAIL"})

        config = attrs.get("config") or {}
        emails = config.get("emails") or []
        if isinstance(emails, str):
            emails = [emails]
        if not emails:
            raise serializers.ValidationError({"config": {"emails": "emails is required"}})

        title = config.get("title") or ""
        description = config.get("description") or ""

        attrs["config"] = {
            "emails": emails,
            "title": title,
            "description": description,
        }
        attrs["secret_config"] = {}
        return attrs


class WriteLogActionCreateSerializer(BaseActionCreateSerializer):
    def validate(self, attrs):
        if attrs.get("action_type") != AISTLaunchConfigAction.ActionType.WRITE_LOG:
            raise serializers.ValidationError({"action_type": "action_type must be WRITE_LOG"})

        config = attrs.get("config") or {}
        level = config.get("level") or "INFO"
        description = config.get("description") or ""

        attrs["config"] = {
            "level": level,
            "description": description,
        }
        attrs["secret_config"] = {}
        return attrs


ACTION_CREATE_SERIALIZERS = {
    AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK: SlackActionCreateSerializer,
    AISTLaunchConfigAction.ActionType.SEND_EMAIL: EmailActionCreateSerializer,
    AISTLaunchConfigAction.ActionType.WRITE_LOG: WriteLogActionCreateSerializer,
}


def create_launch_config_for_project(
    *,
    project: AISTProject,
    name: str,
    description: str,
    is_default: bool,
    raw_params: dict,
) -> AISTProjectLaunchConfig:
    """
    Shared create logic for BOTH API and UI.
    SSOT for params validation/defaulting: PipelineArguments.normalize_params.
    """
    normalized = PipelineArguments.normalize_params(project=project, raw_params=raw_params)

    with transaction.atomic():
        if is_default:
            AISTProjectLaunchConfig.objects.filter(project=project, is_default=True).update(is_default=False)

        return AISTProjectLaunchConfig.objects.create(
            project=project,
            name=name,
            description=description or "",
            params=normalized,
            is_default=is_default,
        )


class ProjectLaunchConfigListCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List launch configs for project",
        responses={200: LaunchConfigSerializer(many=True)},
    )
    def get(self, request, project_id: int, *args, **kwargs):
        project = get_object_or_404(AISTProject, id=project_id)
        qs = AISTProjectLaunchConfig.objects.filter(project=project).order_by("-updated")
        return Response(LaunchConfigSerializer(qs, many=True).data)

    @extend_schema(
        tags=["aist"],
        summary="Create launch config for project",
        request=LaunchConfigCreateRequestSerializer,
        responses={201: LaunchConfigSerializer},
        examples=[
            OpenApiExample(
                "Create preset (AUTO_DEFAULT, pin to project version id)",
                description=(
                        "Creates a reusable launch configuration. "
                        "All pipeline options live in `params` (validated by PipelineArguments.normalize_params). "
                        "`project_version` can be an integer (AISTProjectVersion id) or an object."
                ),
                value={
                    "name": "Nightly AUTO_DEFAULT (main)",
                    "description": "Use default AI filter + run on main",
                    "is_default": True,
                    "params": {
                        "project_version": 123,
                        "ai_mode": "AUTO_DEFAULT",
                        "ai_filter_snapshot": {"limit": 50, "severity": [{"comparison": "EQUALS", "value": "HIGH"}]},
                        "analyzers": ["semgrep", "trivy"],
                        "selected_languages": ["python", "cpp"],
                        "log_level": "INFO",
                        "rebuild_images": False,
                        "time_class_level": "slow",
                        "env": {"SOME_FLAG": "1"},
                    },
                },
                request_only=True,
            ),
            OpenApiExample(
                "Create preset (MANUAL, no AI snapshot)",
                description="In MANUAL mode any provided ai_filter_snapshot is ignored/normalized to null.",
                value={
                    "name": "Manual run (no auto push AI)",
                    "description": "",
                    "is_default": False,
                    "params": {
                        "project_version": {"id": 123},
                        "ai_mode": "MANUAL",
                        "analyzers": ["semgrep", "snyk"],
                        "selected_languages": [],
                        "log_level": "INFO",
                        "rebuild_images": False,
                    },
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request, project_id: int, *args, **kwargs):
        project = get_object_or_404(AISTProject, id=project_id)

        s = LaunchConfigCreateRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        obj = create_launch_config_for_project(
            project=project,
            name=s.validated_data["name"],
            description=s.validated_data.get("description", ""),
            is_default=bool(s.validated_data.get("is_default", False)),
            raw_params=s.validated_data["params"],
        )

        return Response(LaunchConfigSerializer(obj).data, status=status.HTTP_201_CREATED)


class ProjectLaunchConfigDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Get launch config",
        responses={200: LaunchConfigSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, project_id: int, config_id: int, *args, **kwargs):
        obj = get_object_or_404(AISTProjectLaunchConfig, id=config_id, project_id=project_id)
        return Response(LaunchConfigSerializer(obj).data)

    @extend_schema(
        tags=["aist"],
        summary="Delete launch config",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, project_id: int, config_id: int, *args, **kwargs):
        obj = get_object_or_404(AISTProjectLaunchConfig, id=config_id, project_id=project_id)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectLaunchConfigStartAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Start pipeline using launch config",
        request=LaunchConfigStartRequestSerializer,
        responses={
            201: OpenApiResponse(PipelineResponseSerializer, description="Pipeline created"),
            404: OpenApiResponse(description="Not found"),
            405: OpenApiResponse(description="There is already a running pipeline for this project version"),
            400: OpenApiResponse(description="Bad request"),
        },
        examples=[
            OpenApiExample(
                "Start using saved config only (no overrides)",
                description=(
                        "Starts pipeline using launch config params as-is. "
                        "Body may be empty or `{}`; `params` defaults to `{}`."
                ),
                value={},
                request_only=True,
            ),
            OpenApiExample(
                "Start with params overrides",
                description=(
                        "Provide partial PipelineArguments-like fields inside `params` to override saved config. "
                        "All validation/defaulting happens in PipelineArguments.normalize_params."
                ),
                value={
                    "params": {
                        "project_version": 123,
                        "ai_mode": "AUTO_DEFAULT",
                        "ai_filter_snapshot": {
                            "limit": 50,
                            "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                        },
                        "analyzers": ["semgrep", "snyk"],
                        "selected_languages": ["python", "cpp"],
                        "log_level": "INFO",
                        "rebuild_images": False,
                    },
                },
                request_only=True,
            ),
            OpenApiExample(
                "Start on latest project version (no explicit project_version)",
                description=(
                        "If `project_version` is omitted, normalize_params should pick the latest available version "
                ),
                value={
                    "params": {
                        "ai_mode": "AUTO_DEFAULT",
                        "analyzers": [],
                        "selected_languages": [],
                        "log_level": "INFO",
                        "rebuild_images": False,
                    },
                },
                request_only=True,
            ),
        ],
    )
    def post(self, request, project_id: int, config_id: int, *args, **kwargs):
        project = get_object_or_404(AISTProject, id=project_id)
        cfg = get_object_or_404(AISTProjectLaunchConfig, id=config_id, project=project)

        s = LaunchConfigStartRequestSerializer(data=request.data or {})
        s.is_valid(raise_exception=True)

        req_params = s.validated_data.get("params") or {}
        if req_params and not isinstance(req_params, dict):
            return Response({"params": "params must be an object"}, status=status.HTTP_400_BAD_REQUEST)

        # Start with saved preset params and allow request to override any of them.
        # All fields must be PipelineArguments-like and validated in one place.
        raw = dict(cfg.params or {})
        raw.update(req_params)

        # Normalize + validate + fill defaults (including project_version) in ONE place
        params = PipelineArguments.normalize_params(project=project, raw_params=raw)

        # project_version must exist after normalization (may be {})
        pv_dict = params.get("project_version") or {}
        pv_id = pv_dict.get("id")
        if not pv_id:
            return Response({"project_version": "No versions found for project"}, status=status.HTTP_400_BAD_REQUEST)

        project_version = get_object_or_404(AISTProjectVersion, pk=pv_id, project=project)

        if has_unfinished_pipeline(project_version):
            return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

        with transaction.atomic():
            p = create_pipeline_object(project, project_version, None)

        params["launch_config_id"] = cfg.id
        async_result = run_sast_pipeline.delay(p.id, params)
        p.run_task_id = async_result.id
        p.save(update_fields=["run_task_id"])

        out = PipelineResponseSerializer(
            {
                "id": p.id,
                "status": p.status,
                "response_from_ai": p.response_from_ai,
                "created": p.created,
                "updated": p.updated,
            },
        )
        return Response(out.data, status=status.HTTP_201_CREATED)


class ProjectLaunchConfigActionListCreateAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List actions for launch config",
        responses={200: LaunchConfigActionSerializer(many=True)},
    )
    def get(self, request, project_id: int, config_id: int, *args, **kwargs):
        cfg = get_object_or_404(AISTProjectLaunchConfig, id=config_id, project_id=project_id)
        qs = AISTLaunchConfigAction.objects.filter(launch_config=cfg).order_by("-updated")
        return Response(LaunchConfigActionSerializer(qs, many=True).data)

    @extend_schema(
        tags=["aist"],
        summary="Create action for launch config",
        request=BaseActionCreateSerializer,
        responses={201: LaunchConfigActionSerializer},
    )
    def post(self, request, project_id: int, config_id: int, *args, **kwargs):
        cfg = get_object_or_404(AISTProjectLaunchConfig, id=config_id, project_id=project_id)
        action_type = (request.data or {}).get("action_type")
        serializer_cls = ACTION_CREATE_SERIALIZERS.get(action_type)
        if serializer_cls is None:
            return Response({"action_type": "Unsupported action_type"}, status=status.HTTP_400_BAD_REQUEST)
        s = serializer_cls(data=request.data)
        s.is_valid(raise_exception=True)

        obj = AISTLaunchConfigAction(
            launch_config=cfg,
            trigger_status=s.validated_data["trigger_status"],
            action_type=s.validated_data["action_type"],
            config=s.validated_data.get("config") or {},
        )
        obj.set_secret_config(s.validated_data.get("secret_config") or {})
        obj.save()
        return Response(LaunchConfigActionSerializer(obj).data, status=status.HTTP_201_CREATED)


class ProjectLaunchConfigActionDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Get action for launch config",
        responses={200: LaunchConfigActionSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, project_id: int, config_id: int, action_id: int, *args, **kwargs):
        obj = get_object_or_404(
            AISTLaunchConfigAction,
            id=action_id,
            launch_config_id=config_id,
            launch_config__project_id=project_id,
        )
        return Response(LaunchConfigActionSerializer(obj).data)


class LaunchConfigDashboardListAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: LaunchConfigDashboardSerializer(many=True)},
    )
    def get(self, request):
        qs = (
            AISTProjectLaunchConfig.objects.select_related("project__product", "project__organization")
            .prefetch_related("actions")
            .order_by("-updated")
        )

        org_id = request.query_params.get("organization_id")
        if org_id:
            qs = qs.filter(project__organization_id=org_id)

        project_id = request.query_params.get("project_id")
        if project_id:
            qs = qs.filter(project_id=project_id)

        is_default = request.query_params.get("is_default")
        if is_default in {"1", "true", "True"}:
            qs = qs.filter(is_default=True)
        elif is_default in {"0", "false", "False"}:
            qs = qs.filter(is_default=False)

        return Response(LaunchConfigDashboardSerializer(qs, many=True).data)

    @extend_schema(
        tags=["aist"],
        summary="Delete action for launch config",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, project_id: int, config_id: int, action_id: int, *args, **kwargs):
        obj = get_object_or_404(
            AISTLaunchConfigAction,
            id=action_id,
            launch_config_id=config_id,
            launch_config__project_id=project_id,
        )
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
