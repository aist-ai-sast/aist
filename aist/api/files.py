from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path

import requests
from django.http import FileResponse, Http404, HttpResponse
from django.utils.encoding import iri_to_uri
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.response import Response

from aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAuthzMixin, ResourcePolicy
from aist.integrations import egress
from aist.link_builder import LinkBuilder
from aist.models import AISTProjectVersion, VersionType

# ----------------------------
# Module-level error messages
# ----------------------------
ERR_FILE_NOT_FOUND_IN_ARCHIVE = "File not found in version archive"
ERR_FILE_NOT_FOUND_IN_REPOSITORY = "File not found in remote repository"
ERR_BRANCH_HAS_NO_RESOLVED_COMMIT = "Branch has no resolved commit yet"

# Seconds the UI should wait before retrying while a cold VPN egress warms up.
WARMING_RETRY_AFTER_SECONDS = 3


class _NoBodySerializer(serializers.Serializer):

    """Empty serializer used to satisfy schema generation for APIView-like endpoints."""


class ProjectVersionFileBlobAPI(AISTAuthzMixin, generics.GenericAPIView):

    """
    GET /projects_version/<id>/files/blob/<path:subpath>
    Returns the specified file from project version.
    """

    serializer_class = _NoBodySerializer
    authz = ResourcePolicy(resource=AISTProjectVersion, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        tags=[AISTApiTag.PROJECTS.value],
        summary="Get file from extracted project version archive",
        description=(
            "Returns the raw bytes of a file located **inside** the extracted archive of the specified "
            "AIST project version. If the archive hasn't been extracted yet, it will be extracted once."
        ),
        parameters=[
            OpenApiParameter(
                name="project_version_id",
                location=OpenApiParameter.PATH,
                description="AISTProjectVersion ID",
                required=True,
                type=int,
            ),
            OpenApiParameter(
                name="subpath",
                location=OpenApiParameter.PATH,
                description="Relative path inside the extracted archive (e.g. `src/main.py`)",
                required=True,
                type=str,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.BINARY,
                description="Raw file content (binary stream)",
            ),
            404: OpenApiResponse(description="Project version or file not found"),
        },
    )
    @staticmethod
    def _bytes_response(data: bytes, filename: str):
        """Wrap raw bytes in an inline HttpResponse with a guessed content type."""
        content_type, _ = guess_type(filename)
        content_type = content_type or "application/octet-stream"
        resp = HttpResponse(data, content_type=content_type)
        resp["Content-Disposition"] = f'inline; filename="{iri_to_uri(filename)}"'
        return resp

    def _return_remote_bytes(
        self,
        url: str,
        filename: str,
        extra_headers: dict | None = None,
        *,
        proxy_url: str | None = None,
    ):
        """
        Download the file from a remote URL and return as HttpResponse.

        When ``proxy_url`` is set the request is routed through the warm per-VPN
        egress HTTP CONNECT proxy; otherwise it goes out directly (public SCM).
        """
        headers = dict(extra_headers or {})
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True, proxies=proxies)

        if response.status_code == 404:
            raise Http404(ERR_FILE_NOT_FOUND_IN_REPOSITORY)
        response.raise_for_status()

        return self._bytes_response(response.content, filename)

    @staticmethod
    def _return_local_file(project_version, subpath):
        try:
            root = project_version.ensure_extracted()
        except FileNotFoundError as exc:
            raise Http404(ERR_FILE_NOT_FOUND_IN_ARCHIVE) from exc
        if root is None:
            raise Http404(ERR_FILE_NOT_FOUND_IN_ARCHIVE)

        safe_rel = subpath.lstrip("/").replace("\\", "/")
        file_path = (root / safe_rel).resolve()
        if not file_path.exists() or not file_path.is_file():
            raise Http404(ERR_FILE_NOT_FOUND_IN_ARCHIVE)

        content_type, _ = guess_type(str(file_path))
        content_type = content_type or "application/octet-stream"
        resp = FileResponse(file_path.open("rb"), content_type=content_type)
        resp["Content-Disposition"] = f'inline; filename="{iri_to_uri(file_path.name)}"'
        return resp

    def get(self, request, project_version_id: int, subpath: str, *args, **kwargs):
        project_version = self.resolve(pk=project_version_id)

        # --- Case 1: Local FILE_HASH (from extracted archive) ---
        if project_version.version_type == VersionType.FILE_HASH:
            return self._return_local_file(project_version, subpath)

        # --- Case 2: Git-based version (GIT_BRANCH/GIT_HASH) ---
        if project_version.version_type == VersionType.GIT_BRANCH:
            ref = (project_version.last_resolved_commit or "").strip()
            if not ref:
                ref = (project_version.version or "master").strip()
        else:
            ref = (project_version.version or "master").strip()
        repo_obj = getattr(project_version.project, "repository", None)
        if not repo_obj:
            # Case 3: No repository_info and no SCM binding — cannot serve file.
            # Pipeline workspaces are ephemeral (per-run) and unavailable at request time.
            raise Http404(ERR_FILE_NOT_FOUND_IN_REPOSITORY)

        # If the repo is behind a VPN, route the fetch through this VPN's warm
        # egress proxy (started ahead of time by prewarm_egress).  proxy_url is
        # derived from the authorized project version — never from user input —
        # so one org can never reach another org's tunnel.  None = public SCM.
        proxy_url = egress.proxy_url_for_project_version(project_version)

        try:
            return self._fetch_git_file(project_version, repo_obj, ref, subpath, proxy_url)
        except (requests.ConnectionError, requests.Timeout):
            if proxy_url is None:
                raise  # public SCM: preserve prior behaviour (propagate)
            # Cold egress tunnel: warm it in the background and ask the UI to retry.
            from aist.tasks.egress import prewarm_egress  # noqa: PLC0415 avoid import cycle

            prewarm_egress.delay(project_version.id)
            return Response(
                {"status": "warming", "retry_after": WARMING_RETRY_AFTER_SECONDS},
                status=status.HTTP_202_ACCEPTED,
                headers={"Retry-After": str(WARMING_RETRY_AFTER_SECONDS)},
            )

    def _fetch_git_file(self, project_version, repo_obj, ref: str, subpath: str, proxy_url: str | None):
        """Fetch a single file from the SCM (binding, or public raw URL) at ``ref``."""
        binding = repo_obj.get_binding()
        if binding:
            # Some providers (Gerrit) return base64 content that must be decoded
            # by the binding rather than streamed verbatim.
            fetch_raw = getattr(binding, "fetch_raw_bytes", None)
            if callable(fetch_raw):
                data = fetch_raw(repo_obj, ref, subpath, proxy_url=proxy_url)
                if data is None:
                    raise Http404(ERR_FILE_NOT_FOUND_IN_REPOSITORY)
                return self._bytes_response(data, Path(subpath).name)
            raw_url = binding.build_raw_url(repo_obj, ref, subpath)
            headers = binding.get_auth_headers() or {}
            return self._return_remote_bytes(raw_url, Path(subpath).name, headers, proxy_url=proxy_url)

        # Fallback to public blob/raw URL if no binding configured.
        raw_url = LinkBuilder({"id": project_version.id}).build_raw_url(repo_obj.host(), ref, subpath)
        return self._return_remote_bytes(raw_url, Path(subpath).name, {}, proxy_url=proxy_url)


