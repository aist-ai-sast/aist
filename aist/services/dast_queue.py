"""
Reading and steering one DAST run's work queue from AIST.

AIST owns none of the queue's meaning. The gateway sends the vocabulary, the per-row affordances and
the form fields; this service keeps the last payload a worker read and relays one authorized operator
decision back.

Two boundaries decide the shape of everything here:

- **Only a worker may reach the gateway.** The route runs through the integration's VPN sidecar,
  which needs the Docker socket; a web process has neither. So nothing on a request path calls the
  gateway. A read serves the stored snapshot and asks a worker to refresh it when it is old; a
  command is handed to a worker and its answer collected afterwards.
- **A command never carries an address from the browser.** The browser names an action and an item;
  the worker re-reads that item, takes the method and href the gateway published for it, and drops
  every field that action did not declare writable.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.core.cache import caches
from django.utils import timezone

from aist.integrations.dast_gateway_client import (
    DastGatewayClientError,
    scoped_dast_gateway_client,
)
from aist.integrations.dast_queue_contract import (
    MUTATION_TOP_LEVEL_FIELDS,
    DastQueueContractError,
    find_affordance,
    href_addresses_run,
    normalise_descriptor,
    normalise_page,
    normalise_receipt,
    normalise_refusal,
    submitted_values,
)
from aist.models import DastExecutionState, DastQueueSnapshot, PipelineExecutionType
from aist.services.pipeline_lifecycle import is_terminal_pipeline_status
from aist.utils.cache import QUEUE_CACHE_ALIAS

if TYPE_CHECKING:
    from aist.models import AISTPipeline, OrgIntegration

logger = logging.getLogger(__name__)

#: One snapshot holds the whole queue, and the gateway caps a page at 500 rows. Filtering then
#: happens in the page the operator is looking at, so changing a filter costs no round trip and the
#: counts stay the gateway's own.
SNAPSHOT_PAGE_LIMIT = 500

#: How old a snapshot may be before a read asks for a fresh one. The gateway states its own cadence
#: in the descriptor; this is the fallback for a pipeline whose descriptor has never been read.
DEFAULT_REFRESH_SECONDS = 60

#: Bounds how often a stale snapshot asks for a refresh, so a page open in ten tabs still asks the
#: worker once.
REFRESH_DEBOUNCE_SECONDS = 20

#: How long one command's receipt or refusal stays collectable by the page that sent it.
COMMAND_OUTCOME_SECONDS = 600

#: Gateway codes that mean "this run's queue is simply not available to us any more" rather than
#: "something is broken". An old run whose target left the token's allowlist answers one of these,
#: and a finished pipeline showing a quiet line is the correct rendering.
UNAVAILABLE_CODES = frozenset({"UNKNOWN_RUN", "POLICY_DENIED", "TOKEN_REJECTED", "AUTHENTICATION_FAILED"})


@dataclass(frozen=True, slots=True)
class QueueUnavailable:

    """Why there is nothing to show, in words a page can print without alarming anyone."""

    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class QueueView:

    """What a page can render right now, and how fresh it is."""

    run_id: str
    page: dict[str, Any]
    descriptor: dict[str, Any]
    fetched_at: str = ""
    stale: bool = False
    reading: bool = False
    unavailable: QueueUnavailable | None = None

    @property
    def available(self) -> bool:
        return self.unavailable is None and bool(self.page)


@dataclass(frozen=True, slots=True)
class CommandOutcome:

    """What one operator command produced, as the page needs to read it."""

    state: str
    accepted: bool = False
    receipt: dict[str, Any] = field(default_factory=dict)
    refusal: dict[str, Any] = field(default_factory=dict)


class DastQueueError(RuntimeError):

    """A failure the operator should see as a failure, with a stable code."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


def queue_is_addressable(pipeline: AISTPipeline) -> bool:
    """
    Whether this pipeline's queue is worth a block on the page.

    A run id is what addresses the queue, and AIST records one as soon as its connector reports that
    the provider accepted the run. A pipeline still going may not have one yet, and silence would
    read as "this build has no such feature" -- so the block is offered and says what it waits for.
    A finished pipeline that never recorded one has nothing to say and gets no block.
    """
    if pipeline.execution_type != PipelineExecutionType.DAST or _integration(pipeline) is None:
        return False
    if _snapshot(pipeline) is not None:
        return True
    return bool(_run_id(pipeline)) or not is_terminal_pipeline_status(pipeline.status)


