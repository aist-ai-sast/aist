from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from operator import itemgetter
from urllib.parse import quote

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_github_app.models import Installation
from dojo.models import DojoMeta, Product
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from aist.api.projects import _create_initial_script
from aist.api.schema import AISTApiTag
from aist.authz import PUBLIC, Action, AISTAPIView, ResourcePolicy, queryset_for_action
from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    Organization,
    RepositoryInfo,
    ScmGithubBinding,
    ScmType,
    VersionType,
)
from aist.utils.pipeline_imports import _load_analyzers_config

STATE_SALT = "aist.github.connect"
STATE_MAX_AGE_SECONDS = 600
STATE_SESSION_NONCES_KEY = "aist_github_connect_nonces"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GithubConnectState:
    flow: str
    user_id: int
    nonce: str
    organization_id: int | None = None
    project_id: int | None = None

    def to_payload(self) -> dict[str, str | int | None]:
        return {
            "flow": self.flow,
            "user_id": self.user_id,
            "nonce": self.nonce,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> GithubConnectState:
        return cls(
            flow=str(payload.get("flow") or ""),
            user_id=int(payload.get("user_id") or 0),
            nonce=str(payload.get("nonce") or ""),
            organization_id=_coerce_optional_int(payload.get("organization_id")),
            project_id=_coerce_optional_int(payload.get("project_id")),
        )


class OrganizationOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class GithubImportOptionsResponseSerializer(serializers.Serializer):
    organizations = OrganizationOutSerializer(many=True)


class GithubConnectStartImportSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField()


class GithubConnectStartResponseSerializer(serializers.Serializer):
    redirect_url = serializers.URLField()


class GithubConnectCallbackSerializer(serializers.Serializer):
    state = serializers.CharField()
    installation_id = serializers.IntegerField(min_value=1)
    setup_action = serializers.CharField(required=False, allow_blank=True)


class GithubRepositorySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    private = serializers.BooleanField()
    default_branch = serializers.CharField(allow_blank=True)


class GithubRepositoriesResponseSerializer(serializers.Serializer):
    installation_id = serializers.IntegerField()
    repositories = GithubRepositorySerializer(many=True)


class GithubImportRepositoriesQuerySerializer(serializers.Serializer):
    organization_id = serializers.IntegerField()
    installation_id = serializers.IntegerField(required=False, min_value=1)


class GithubImportExecuteSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField()
    installation_id = serializers.IntegerField(min_value=1)
    repositories = serializers.ListField(
        child=serializers.RegexField(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
        min_length=1,
    )
    auto_analyze = serializers.BooleanField(default=False, required=False)


class GithubImportResultItemSerializer(serializers.Serializer):
    repo = serializers.CharField()
    aist_project_id = serializers.IntegerField(required=False)
    reason = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)


class GithubImportExecuteResponseSerializer(serializers.Serializer):
    imported = GithubImportResultItemSerializer(many=True)
    skipped = GithubImportResultItemSerializer(many=True)
    failed = GithubImportResultItemSerializer(many=True)


class GithubProjectStatusResponseSerializer(serializers.Serializer):
    connected = serializers.BooleanField()
    installation_id = serializers.IntegerField(allow_null=True)
    repository = serializers.DictField(allow_null=True)


class GithubProjectRepositoriesQuerySerializer(serializers.Serializer):
    installation_id = serializers.IntegerField(required=False, min_value=1)


class GithubProjectLinkRepositorySerializer(serializers.Serializer):
    installation_id = serializers.IntegerField(min_value=1)
    repository_full_name = serializers.RegexField(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GithubProjectLinkRepositoryResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    repository_id = serializers.IntegerField()
    repository_full_name = serializers.CharField()


class GithubImportOptionsAPI(AISTAPIView):
    authz = ResourcePolicy(resource=Organization, read=Action.PROJECT_CREATE, write=Action.PROJECT_CREATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        responses={200: GithubImportOptionsResponseSerializer},
        summary="GitHub import options",
    )
    def get(self, request, *args, **kwargs):
        organizations = self.authorized_queryset().order_by("name")
        serializer = GithubImportOptionsResponseSerializer(
            {"organizations": [{"id": org.id, "name": org.name} for org in organizations]},
        )
        return Response(serializer.data)


class GithubImportConnectStartAPI(AISTAPIView):
    authz = ResourcePolicy(resource=Organization, read=Action.PROJECT_CREATE, write=Action.PROJECT_CREATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        request=GithubConnectStartImportSerializer,
        responses={200: GithubConnectStartResponseSerializer},
        summary="Start GitHub import connect",
    )
    def post(self, request, *args, **kwargs):
        serializer = GithubConnectStartImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        org = self.resolve(id=serializer.validated_data["organization_id"])
        state = _create_state(
            GithubConnectState(
                flow="import",
                user_id=request.user.id,
                nonce=_create_nonce(request),
                organization_id=org.id,
            ),
        )
        response = GithubConnectStartResponseSerializer({"redirect_url": _build_install_redirect_url(state)})
        return Response(response.data)


class GithubConnectCallbackAPI(AISTAPIView):
    # import action; scopes org/project internally via getters
    authz = PUBLIC

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        parameters=[GithubConnectCallbackSerializer],
        responses={302: OpenApiResponse(description="Redirect to AIST admin page")},
        summary="GitHub connect callback",
    )
    def get(self, request, *args, **kwargs):
        serializer = GithubConnectCallbackSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        state = _load_state(serializer.validated_data["state"])
        if state.user_id != request.user.id:
            raise PermissionDenied(_("GitHub connect state does not belong to current user."))
        _consume_nonce_or_raise(request, state.nonce)

        installation_id = serializer.validated_data["installation_id"]
        _ensure_installation_exists(installation_id)

        if state.flow == "import":
            if state.organization_id is None:
                raise serializers.ValidationError({"state": "organization_id is required in import flow."})
            _get_organization_for_import_or_403(request.user, state.organization_id)
            cache.set(_import_installation_cache_key(request.user.id, state.organization_id), installation_id, timeout=1800)
            target = reverse("aist:aist_project_list")
            query = f"organization_id={state.organization_id}&github_installation_id={installation_id}"
            return redirect(f"{target}?{query}")

        if state.flow == "project_link":
            if state.project_id is None:
                raise serializers.ValidationError({"state": "project_id is required in project_link flow."})
            project = get_object_or_404(
                queryset_for_action(
                    resource=AISTProject,
                    action=Action.PROJECT_OPERATE,
                    user=request.user,
                ),
                id=state.project_id,
            )
            cache.set(_project_installation_cache_key(request.user.id, project.id), installation_id, timeout=1800)
            target = reverse("aist:aist_project_update", kwargs={"project_id": project.id})
            return redirect(f"{target}?github_installation_id={installation_id}")

        raise serializers.ValidationError({"state": "Unsupported flow."})


class GithubImportRepositoriesAPI(AISTAPIView):
    authz = ResourcePolicy(resource=Organization, read=Action.PROJECT_CREATE, write=Action.PROJECT_CREATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        parameters=[GithubImportRepositoriesQuerySerializer],
        responses={200: GithubRepositoriesResponseSerializer},
        summary="List GitHub repositories for import",
    )
    def get(self, request, *args, **kwargs):
        serializer = GithubImportRepositoriesQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        org = self.resolve(id=serializer.validated_data["organization_id"])
        installation_id = serializer.validated_data.get("installation_id")
        if installation_id is None:
            installation_id = cache.get(_import_installation_cache_key(request.user.id, org.id))
        if not installation_id:
            raise serializers.ValidationError({"installation_id": "GitHub installation is not connected."})

        repos = _list_installation_repositories(int(installation_id))
        response = GithubRepositoriesResponseSerializer(
            {
                "installation_id": int(installation_id),
                "repositories": repos,
            },
        )
        return Response(response.data)


class GithubImportExecuteAPI(AISTAPIView):
    authz = ResourcePolicy(resource=Organization, read=Action.PROJECT_CREATE, write=Action.PROJECT_CREATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        request=GithubImportExecuteSerializer,
        responses={200: GithubImportExecuteResponseSerializer},
        summary="Execute GitHub import",
    )
    def post(self, request, *args, **kwargs):
        serializer = GithubImportExecuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        org = self.resolve(id=serializer.validated_data["organization_id"])
        installation_id = serializer.validated_data["installation_id"]
        requested_repos = serializer.validated_data["repositories"]

        repos_by_name = {repo["full_name"]: repo for repo in _list_installation_repositories(installation_id)}
        result: dict[str, list[dict[str, str | int]]] = {"imported": [], "skipped": [], "failed": []}

        auto_analyze = serializer.validated_data.get("auto_analyze", False)

        for repo_full in requested_repos:
            repo = repos_by_name.get(repo_full)
            if repo is None:
                result["failed"].append({"repo": repo_full, "reason": "repository_not_in_installation"})
                continue
            try:
                aist_project = _import_github_repository(
                    installation_id=installation_id,
                    organization=org,
                    repo_full=repo_full,
                    auto_analyze=auto_analyze,
                )
            except _ImportConflictError as exc:
                result["skipped"].append({"repo": repo_full, "reason": str(exc)})
                continue
            except Exception as exc:
                logger.exception("GitHub import failed for repository %s", repo_full)
                result["failed"].append({"repo": repo_full, "reason": "import_failed", "detail": str(exc)})
                continue
            result["imported"].append({"repo": repo_full, "aist_project_id": aist_project.id})

        return Response(GithubImportExecuteResponseSerializer(result).data)


class GithubProjectStatusAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        responses={200: GithubProjectStatusResponseSerializer},
        summary="GitHub status for AIST project",
    )
    def get(self, request, project_id: int, *args, **kwargs):
        project = self.resolve(id=project_id)
        repo = project.repository
        if not repo or repo.type != ScmType.GITHUB:
            return Response(
                GithubProjectStatusResponseSerializer(
                    {"connected": False, "installation_id": None, "repository": None},
                ).data,
            )

        binding = ScmGithubBinding.objects.filter(scm=repo).first()
        return Response(
            GithubProjectStatusResponseSerializer(
                {
                    "connected": bool(binding and binding.installation_id),
                    "installation_id": binding.installation_id if binding else None,
                    "repository": {"full_name": repo.repo_full},
                },
            ).data,
        )


class GithubProjectConnectStartAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        request=None,
        responses={200: GithubConnectStartResponseSerializer},
        summary="Start GitHub connect for existing project",
    )
    def post(self, request, project_id: int, *args, **kwargs):
        project = self.resolve(id=project_id)

        state = _create_state(
            GithubConnectState(
                flow="project_link",
                user_id=request.user.id,
                nonce=_create_nonce(request),
                project_id=project.id,
            ),
        )
        response = GithubConnectStartResponseSerializer({"redirect_url": _build_install_redirect_url(state)})
        return Response(response.data)


class GithubProjectRepositoriesAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        parameters=[GithubProjectRepositoriesQuerySerializer],
        responses={200: GithubRepositoriesResponseSerializer},
        summary="List GitHub repositories for existing project",
    )
    def get(self, request, project_id: int, *args, **kwargs):
        project = self.resolve(id=project_id)

        serializer = GithubProjectRepositoriesQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        installation_id = serializer.validated_data.get("installation_id")
        if installation_id is None:
            installation_id = cache.get(_project_installation_cache_key(request.user.id, project.id))

        if installation_id is None and project.repository and project.repository.type == ScmType.GITHUB:
            binding = ScmGithubBinding.objects.filter(scm=project.repository).first()
            installation_id = binding.installation_id if binding else None

        if not installation_id:
            raise serializers.ValidationError({"installation_id": "GitHub installation is not connected."})

        repos = _list_installation_repositories(int(installation_id))
        response = GithubRepositoriesResponseSerializer(
            {
                "installation_id": int(installation_id),
                "repositories": repos,
            },
        )
        return Response(response.data)


class GithubProjectLinkRepositoryAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTProject, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.GITHUB.value],
        request=GithubProjectLinkRepositorySerializer,
        responses={200: GithubProjectLinkRepositoryResponseSerializer},
        summary="Link GitHub repository to existing AIST project",
    )
    def post(self, request, project_id: int, *args, **kwargs):
        project = self.resolve(id=project_id)

        serializer = GithubProjectLinkRepositorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        installation_id = serializer.validated_data["installation_id"]
        repo_full = serializer.validated_data["repository_full_name"]

        allowed_repos = {repo["full_name"] for repo in _list_installation_repositories(installation_id)}
        if repo_full not in allowed_repos:
            raise serializers.ValidationError({"repository_full_name": "Repository is not available for this installation."})

        owner, name = repo_full.split("/", 1)
        repo_info, _ = RepositoryInfo.objects.get_or_create(
            type=ScmType.GITHUB,
            repo_owner=owner,
            repo_name=name,
            defaults={"base_url": "https://github.com"},
        )

        other_project = AISTProject.objects.filter(repository=repo_info).exclude(id=project.id).first()
        if other_project is not None:
            return Response(
                {"detail": "Repository is already linked to another AIST project."},
                status=status.HTTP_409_CONFLICT,
            )

        binding, _ = ScmGithubBinding.objects.get_or_create(scm=repo_info)
        if binding.installation_id != installation_id:
            binding.installation_id = installation_id
            binding.save(update_fields=["installation_id"])

        if project.repository_id != repo_info.id:
            project.repository = repo_info
            project.save(update_fields=["repository"])

        details, languages = _fetch_repository_details(installation_id, repo_full)
        cfg = _load_analyzers_config()
        if cfg:
            project.supported_languages = cfg.convert_languages(languages or {})
            project.save(update_fields=["supported_languages"])

        if details.get("description"):
            project.product.description = details["description"]
            project.product.save(update_fields=["description"])
        DojoMeta.objects.update_or_create(
            product=project.product,
            name="scm-type",
            defaults={"value": "github"},
        )

        return Response(
            GithubProjectLinkRepositoryResponseSerializer(
                {
                    "ok": True,
                    "repository_id": repo_info.id,
                    "repository_full_name": repo_info.repo_full,
                },
            ).data,
        )


