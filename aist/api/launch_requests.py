from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import exceptions, serializers, status

from aist.execution.enqueue import (
    LaunchEnqueueError,
    LaunchIdempotencyConflictError,
    LaunchPrincipal,
    enqueue_pipeline_launch,
)
from aist.models import AISTApiToken, PipelineLaunchRequest

if TYPE_CHECKING:
    from aist.models import AISTProjectLaunchConfig, LaunchSchedule
    from aist.pipeline_args import PipelineArguments


class LaunchRequestHeadersSerializer(serializers.Serializer):
    client_request_key = serializers.CharField(required=False, allow_blank=False, max_length=255)


class LaunchRequestResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    state = serializers.CharField()
    origin = serializers.CharField()
    execution_type = serializers.CharField()
    created = serializers.DateTimeField()
    not_before = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(allow_null=True)
    pipeline_id = serializers.CharField(allow_null=True)
    queued = serializers.BooleanField()


def launch_request_headers(request) -> dict:
    value = request.headers.get("Idempotency-Key")
    serializer = LaunchRequestHeadersSerializer(
        data={"client_request_key": value} if value is not None else {},
    )
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def launch_principal_token(request) -> AISTApiToken | None:
    return request.auth if isinstance(request.auth, AISTApiToken) else None


def launch_request_response(launch_request: PipelineLaunchRequest) -> dict:
    return LaunchRequestResponseSerializer({
        "id": launch_request.pk,
        "state": launch_request.state,
        "origin": launch_request.origin,
        "execution_type": launch_request.execution_type,
        "created": launch_request.created,
        "not_before": launch_request.not_before,
        "expires_at": launch_request.expires_at,
        "pipeline_id": launch_request.pipeline_id,
        "queued": launch_request.pipeline_id is None,
    }).data


class LaunchConflict(exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The idempotency key was already used for a different launch."
    default_code = "conflict"


def enqueue_requested_launch(
    request,
    *,
    arguments: PipelineArguments,
    launch_config: AISTProjectLaunchConfig | None = None,
    schedule: LaunchSchedule | None = None,
) -> PipelineLaunchRequest:
    """
    Queue a launch on behalf of the request's user or API token, keyed by its ``Idempotency-Key``.

    A replayed key with a different launch raises :class:`LaunchConflict` (409); any other rejected launch
    raises ``ValidationError`` (400).
    """
    try:
        return enqueue_pipeline_launch(
            arguments=arguments,
            principal=LaunchPrincipal.for_user(
                organization=arguments.project.organization,
                requester=request.user,
                api_token=launch_principal_token(request),
            ),
            launch_config=launch_config,
            schedule=schedule,
            client_request_key=launch_request_headers(request).get("client_request_key"),
        ).request
    except LaunchIdempotencyConflictError as exc:
        raise LaunchConflict({"detail": str(exc)}) from exc
    except LaunchEnqueueError as exc:
        raise serializers.ValidationError({"detail": str(exc)}) from exc
