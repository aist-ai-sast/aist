from __future__ import annotations

from rest_framework import serializers

from aist.models import AISTApiToken, PipelineLaunchRequest


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
