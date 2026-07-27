from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003

from croniter import croniter
from django.db import transaction
from django.utils import timezone
from django_filters import rest_framework as django_filters
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.authz import PUBLIC, Action, AISTAPIView, ResourcePolicy
from aist.execution.enqueue import (
    LaunchIdempotencyConflictError,
    LaunchPrincipal,
    enqueue_pipeline_launch,
)
from aist.models import (
    AISTApiToken,
    AISTProjectLaunchConfig,
    LaunchSchedule,
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
        "max_concurrent_runs",
        "-max_concurrent_runs",
        "next_tick",
        "-next_tick",
    ],
)


class LaunchRunOnceHeadersSerializer(serializers.Serializer):
    client_request_key = serializers.CharField(required=False, allow_blank=False, max_length=255)


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
            "max_concurrent_runs",
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


class LaunchConfigScheduleWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = LaunchSchedule
        fields = [
            "cron_expression",
            "enabled",
            "max_concurrent_runs",
        ]

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "Unknown field."))
        return super().to_internal_value(data)

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

    def validate_max_concurrent_runs(self, v: int) -> int:
        # keep exactly the same constraints as before
        if v is None:
            msg = "max_concurrent_runs is required."
            raise serializers.ValidationError(msg)
        if v < 1:
            msg = "max_concurrent_runs must be >= 1."
            raise serializers.ValidationError(msg)
        if v > 8:
            msg = "max_concurrent_runs must be <= 8."
            raise serializers.ValidationError(msg)
        return v


