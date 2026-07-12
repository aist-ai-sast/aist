from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003

from croniter import croniter
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters import rest_framework as django_filters
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.models import AISTProject, AISTProjectLaunchConfig, LaunchSchedule, PipelineLaunchQueue
from aist.queries import (
    get_authorized_aist_launch_schedules,
    get_authorized_aist_projects,
)


@dataclass(frozen=True, slots=True)
class LaunchScheduleApiChoices:
    ordering: list[str]


LAUNCH_SCHEDULE_API_CHOICES = LaunchScheduleApiChoices(
    ordering=[
        "id",
        "-id",
        "enabled",
        "-enabled",
        "max_concurrent_per_worker",
        "-max_concurrent_per_worker",
        "next_tick",
        "-next_tick",
    ],
)


def _coerce_strict_bool(raw: str) -> bool:
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    msg = "invalid boolean value"
    raise ValueError(msg)


class LaunchScheduleSerializer(serializers.ModelSerializer):
    project_id = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    organization_id = serializers.SerializerMethodField()
    organization_name = serializers.SerializerMethodField()

    launch_config_id = serializers.SerializerMethodField()
    launch_config_name = serializers.SerializerMethodField()

    # SSOT: computed by backend using LaunchSchedule methods
    due_tick = serializers.SerializerMethodField()
    next_tick = serializers.SerializerMethodField()
    due_now = serializers.SerializerMethodField()

    # Human-readable strings (backend formatting, UI just renders)
    due_tick_human = serializers.SerializerMethodField()
    next_tick_human = serializers.SerializerMethodField()
    timezone = serializers.SerializerMethodField()
    server_now = serializers.SerializerMethodField()

    class Meta:
        model = LaunchSchedule
        fields = [
            "id",
            "cron_expression",
            "enabled",
            "max_concurrent_per_worker",
            "last_run_at",

            "project_id",
            "project_name",
            "organization_id",
            "organization_name",

            "launch_config_id",
            "launch_config_name",

            "due_tick",
            "next_tick",
            "due_now",

            "due_tick_human",
            "next_tick_human",
            "timezone",
            "server_now",
        ]

    def _get_project(self, obj):
        cfg = getattr(obj, "launch_config", None)
        return getattr(cfg, "project", None)

    @extend_schema_field(OpenApiTypes.INT)
    def get_project_id(self, obj) -> int | None:
        pr = self._get_project(obj)
        return getattr(pr, "id", None)

    @extend_schema_field(OpenApiTypes.STR)
    def get_project_name(self, obj) -> str | None:
        pr = self._get_project(obj)
        product = getattr(pr, "product", None)
        return getattr(product, "name", None) or (str(getattr(pr, "id", "")) if pr else None)

    @extend_schema_field(OpenApiTypes.INT)
    def get_organization_id(self, obj) -> int | None:
        pr = self._get_project(obj)
        org = getattr(pr, "organization", None)
        return getattr(org, "id", None)

    @extend_schema_field(OpenApiTypes.STR)
    def get_organization_name(self, obj) -> str | None:
        pr = self._get_project(obj)
        org = getattr(pr, "organization", None)
        return getattr(org, "name", None)

    @extend_schema_field(OpenApiTypes.INT)
    def get_launch_config_id(self, obj) -> int | None:
        return getattr(obj, "launch_config_id", None)

    @extend_schema_field(OpenApiTypes.STR)
    def get_launch_config_name(self, obj) -> str | None:
        cfg = getattr(obj, "launch_config", None)
        return getattr(cfg, "name", None)

    def _safe_due_next(self, obj):
        """
        SSOT: rely ONLY on LaunchSchedule model methods.
        - due_tick uses get_next_run_time(prev <= now) semantics :contentReference[oaicite:1]{index=1}
        - next_tick uses get_next_scheduled_time(strictly > now) :contentReference[oaicite:2]{index=2}
        """
        now = timezone.now()
        try:
            due = obj.get_next_run_time(now=now)
        except Exception:
            due = None
        try:
            nxt = obj.get_next_scheduled_time(now=now)
        except Exception:
            nxt = None
        return now, due, nxt

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_due_tick(self, obj) -> datetime | None:
        _, due, _ = self._safe_due_next(obj)
        return due

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_next_tick(self, obj) -> datetime | None:
        _, _, nxt = self._safe_due_next(obj)
        return nxt

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_due_now(self, obj) -> bool:
        now, due, _ = self._safe_due_next(obj)
        if not obj.enabled or not due:
            return False
        # "Due now" means within last 2 minutes (UI note uses this)
        return 0 <= (now - due).total_seconds() <= 120

    def _fmt_local(self, value) -> str | None:
        if not value:
            return None
        try:
            # show in server default timezone
            v = timezone.localtime(value)
            return v.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_due_tick_human(self, obj) -> str | None:
        _, due, _ = self._safe_due_next(obj)
        return self._fmt_local(due)

    @extend_schema_field(OpenApiTypes.STR)
    def get_next_tick_human(self, obj) -> str | None:
        _, _, nxt = self._safe_due_next(obj)
        return self._fmt_local(nxt)

    @extend_schema_field(OpenApiTypes.STR)
    def get_timezone(self, obj) -> str | None:
        try:
            return str(timezone.get_default_timezone())
        except Exception:
            return None

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_server_now(self, obj) -> datetime:
        return timezone.now()


