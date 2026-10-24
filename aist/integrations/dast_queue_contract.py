"""
The envelope AIST accepts from the DAST gateway's queue surface.

The queue is deliberately self-describing: the gateway sends the vocabulary, the per-item
affordances and the form fields, and AIST renders them without knowing what a status, a wait reason
or a skill means. That inversion moves the trust boundary rather than removing it -- the payload is
tenant-supplied data driving a superuser-facing page, so it is bounded and shape-checked here, once,
before anything renders it or acts on it.

The shapes are declared as serializers because that is how everything else in ``aist`` states a
payload contract: lengths and list sizes are bounds, not assertions sprinkled through the reader, and
an unknown key is dropped rather than refused, which is what keeps an additive contract change on the
gateway side from breaking this page.

Two refusals here are load-bearing rather than hygienic:

- an address that is not relative to the gateway's own ``/integrations/v2/`` prefix, so a descriptor
  can never aim AIST's own outbound request at another host;
- a form field whose name reads like a credential, because the queue has no business asking AIST to
  collect one.

What it deliberately does not refuse: an unknown vocabulary id, action name or icon token. Those are
additive by contract -- the renderer shows the raw id, and a field type it cannot render it shows
read-only -- and refusing them would reintroduce the coupling this surface exists to remove.
"""

from __future__ import annotations

import logging
from typing import Any

from rest_framework import serializers

logger = logging.getLogger(__name__)

# Addresses arrive relative to this prefix (`runs/<run id>/queue/...`) and are joined onto the
# configured gateway URL. Anything that could leave that subtree is refused rather than rewritten.
GATEWAY_API_PREFIX = "/integrations/v2/"

MAX_PAYLOAD_ITEMS = 500
MAX_ACTIONS_PER_ITEM = 8
MAX_FIELDS_PER_ACTION = 32
MAX_ENUM_VALUES = 1000
MAX_VOCABULARY_ENTRIES = 128
MAX_HISTORY_ROWS = 200
MAX_WORK_SUBJECTS = 64
MAX_TEXT = 64 * 1024
MAX_ID = 256

#: Form controls this build knows how to render. A field of any other type is kept and shown
#: read-only, so a type the gateway adds later degrades instead of disappearing.
RENDERABLE_FIELD_TYPES = frozenset({"text", "enum", "bool", "int"})

#: Methods the gateway may ask AIST to send. Anything else is a contract error, not a new feature.
ALLOWED_METHODS = ("GET", "POST", "PATCH", "DELETE")

#: `QueueMutation`'s own fields; everything else a form declares travels under `changes`.
MUTATION_TOP_LEVEL_FIELDS = frozenset({"request_id", "expected_revision", "reason", "comment"})

#: A form field named like this would mean the queue is asking AIST to collect a credential.
_CREDENTIAL_FIELD_HINTS = ("password", "secret", "token", "credential", "passphrase", "private_key")