class LaunchConfigScheduleAPI(AISTAPIView):
    authz = ResourcePolicy(
        resource=AISTProjectLaunchConfig,
        read=Action.PRODUCT_READ,
        write=Action.PROJECT_OPERATE,
    )

    def _resolve_config(self, *, project_id: int, config_id: int) -> AISTProjectLaunchConfig:
        return self.resolve(id=config_id, project_id=project_id)

    @staticmethod
    def _schedule(config: AISTProjectLaunchConfig) -> LaunchSchedule:
        try:
            return config.launch_schedule
        except LaunchSchedule.DoesNotExist as exc:
            msg = "Launch schedule not found for this launch configuration."
            raise NotFound(msg) from exc

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Get the launch schedule for a launch configuration",
        responses={200: LaunchScheduleSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, project_id: int, config_id: int, *args, **kwargs):
        config = self._resolve_config(project_id=project_id, config_id=config_id)
        return Response(LaunchScheduleSerializer(self._schedule(config)).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Create or replace the launch schedule for a launch configuration",
        request=LaunchConfigScheduleWriteSerializer,
        responses={
            200: LaunchScheduleSerializer,
            201: LaunchScheduleSerializer,
            404: OpenApiResponse(description="Not found"),
        },
    )
    def put(self, request, project_id: int, config_id: int, *args, **kwargs):
        config = self._resolve_config(project_id=project_id, config_id=config_id)
        serializer = LaunchConfigScheduleWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            locked_config = (
                self.authorized_queryset_for_request()
                .select_for_update()
                .get(pk=config.pk, project_id=project_id)
            )
            obj, created = LaunchSchedule.objects.update_or_create(
                launch_config=locked_config,
                defaults=serializer.validated_data,
            )
        return Response(
            LaunchScheduleSerializer(obj).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Partially update the launch schedule for a launch configuration",
        request=LaunchConfigScheduleWriteSerializer,
        responses={200: LaunchScheduleSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def patch(self, request, project_id: int, config_id: int, *args, **kwargs):
        config = self._resolve_config(project_id=project_id, config_id=config_id)
        schedule = self._schedule(config)
        serializer = LaunchConfigScheduleWriteSerializer(schedule, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(LaunchScheduleSerializer(schedule).data)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Delete the launch schedule for a launch configuration",
        responses={204: OpenApiResponse(description="Deleted"), 404: OpenApiResponse(description="Not found")},
    )
    def delete(self, request, project_id: int, config_id: int, *args, **kwargs):
        config = self._resolve_config(project_id=project_id, config_id=config_id)
        self._schedule(config).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class LaunchScheduleListAPI(AISTAPIView):
    authz = ResourcePolicy(resource=LaunchSchedule, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    class FilterSet(django_filters.FilterSet):
        project_id = django_filters.NumberFilter(field_name="launch_config__project_id")
        organization_id = django_filters.NumberFilter(
            field_name="launch_config__project__product__prod_type__aist_organization_id",
        )
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
            queryset=self.authorized_queryset_for_request().select_related(
                "launch_config",
                "launch_config__project",
                "launch_config__project__product__prod_type__aist_organization",
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


class LaunchScheduleDetailAPI(AISTAPIView):
    serializer_class = LaunchScheduleSerializer
    authz = ResourcePolicy(resource=LaunchSchedule, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        operation_id="aist_launch_schedules_retrieve",
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Get launch schedule by id",
        responses={200: LaunchScheduleSerializer, 404: OpenApiResponse(description="Not found")},
    )
    def get(self, request, launch_schedule_id: int, *args, **kwargs):
        obj = self.resolve(id=launch_schedule_id)
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


class LaunchSchedulePreviewAPI(AISTAPIView):

    """
    UI helper endpoint: preview next N runs for a cron expression.
    Backend calculates, UI only renders.
    """

    # No org-owned resource (pure cron math). A POST that only computes a preview
    # (no state change) — read-only tokens may call it.
    authz = PUBLIC
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

        tmp = LaunchSchedule(cron_expression=cron_expression, enabled=True, max_concurrent_runs=1)
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


class LaunchScheduleBulkDisableAPI(AISTAPIView):

    """Quick action: disable schedules for org and/or project."""

    authz = ResourcePolicy(resource=LaunchSchedule, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

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

        qs = self.authorized_queryset_for_request()
        if org_id:
            qs = qs.filter(
                launch_config__project__product__prod_type__aist_organization_id=org_id,
            )
        if project_id:
            qs = qs.filter(launch_config__project_id=project_id)

        updated = qs.update(enabled=False)
        return Response({"updated": updated}, status=status.HTTP_200_OK)


class LaunchScheduleRunOnceAPI(AISTAPIView):

    """
    UI helper: enqueue a single run for this schedule (does not touch cron/last_run_at).
    Creates a PipelineLaunchRequest and returns its legacy queue representation.
    """

    serializer_class = serializers.Serializer
    authz = ResourcePolicy(resource=LaunchSchedule, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.LAUNCH_SCHEDULES.value],
        summary="Enqueue one run for a schedule",
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=False,
                description="Optional producer key for idempotent launch request creation.",
            ),
        ],
        responses={
            200: OpenApiResponse(description="Enqueued queue item"),
            404: OpenApiResponse(description="Not found"),
            409: OpenApiResponse(description="Idempotency key conflict"),
        },
    )
    def post(self, request, launch_schedule_id: int, *args, **kwargs):
        obj = self.resolve(id=launch_schedule_id)

        # Use launch_config snapshot. Project is derived from launch_config.project :contentReference[oaicite:6]{index=6}
        project = obj.launch_config.project

        token = request.auth if isinstance(request.auth, AISTApiToken) else None
        principal = LaunchPrincipal.for_user(
            organization=project.organization,
            requester=request.user,
            api_token=token,
        )
        key_serializer = LaunchRunOnceHeadersSerializer(data={
            "client_request_key": request.headers.get("Idempotency-Key"),
        } if request.headers.get("Idempotency-Key") is not None else {})
        key_serializer.is_valid(raise_exception=True)
        try:
            q = enqueue_pipeline_launch(
                project=project,
                principal=principal,
                raw_params={},
                schedule=obj,
                launch_config=obj.launch_config,
                client_request_key=key_serializer.validated_data.get("client_request_key"),
            ).request
        except LaunchIdempotencyConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

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