def _coerce_optional_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _import_installation_cache_key(user_id: int, organization_id: int) -> str:
    return f"aist:github:import_installation:{user_id}:{organization_id}"


def _project_installation_cache_key(user_id: int, project_id: int) -> str:
    return f"aist:github:project_installation:{user_id}:{project_id}"


def _create_nonce(request) -> str:
    nonce = secrets.token_urlsafe(24)
    nonces = request.session.get(STATE_SESSION_NONCES_KEY, [])
    nonces.append(nonce)
    request.session[STATE_SESSION_NONCES_KEY] = nonces[-20:]
    request.session.modified = True
    return nonce


def _consume_nonce_or_raise(request, nonce: str) -> None:
    nonces = request.session.get(STATE_SESSION_NONCES_KEY, [])
    if nonce not in nonces:
        raise serializers.ValidationError({"state": "Expired or already used state token."})
    nonces.remove(nonce)
    request.session[STATE_SESSION_NONCES_KEY] = nonces
    request.session.modified = True


def _create_state(state: GithubConnectState) -> str:
    return signing.dumps(state.to_payload(), salt=STATE_SALT)


def _load_state(raw_state: str) -> GithubConnectState:
    try:
        payload = signing.loads(raw_state, salt=STATE_SALT, max_age=STATE_MAX_AGE_SECONDS)
    except signing.BadSignature as exc:
        raise serializers.ValidationError({"state": "Invalid state token."}) from exc
    return GithubConnectState.from_payload(payload)


def _build_install_redirect_url(state: str) -> str:
    app_slug = ((settings.GITHUB_APP or {}).get("NAME") or "").strip()
    if not app_slug:
        raise serializers.ValidationError({"detail": "GitHub App name is not configured."})
    return f"https://github.com/apps/{quote(app_slug)}/installations/new?state={quote(state)}"


def _get_authorized_organizations_for_import(user):
    return queryset_for_action(resource=Organization, action=Action.PROJECT_CREATE, user=user)


def _get_organization_for_import_or_403(user, organization_id: int) -> Organization:
    org = get_object_or_404(
        queryset_for_action(resource=Organization, action=Action.PROJECT_CREATE, user=user),
        id=organization_id,
    )

    if org.product_type_id is None:
        if not user.is_superuser:
            raise PermissionDenied(_("Organization is not initialized for imports."))
        org.ensure_product_type()

    return org


def _ensure_installation_exists(installation_id: int) -> Installation:
    installation, _ = Installation.objects.get_or_create(
        installation_id=installation_id,
        defaults={"data": {"app_slug": ((settings.GITHUB_APP or {}).get("NAME") or "")}},
    )
    return installation


def _list_installation_repositories(installation_id: int) -> list[dict[str, str | bool | int]]:
    installation = _ensure_installation_exists(installation_id)
    repos = async_to_sync(_alist_installation_repositories)(installation)
    return sorted(
        [
            {
                "id": int(repo.get("id") or 0),
                "full_name": str(repo.get("full_name") or ""),
                "private": bool(repo.get("private", False)),
                "default_branch": str(repo.get("default_branch") or ""),
            }
            for repo in repos
            if repo.get("full_name")
        ],
        key=itemgetter("full_name"),
    )


