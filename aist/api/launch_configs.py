from __future__ import annotations

from django.db import transaction
from django_filters import rest_framework as django_filters
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from aist.api.launch_requests import (
    LaunchRequestResponseSerializer,
    launch_principal_token,
    launch_request_headers,
    launch_request_response,
)
from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.execution.enqueue import LaunchEnqueueError, LaunchPrincipal, enqueue_pipeline_launch
from aist.integrations.dast_config import DastBindingParameters, DastConfigError
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.models import (
    AISTLaunchConfigAction,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    AISTStatus,
    DastProjectBinding,
    PipelineExecutionType,
)
from aist.pipeline_args import PipelineArguments
from aist.queries import get_authorized_aist_project_versions


class LaunchConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISTProjectLaunchConfig
        fields = [
            "id",
            "project",
            "execution_type",
            "dast_binding",
            "name",
            "description",
            "params",
            "is_default",
            "created",
            "updated",
        ]
        read_only_fields = ["id", "project", "created", "updated"]


class LaunchConfigCreateRequestSerializer(serializers.Serializer):
    name = serializers.CharField(required=True, max_length=128)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_default = serializers.BooleanField(required=False, default=False)
    params = serializers.JSONField(required=True)
    execution_type = serializers.ChoiceField(
        choices=(PipelineExecutionType.SAST, PipelineExecutionType.DAST),
        default=PipelineExecutionType.SAST,
    )
    dast_binding_id = serializers.PrimaryKeyRelatedField(
        source="dast_binding",
        queryset=DastProjectBinding.objects.none(),
        required=False,
        allow_null=True,
        write_only=True,
    )

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "Unknown field."))
        return super().to_internal_value(data)

    def get_fields(self):
        fields = super().get_fields()
        project = self.context.get("project")
        if project is not None:
            fields["dast_binding_id"].queryset = project.dast_bindings.select_related("target")
        return fields

    def validate(self, attrs):
        execution_type = attrs["execution_type"]
        binding = attrs.get("dast_binding")
        if execution_type == PipelineExecutionType.SAST:
            if binding is not None:
                raise serializers.ValidationError({"dast_binding_id": "SAST launch config cannot select a DAST binding."})
            return attrs
        if binding is None:
            raise serializers.ValidationError({"dast_binding_id": "DAST launch config requires an explicit binding."})
        if not binding.enabled:
            raise serializers.ValidationError({"dast_binding_id": "DAST binding must be enabled."})
        try:
            attrs["params"] = DastBindingParameters.from_snapshot(
                attrs["params"],
                target=binding.target.get_snapshot(),
            ).to_snapshot()
        except DastConfigError as exc:
            raise serializers.ValidationError({"params": str(exc)}) from exc
        return attrs


class LaunchConfigUpdateRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISTProjectLaunchConfig
        fields = ["name", "description", "is_default", "params"]

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "Unknown field."))
        return super().to_internal_value(data)

    def validate_params(self, value):
        if value is None:
            return value
        if not isinstance(value, dict):
            msg = "params must be a JSON object"
            raise serializers.ValidationError(msg)
        if self.instance is not None and self.instance.execution_type == PipelineExecutionType.DAST:
            try:
                return DastBindingParameters.from_snapshot(
                    value,
                    target=self.instance.dast_binding.target.get_snapshot(),
                ).to_snapshot()
            except DastConfigError as exc:
                raise serializers.ValidationError(str(exc)) from exc
        return value


class LaunchConfigStartRequestSerializer(serializers.Serializer):

    """All runtime options must live in `params` (PipelineArguments-like dict)."""

    params = serializers.JSONField(required=False, default=dict)
    project_version_id = serializers.PrimaryKeyRelatedField(
        queryset=AISTProjectVersion.objects.none(),
        required=False,
        write_only=True,
    )

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "Unknown field."))
        return super().to_internal_value(data)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        project = self.context.get("project")
        if request is not None and project is not None:
            fields["project_version_id"].queryset = get_authorized_aist_project_versions(
                Permissions.Product_Edit,
                user=request.user,
            ).filter(project=project)
        return fields

    def validate_params(self, value):
        if not isinstance(value, dict):
            msg = "params must be a JSON object"
            raise serializers.ValidationError(msg)
        return value

    def validate(self, attrs):
        config = self.context.get("config")
        if (
            config is not None
            and config.execution_type == PipelineExecutionType.DAST
            and attrs.get("project_version_id") is None
        ):
            raise serializers.ValidationError(
                {"project_version_id": "DAST launches require a source project version."},
            )
        return attrs


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