def _run_id(pipeline: AISTPipeline) -> str:
    if pipeline.execution_type != PipelineExecutionType.DAST:
        return ""
    try:
        state = pipeline.dast_execution_state
    except DastExecutionState.DoesNotExist:
        return ""
    return (state.run_id or "").strip()


def _integration(pipeline: AISTPipeline) -> OrgIntegration | None:
    binding = pipeline.dast_binding
    if binding is None:
        return None
    return binding.target.integration


def _snapshot(pipeline: AISTPipeline) -> DastQueueSnapshot | None:
    return DastQueueSnapshot.objects.filter(pipeline=pipeline).first()


def _refresh_interval(snapshot: DastQueueSnapshot | None) -> int:
    if snapshot is None:
        return DEFAULT_REFRESH_SECONDS
    hints = (snapshot.descriptor or {}).get("client_hints") or {}
    return int(hints.get("poll_interval_seconds") or DEFAULT_REFRESH_SECONDS)


def _command_outcome_key(pipeline_id: str, actor: str, request_id: str) -> str:
    """
    Where one command's answer waits for the page that sent it.

    The operator is part of the key: ``request_id`` is minted by the browser, so without it one
    reader of this pipeline could collect -- or overwrite -- a receipt another operator is waiting
    for, with the reason they typed in it. Both travel as one digest: the store is shared by every
    process, and a digest keeps an operator's name out of it while making the two fields impossible
    to spell into one another.
    """
    identity = hashlib.sha256(f"{actor}\0{request_id}".encode()).hexdigest()
    return f"aist:dast-queue:outcome:{pipeline_id}:{identity}"


def _cache_get(key: str) -> Any:
    """
    Read one key, treating an unreachable cache as an empty one.

    Nothing kept here is authoritative -- a debounce window and a command's answer -- so a cache
    that is down costs another read or another wait, and never the request that was asking.
    """
    try:
        return caches[QUEUE_CACHE_ALIAS].get(key)
    except Exception:
        logger.warning("DAST queue cache could not be read", exc_info=True)
        return None


def _cache_set(key: str, value: Any, *, timeout: int, only_if_absent: bool = False) -> bool:
    """Write one key; says whether it stuck, because for one caller a lost write is not harmless."""
    try:
        if only_if_absent:
            caches[QUEUE_CACHE_ALIAS].add(key, value, timeout=timeout)
        else:
            caches[QUEUE_CACHE_ALIAS].set(key, value, timeout=timeout)
    except Exception:
        logger.warning("DAST queue cache could not be written", exc_info=True)
        return False
    return True


def _refresh_debounce_key(pipeline_id: str) -> str:
    return f"aist:dast-queue:refreshing:{pipeline_id}"


def read_queue(pipeline: AISTPipeline, *, force: bool = False) -> QueueView:
    """
    What a page can render, from the snapshot a worker last stored.

    Nothing here reaches the gateway: a stale or missing snapshot asks a worker for a fresh one and
    the page shows what it has meanwhile. ``force`` is the operator's own Refresh, which skips the
    staleness check but still goes through the worker.
    """
    snapshot = _snapshot(pipeline)
    run_id = _run_id(pipeline)
    if snapshot is None:
        if not run_id:
            return QueueView(run_id="", page={}, descriptor={}, unavailable=_no_run(pipeline))
        # Debounced like any other read: while the gateway is unreachable every open tab keeps
        # asking, and each ask costs a worker a VPN-routed call.
        _ask_for_refresh(pipeline, force=False)
        return QueueView(run_id=run_id, page={}, descriptor={}, reading=True)

    age = (timezone.now() - snapshot.fetched_at).total_seconds() if snapshot.fetched_at else None
    # A snapshot of a run the pipeline no longer names is stale by identity, not by age.
    stale = (
        age is None
        or age > _refresh_interval(snapshot)
        or (bool(run_id) and snapshot.run_id != run_id)
    )
    if (stale or force) and not _is_frozen(snapshot):
        _ask_for_refresh(pipeline, force=force)
    fetched_at = snapshot.fetched_at.isoformat(timespec="seconds") if snapshot.fetched_at else ""
    if snapshot.unavailable_code:
        return QueueView(
            run_id=snapshot.run_id,
            page={},
            descriptor={},
            fetched_at=fetched_at,
            unavailable=QueueUnavailable(snapshot.unavailable_code, snapshot.unavailable_detail),
        )
    return QueueView(
        run_id=snapshot.run_id,
        page=snapshot.page or {},
        descriptor=snapshot.descriptor or {},
        fetched_at=fetched_at,
        stale=stale,
    )