class DastQueueContractError(ValueError):

    """A queue payload AIST will not render. ``code`` is stable and safe to show."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def validate_relative_href(value: str) -> str:
    """
    A gateway-issued address, proven to stay inside ``/integrations/v2/``.

    The affordance contract only works while AIST calls back addresses it was given instead of
    composing them, and that is only safe while the address cannot leave the gateway. An absolute
    URL, a scheme-relative or root-relative one, a traversal, or anything carrying a query or a
    fragment is refused here; the caller joins what survives onto the configured gateway URL.
    """
    href = (value or "").strip()
    lowered = href.lower()
    if not href:
        msg = "href is empty"
        raise serializers.ValidationError(msg)
    if "://" in lowered or lowered.startswith(("//", "/", "\\")):
        msg = f"href must be relative to {GATEWAY_API_PREFIX}"
        raise serializers.ValidationError(msg)
    if any(segment in {"", ".", ".."} for segment in href.split("/")):
        msg = "href must not traverse or hold an empty segment"
        raise serializers.ValidationError(msg)
    if any(character in href for character in "?#\n\r\t "):
        msg = "href must be a plain path"
        raise serializers.ValidationError(msg)
    if "%" in href:
        # `requests` decodes percent-escapes while preparing the URL, so `%2e%2e` would arrive at
        # the gateway as `..` and leave this prefix after the server normalises it. A gateway
        # address names run and item ids from a plain alphabet and never needs an escape, so the
        # escape itself is the refusal rather than a decode-then-check dance.
        msg = "href must not contain percent-escapes"
        raise serializers.ValidationError(msg)
    if not href.startswith("runs/"):
        msg = "href must address a run"
        raise serializers.ValidationError(msg)
    return href


def validate_form_field_name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        msg = "field name is empty"
        raise serializers.ValidationError(msg)
    if any(hint in name.lower() for hint in _CREDENTIAL_FIELD_HINTS):
        msg = f"field {name!r} asks for a credential"
        raise serializers.ValidationError(msg)
    return name


class BoundedTextField(serializers.CharField):

    """
    Free text that is cut to its bound instead of refusing the payload it arrived in.

    The bounds here exist so one page cannot exhaust a worker, not to police wording. An objective or
    a verdict's conclusion that came out longer than expected is still worth showing: truncating it
    costs the operator a few words, while refusing it would blank the whole queue because somebody
    wrote a long sentence. Ids are the exception and stay strict -- a truncated id addresses the
    wrong thing -- so they are declared with `max_length` as usual.
    """

    def __init__(self, limit: int = MAX_TEXT, **kwargs: Any):
        self.limit = limit
        kwargs.setdefault("allow_blank", True)
        kwargs.setdefault("required", False)
        kwargs.setdefault("default", "")
        kwargs.setdefault("trim_whitespace", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> str:
        return super().to_internal_value(data)[: self.limit]


def _text(limit: int = MAX_TEXT, **kwargs: Any) -> serializers.CharField:
    """Optional free text, cut to ``limit``. Absent and empty are the same thing in this payload."""
    return BoundedTextField(limit, **kwargs)


def _ids(limit: int) -> serializers.ListField:
    return serializers.ListField(
        child=serializers.CharField(max_length=MAX_ID, allow_blank=True, trim_whitespace=False),
        max_length=limit, required=False, default=list,
    )


def _count(**kwargs: Any) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=0, required=False, default=0, **kwargs)


def _each(
    serializer_class: type[serializers.Serializer], rows: list[Any], label: str,
) -> tuple[list[Any], int]:
    """
    Validate rows one at a time, keeping the ones this build can read.

    A page is a list of independent things. Refusing all of them because one carries a shape this
    build has never seen would turn an additive change on the gateway side into a blank screen,
    which is the failure this whole surface exists to avoid. The count of what was left out travels
    with the result, because showing 499 of 500 rows without saying so would be worse.
    """
    kept, dropped = [], 0
    for row in rows:
        serializer = serializer_class(data=row) if isinstance(row, dict) else None
        if serializer is not None and serializer.is_valid():
            kept.append(serializer.validated_data)
        else:
            dropped += 1
            logger.warning(
                "DAST queue: dropped one %s this build cannot read: %s",
                label, "not an object" if serializer is None else str(serializer.errors)[:200],
            )
    return kept, dropped


class EnumValueSerializer(serializers.Serializer):
    id = _text(MAX_ID)
    title = _text(512)


class ActionFieldSerializer(serializers.Serializer):

    """One control a form offers, exactly as the gateway described it."""

    name = serializers.CharField(max_length=64, validators=[validate_form_field_name])
    type = _text(32)
    required = serializers.BooleanField(required=False, default=False)
    read_only = serializers.BooleanField(required=False, default=False)
    max_length = _count()
    values_from = _text(MAX_ID)
    help = _text(4096)
    values = EnumValueSerializer(many=True, required=False, default=list)
    # A value can be text, a number, a flag or a list of ids, so it is carried through as-is and
    # rendered by type rather than re-typed here.
    value = serializers.JSONField(required=False, default="")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # `renderable` says whether this build has a control for the declared type. A field it
        # cannot render is shown read-only rather than dropped, so a newer type still reaches the
        # operator instead of vanishing from the form.
        attrs["renderable"] = bool(attrs["read_only"] or attrs["type"] in RENDERABLE_FIELD_TYPES)
        return attrs


class AffordanceSerializer(serializers.Serializer):

    """What may be done to one item, with the address and the exact fields it takes."""

    name = _text(MAX_ID)
    method = serializers.ChoiceField(choices=ALLOWED_METHODS)
    href = serializers.CharField(max_length=1024, validators=[validate_relative_href])
    fields = ActionFieldSerializer(many=True, required=False, default=list)

    def validate_fields(self, value: list[Any]) -> list[Any]:
        if len(value) > MAX_FIELDS_PER_ACTION:
            msg = f"more than {MAX_FIELDS_PER_ACTION} fields"
            raise serializers.ValidationError(msg)
        return value


class VocabularyEntrySerializer(serializers.Serializer):

    """One id's wording: how it reads, what it means, and which generic icon stands for it."""

    id = _text(MAX_ID)
    label = _text(512)
    help = _text(4096)
    icon = _text(MAX_ID)
    severity = _text(32)
    group = _text(32)


