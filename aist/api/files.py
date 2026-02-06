from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path

import requests
from django.http import FileResponse, Http404, HttpResponse, HttpResponseServerError
from django.shortcuts import get_object_or_404
from django.utils.encoding import iri_to_uri
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from pipeline.defect_dojo.repo_info import read_repo_params  # type: ignore[import-not-found]
from rest_framework import generics, serializers
from rest_framework.permissions import IsAuthenticated

from aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from aist.link_builder import LinkBuilder
from aist.models import VersionType
from aist.queries import get_authorized_aist_project_versions
from aist.utils.pipeline import get_project_build_path

# ----------------------------
# Module-level error messages
# ----------------------------
ERR_FILE_NOT_FOUND_IN_ARCHIVE = "File not found in version archive"
ERR_FILE_NOT_FOUND_IN_REPOSITORY = "File not found in remote repository"


class _NoBodySerializer(serializers.Serializer):

    """Empty serializer used to satisfy schema generation for APIView-like endpoints."""


class ProjectVersionFileBlobAPI(generics.GenericAPIView):

    """
    GET /projects_version/<id>/files/blob/<path:subpath>
    Returns the specified file from project version.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = _NoBodySerializer

    @extend_schema(
        tags=["aist"],
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
    def _return_remote_bytes(self, url: str, filename: str, extra_headers: dict | None = None):
        """Download the file from a remote URL and return as HttpResponse."""
        headers = dict(extra_headers or {})
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)

        if response.status_code == 404:
            raise Http404(ERR_FILE_NOT_FOUND_IN_REPOSITORY)
        response.raise_for_status()

        content_type, _ = guess_type(filename)
        content_type = content_type or "application/octet-stream"

        resp = HttpResponse(response.content, content_type=content_type)
        resp["Content-Disposition"] = f'inline; filename="{iri_to_uri(filename)}"'
        return resp

    @staticmethod
    def _return_local_file(project_version, subpath):
        root = project_version.ensure_extracted()
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
        project_version = get_object_or_404(
            get_authorized_aist_project_versions(Permissions.Product_View, user=request.user),
            pk=project_version_id,
        )

        # --- Case 1: Local FILE_HASH (from extracted archive) ---
        if project_version.version_type == VersionType.FILE_HASH:
            return self._return_local_file(project_version, subpath)

        # --- Case 2: Git-based version (GIT_HASH) ---
        ref = (project_version.version or "master").strip()
        repo_obj = getattr(project_version.project, "repository", None)

        link_builder = LinkBuilder({"id": project_version.id})
        if repo_obj:
            # Try using existing SCM binding (GitHub/GitLab)
            binding = repo_obj.get_binding()
            if binding:
                raw_url = binding.build_raw_url(repo_obj, ref, subpath)
                headers = binding.get_auth_headers() or {}
                return self._return_remote_bytes(raw_url, Path(subpath).name, headers)

            # Fallback to public blob/raw URL if no binding configured
            raw_url = link_builder.build_raw_url(repo_obj.host(), ref, subpath)
            return self._return_remote_bytes(raw_url, Path(subpath).name, {})

        # --- Case 3: No repository_info, use local build path + repo_info ---
        try:
            repo_path = get_project_build_path(project_version.project.product.name,
                                               project_version.version or "default")
        except RuntimeError:
            return HttpResponseServerError()

        # Read Git metadata from local repo
        params = read_repo_params(str(repo_path))
        raw_url = link_builder.build_raw_url(params.repo_url, params.branch_tag or ref, subpath)
        return self._return_remote_bytes(raw_url, Path(subpath).name, {})