def _is_frozen(snapshot: DastQueueSnapshot) -> bool:
    """A finished run is read once: its queue is a record, and nothing in it will move again."""
    return bool((snapshot.page or {}).get("mutations_frozen"))


def _ask_for_refresh(pipeline: AISTPipeline, *, force: bool) -> None:
    """Hand the gateway read to a worker, at most once per debounce window."""
    from aist.tasks.dast_queue import refresh_dast_queue  # noqa: PLC0415 -- breaks an import cycle

    key = _refresh_debounce_key(str(pipeline.id))
    if not force and _cache_get(key):
        return
    _cache_set(key, value=True, timeout=REFRESH_DEBOUNCE_SECONDS)
    try:
        refresh_dast_queue.delay(str(pipeline.id))
    except Exception:
        # The queue is read on the way to rendering a page. A broker that will not take the read --
        # the same Valkey outage that loses the debounce above -- costs the operator freshness, and
        # they keep the snapshot they had, with its age on it, instead of losing the page.
        logger.warning("DAST queue refresh could not be dispatched for %s", pipeline.id, exc_info=True)


def _no_run(pipeline: AISTPipeline) -> QueueUnavailable:
    """Why there is no run to read: not yet reported, or never recorded at all."""
    if pipeline.execution_type == PipelineExecutionType.DAST and not is_terminal_pipeline_status(pipeline.status):
        return QueueUnavailable(
            "RUN_NOT_REPORTED",
            "The provider has not reported a run id to AIST yet, so its queue cannot be addressed.",
        )
    return QueueUnavailable("NO_RUN", "This pipeline has no DAST run to read.")


def enqueue_command(
    pipeline: AISTPipeline,
    *,
    action: str,
    item_id: str = "",
    values: dict[str, Any] | None = None,
    request_id: str,
    expected_revision: int,
    actor: str,
) -> None:
    """
    Hand one named command to a worker, which is the only process that may send it.

    The answer is collected afterwards with :func:`command_outcome`, by the operator who sent it
    and under their own ``request_id`` -- the same id that makes an identical retry idempotent at
    the gateway.
    """
    from aist.tasks.dast_queue import apply_dast_queue_command  # noqa: PLC0415 -- breaks an import cycle

    if not _run_id(pipeline):
        # Said the same way the read path says it: a run that has not been reported yet is a
        # different answer from a pipeline that will never name one.
        reason = _no_run(pipeline)
        raise DastQueueError(reason.code, reason.detail)
    if _integration(pipeline) is None:
        code = "NO_RUN"
        raise DastQueueError(code, "This pipeline's DAST connection is gone.")
    apply_dast_queue_command.delay(
        str(pipeline.id),
        action=action,
        item_id=item_id,
        values=values or {},
        request_id=request_id,
        expected_revision=expected_revision,
        actor=actor,
    )
    # Marked only once the broker has the command: a command that was never published must not
    # leave its page waiting on an answer no worker will ever write.
    _mark_command_queued(str(pipeline.id), actor, request_id)


def _mark_command_queued(pipeline_id: str, actor: str, request_id: str) -> None:
    """
    Say this command is on its way, without overwriting an answer it already has.

    The same ``request_id`` may be sent twice -- a reloaded page, a retried click. The gateway
    treats the repeat as the same decision, and so must the page: an answer already collected stays
    collectable instead of reverting to "waiting" until the worker runs again.
    """
    _cache_set(
        _command_outcome_key(pipeline_id, actor, request_id),
        {"state": "queued", "accepted": False, "receipt": {}, "refusal": {}},
        timeout=COMMAND_OUTCOME_SECONDS,
        only_if_absent=True,
    )