class ClientHintsSerializer(serializers.Serializer):

    """
    How often the gateway wants to be asked, and how big a page it will serve.

    Both are clamped rather than range-checked. The gateway is entitled to change its own cadence,
    and a value outside what this page can usefully do is not a reason to refuse the descriptor and
    leave the operator with nothing -- it is a reason to poll at the nearest cadence that works.
    """

    poll_interval_seconds = serializers.IntegerField(min_value=0, required=False, default=60)
    max_items_per_page = serializers.IntegerField(min_value=1, required=False, default=100)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs["poll_interval_seconds"] = min(max(attrs["poll_interval_seconds"] or 60, 15), 600)
        attrs["max_items_per_page"] = min(attrs["max_items_per_page"], MAX_PAYLOAD_ITEMS)
        return attrs


class ExecutionFactsSerializer(serializers.Serializer):
    claim_statuses = _ids(32)
    parallel_contracts = _ids(32)
    serial_note = _text(4096)
    parallel_note = _text(4096)
    session_capacity = _count()


class MutationEnvelopeSerializer(serializers.Serializer):
    fields = ActionFieldSerializer(many=True, required=False, default=list)


class ActionCatalogEntrySerializer(serializers.Serializer):
    title = _text(256)
    verb = _text(256)
    short_verb = _text(64)
    icon = _text(MAX_ID)
    danger = serializers.BooleanField(required=False, default=False)
    effect = _text(8192)


class QueueDescriptorSerializer(serializers.Serializer):

    """The run's description of its own queue."""

    contract_version = _text(32)
    descriptor_digest = _text(128)
    run_state = _text(MAX_ID)
    capabilities = _ids(32)
    client_hints = ClientHintsSerializer(required=False, default=dict)
    mutation_envelope = MutationEnvelopeSerializer(required=False, default=dict)
    execution = ExecutionFactsSerializer(required=False, default=dict)
    # Dimension names and enum keys are the gateway's to choose, so both are dictionaries whose
    # keys travel as data rather than as declared fields.
    vocabulary = serializers.DictField(
        child=VocabularyEntrySerializer(many=True), required=False, default=dict,
    )
    enums = serializers.DictField(child=EnumValueSerializer(many=True), required=False, default=dict)
    run_actions = AffordanceSerializer(many=True, required=False, default=list)
    action_catalog = serializers.DictField(child=ActionCatalogEntrySerializer(), required=False, default=dict)

    def validate_vocabulary(self, value: dict[str, Any]) -> dict[str, Any]:
        for dimension, entries in value.items():
            if len(entries) > MAX_VOCABULARY_ENTRIES:
                msg = f"{dimension} holds more than {MAX_VOCABULARY_ENTRIES} entries"
                raise serializers.ValidationError(msg)
        return value

    def validate_enums(self, value: dict[str, Any]) -> dict[str, Any]:
        for key, entries in value.items():
            if len(entries) > MAX_ENUM_VALUES:
                msg = f"{key} holds more than {MAX_ENUM_VALUES} values"
                raise serializers.ValidationError(msg)
        return value


class WorkSubjectSerializer(serializers.Serializer):

    """A result this work owes, recorded, or was raised from."""

    role = _text(MAX_ID)
    verdict_id = _text(MAX_ID)
    item = _text(2048)
    target = _text(1024)
    status = _text(MAX_ID)
    conclusion = _text(8192)