async def _alist_installation_repositories(installation: Installation) -> list[dict]:
    return await installation.aget_repos(params={"per_page": 100})


def _fetch_repository_details(installation_id: int, repo_full: str) -> tuple[dict, dict]:
    installation = _ensure_installation_exists(installation_id)
    return async_to_sync(_afetch_repository_details)(installation, repo_full)


async def _afetch_repository_details(installation: Installation, repo_full: str) -> tuple[dict, dict]:
    owner, name = repo_full.split("/", 1)
    async with installation.get_gh_client() as gh:
        details = await gh.getitem(f"/repos/{owner}/{name}")
        languages = await gh.getitem(f"/repos/{owner}/{name}/languages")
    return details, languages


class _ImportConflictError(Exception):
    pass


def _import_github_repository(
    *,
    installation_id: int,
    organization: Organization,
    repo_full: str,
    auto_analyze: bool = False,
) -> AISTProject:
    owner, name = repo_full.split("/", 1)
    details, languages = _fetch_repository_details(installation_id, repo_full)

    cfg = _load_analyzers_config()
    supported_languages = cfg.convert_languages(languages or {}) if cfg else []

    html_url = (details.get("html_url") or f"https://github.com/{repo_full}").rstrip("/")
    base_url = html_url.split("/" + owner + "/")[0]
    description = details.get("description") or "Empty description. Admin, fix me"

    product_type = organization.ensure_product_type()
    product, created_product = Product.objects.get_or_create(
        name=repo_full,
        defaults={"prod_type": product_type, "description": description},
    )
    if not created_product and product.prod_type_id != product_type.id:
        reason = "product_exists_under_another_product_type"
        raise _ImportConflictError(reason)

    DojoMeta.objects.update_or_create(
        product=product,
        name="scm-type",
        defaults={"value": "github"},
    )

    repo_info, _ = RepositoryInfo.objects.get_or_create(
        type=ScmType.GITHUB,
        repo_owner=owner,
        repo_name=name,
        defaults={"base_url": base_url},
    )

    other_project = AISTProject.objects.filter(repository=repo_info).exclude(product=product).first()
    if other_project is not None:
        reason = "repository_linked_to_another_project"
        raise _ImportConflictError(reason)

    binding, _ = ScmGithubBinding.objects.get_or_create(scm=repo_info)
    if binding.installation_id != installation_id:
        binding.installation_id = installation_id
        binding.save(update_fields=["installation_id"])

    with transaction.atomic():
        aist_project, created_project = AISTProject.objects.get_or_create(
            product=product,
            defaults={
                "supported_languages": supported_languages,
                "compilable": False,
                "profile": {},
                "repository": repo_info,
            },
        )

        if created_project:
            _create_initial_script(aist_project, DEFAULT_ENTRYPOINT_SCRIPT)
            default_branch = details.get("default_branch") or ""
            if default_branch:
                # Seed the initial version with the real default branch now,
                # while it's still committed inside this transaction — this
                # pre-empts create_default_master_version's own "master"
                # fallback lookup (which has no VPN/proxy awareness and would
                # silently fall back for GitHub Enterprise hosts only
                # reachable via VPN). `details` already carries this from the
                # GitHub API fetch above.
                AISTProjectVersion.objects.get_or_create(
                    project=aist_project,
                    version=default_branch,
                    defaults={"version_type": VersionType.GIT_BRANCH},
                )
            if auto_analyze:
                from aist.tasks.claude import analyze_project_after_import  # noqa: PLC0415
                pid = aist_project.id
                transaction.on_commit(lambda: analyze_project_after_import.delay(pid))
        else:
            if aist_project.organization_id and aist_project.organization_id != organization.id:
                reason = "project_linked_to_another_organization"
                raise _ImportConflictError(reason)

            updates: list[str] = []
            if aist_project.repository_id != repo_info.id:
                aist_project.repository = repo_info
                updates.append("repository")
            if aist_project.supported_languages != supported_languages:
                aist_project.supported_languages = supported_languages
                updates.append("supported_languages")
            if updates:
                aist_project.save(update_fields=updates)

    return aist_project