def command_outcome(pipeline: AISTPipeline, actor: str, request_id: str) -> CommandOutcome:
    """What became of one command this operator sent."""
    stored = _cache_get(_command_outcome_key(str(pipeline.id), actor, request_id))
    if stored is None:
        return CommandOutcome(state="unknown")
    return CommandOutcome(
        state=stored.get("state", "queued"),
        accepted=bool(stored.get("accepted")),
        receipt=stored.get("receipt") or {},
        refusal=stored.get("refusal") or {},
    )


def store_command_outcome(
    pipeline_id: str, actor: str, request_id: str, outcome: CommandOutcome,
) -> None:
    """Leave one command's answer where the page that sent it can collect it."""
    stored = _cache_set(
        _command_outcome_key(pipeline_id, actor, request_id),
        {
            "state": outcome.state,
            "accepted": outcome.accepted,
            "receipt": outcome.receipt,
            "refusal": outcome.refusal,
        },
        timeout=COMMAND_OUTCOME_SECONDS,
    )
    if not stored:
        # The command was sent and the gateway answered; only the answer was lost. The operator is
        # left to read the result off the queue itself, and may well send the command again, so the
        # answer goes to the log loudly enough to reconstruct what was applied.
        logger.error(
            "DAST queue command answer not stored for pipeline %s request %r: accepted=%s %r",
            pipeline_id, request_id, outcome.accepted, outcome.refusal or outcome.receipt,
        )


# ---------------------------------------------------------------------------
# Worker side: the only code here that talks to a gateway.
# ---------------------------------------------------------------------------


def refresh_snapshot(pipeline: AISTPipeline) -> DastQueueSnapshot:
    """
    Read the run's queue and store what it said. Runs on a worker.

    The whole queue is taken in one page, so the page an operator is looking at can filter, count
    and search without another round trip.
    """
    run_id = _run_id(pipeline)
    integration = _integration(pipeline)
    if not run_id or integration is None:
        return _store_unavailable(pipeline, run_id, _no_run(pipeline))

    snapshot = _snapshot(pipeline)
    known_etag = snapshot.etag if snapshot is not None and snapshot.run_id == run_id else ""
    try:
        read = _read_from_gateway(integration, pipeline, run_id, known_etag, snapshot)
    except DastGatewayClientError as exc:
        if exc.code in UNAVAILABLE_CODES:
            return _store_unavailable(pipeline, run_id, QueueUnavailable(
                exc.code, "This run's queue is no longer available to AIST.",
            ))
        logger.warning("DAST queue refresh failed for pipeline %s: %s", pipeline.id, exc.code)
        raise DastQueueError(exc.code) from exc
    except DastQueueContractError as exc:
        logger.warning("DAST queue payload refused for pipeline %s: %s", pipeline.id, exc.code)
        return _store_unavailable(
            pipeline, run_id,
            QueueUnavailable(exc.code, "The gateway sent a queue this build will not render."),
        )

    if read is None:
        # Unchanged since the stored entity-tag: the snapshot is still what the queue says.
        DastQueueSnapshot.objects.filter(pipeline=pipeline).update(fetched_at=timezone.now())
        return _snapshot(pipeline)
    page, descriptor, etag = read
    stored, _created = DastQueueSnapshot.objects.update_or_create(
        pipeline=pipeline,
        defaults={
            "run_id": run_id,
            "descriptor": descriptor,
            "page": page,
            "etag": etag,
            "descriptor_digest": page.get("descriptor_digest", ""),
            "unavailable_code": "",
            "unavailable_detail": "",
            "source": DastQueueSnapshot.SOURCE_GATEWAY,
            "fetched_at": timezone.now(),
        },
    )
    return stored


def _read_from_gateway(integration, pipeline, run_id, known_etag, snapshot):
    """One page of the queue and the descriptor it renders with, or nothing when unchanged."""
    with scoped_dast_gateway_client(integration, execution_id=f"queue-{pipeline.id}") as client:
        document = client.queue_page(
            run_id, offset=0, limit=SNAPSHOT_PAGE_LIMIT, include_planned=True, etag=known_etag,
        )
        if document.not_modified and snapshot is not None:
            return None
        page = normalise_page(document.payload)
        descriptor = _descriptor(client, run_id, page.get("descriptor_digest", ""), snapshot)
        return page, descriptor, document.etag


def _descriptor(client, run_id: str, digest: str, snapshot: DastQueueSnapshot | None) -> dict[str, Any]:
    """The descriptor for this digest, re-read only when the page reports a different one."""
    if snapshot is not None and snapshot.descriptor and snapshot.descriptor_digest == digest:
        return snapshot.descriptor
    return normalise_descriptor(client.queue_descriptor(run_id).payload)


