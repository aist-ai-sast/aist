from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from dojo.models import Product, SLA_Configuration
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from aist.api.project_versions import (
    SCRIPT_SOURCE_PROJECT_REVISION,
    SCRIPT_SOURCE_SHARED_DEFAULT,
    SCRIPT_SOURCE_VERSION,
    _serialize_version_script,
)
from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAPIView, AISTAuthzMixin, ResourcePolicy, queryset_for_action
from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT
from aist.integrations.claude import claude_auth_env
from aist.integrations.resolver import resolve_integration
from aist.models import AISTProject, AISTProjectScript, Organization, OrgIntegrationType
from aist.profile import ProjectProfile
from aist.utils.pipeline_imports import _load_analyzers_config

# Script content hard cap: 256 KB is more than enough for any real entrypoint script.
_SCRIPT_MAX_BYTES = 256 * 1024


def _create_initial_script(project: AISTProject, content: str, user=None) -> AISTProjectScript:
    """
    Create a project-scoped script revision for a newly created project.

    The script is deduplicated by sha256 within the project.  It is stored as
    a project revision (script.project=project) and will be used as the default
    when the first AISTProjectVersion is created for this project.
    """
    content_stripped = (content or "").strip() or DEFAULT_ENTRYPOINT_SCRIPT
    script, _ = AISTProjectScript.get_or_create_for_project(
        content=content_stripped,
        project=project,
        user=user,
    )
    return script


class AISTProjectSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_id = serializers.IntegerField(source="product.id", read_only=True)
    organization_id = serializers.IntegerField(read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True, allow_null=True)

    class Meta:
        model = AISTProject
        fields = [
            "id",
            "product_id",
            "product_name",
            "supported_languages",
            "compilable",
            "organization_id",
            "organization_name",
            "created",
            "updated",
            "repository",
        ]


class AISTProjectScriptSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, allow_null=True,
    )

    class Meta:
        model = AISTProjectScript
        fields = ["id", "sha256", "created_at", "created_by_id", "created_by_username"]


class AISTProjectScriptContentSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, allow_null=True,
    )

    class Meta:
        model = AISTProjectScript
        fields = ["id", "content", "sha256", "is_shared", "created_at", "created_by_id", "created_by_username"]


def _validate_script_content(value: str) -> str:
    """Shared validator: size cap only. Shellcheck is advisory — see validate_with_shellcheck."""
    if len(value.encode()) > _SCRIPT_MAX_BYTES:
        msg = f"Script content must not exceed {_SCRIPT_MAX_BYTES // 1024} KB."
        raise serializers.ValidationError(msg)
    return value


class AISTProjectScriptCreateSerializer(serializers.Serializer):
    content = serializers.CharField(allow_blank=False)
    set_active = serializers.BooleanField(default=True)
    scope = serializers.ChoiceField(
        choices=["local", "global"],
        default="local",
        help_text=(
            "'local' creates a project-specific revision (only this project is affected). "
            "'global' updates the shared default script in-place (all projects using the "
            "shared default see the change immediately)."
        ),
    )

    def validate_content(self, value: str) -> str:
        return _validate_script_content(value)