class QueueItemSerializer(serializers.Serializer):

    """One queue row: a job, work the run owes, a pause, or a lead it would not admit."""

    item_id = _text(MAX_ID)
    type = _text(MAX_ID)
    status = _text(MAX_ID)
    wait_reason = _text(MAX_ID)
    phase = _text(MAX_ID)
    purpose = _text(MAX_ID)
    agent_contract = _text(MAX_ID)
    origin = _text(MAX_ID)
    executor = _text(MAX_ID)
    queue_name = _text(MAX_ID)
    queue_item = _text(4096)
    objective = _text()
    operator_note = _text()
    skill = _text(MAX_ID)
    scope = _ids(128)
    inputs = _ids(128)
    plan_rows = _ids(128)
    required_outputs = _ids(64)
    concurrency_key = _text(MAX_ID)
    parent_job_id = _text(MAX_ID)
    dependent_jobs = _ids(128)
    lead_depth = _count()
    deferred_from_run = _text(MAX_ID)
    disposition = _text(MAX_ID)
    disposition_reason = _text()
    attempt = _count()
    futile_attempts = _count()
    publication_failures = _count()
    session_count = _count()
    not_before = _text(64)
    updated_at = _text(64)
    created_at = _text(64)
    released_at = _text(64)
    actor = _text(256)
    reason = _text()
    work_subjects = WorkSubjectSerializer(many=True, required=False, default=list)
    # Validated one at a time below: an action this build will not send must cost that one control,
    # not the row it sits on.
    actions = serializers.ListField(child=serializers.JSONField(), required=False, default=list)

    def validate_actions(self, value: list[Any]) -> list[Any]:
        if len(value) > MAX_ACTIONS_PER_ITEM:
            msg = f"more than {MAX_ACTIONS_PER_ITEM} actions"
            raise serializers.ValidationError(msg)
        kept, _dropped = _each(AffordanceSerializer, value, "action")
        return kept

    def validate_work_subjects(self, value: list[Any]) -> list[Any]:
        if len(value) > MAX_WORK_SUBJECTS:
            msg = f"more than {MAX_WORK_SUBJECTS} work subjects"
            raise serializers.ValidationError(msg)
        return value


class QueueReceiptSerializer(serializers.Serializer):

    """One operator command as the run recorded it."""

    request_id = _text(MAX_ID)
    actor = _text(256)
    action = _text(MAX_ID)
    item_id = _text(MAX_ID)
    reason = _text()
    created_at = _text(64)
    before_revision = _count()
    revision = _count()
    status = _text(MAX_ID)


class QueueCountsSerializer(serializers.Serializer):
    by_status = serializers.DictField(child=serializers.IntegerField(min_value=0), required=False, default=dict)
    planned = _count()
    total = _count()


class HandoffSerializer(serializers.Serializer):
    handoff_id = _text(MAX_ID)
    state = _text(MAX_ID)
    reserved_run = _text(MAX_ID)
    accepted_job = _text(MAX_ID)
    blocked_reason = _text()


class QueuePageSerializer(serializers.Serializer):

    """One page of the queue."""

    run_id = _text(MAX_ID)
    revision = _count()
    # `state` is the queue's (active / pausing / paused); `run_state` is the run's own lifecycle.
    # A finished run with no operator pause still reports state "active", so the two disagreeing is
    # the normal case and the page must read `run_state` to know the run is over.
    state = _text(MAX_ID)
    run_state = _text(MAX_ID)
    mutations_frozen = serializers.BooleanField(required=False, default=False)
    descriptor_digest = _text(128)
    total = _count()
    uncovered = _count()
    context_revision = _count()
    next_offset = serializers.IntegerField(min_value=0, required=False, allow_null=True, default=None)
    counts = QueueCountsSerializer(required=False, default=dict)
    # Each row is validated on its own, so a row shaped in a way this build cannot read costs that
    # row rather than the whole queue. `dropped_rows` says how many were left out, because silently
    # showing 499 of 500 items would be worse than saying so.
    items = serializers.ListField(child=serializers.JSONField(), required=False, default=list)
    history = serializers.ListField(child=serializers.JSONField(), required=False, default=list)
    handoffs = serializers.ListField(child=serializers.JSONField(), required=False, default=list)

    def validate_items(self, value: list[Any]) -> list[Any]:
        if len(value) > MAX_PAYLOAD_ITEMS:
            msg = f"more than {MAX_PAYLOAD_ITEMS} items"
            raise serializers.ValidationError(msg)
        return value

    def validate_history(self, value: list[Any]) -> list[Any]:
        if len(value) > MAX_HISTORY_ROWS:
            msg = f"more than {MAX_HISTORY_ROWS} history rows"
            raise serializers.ValidationError(msg)
        return value

    def validate_handoffs(self, value: list[Any]) -> list[Any]:
        if len(value) > MAX_HISTORY_ROWS:
            msg = f"more than {MAX_HISTORY_ROWS} handoffs"
            raise serializers.ValidationError(msg)
        return value


class QueueRefusalSerializer(serializers.Serializer):

    """A typed refusal, kept whole so a form can point at the field that caused it."""

    code = _text(MAX_ID)
    message = _text(4096)
    retryable = serializers.BooleanField(required=False, default=False)
    field = _text(MAX_ID)
    allowed = _ids(MAX_ENUM_VALUES)


