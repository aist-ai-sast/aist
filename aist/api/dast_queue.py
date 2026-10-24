"""
Tenant-scoped proxy over one DAST run's work queue.

The page this serves holds no DAST vocabulary of its own: the gateway's descriptor carries the
wording, the icons and the form fields, and each row carries what may be done to it. These two
endpoints pass that through under AIST's own authorization, and the command endpoint re-resolves
every action against the gateway before sending it, so a browser can neither choose an address nor
reach an action it was not offered.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, ResourcePolicy
from aist.models import AISTPipeline
from aist.services.dast_queue import (
    DastQueueError,
    QueueView,
    command_outcome,
    enqueue_command,
    read_queue,
)

logger = logging.getLogger(__name__)


class QueueNotAddressableYet(APIException):

    """
    The run exists as far as AIST is concerned, but the provider has not named it yet.

    Not a 404: the read path reports the same state as `RUN_NOT_REPORTED` on a live pipeline, and a
    caller that was told "gone" would stop asking about something that is about to arrive.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The provider has not reported a run id for this pipeline yet."
    default_code = "run_not_reported"


class DastQueueReadRequestSerializer(serializers.Serializer):

    """
    How a page asks for the queue.

    There is no filter here: one snapshot holds the whole queue, and the page filters what it
    already has. ``force`` is the operator's own Refresh, which asks a worker for a fresh read
    instead of waiting for the gateway's own cadence to come round.
    """

    force = serializers.BooleanField(required=False, default=False)


class DastQueueCommandRequestSerializer(serializers.Serializer):

    """
    One operator command, named rather than addressed.

    ``action`` and ``item_id`` are all the caller may choose. The method, the address and the fields
    that may travel come from the affordance the gateway publishes for that item, read fresh when
    the command is sent.
    """

    #: What one action's form may carry. The gateway's own field list decides what actually
    #: travels, and the worker applies it; these bounds only keep an unbounded body out of the
    #: broker, and hold to the scalar field types the contract itself declares.
    MAX_FIELDS = 20
    MAX_FIELD_NAME = 64
    MAX_FIELD_LENGTH = 4096

    action = serializers.CharField(max_length=64)
    item_id = serializers.CharField(max_length=256, required=False, allow_blank=True, default="")
    # Minted per decision by the caller so an identical retry returns the first receipt instead of
    # applying twice.
    request_id = serializers.CharField(max_length=128)
    expected_revision = serializers.IntegerField(min_value=0)
    values = serializers.DictField(child=serializers.JSONField(), required=False, default=dict)

    def validate_values(self, values: dict) -> dict:
        if len(values) > self.MAX_FIELDS:
            msg = f"At most {self.MAX_FIELDS} fields may travel with one command."
            raise serializers.ValidationError(msg)
        for name, value in values.items():
            if len(name) > self.MAX_FIELD_NAME:
                msg = "A field name that long belongs to no action this gateway declares."
                raise serializers.ValidationError(msg)
            if isinstance(value, (dict, list)):
                msg = f"Field {name!r} takes text, a number or a flag."
                raise serializers.ValidationError(msg)
            if isinstance(value, str) and len(value) > self.MAX_FIELD_LENGTH:
                msg = f"Field {name!r} is longer than {self.MAX_FIELD_LENGTH} characters."
                raise serializers.ValidationError(msg)
        return values