class LaunchConfigActionUpdateSerializer(serializers.Serializer):
    trigger_status = serializers.ChoiceField(choices=AISTStatus.choices, required=False)
    action_type = serializers.ChoiceField(choices=AISTLaunchConfigAction.ActionType.choices, required=False)
    config = serializers.JSONField(required=False)

    def validate_config(self, value):
        if value is None:
            return value
        if not isinstance(value, dict):
            msg = "config must be a JSON object"
            raise serializers.ValidationError(msg)
        return value


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
            "execution_type",
            "dast_binding",
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

    def validate_config(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            msg = "config must be a JSON object"
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
        include_ai_csv = bool(config.get("include_ai_csv"))
        include_common_summary = bool(config.get("include_common_summary"))
        if include_ai_csv and include_common_summary:
            raise serializers.ValidationError(
                {"config": {"include_common_summary": "Choose either AI summary or common summary, not both."}},
            )

        attrs["config"] = {
            "channels": channels,
            "title": title,
            "description": description,
            "include_ai_csv": include_ai_csv,
            "include_common_summary": include_common_summary,
        }

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
        include_ai_csv = bool(config.get("include_ai_csv"))
        include_common_summary = bool(config.get("include_common_summary"))
        if include_ai_csv and include_common_summary:
            raise serializers.ValidationError(
                {"config": {"include_common_summary": "Choose either AI summary or common summary, not both."}},
            )

        attrs["config"] = {
            "emails": emails,
            "title": title,
            "description": description,
            "include_ai_csv": include_ai_csv,
            "include_common_summary": include_common_summary,
        }
        return attrs


class WriteLogActionCreateSerializer(BaseActionCreateSerializer):
    def validate(self, attrs):
        if attrs.get("action_type") != AISTLaunchConfigAction.ActionType.WRITE_LOG:
            raise serializers.ValidationError({"action_type": "action_type must be WRITE_LOG"})

        config = attrs.get("config") or {}
        level = config.get("level") or "INFO"
        description = config.get("description") or ""
        include_ai_csv = bool(config.get("include_ai_csv"))

        attrs["config"] = {
            "level": level,
            "description": description,
            "include_ai_csv": include_ai_csv,
        }
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
    execution_type: str = PipelineExecutionType.SAST,
    dast_binding: DastProjectBinding | None = None,
) -> AISTProjectLaunchConfig:
    """
    Shared create logic for BOTH API and UI.
    SSOT for params validation/defaulting: PipelineArguments.normalize_params.
    """
    if execution_type == PipelineExecutionType.SAST:
        normalized = PipelineArguments.normalize_params(project=project, raw_params=raw_params)
    elif execution_type == PipelineExecutionType.DAST and dast_binding is not None:
        try:
            normalized = DastBindingParameters.from_snapshot(
                raw_params,
                target=dast_binding.target.get_snapshot(),
            ).to_snapshot()
        except DastConfigError as exc:
            raise serializers.ValidationError({"params": str(exc)}) from exc
    else:
        raise serializers.ValidationError({"dast_binding_id": "DAST launch config requires an explicit binding."})

    with transaction.atomic():
        if is_default:
            AISTProjectLaunchConfig.objects.filter(project=project, is_default=True).update(is_default=False)

        launch_config = AISTProjectLaunchConfig(
            project=project,
            execution_type=execution_type,
            dast_binding=dast_binding,
            name=name,
            description=description or "",
            params=normalized,
            is_default=is_default,
        )
        launch_config.full_clean()
        launch_config.save()
        return launch_config


class ProjectLaunchConfigListCreateAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="List launch configs for project",
        responses={200: LaunchConfigSerializer(many=True)},
    )
    def get(self, request, project_id: int, *args, **kwargs):
        project = self.resolve(id=project_id)
        qs = AISTProjectLaunchConfig.objects.filter(project=project).order_by("-updated")
        return Response(LaunchConfigSerializer(qs, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
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
        project = self.resolve(id=project_id)

        s = LaunchConfigCreateRequestSerializer(data=request.data, context={"project": project})
        s.is_valid(raise_exception=True)

        obj = create_launch_config_for_project(
            project=project,
            name=s.validated_data["name"],
            description=s.validated_data.get("description", ""),
            is_default=bool(s.validated_data.get("is_default", False)),
            raw_params=s.validated_data["params"],
            execution_type=s.validated_data["execution_type"],
            dast_binding=s.validated_data.get("dast_binding"),
        )

        return Response(LaunchConfigSerializer(obj).data, status=status.HTTP_201_CREATED)


class ProjectLaunchConfigDetailAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProjectLaunchConfig, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Get launch config",
        responses={200: LaunchConfigSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, project_id: int, config_id: int, *args, **kwargs):
        obj = self.resolve(id=config_id, project_id=project_id)
        return Response(LaunchConfigSerializer(obj).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Delete launch config",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, project_id: int, config_id: int, *args, **kwargs):
        obj = self.resolve(id=config_id, project_id=project_id)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Update launch config",
        request=LaunchConfigUpdateRequestSerializer,
        responses={200: LaunchConfigSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def patch(self, request, project_id: int, config_id: int, *args, **kwargs):
        obj = self.resolve(id=config_id, project_id=project_id)
        s = LaunchConfigUpdateRequestSerializer(instance=obj, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        with transaction.atomic():
            if data.get("is_default"):
                AISTProjectLaunchConfig.objects.filter(project=obj.project, is_default=True).exclude(id=obj.id).update(
                    is_default=False,
                )
                obj.is_default = True
            elif "is_default" in data:
                obj.is_default = False

            if "name" in data:
                obj.name = data["name"]
            if "description" in data:
                obj.description = data["description"] or ""
            if "params" in data:
                normalized = data["params"]
                if obj.execution_type == PipelineExecutionType.SAST:
                    normalized = PipelineArguments.normalize_params(project=obj.project, raw_params=normalized)
                obj.params = normalized

            obj.full_clean()
            obj.save()

        return Response(LaunchConfigSerializer(obj).data)


class ProjectLaunchConfigStartAPI(AISTAPIView):
    authz = ResourcePolicy(
        resource=AISTProjectLaunchConfig,
        read=Action.PRODUCT_READ,
        write=Action.PROJECT_OPERATE,
    )

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Queue pipeline launch using launch config",
        request=LaunchConfigStartRequestSerializer,
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=False,
            ),
        ],
        responses={
            202: OpenApiResponse(LaunchRequestResponseSerializer, description="Launch request queued"),
            404: OpenApiResponse(description="Not found"),
            400: OpenApiResponse(description="Bad request"),
        },
        examples=[
            OpenApiExample(
                "Start using saved config only (no overrides)",
                description=(
                        "Queues a launch using the saved config params as-is. "
                        "Body may be empty or `{}`; `params` defaults to `{}`."
                ),
                value={},
                request_only=True,
            ),
            OpenApiExample(
                "Queue with params overrides",
                description=(
                        "Provide partial PipelineArguments-like fields inside `params` to override saved config. "
                        "All validation/defaulting happens before the durable request is stored."
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
                "Queue on latest project version (no explicit project_version)",
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
        cfg = self.resolve(id=config_id, project_id=project_id)
        project = cfg.project
        if cfg.execution_type == PipelineExecutionType.DAST:
            readiness = check_dast_binding_readiness(cfg.dast_binding)
            if not readiness.ready:
                return Response(
                    {"readiness": readiness.to_snapshot()},
                    status=status.HTTP_409_CONFLICT,
                )
        elif cfg.execution_type != PipelineExecutionType.SAST:
            return Response(
                {"execution_type": "Unsupported pipeline execution type."},
                status=status.HTTP_409_CONFLICT,
            )

        s = LaunchConfigStartRequestSerializer(
            data=request.data or {},
            context={"request": request, "project": project, "config": cfg},
        )
        s.is_valid(raise_exception=True)

        req_params = s.validated_data.get("params") or {}
        project_version = s.validated_data.get("project_version_id")
        if project_version is not None and cfg.execution_type == PipelineExecutionType.SAST:
            req_params = {**req_params, "project_version": project_version.as_dict()}

        organization = project.organization
        if organization is None:
            return Response(
                {"organization": "Project does not belong to an AIST organization."},
                status=status.HTTP_409_CONFLICT,
            )
        principal = LaunchPrincipal.for_user(
            organization=organization,
            requester=request.user,
            api_token=launch_principal_token(request),
        )
        try:
            launch_request = enqueue_pipeline_launch(
                project=project,
                principal=principal,
                raw_params=req_params,
                launch_config=cfg,
                trigger_project_version=(
                    project_version
                    if cfg.execution_type == PipelineExecutionType.DAST
                    else None
                ),
                client_request_key=launch_request_headers(request).get("client_request_key"),
            ).request
        except LaunchEnqueueError as exc:
            raise serializers.ValidationError({"detail": str(exc)}) from exc
        return Response(launch_request_response(launch_request), status=status.HTTP_202_ACCEPTED)


class ProjectLaunchConfigActionListCreateAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProjectLaunchConfig, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="List actions for launch config",
        responses={200: LaunchConfigActionSerializer(many=True)},
    )
    def get(self, request, project_id: int, config_id: int, *args, **kwargs):
        cfg = self.resolve(id=config_id, project_id=project_id)
        qs = AISTLaunchConfigAction.objects.filter(launch_config=cfg).order_by("-updated")
        return Response(LaunchConfigActionSerializer(qs, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Create action for launch config",
        request=BaseActionCreateSerializer,
        responses={201: LaunchConfigActionSerializer},
    )
    def post(self, request, project_id: int, config_id: int, *args, **kwargs):
        cfg = self.resolve(id=config_id, project_id=project_id)
        action_type_serializer = BaseActionCreateSerializer(data=request.data)
        action_type_serializer.is_valid(raise_exception=True)
        action_type = action_type_serializer.validated_data["action_type"]
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
        obj.save()
        return Response(LaunchConfigActionSerializer(obj).data, status=status.HTTP_201_CREATED)


class ProjectLaunchConfigActionDetailAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTLaunchConfigAction, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Get action for launch config",
        responses={200: LaunchConfigActionSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, project_id: int, config_id: int, action_id: int, *args, **kwargs):
        obj = self.resolve(
            id=action_id,
            launch_config_id=config_id,
            launch_config__project_id=project_id,
        )
        return Response(LaunchConfigActionSerializer(obj).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Update action for launch config",
        request=LaunchConfigActionUpdateSerializer,
        responses={200: LaunchConfigActionSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def patch(self, request, project_id: int, config_id: int, action_id: int, *args, **kwargs):
        obj = self.resolve(
            id=action_id,
            launch_config_id=config_id,
            launch_config__project_id=project_id,
        )
        s = LaunchConfigActionUpdateSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        data = s.validated_data

        action_type = data.get("action_type") or obj.action_type
        serializer_cls = ACTION_CREATE_SERIALIZERS.get(action_type)
        if serializer_cls is None:
            return Response({"action_type": "Unsupported action_type"}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "trigger_status": data.get("trigger_status", obj.trigger_status),
            "action_type": action_type,
            "config": data.get("config", obj.config),
        }
        validator = serializer_cls(data=payload)
        validator.is_valid(raise_exception=True)

        obj.trigger_status = validator.validated_data["trigger_status"]
        obj.action_type = validator.validated_data["action_type"]
        obj.config = validator.validated_data.get("config") or {}
        obj.save()

        return Response(LaunchConfigActionSerializer(obj).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Delete action for launch config",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, project_id: int, config_id: int, action_id: int, *args, **kwargs):
        obj = self.resolve(
            id=action_id,
            launch_config_id=config_id,
            launch_config__project_id=project_id,
        )
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LaunchConfigDashboardListAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProjectLaunchConfig, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    class FilterSet(django_filters.FilterSet):
        organization_id = django_filters.NumberFilter(
            field_name="project__product__prod_type__aist_organization_id",
        )
        project_id = django_filters.NumberFilter(field_name="project_id")
        is_default = django_filters.BooleanFilter(field_name="is_default")

        class Meta:
            model = AISTProjectLaunchConfig
            fields = ("organization_id", "project_id", "is_default")

    @extend_schema(
        responses={200: LaunchConfigDashboardSerializer(many=True)},
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
    )
    def get(self, request):
        filterset = self.FilterSet(
            data=request.query_params,
            queryset=(
                self.authorized_queryset()
                .select_related("project__product__prod_type__aist_organization")
                .prefetch_related("actions")
                .order_by("-updated")
            ),
            request=request,
        )
        if not filterset.is_valid():
            return Response(filterset.errors, status=status.HTTP_400_BAD_REQUEST)
        qs = filterset.qs

        return Response(LaunchConfigDashboardSerializer(qs, many=True).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_CONFIGS.value],
        summary="Delete action for launch config",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, project_id: int, config_id: int, action_id: int, *args, **kwargs):
        obj = self.resolve(
            resource=AISTLaunchConfigAction,
            id=action_id,
            launch_config_id=config_id,
            launch_config__project_id=project_id,
        )
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