class AISTProjectCreateRequestSerializer(serializers.Serializer):
    organization_id = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.none(),
    )
    product_name = serializers.CharField(allow_blank=False, trim_whitespace=True)
    # Optional: if omitted or blank the DEFAULT_ENTRYPOINT_SCRIPT is used.
    script_content = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Shell script content for the entrypoint. Defaults to the standard pipeline script.",
    )
    compilable = serializers.BooleanField(required=False, default=False)

    def validate_script_content(self, value: str) -> str:
        if value.strip():
            return _validate_script_content(value)
        return value
    supported_languages = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    profile = serializers.JSONField(required=False, default=dict)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["organization_id"].queryset = queryset_for_action(
                resource=Organization,
                action=Action.PROJECT_CREATE,
                user=request.user,
            )
        return fields

    def validate_profile(self, value):
        if not value:
            return {}
        try:
            ProjectProfile.validate_dict(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class AISTProjectCreateResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    project = AISTProjectSerializer()


class DefaultAnalyzersRequestSerializer(serializers.Serializer):
    project = serializers.IntegerField(required=False)
    time_class_level = serializers.CharField(required=False)
    languages = serializers.ListField(child=serializers.CharField(), required=False)


class ProjectUpdateRequestSerializer(serializers.Serializer):
    supported_languages = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    compilable = serializers.BooleanField(required=False, default=False)
    profile = serializers.JSONField(required=False, default=dict)

    def to_internal_value(self, data):
        if "organization" in data:
            raise serializers.ValidationError({
                "organization": "Project organization is read-only and derived from its product type.",
            })
        mutable = data.copy()
        raw_languages = mutable.get("supported_languages")
        if isinstance(raw_languages, str):
            parsed_languages = [token.strip() for token in raw_languages.split(",") if token.strip()]
            if hasattr(mutable, "setlist"):
                mutable.setlist("supported_languages", parsed_languages)
            else:
                mutable["supported_languages"] = parsed_languages
        return super().to_internal_value(mutable)

    def validate_profile(self, value):
        if not value:
            return {}
        try:
            ProjectProfile.validate_dict(value)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class AISTProjectListAPI(AISTAuthzMixin, generics.ListAPIView):

    """List all current AISTProjects."""

    serializer_class = AISTProjectSerializer
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.PROJECTS.value],
        summary="List all AISTProjects",
        description="Returns all existing AISTProject records with their metadata.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return (
            self.authorized_queryset_for_request()
            .select_related("product")
            .order_by("created")
        )

    @extend_schema(
        operation_id="aist_projects_create",
        tags=[AISTApiTag.PROJECTS.value],
        request=AISTProjectCreateRequestSerializer,
        responses={
            201: AISTProjectCreateResponseSerializer,
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Forbidden"),
            404: OpenApiResponse(description="Organization not found"),
            409: OpenApiResponse(description="Conflict"),
        },
        summary="Create empty AIST project",
    )
    def post(self, request, *args, **kwargs):
        serializer = AISTProjectCreateRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        org = serializer.validated_data["organization_id"]
        product_type = org.ensure_product_type()

        product_name = serializer.validated_data["product_name"]
        default_sla = SLA_Configuration.objects.order_by("id").first()
        product_defaults = {
            "prod_type": product_type,
            "description": "Created from AIST Projects UI",
        }
        if default_sla is not None:
            product_defaults["sla_configuration"] = default_sla

        product, created_product = Product.objects.get_or_create(
            name=product_name,
            defaults=product_defaults,
        )

        if not created_product:
            self.resolve(action=Action.PROJECT_OPERATE, resource=Product, pk=product.pk)
            if product.prod_type_id != product_type.id:
                msg = "Product already exists in another organization product type."
                return Response({"detail": msg}, status=status.HTTP_409_CONFLICT)

        if AISTProject.objects.filter(product=product).exists():
            msg = "AIST project for this product already exists."
            return Response({"detail": msg}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            project = AISTProject.objects.create(
                product=product,
                compilable=serializer.validated_data["compilable"],
                supported_languages=serializer.validated_data["supported_languages"],
                profile=serializer.validated_data["profile"] or {},
            )
            # Create an initial project-scoped script revision for use when the first version is created.
            script_content = (serializer.validated_data.get("script_content") or "").strip()
            _create_initial_script(project, script_content, user=request.user)

        out = AISTProjectSerializer(project)
        return Response({"ok": True, "project": out.data}, status=status.HTTP_201_CREATED)


class AISTProjectDetailAPI(AISTAuthzMixin, generics.RetrieveDestroyAPIView):
    serializer_class = AISTProjectSerializer
    lookup_field = "id"
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={204: OpenApiResponse(description="AIST project deleted"), 404: OpenApiResponse(description="Not found")},
        tags=[AISTApiTag.PROJECTS.value],
        summary="Delete AIST project",
        description="Deletes the specified AISTProject by id.",
    )
    def delete(self, request, project_id: int, *args, **kwargs) -> Response:
        p = self.resolve(id=project_id)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        responses={404: OpenApiResponse(description="Not found")},
        tags=[AISTApiTag.PROJECTS.value],
        summary="Get AIST project",
        description="Get the specified AISTProject by id.",
    )
    def get(self, request, project_id: int, *args, **kwargs) -> Response:
        project = self.resolve(id=project_id)
        serializer = AISTProjectSerializer(project)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="aist_projects_update",
        request=ProjectUpdateRequestSerializer,
        responses={200: OpenApiResponse(description="Project updated")},
        tags=[AISTApiTag.PROJECTS.value],
    )
    def post(self, request, project_id: int, *args, **kwargs) -> Response:
        project = self.resolve(id=project_id)
        serializer = ProjectUpdateRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        payload, errors = update_project_from_payload(project=project, payload=serializer.validated_data)
        if errors:
            return Response({"ok": False, "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True, "project": payload})


class AISTProjectRegenerateAnalysisAPI(AISTAPIView):

    """
    Manually re-trigger the Claude-based init-script + exclusion-profile
    generation for an existing project.

    Reuses the exact same Celery task (``analyze_project_after_import``) that
    otherwise only runs once, automatically, at SCM import time when
    ``auto_analyze=True`` — so a project can be regenerated on demand at any
    later point (e.g. after fixing a broken SCM credential, or after the repo
    has changed significantly).
    """

    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=None,
        responses={
            202: {"type": "object", "properties": {"queued": {"type": "boolean"}}},
            400: OpenApiResponse(description="Project has no repository or no active Claude Code integration"),
        },
        tags=[AISTApiTag.PROJECTS.value],
        summary="Regenerate init script and exclusions",
        description=(
            "Re-runs the Claude-based init-script-generator and project-profile-analyzer "
            "skills against a fresh clone of the project's repository — the same analysis "
            "that runs once automatically at SCM import time when auto_analyze is enabled. "
            "Requires the project to have a repository and the organization to have an "
            "active Claude Code integration."
        ),
    )
    def post(self, request, project_id: int, *args, **kwargs) -> Response:
        project = self.resolve(id=project_id)

        if not project.repository:
            return Response(
                {"detail": "This project has no repository configured; there is nothing to clone."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not claude_auth_env(project):
            return Response(
                {
                    "detail": (
                        "This project's organization has no active Claude Code integration "
                        "configured. Configure one under Org Integrations before regenerating."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from aist.tasks.claude import analyze_project_after_import  # noqa: PLC0415

        analyze_project_after_import.delay(project.id)
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)


class AISTProjectScriptListCreateAPI(AISTAPIView):

    """List script revisions or create a new revision for a project."""

    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: AISTProjectScriptSerializer(many=True)},
        tags=[AISTApiTag.PROJECTS.value],
        summary="List script revisions",
        description="Returns all script revisions for the project (metadata only, no content).",
    )
    def get(self, request, project_id: int) -> Response:
        project = self.resolve(id=project_id)
        scripts = project.script_revisions.select_related("created_by").order_by("-created_at")
        serializer = AISTProjectScriptSerializer(scripts, many=True)
        return Response(serializer.data)

    @extend_schema(
        request=AISTProjectScriptCreateSerializer,
        responses={
            200: OpenApiResponse(description="Shared default script updated in-place (scope=global)"),
            201: AISTProjectScriptContentSerializer,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Shared default not found (scope=global only)"),
        },
        tags=[AISTApiTag.PROJECTS.value],
        summary="Create or update script",
        description=(
            "scope=local (default): creates a new project-specific revision — only this project is affected. "
            "scope=global: updates the shared default script in-place — all projects that still use the "
            "shared default immediately see the new content. Returns 200 for global, 201 for local."
        ),
    )
    def post(self, request, project_id: int) -> Response:
        project = self.resolve(id=project_id)
        serializer = AISTProjectScriptCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        content = serializer.validated_data["content"]
        scope = serializer.validated_data["scope"]
        set_active = serializer.validated_data["set_active"]

        if scope == "global":
            if not request.user.is_superuser:
                msg = "Only superusers may update the shared default script (scope=global)."
                raise PermissionDenied(msg)
            # Lock the shared singleton row before reading + writing to prevent
            # a lost-update race when two requests update the global script
            # concurrently.  select_for_update() acquires a row-level lock that
            # is held until the surrounding atomic block commits.
            try:
                with transaction.atomic():
                    script = AISTProjectScript.objects.select_for_update().get(is_shared=True)
                    script.content = content
                    script.save(update_fields=["content", "sha256"])
            except AISTProjectScript.DoesNotExist:
                return Response(
                    {"detail": "Shared default script not found. Run migrations to initialise it."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            response_status = status.HTTP_200_OK
        else:
            # Create a project-scoped revision (deduplicated by sha256).
            script, created = AISTProjectScript.get_or_create_for_project(
                content=content,
                project=project,
                user=request.user,
            )
            if set_active:
                latest_version = project.versions.order_by("-created").select_related("script").first()
                if latest_version and latest_version.script_id != script.pk:
                    latest_version.script = script
                    latest_version.save(update_fields=["script", "updated"])
            response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK

        return Response(AISTProjectScriptContentSerializer(script).data, status=response_status)


class AISTProjectScriptDetailAPI(AISTAPIView):

    """Retrieve a single script revision (with content)."""

    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={
            200: AISTProjectScriptContentSerializer,
            404: OpenApiResponse(description="Not found"),
        },
        tags=[AISTApiTag.PROJECTS.value],
        summary="Get script revision",
        description="Returns the content of a specific script revision.",
    )
    def get(self, request, project_id: int, script_id: int) -> Response:
        project = self.resolve(id=project_id)
        script = get_object_or_404(
            AISTProjectScript.objects.select_related("created_by").filter(
                Q(project_id=None) | Q(project_id=project.id),
            ),
            pk=script_id,
        )
        return Response(AISTProjectScriptContentSerializer(script).data)


class AISTProjectActiveScriptAPI(AISTAPIView):

    """Retrieve the active script content for a project (shared or project-specific)."""

    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={
            200: AISTProjectScriptContentSerializer,
        },
        tags=[AISTApiTag.PROJECTS.value],
        summary="Get active script",
        description=(
            "Returns the active script for the project. "
            "Resolution order: latest version's script → latest project-scoped revision → shared default. "
            "Always returns 200 — the shared default is the final fallback. "
            "Response includes `inherited` and `source` flags matching the version-script endpoint."
        ),
    )
    def get(self, request, project_id: int) -> Response:
        project = self.resolve(id=project_id)
        source = self._resolve_active_script_source(project)
        return Response(_serialize_version_script(project.active_script, source))

    @staticmethod
    def _resolve_active_script_source(project: AISTProject) -> str:
        latest_version = (
            project.versions
            .order_by("-created")
            .only("id", "script_id")
            .first()
        )
        if latest_version and latest_version.script_id:
            return SCRIPT_SOURCE_VERSION
        if project.script_revisions.exists():
            return SCRIPT_SOURCE_PROJECT_REVISION
        return SCRIPT_SOURCE_SHARED_DEFAULT


def project_meta_payload(project: AISTProject) -> dict:
    versions = [
        {
            "id": str(version.id),
            "label": str(version),
            "version_type": version.version_type,
        }
        for version in project.versions.all()
    ]

    integration_defaults: dict[str, dict] = {}
    for itype in (OrgIntegrationType.SLACK, OrgIntegrationType.EMAIL):
        resolved = resolve_integration(project, itype)
        if resolved:
            integration_defaults[itype.value] = resolved.config

    return {
        "supported_languages": project.supported_languages or [],
        "versions": versions,
        "integration_defaults": integration_defaults,
    }


def default_analyzers_payload(*, project: AISTProject | None, project_id: str | None, langs: list[str], time_class: str):
    cfg = _load_analyzers_config()
    if not cfg:
        return None, "config not loaded"

    filtered = cfg.get_filtered_analyzers(
        analyzers_to_run=None,
        max_time_class=time_class,
        non_compile_project=not project.compilable if project else False,
        target_languages=langs,
        show_only_parent=True,
    )
    defaults = cfg.get_names(filtered)
    return {
        "defaults": defaults,
        "signature": f"{project.id if project else (project_id or '')}::{time_class}::{','.join(sorted(set(langs or [])))}",
    }, None


def update_project_from_payload(*, project: AISTProject, payload: dict):
    compilable = bool(payload.get("compilable"))
    supported_languages_raw = payload.get("supported_languages") or []
    profile = payload.get("profile") or {}

    cfg = _load_analyzers_config()
    if not cfg:
        return None, {"__all__": "config not loaded"}
    languages = cfg.convert_languages(supported_languages_raw)

    project.compilable = compilable
    project.supported_languages = languages
    project.profile = profile or {}
    project.save(
        update_fields=[
            "compilable",
            "supported_languages",
            "profile",
            "updated",
        ],
    )

    return {
        "id": project.id,
        "product_name": getattr(project.product, "name", str(project.id)),
        "active_script_id": project.active_script.id if project.active_script else None,
        "compilable": project.compilable,
        "supported_languages": project.supported_languages,
        "profile": project.profile,
        "organization_id": project.organization_id,
        "organization_name": getattr(project.organization, "name", None),
    }, None


class AISTProjectMetaAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Project meta")},
        tags=[AISTApiTag.PROJECTS.value],
    )
    def get(self, request, project_id: int):
        project = self.resolve(id=project_id)
        return Response(project_meta_payload(project))


class AISTDefaultAnalyzersAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=DefaultAnalyzersRequestSerializer,
        responses={200: OpenApiResponse(description="Default analyzers")},
        tags=[AISTApiTag.PROJECTS.value],
    )
    def post(self, request):
        serializer = DefaultAnalyzersRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project_id = serializer.validated_data.get("project")
        time_class = serializer.validated_data.get("time_class_level") or "slow"
        langs = serializer.validated_data.get("languages", [])

        project = self.authorized_queryset_for_request().filter(id=project_id).first()
        if not project:
            return Response({"detail": "Project not found"}, status=status.HTTP_404_NOT_FOUND)

        payload, error = default_analyzers_payload(
            project=project,
            project_id=str(project_id) if project_id is not None else None,
            langs=langs,
            time_class=time_class,
        )
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)