def _validated(serializer_class: type[serializers.Serializer], payload: Any, code: str) -> dict[str, Any]:
    serializer = serializer_class(data=payload if isinstance(payload, dict) else {})
    if not isinstance(payload, dict) or not serializer.is_valid():
        detail = "payload is not a JSON object" if not isinstance(payload, dict) else str(serializer.errors)[:400]
        raise DastQueueContractError(code, detail)
    return serializer.validated_data


def normalise_descriptor(payload: Any) -> dict[str, Any]:
    return _validated(QueueDescriptorSerializer, payload, "QUEUE_DESCRIPTOR_INVALID")


def normalise_page(payload: Any) -> dict[str, Any]:
    """
    One page: its envelope validated as a whole, its rows one at a time.

    The envelope carries the revision a command is decided against, so a page whose envelope is
    unreadable is refused. The rows are independent, so each is kept or dropped on its own and
    ``dropped_rows`` reports how many this build could not read.
    """
    page = _validated(QueuePageSerializer, payload, "QUEUE_PAGE_INVALID")
    items, dropped = _each(QueueItemSerializer, page["items"], "item")
    history, _ = _each(QueueReceiptSerializer, page["history"], "receipt")
    handoffs, _ = _each(HandoffSerializer, page["handoffs"], "handoff")
    page["items"] = items
    page["history"] = history
    page["handoffs"] = handoffs
    page["dropped_rows"] = dropped
    return page


def normalise_receipt(payload: Any) -> dict[str, Any]:
    return _validated(QueueReceiptSerializer, payload, "QUEUE_RECEIPT_INVALID")


def normalise_refusal(payload: Any) -> dict[str, Any]:
    return _validated(QueueRefusalSerializer, payload, "QUEUE_REFUSAL_INVALID")


def href_addresses_run(href: str, run_id: str) -> bool:
    """
    Whether this address stays inside the run the caller was authorized for.

    A payload for one pipeline's run must not aim a command at another run on the same gateway:
    the caller passed AIST's check for this pipeline only, and the other run may belong to a project
    it may not operate.
    """
    return bool(run_id) and href.startswith(f"runs/{run_id}/")


def find_affordance(*, actions: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """
    The action this item advertises under ``name``, or nothing.

    A command never carries an address from the browser: the caller re-reads the item and takes the
    method and href from what the gateway published for it. A name the gateway did not offer has no
    address here, which is how an action nobody was granted stays unreachable.
    """
    for action in actions:
        if action.get("name") == name:
            return action
    return None


def submitted_values(
    action: dict[str, Any],
    values: dict[str, Any],
    *,
    enums: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    The subset of ``values`` this action declared writable, validated against its own fields.

    Anything else the browser sent is dropped rather than forwarded: the gateway would refuse a field
    it never offered, and forwarding it would make AIST the author of a request it cannot justify.
    The declared fields are turned into a serializer, so the lengths, types and options the gateway
    stated are the ones enforced, instead of a second rule-set written here. ``enums`` resolves a
    field that names its options by key (``values_from``) rather than carrying them inline.
    """
    declared = {
        field["name"]: field
        for field in action.get("fields", [])
        if not field.get("read_only") and field.get("renderable")
    }
    form: dict[str, Any] = {}
    for name, field in declared.items():
        required = bool(field.get("required"))
        optional: dict[str, Any] = {"required": True} if required else {"required": False, "default": ""}
        if field["type"] == "bool":
            form[name] = serializers.BooleanField(required=required, **({} if required else {"default": False}))
            continue
        if field["type"] == "int":
            form[name] = serializers.IntegerField(
                min_value=0, required=required, **({} if required else {"default": 0}),
            )
            continue
        options = field.get("values") or (enums or {}).get(field.get("values_from")) or []
        if field["type"] == "enum" and options:
            form[name] = serializers.ChoiceField(
                choices=[entry["id"] for entry in options], allow_blank=not required, **optional,
            )
            continue
        form[name] = serializers.CharField(
            max_length=field.get("max_length") or MAX_TEXT,
            allow_blank=not required,
            trim_whitespace=False,
            **optional,
        )
    serializer = type("SubmittedValues", (serializers.Serializer,), form)(
        data={name: values[name] for name in declared if name in values},
    )
    if not serializer.is_valid():
        code = "QUEUE_FIELD_INVALID"
        raise DastQueueContractError(code, str(serializer.errors)[:400])
    return {
        name: value
        for name, value in serializer.validated_data.items()
        if value or declared[name]["required"]
    }
