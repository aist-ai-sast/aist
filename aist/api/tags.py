from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count
from dojo.models import Finding
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.response import Response

from aist.api.schema import AISTApiTag
from aist.api.throttling import ProjectScopedRateThrottle, SharedScopedRateThrottle
from aist.authz import Action, AISTAPIView, ResourcePolicy, queryset_for_action
from aist.models import AISTProject

# Tags whose name starts with this prefix are written by the platform itself.
# A client may neither add nor remove them; a write silently keeps the ones
# already on the object.
SYSTEM_TAG_PREFIX = "aist:"
MAX_TAG_LENGTH = 64
MAX_TAGS_PER_OBJECT = 50

# The available-tags response is cached per (user, scope). Any tag write bumps
# this version so a tag created on one object shows up in everyone's picker
# immediately instead of after the 300s TTL.
_TAGS_VERSION_KEY = "aist_tags_vocabulary_version"


def tags_vocabulary_version() -> int:
    return cache.get(_TAGS_VERSION_KEY) or 1


def bump_tags_vocabulary_version() -> None:
    cache.set(_TAGS_VERSION_KEY, tags_vocabulary_version() + 1, None)


def normalize_tag(raw: str) -> str:
    """Apply the same shape the model would force anyway (``force_lowercase``)."""
    return " ".join(str(raw).strip().lower().split()).replace(" ", "-")


class TagWriteSerializer(serializers.Serializer):

    """
    The whole tag list a client wants on an object, system tags excluded.

    A tag the object already carries is passed through untouched. Data imported
    by a scanner can break these rules — a name longer than the limit, or more
    tags than the cap — and refusing the write would leave the user unable to
    remove the very tag that is refused.
    """

    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=True, trim_whitespace=False, max_length=MAX_TAG_LENGTH * 4),
        allow_empty=True,
        max_length=MAX_TAGS_PER_OBJECT * 4,
    )

    def validate_tags(self, value: list[str]) -> list[str]:
        existing = set(self.context.get("existing") or ())
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            if raw in existing:
                if raw not in seen:
                    seen.add(raw)
                    normalized.append(raw)
                continue
            if "," in raw:
                msg = "A tag may not contain a comma — it separates tags."
                raise serializers.ValidationError(msg)
            tag = normalize_tag(raw)
            if not tag:
                msg = "A tag may not be empty."
                raise serializers.ValidationError(msg)
            if tag.startswith(SYSTEM_TAG_PREFIX):
                msg = f"The {SYSTEM_TAG_PREFIX} prefix is reserved for the platform."
                raise serializers.ValidationError(msg)
            if len(tag) > MAX_TAG_LENGTH:
                msg = f"A tag may not be longer than {MAX_TAG_LENGTH} characters."
                raise serializers.ValidationError(msg)
            if tag not in seen:
                seen.add(tag)
                normalized.append(tag)
        if len(normalized) > MAX_TAGS_PER_OBJECT and len(normalized) > len(existing):
            msg = f"At most {MAX_TAGS_PER_OBJECT} tags may be set on one object."
            raise serializers.ValidationError(msg)
        return normalized


def current_tags(obj) -> list[str]:
    return sorted(tag.name for tag in obj.tags.all())


def inherited_tags(obj) -> list[str]:
    """Tags ``obj`` carries because its product carries them; empty for a product."""
    relation = getattr(obj, "inherited_tags", None)
    if relation is None:
        return []
    return sorted(tag.name for tag in relation.all())


def uneditable_tags(obj) -> list[str]:
    """The ``aist:`` and inherited tags of ``obj``: preserved on every write."""
    system = [name for name in current_tags(obj) if name.lower().startswith(SYSTEM_TAG_PREFIX)]
    return sorted(set(system + inherited_tags(obj)))


def replace_tags(obj, submitted: list[str]) -> list[str]:
    """
    Replace the editable tags of ``obj``, keeping its uneditable ones.

    The row lock serialises concurrent client writes only; the platform's tag
    propagation takes no such lock.
    """
    with transaction.atomic():
        locked = type(obj).objects.select_for_update().get(pk=obj.pk)
        before = current_tags(locked)
        after = sorted(set(uneditable_tags(locked) + submitted))
        if after != before:
            locked.tags.set(after)
            transaction.on_commit(bump_tags_vocabulary_version)
        return after