class LaunchScheduleUpsertSerializer(serializers.ModelSerializer):
    # incoming field (so we don't require nested object)
    launch_config_id = serializers.PrimaryKeyRelatedField(
        source="launch_config",
        queryset=AISTProjectLaunchConfig.objects.none(),
        write_only=True,
    )

    class Meta:
        model = LaunchSchedule
        fields = [
            "cron_expression",
            "enabled",
            "max_concurrent_per_worker",
            "launch_config_id",
        ]

    def get_fields(self):
        fields = super().get_fields()
        project: AISTProject | None = self.context.get("project")
        if project is not None:
            fields["launch_config_id"].queryset = AISTProjectLaunchConfig.objects.filter(project=project)
        return fields

    def validate_cron_expression(self, v: str) -> str:
        v = (v or "").strip()
        if not v:
            msg = "cron_expression cannot be empty"
            raise serializers.ValidationError(msg)
        try:
            croniter(v, timezone.now())
        except Exception as exc:
            msg = (
                "Invalid cron expression. Expected standard 5-field cron, e.g. '*/5 * * * *' or '45 13 * * 5'."
            )
            raise serializers.ValidationError(msg) from exc
        return v

    def validate_max_concurrent_per_worker(self, v: int) -> int:
        # keep exactly the same constraints as before
        if v is None:
            msg = "max_concurrent_per_worker is required."
            raise serializers.ValidationError(msg)
        if v < 1:
            msg = "max_concurrent_per_worker must be >= 1."
            raise serializers.ValidationError(msg)
        if v > 8:
            msg = "max_concurrent_per_worker must be <= 8."
            raise serializers.ValidationError(msg)
        return v

    def validate(self, attrs: dict) -> dict:
        project: AISTProject | None = self.context.get("project")
        if project is None:
            msg = "Internal error: project is missing in serializer context."
            raise serializers.ValidationError(msg)
        return attrs


class ProjectLaunchScheduleUpsertAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_projects,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Create or update launch schedule for project",
        request=LaunchScheduleUpsertSerializer,
        responses={201: OpenApiResponse(description="Schedule created or updated")},
    )
    def post(self, request, project_id: int, *args, **kwargs):
        project = self.get_authorized_object(permission=Permissions.Product_Edit, id=project_id)
        user_has_permission_or_403(request.user, project.product, Permissions.Product_Edit)

        s = LaunchScheduleUpsertSerializer(data=request.data, context={"project": project})
        s.is_valid(raise_exception=True)

        cron_expression = s.validated_data["cron_expression"]
        enabled = s.validated_data.get("enabled", True)
        max_concurrent = s.validated_data["max_concurrent_per_worker"]
        launch_config = s.validated_data["launch_config"]

        with transaction.atomic():
            existing = (
                LaunchSchedule.objects.select_for_update()
                .select_related("launch_config", "launch_config__project")
                .filter(launch_config__project=project)
                .first()
            )

            if existing is None:
                obj = LaunchSchedule.objects.create(
                    cron_expression=cron_expression,
                    enabled=enabled,
                    max_concurrent_per_worker=max_concurrent,
                    launch_config=launch_config,
                )
                created = True
            else:
                existing.cron_expression = cron_expression
                existing.enabled = enabled
                existing.max_concurrent_per_worker = max_concurrent
                if existing.launch_config_id != launch_config.id:
                    existing.launch_config = launch_config
                existing.save()
                obj = existing
                created = False

        return Response(
            {"ok": True, "created": created, "schedule": LaunchScheduleSerializer(obj).data},
            status=status.HTTP_201_CREATED,
        )


class LaunchScheduleListAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_launch_schedules,
        permission=Permissions.Product_View,
    )

    class FilterSet(django_filters.FilterSet):
        project_id = django_filters.NumberFilter(field_name="launch_config__project_id")
        organization_id = django_filters.NumberFilter(field_name="launch_config__project__organization_id")
        launch_config_id = django_filters.NumberFilter(field_name="launch_config_id")
        enabled = django_filters.TypedChoiceFilter(
            field_name="enabled",
            choices=(("true", "true"), ("false", "false")),
            coerce=_coerce_strict_bool,
        )
        search = django_filters.CharFilter(field_name="cron_expression", lookup_expr="icontains")

        class Meta:
            model = LaunchSchedule
            fields = ("project_id", "organization_id", "launch_config_id", "enabled", "search")

    class QuerySerializer(serializers.Serializer):
        ordering = serializers.ChoiceField(required=False, choices=LAUNCH_SCHEDULE_API_CHOICES.ordering, default="-id")
        limit = serializers.IntegerField(required=False, min_value=1, max_value=500, default=50)
        offset = serializers.IntegerField(required=False, min_value=0, default=0)

    @extend_schema(
        operation_id="aist_launch_schedules_list",
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="List all launch schedules",
        parameters=[
            OpenApiParameter(name="project_id", type=int, required=False),
            OpenApiParameter(name="organization_id", type=int, required=False),
            OpenApiParameter(name="launch_config_id", type=int, required=False),
            OpenApiParameter(name="enabled", type=bool, required=False),
            OpenApiParameter(name="search", type=str, required=False, description="Search in cron_expression"),
            OpenApiParameter(name="ordering", type=str, required=False, enum=LAUNCH_SCHEDULE_API_CHOICES.ordering),
            OpenApiParameter(name="limit", type=int, required=False),
            OpenApiParameter(name="offset", type=int, required=False),
        ],
        responses={200: OpenApiResponse(description="Paginated list")},
    )
    def get(self, request, *args, **kwargs):
        filterset = self.FilterSet(
            data=request.query_params,
            queryset=self.get_authorized_queryset().select_related(
                "launch_config",
                "launch_config__project",
                "launch_config__project__organization",
                "launch_config__project__product",
            ),
            request=request,
        )
        if not filterset.is_valid():
            return Response(filterset.errors, status=status.HTTP_400_BAD_REQUEST)

        query_serializer = self.QuerySerializer(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)
        query_data = query_serializer.validated_data

        qs = filterset.qs

        ordering = query_data["ordering"]
        limit = query_data["limit"]
        offset = query_data["offset"]
        qs = qs.order_by("id") if ordering in {"next_tick", "-next_tick"} else qs.order_by(ordering)

        page = qs[offset : offset + limit]
        results = LaunchScheduleSerializer(page, many=True).data
        if ordering in {"next_tick", "-next_tick"}:
            reverse = ordering.startswith("-")
            # Put None to the end
            results.sort(
                key=lambda x: (x.get("next_tick") is None, x.get("next_tick")),
                reverse=reverse,
            )

        return Response(results, status=status.HTTP_200_OK)


class LaunchScheduleDetailAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LaunchScheduleSerializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_launch_schedules,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        operation_id="aist_launch_schedules_retrieve",
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Get launch schedule by id",
        responses={200: LaunchScheduleSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, launch_schedule_id: int, *args, **kwargs):
        obj = get_object_or_404(
            self.get_authorized_queryset()
            .select_related(
                "launch_config",
                "launch_config__project",
                "launch_config__project__organization",
                "launch_config__project__product",
            ),
            id=launch_schedule_id,
        )
        return Response(LaunchScheduleSerializer(obj).data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Delete launch schedule by id",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, launch_schedule_id: int, *args, **kwargs):
        obj = self.get_authorized_object(permission=Permissions.Product_Edit, id=launch_schedule_id)
        user_has_permission_or_403(request.user, obj.launch_config.project.product, Permissions.Product_Edit)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Partially update launch schedule",
        responses={200: LaunchScheduleSerializer, 400: OpenApiResponse(description="Validation error")},
    )
    def patch(self, request, launch_schedule_id: int):
        """
        Partial update for LaunchSchedule.
        Currently used by UI to toggle 'enabled' without resending the full schedule payload.
        """
        obj = get_object_or_404(
            self.get_authorized_queryset(permission=Permissions.Product_Edit)
            .select_related(
                "launch_config",
                "launch_config__project",
                "launch_config__project__product",
                "launch_config__project__organization",
            ),
            id=launch_schedule_id,
        )
        user_has_permission_or_403(request.user, obj.launch_config.project.product, Permissions.Product_Edit)

        # Only allow whitelisted fields to be patched
        allowed_fields = {"enabled"}
        payload = request.data or {}
        unknown = set(payload.keys()) - allowed_fields
        if unknown:
            return Response(
                {"detail": f"Only fields {sorted(allowed_fields)} can be patched."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "enabled" in payload:
            obj.enabled = bool(payload["enabled"])
            obj.save(update_fields=["enabled"])

        return Response(LaunchScheduleSerializer(obj).data, status=status.HTTP_200_OK)


class LaunchSchedulePreviewSerializer(serializers.Serializer):
    cron_expression = serializers.CharField()
    count = serializers.IntegerField(required=False, default=5, min_value=1, max_value=20)

    def validate_cron_expression(self, v: str) -> str:
        v = (v or "").strip()
        if not v:
            msg = "cron_expression cannot be empty"
            raise serializers.ValidationError(msg)
        try:
            croniter(v, timezone.now())
        except Exception as exc:
            msg = "Invalid cron expression"
            raise serializers.ValidationError(msg) from exc
        return v


class LaunchSchedulePreviewAPI(AuthorizedQuerySetMixin, APIView):

    """
    UI helper endpoint: preview next N runs for a cron expression.
    Backend calculates, UI only renders.
    """

    permission_classes = [IsAuthenticated]
    # A POST that only computes a preview (no state change) — read-only tokens may call it.
    token_read_only = True

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Preview next N runs for a cron expression",
        request=LaunchSchedulePreviewSerializer,
        responses={200: OpenApiResponse(description="Preview list")},
    )
    def post(self, request, *args, **kwargs):
        s = LaunchSchedulePreviewSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        cron_expression = s.validated_data["cron_expression"]
        count = s.validated_data["count"]

        tmp = LaunchSchedule(cron_expression=cron_expression, enabled=True, max_concurrent_per_worker=1)
        runs = tmp.preview_next_runs(count=count, now=timezone.now())
        return Response(
            {"cron_expression": cron_expression, "count": count, "runs": runs},
            status=status.HTTP_200_OK,
        )


class LaunchScheduleBulkDisableSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField(required=False)
    project_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if not attrs.get("organization_id") and not attrs.get("project_id"):
            msg = "Either organization_id or project_id is required."
            raise serializers.ValidationError(msg)
        return attrs


class LaunchScheduleBulkDisableAPI(AuthorizedQuerySetMixin, APIView):

    """Quick action: disable schedules for org and/or project."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_launch_schedules,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Disable schedules for an organization or a project",
        request=LaunchScheduleBulkDisableSerializer,
        responses={200: OpenApiResponse(description="Updated count")},
    )
    def post(self, request, *args, **kwargs):
        s = LaunchScheduleBulkDisableSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        org_id = s.validated_data.get("organization_id")
        project_id = s.validated_data.get("project_id")

        qs = self.get_authorized_queryset(permission=Permissions.Product_Edit)
        if org_id:
            qs = qs.filter(launch_config__project__organization_id=org_id)
        if project_id:
            qs = qs.filter(launch_config__project_id=project_id)

        updated = qs.update(enabled=False)
        return Response({"updated": updated}, status=status.HTTP_200_OK)


class LaunchScheduleRunOnceAPI(AuthorizedQuerySetMixin, APIView):

    """
    UI helper: enqueue a single run for this schedule (does not touch cron/last_run_at).
    Creates PipelineLaunchQueue item and returns it.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = serializers.Serializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_launch_schedules,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Enqueue one run for a schedule",
        responses={200: OpenApiResponse(description="Enqueued queue item"), 404: OpenApiResponse(description="Not found")},
    )
    def post(self, request, launch_schedule_id: int, *args, **kwargs):
        obj = get_object_or_404(
            self.get_authorized_queryset(permission=Permissions.Product_Edit)
            .select_related(
                "launch_config",
                "launch_config__project",
                "launch_config__project__product",
            ),
            id=launch_schedule_id,
        )
        user_has_permission_or_403(request.user, obj.launch_config.project.product, Permissions.Product_Edit)

        # Use launch_config snapshot. Project is derived from launch_config.project :contentReference[oaicite:6]{index=6}
        project = obj.launch_config.project

        with transaction.atomic():
            q = PipelineLaunchQueue.objects.create(
                project=project,
                schedule=obj,
                launch_config=obj.launch_config,
            )

        project_name = getattr(getattr(project, "product", None), "name", str(project.id))
        return Response(
            {
                "ok": True,
                "queue_item": {
                    "id": q.id,
                    "created": q.created,
                    "project_id": project.id,
                    "project_name": project_name,
                    "schedule_id": obj.id,
                    "launch_config_id": obj.launch_config_id,
                    "dispatched": q.dispatched,
                    "dispatched_at": q.dispatched_at,
                    "pipeline_id": None,
                },
            },
            status=status.HTTP_200_OK,
        )
