from __future__ import annotations

import logging
from mimetypes import guess_type
from pathlib import Path

import requests
from django.http import FileResponse, Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.response import Response

from aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from aist.api.schema import AISTApiTag
from aist.authz import Action, AISTAuthzMixin, ResourcePolicy
from aist.integrations import egress
from aist.integrations.scm_errors import ScmFetchError
from aist.models import AISTProjectVersion
from aist.models.projects import SourceFileNotFoundError

# Client-facing SCM failures. Deliberately generic: they never name the SCM host,
# the ref, the request URL, or the credential.
ERR_SCM_AUTH_FAILED = (
    "The source code repository rejected the organization's SCM integration credentials. "
    "The access token may be expired, revoked, or lack access to this repository — "
    "update it in the organization's integration settings."
)
ERR_SCM_UNAVAILABLE = "The source code repository is temporarily unavailable. Try again later."
SCM_AUTH_FAILED_CODE = "scm_auth_failed"
SCM_UNAVAILABLE_CODE = "scm_unavailable"

logger = logging.getLogger(__name__)

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
        summary="Get one source file of a project version",
        description=(
            "Returns the raw bytes of a file of the project version's source: its extracted archive for an "
            "uploaded-archive version, else the project's repository at the version's commit."
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
                description="Path relative to the source root (e.g. `src/main.py`)",
                required=True,
                type=str,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.BINARY,
                description="Raw file content (binary stream)",
            ),
            202: OpenApiResponse(
                description='The VPN egress tunnel is warming: `{"status": "warming", "retry_after": <seconds>}`',
            ),
            404: OpenApiResponse(description="Project version or file not found, or the path is refused"),
            502: OpenApiResponse(
                description=(
                    'SCM rejected the fetch: `code` is "scm_auth_failed" (integration credentials '
                    'rejected, 401/403 upstream) or "scm_unavailable" (any other upstream failure)'
                ),
            ),
        },
    )
    def get(self, request, project_version_id: int, subpath: str, *args, **kwargs):
        project_version = self.resolve(pk=project_version_id)

        # If the repo is behind a VPN, route the fetch through this VPN's warm
        # egress proxy (started ahead of time by prewarm_egress).  proxy_url is
        # derived from the authorized project version — never from user input —
        # so one org can never reach another org's tunnel.  None = public SCM.
        proxy_url = egress.proxy_url_for_project_version(project_version)

        try:
            stream = project_version.open_source_file(subpath, proxy_url=proxy_url)
        except SourceFileNotFoundError as exc:
            raise Http404(str(exc)) from exc
        except ScmFetchError as exc:
            return self._scm_error_response(project_version, exc)
        except (requests.ConnectionError, requests.Timeout):
            if proxy_url is None:
                raise  # public SCM: no tunnel to warm
            # Cold egress tunnel: warm it in the background and ask the UI to retry.
            from aist.tasks.egress import prewarm_egress  # noqa: PLC0415 avoid import cycle

            prewarm_egress.delay(project_version.id)
            return Response(
                {"status": "warming", "retry_after": WARMING_RETRY_AFTER_SECONDS},
                status=status.HTTP_202_ACCEPTED,
                headers={"Retry-After": str(WARMING_RETRY_AFTER_SECONDS)},
            )
        filename = Path(subpath).name
        return FileResponse(
            stream, content_type=guess_type(filename)[0] or "application/octet-stream", filename=filename,
        )

    @staticmethod
    def _scm_error_response(project_version, exc: ScmFetchError) -> Response:
        """
        Translate an upstream SCM failure into a 502 the UI can explain.

        502 Bad Gateway: this endpoint acts as a gateway to the SCM and the upstream
        answered with a failure. The client's own session is valid, so 401/403 would be
        wrong — and the UI treats a 401 as "your AIST session expired" and logs the user
        out. 503 would claim AIST itself is down. The ``code`` field lets the UI tell a
        credential problem (admin must rotate the integration token) from a transient one.
        """
        logger.warning(
            "SCM file fetch failed: project_version_id=%s repo=%s upstream_status=%s",
            project_version.id,
            project_version.project.repository.repo_full,
            exc.status_code,
        )
        if exc.is_auth_failure:
            body = {"detail": ERR_SCM_AUTH_FAILED, "code": SCM_AUTH_FAILED_CODE}
        else:
            body = {"detail": ERR_SCM_UNAVAILABLE, "code": SCM_UNAVAILABLE_CODE}
        return Response(body, status=status.HTTP_502_BAD_GATEWAY)


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