class ProjectVersionPrewarmAPI(AISTAuthzMixin, generics.GenericAPIView):

    """
    POST /projects_version/<id>/files/prewarm

    Ask the backend to warm this version's VPN egress tunnel ahead of blob
    fetches (UI calls this when opening the findings list / code view so the
    tunnel is up by the time snippets load).  Idempotent and best-effort; returns
    immediately without waiting for the tunnel.  No-op for public (non-VPN) repos.
    """

    serializer_class = _NoBodySerializer
    # Prewarm is a benign cache-warm intentionally at Product_View grade, so its
    # POST maps to a read action (preserves who can call it: Reader+).
    authz = ResourcePolicy(resource=AISTProjectVersion, read=Action.PRODUCT_READ, write=Action.PRODUCT_READ)

    @extend_schema(
        tags=[AISTApiTag.PROJECTS.value],
        summary="Warm the VPN egress tunnel for a project version",
        request=None,
        responses={200: OpenApiResponse(description='{"status": "warming" | "no_vpn"}')},
    )
    def post(self, request, project_version_id: int, *args, **kwargs):
        project_version = self.resolve(pk=project_version_id)
        # Resolution is derived from the authorized object only (no user input) —
        # a user can never warm another org's tunnel.
        vpn_integration = egress.vpn_integration_for_project_version(project_version)
        if vpn_integration is None:
            return Response({"status": "no_vpn"})

        from aist.tasks.egress import prewarm_egress  # noqa: PLC0415 avoid import cycle

        prewarm_egress.delay(project_version.id)
        return Response({"status": "warming"})