_TAGS_RESPONSE = inline_serializer(
    name="ObjectTagsResponse",
    fields={
        "tags": serializers.ListField(child=serializers.CharField()),
        "project_tags": serializers.ListField(child=serializers.CharField()),
    },
)


class AvailableFindingTagsAPI(AISTAPIView):
    # Read-only; write action is a fail-secure default (GET only).
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    class QuerySerializer(serializers.Serializer):
        project_id = serializers.PrimaryKeyRelatedField(
            queryset=AISTProject.objects.none(),
            required=False,
        )

        def get_fields(self):
            fields = super().get_fields()
            request = self.context.get("request")
            if request and getattr(request, "user", None) and request.user.is_authenticated:
                fields["project_id"].queryset = queryset_for_action(
                    resource=AISTProject,
                    action=Action.PRODUCT_READ,
                    user=request.user,
                )
            return fields

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="List available finding tags",
        parameters=[OpenApiParameter(name="project_id", required=False, type=int)],
        responses={
            200: inline_serializer(
                name="AvailableFindingTagsResponse",
                fields={
                    "tags": serializers.ListField(child=serializers.CharField()),
                    "counts": serializers.DictField(child=serializers.IntegerField()),
                },
            ),
        },
    )
    def get(self, request):
        query_serializer = self.QuerySerializer(data=request.query_params, context={"request": request})
        query_serializer.is_valid(raise_exception=True)
        project = query_serializer.validated_data.get("project_id")
        project_id = project.id if project else None
        cache_key = (
            f"aist_findings_tags_v3_{tags_vocabulary_version()}"
            f"_{request.user.id}_{project_id or 'all'}"
        )
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        findings = queryset_for_action(
            resource=Finding,
            action=Action.FINDING_READ,
            user=request.user,
        )
        if project:
            findings = findings.filter(test__engagement__product_id=project.product_id)
        # Facet counts: findings per tag inside the same (user, project) scope.
        rows = (
            findings.exclude(tags__name__isnull=True)
            .exclude(tags__name__exact="")
            .values("tags__name")
            .annotate(count=Count("id", distinct=True))
            .order_by("tags__name")
        )
        payload = {
            "tags": [row["tags__name"] for row in rows],
            "counts": {row["tags__name"]: row["count"] for row in rows},
        }
        cache.set(cache_key, payload, 300)
        return Response(payload)


class AISTFindingTagsAPI(AISTAPIView):

    """Replace the tags of one finding. Writer and above (``Finding_Edit``)."""

    throttle_scope = "aist_finding_tags"
    throttle_classes = [SharedScopedRateThrottle]

    authz = ResourcePolicy(resource=Finding, read=Action.FINDING_READ, write=Action.FINDING_EDIT)

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Replace the tags of a finding",
        request=TagWriteSerializer,
        responses={
            200: _TAGS_RESPONSE,
            429: OpenApiResponse(description="Editing tags is rate limited"),
        },
    )
    def put(self, request, finding_id: int):
        finding = self.resolve(id=finding_id)
        serializer = TagWriteSerializer(data=request.data, context={"existing": current_tags(finding)})
        serializer.is_valid(raise_exception=True)
        tags = replace_tags(finding, serializer.validated_data["tags"])
        return Response({"tags": tags, "project_tags": inherited_tags(finding)})


class AISTProjectTagsAPI(AISTAPIView):

    """
    Replace the tags of a project. Maintainer and above (``Product_Edit``).

    The tags shown on a project card are the tags of its ``Product`` — that is
    what ``AISTProductSummaryAPI`` reads — so this writes there and not to a
    field of its own.
    """

    throttle_scope = "aist_project_tags"
    throttle_classes = [ProjectScopedRateThrottle]

    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.PROJECTS.value],
        summary="Replace the tags of a project",
        request=TagWriteSerializer,
        responses={
            200: _TAGS_RESPONSE,
            429: OpenApiResponse(description="Editing project tags is rate limited"),
        },
    )
    def put(self, request, project_id: int):
        project = self.resolve(id=project_id)
        serializer = TagWriteSerializer(data=request.data, context={"existing": current_tags(project.product)})
        serializer.is_valid(raise_exception=True)
        # A product owns its tags, so nothing is inherited; the key keeps both endpoints one shape.
        return Response({"tags": replace_tags(project.product, serializer.validated_data["tags"]), "project_tags": []})