class DastQueueUnavailableSerializer(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField(allow_blank=True)


class DastQueueReadResponseSerializer(serializers.Serializer):

    """
    The envelope AIST owns. ``descriptor`` and ``page`` are the gateway's own documents.

    They are passed through as data rather than re-declared field by field: their vocabulary is the
    gateway's to extend, and a schema here would have to be edited every time it did. ``reading``
    says a worker has been asked and nothing has been stored yet; ``stale`` says what is shown is
    older than the cadence and a fresh read is already on its way.
    """

    available = serializers.BooleanField()
    reading = serializers.BooleanField(required=False)
    stale = serializers.BooleanField(required=False)
    run_id = serializers.CharField(allow_blank=True)
    fetched_at = serializers.CharField(allow_blank=True, required=False)
    descriptor = serializers.JSONField(required=False)
    page = serializers.JSONField(required=False)
    unavailable = DastQueueUnavailableSerializer(required=False)


class DastQueueCommandAcceptedSerializer(serializers.Serializer):
    request_id = serializers.CharField()
    state = serializers.CharField()


class DastQueueCommandOutcomeSerializer(serializers.Serializer):
    #: ``queued`` while the worker still has it, ``done`` once it answered, and ``unknown`` for a
    #: request id this operator never sent or whose answer has expired.
    state = serializers.ChoiceField(choices=["queued", "done", "unknown"])
    accepted = serializers.BooleanField()
    receipt = serializers.JSONField(required=False)
    refusal = serializers.JSONField(required=False)


def _view_payload(view: QueueView) -> dict[str, object]:
    if view.unavailable is not None:
        return {
            "available": False,
            "reading": False,
            "run_id": view.run_id,
            "unavailable": {"code": view.unavailable.code, "detail": view.unavailable.detail},
        }
    if not view.available:
        return {"available": False, "reading": view.reading, "run_id": view.run_id}
    return {
        "available": True,
        "reading": False,
        "stale": view.stale,
        "run_id": view.run_id,
        "fetched_at": view.fetched_at,
        "descriptor": view.descriptor,
        "page": view.page,
    }


class PipelineDastQueueAPI(AISTAPIView):

    """Read one page of a DAST run's queue, with the descriptor it is rendered from."""

    # Reading is cached under the gateway's own cadence, but `force` bypasses that cache, so the
    # endpoint is still a way to reach a tenant-supplied host on demand and carries the same bound
    # as the other gateway-facing endpoints.
    throttle_scope = "aist_dast_queue"
    throttle_classes = [ScopedRateThrottle]

    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)
    token_read_only = True

    @extend_schema(
        parameters=[DastQueueReadRequestSerializer],
        responses={
            200: DastQueueReadResponseSerializer,
            400: OpenApiResponse(description="Unreadable query parameter"),
            403: OpenApiResponse(description="The caller may not read this pipeline"),
            404: OpenApiResponse(description="Not found"),
            429: OpenApiResponse(description="Reading this queue is rate limited"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Read the DAST run queue",
        description=(
            "Returns the gateway's own queue page and descriptor for this pipeline's DAST run. "
            "AIST adds no vocabulary: statuses, wait reasons, phases, icons, form fields and the "
            "actions each row offers all come from the gateway. `available` is false, with a reason, "
            "for a pipeline that has no run or whose run the gateway no longer serves."
        ),
    )
    def get(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        pipeline = self.resolve(id=pipeline_id)
        query = DastQueueReadRequestSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        view = read_queue(pipeline, force=query.validated_data["force"])
        return Response(_view_payload(view), status=status.HTTP_200_OK)


class PipelineDastQueueCommandAPI(AISTAPIView):

    """Send one operator command to a DAST run's queue."""

    throttle_scope = "aist_dast_queue"
    throttle_classes = [ScopedRateThrottle]

    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=DastQueueCommandRequestSerializer,
        responses={
            202: DastQueueCommandAcceptedSerializer,
            400: OpenApiResponse(description="The command was not readable"),
            403: OpenApiResponse(description="The caller may not operate this pipeline"),
            404: OpenApiResponse(description="Not found, or this pipeline names no DAST run"),
            409: OpenApiResponse(description="The provider has not reported a run id yet"),
            429: OpenApiResponse(description="Commanding this queue is rate limited"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Command the DAST run queue",
        description=(
            "Accepts one action the gateway advertised for this item -- pause, resume, cancel, "
            "delay, edit, admit or annotate -- and hands it to a worker, which is the only process "
            "that may reach the gateway. The caller names the action and the item and supplies the "
            "fields that action declared; the worker re-reads the item, takes the method and "
            "address from the gateway's own affordance, and drops anything the action did not "
            "declare writable. This returns 202 as soon as the command is accepted: what the "
            "gateway made of it, receipt or refusal, is collected from the outcome endpoint under "
            "the same `request_id`."
        ),
    )
    def post(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        # A POST resolves through the policy's write action, so this is the Maintainer+ path.
        pipeline = self.resolve(id=pipeline_id)
        body = DastQueueCommandRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = body.validated_data
        try:
            enqueue_command(
                pipeline,
                action=payload["action"],
                item_id=payload["item_id"],
                values=payload["values"],
                request_id=payload["request_id"],
                expected_revision=payload["expected_revision"],
                actor=request.user.get_username(),
            )
        except DastQueueError as exc:
            # The only commands AIST itself turns down are those with no run to address.
            # Everything the gateway refuses is heard by the worker and collected from the outcome
            # endpoint, never from here.
            logger.warning("DAST queue command not accepted on pipeline %s: %s %r",
                           pipeline.id, exc.code, exc.detail)
            if exc.code == "RUN_NOT_REPORTED":
                raise QueueNotAddressableYet from exc
            if exc.code != "NO_RUN":
                # A refusal this endpoint has no answer for is a fault to be seen and given its own
                # answer, not a 404 telling the caller the pipeline is gone.
                raise
            msg = "This pipeline has no DAST run to command."
            raise NotFound(msg) from exc
        return Response(
            {"request_id": payload["request_id"], "state": "queued"},
            status=status.HTTP_202_ACCEPTED,
        )


class PipelineDastQueueCommandOutcomeAPI(AISTAPIView):

    """Collect what became of one command this page sent."""

    # Its own budget: this reads the cache the worker wrote and reaches no gateway, while a page
    # collects one answer about twenty times. Sharing the gateway's bound would let an operator's
    # own polling spend what their next command needs.
    throttle_scope = "aist_dast_queue_poll"
    throttle_classes = [ScopedRateThrottle]

    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)
    token_read_only = True

    @extend_schema(
        responses={
            200: DastQueueCommandOutcomeSerializer,
            403: OpenApiResponse(description="The caller may not read this pipeline"),
            404: OpenApiResponse(description="Not found"),
            429: OpenApiResponse(description="Collecting outcomes is rate limited"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Read a DAST queue command outcome",
        description=(
            "A command is sent by a worker, so its receipt arrives after the request that asked for "
            "it. This returns `queued` until the worker answers, then the gateway's own receipt or "
            "its refusal, including which field was at fault and what is allowed. An outcome is "
            "collectable by the operator who sent the command; another caller reading the same "
            "`request_id` is told `unknown`."
        ),
    )
    def get(self, request, pipeline_id: str, request_id: str, *args, **kwargs) -> Response:
        pipeline = self.resolve(id=pipeline_id)
        outcome = command_outcome(pipeline, request.user.get_username(), request_id)
        return Response(
            {
                "state": outcome.state,
                "accepted": outcome.accepted,
                "receipt": outcome.receipt,
                "refusal": outcome.refusal,
            },
            status=status.HTTP_200_OK,
        )