def _store_unavailable(pipeline: AISTPipeline, run_id: str, reason: QueueUnavailable) -> DastQueueSnapshot:
    stored, _created = DastQueueSnapshot.objects.update_or_create(
        pipeline=pipeline,
        defaults={
            "run_id": run_id,
            "descriptor": {},
            "page": {},
            "etag": "",
            "descriptor_digest": "",
            "unavailable_code": reason.code,
            "unavailable_detail": reason.detail,
            "source": DastQueueSnapshot.SOURCE_GATEWAY,
            "fetched_at": timezone.now(),
        },
    )
    return stored


def apply_command(
    pipeline: AISTPipeline,
    *,
    action: str,
    item_id: str,
    values: dict[str, Any],
    request_id: str,
    expected_revision: int,
    actor: str,
) -> CommandOutcome:
    """
    Send one named command to the address the gateway published for it. Runs on a worker.

    The browser named the action and the item; everything else about the request -- its method, its
    address, which fields may travel -- comes from the affordance the gateway itself published, read
    fresh for this command.
    """
    run_id = _run_id(pipeline)
    integration = _integration(pipeline)
    if not run_id or integration is None:
        code = "NO_RUN"
        raise DastQueueError(code)
    try:
        return _send_command(
            pipeline, integration, run_id,
            action=action, item_id=item_id, values=values,
            request_id=request_id, expected_revision=expected_revision, actor=actor,
        )
    except DastGatewayClientError as exc:
        raise DastQueueError(exc.code) from exc
    except DastQueueContractError as exc:
        logger.warning("DAST queue command refused for pipeline %s: %s", pipeline.id, exc.code)
        raise DastQueueError(exc.code, exc.detail) from exc


def _send_command(
    pipeline: AISTPipeline,
    integration: OrgIntegration,
    run_id: str,
    *,
    action: str,
    item_id: str,
    values: dict[str, Any],
    request_id: str,
    expected_revision: int,
    actor: str,
) -> CommandOutcome:
    with scoped_dast_gateway_client(integration, execution_id=f"queue-cmd-{pipeline.id}") as client:
        descriptor = normalise_descriptor(client.queue_descriptor(run_id).payload)
        if item_id:
            document = client.queue_item(run_id, item_id)
            items = normalise_page(document.payload).get("items", [])
            affordance = next(
                (find_affordance(actions=row.get("actions", []), name=action)
                 for row in items if row.get("item_id") == item_id),
                None,
            )
        else:
            affordance = find_affordance(actions=descriptor.get("run_actions", []), name=action)

        if affordance is not None and not href_addresses_run(affordance["href"], run_id):
            # A payload for this pipeline advertising another run's address is not something to
            # forward: the caller was authorized for this pipeline alone.
            logger.warning(
                "DAST queue affordance for pipeline %s addresses another run: %s",
                pipeline.id, affordance["href"],
            )
            affordance = None
        if affordance is None:
            # Either the run never offered it, this caller's capabilities do not cover it, or the
            # queue moved on. All three are the same answer: not something we may send.
            code = "QUEUE_ACTION_NOT_OFFERED"
            raise DastQueueError(code, f"{action!r} is not offered for this item")

        submitted = submitted_values(affordance, values or {}, enums=descriptor.get("enums", {}))
        body: dict[str, Any] = {"request_id": request_id, "expected_revision": expected_revision}
        changes: dict[str, Any] = {}
        for name, value in submitted.items():
            if name in MUTATION_TOP_LEVEL_FIELDS:
                body[name] = value
            else:
                changes[name] = value
        if changes:
            body["changes"] = changes

        result = client.queue_command(
            run_id=run_id, method=affordance["method"], href=affordance["href"], body=body,
        )

    if result.accepted:
        logger.info(
            # %r on the browser-supplied action: a newline in it would otherwise forge a log line.
            "DAST queue command %r applied on pipeline %s item %s by %s",
            action, pipeline.id, item_id or "-", actor,
        )
        return CommandOutcome(state="done", accepted=True, receipt=normalise_receipt(result.payload))
    return CommandOutcome(state="done", accepted=False, refusal=normalise_refusal(result.payload))
