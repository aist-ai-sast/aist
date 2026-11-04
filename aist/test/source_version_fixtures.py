"""Project versions with a readable source, and a fake source-control host, for tests that read source files."""
from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Self
from unittest.mock import patch

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from requests.adapters import HTTPAdapter

from aist.models import (
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmGerritBinding,
    ScmType,
    VersionType,
)

COMMIT = "a1b2c3d4" * 5
OUTSIDE_CONTENT = "OUTSIDE_THE_VERSION_SOURCE"
GERRIT_HTTP_PASSWORD = "gerrit-http-pass-0123456789"  # noqa: S105 -- test fixture


def host_response(body: bytes, status_code: int = 200, url: str = "") -> requests.Response:
    """A real ``requests.Response`` as a source-control host returns it."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response.url = url
    response.headers["Content-Type"] = "application/octet-stream"
    return response


@dataclass(frozen=True)
class HostRequest:
    url: str
    headers: dict[str, str]
    proxies: dict[str, str]


@dataclass
class FakeScmHost:

    """
    Answers every HTTP request the process sends with ``body`` and ``status_code``, or raises ``error``.

    It replaces the transport under ``requests``, so plain requests and API clients built on a requests
    session (python-gitlab) are answered and recorded alike.
    """

    body: bytes = b""
    status_code: int = 200
    error: Exception | None = None
    requests: list[HostRequest] = field(default_factory=list)

    def __enter__(self) -> Self:
        self._patch = patch.object(HTTPAdapter, "send", autospec=True, side_effect=self._send)
        self._patch.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._patch.stop()

    @property
    def last(self) -> HostRequest:
        return self.requests[-1]

    def _send(self, _adapter, request, **kwargs):
        self.requests.append(HostRequest(request.url, dict(request.headers), dict(kwargs.get("proxies") or {})))
        if self.error is not None:
            raise self.error
        response = host_response(self.body, self.status_code, request.url)
        response.request = request
        return response


class SourceVersionMixin:

    """Creates project versions of ``self.project`` whose source is an uploaded archive or a repository."""

    def setUp(self):
        super().setUp()
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        media_override = override_settings(MEDIA_ROOT=media_root)
        media_override.enable()
        self.addCleanup(media_override.disable)

    def archive_version(self, files: dict[str, str]) -> AISTProjectVersion:
        """Return an uploaded-archive version holding ``files`` (path to text)."""
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            for path, text in files.items():
                zf.writestr(path, text)
        return AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.FILE_HASH,
            source_archive=SimpleUploadedFile("src.zip", archive.getvalue(), content_type="application/zip"),
        )

    def repository_version(
        self,
        repository: RepositoryInfo,
        version_type: str = VersionType.GIT_HASH,
        version: str = COMMIT,
        **defaults,
    ) -> AISTProjectVersion:
        """Link ``repository`` to the project and return its version ``version``."""
        self.project.repository = repository
        self.project.save(update_fields=["repository"])
        pv, _ = AISTProjectVersion.objects.get_or_create(
            project=self.project, version_type=version_type, version=version, defaults=defaults,
        )
        return pv

    def gerrit_repository(self, host: str = "gerrit.example.com") -> tuple[RepositoryInfo, ScmGerritBinding]:
        """Return a Gerrit repository bound to an active Gerrit integration of a new organization."""
        organization = Organization.objects.create(name="Gerrit Org", product_type=self.prod_type)
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.GERRIT,
            name="Corporate Gerrit",
            config={"username": "svc-aist"},
            secret=GERRIT_HTTP_PASSWORD,
            is_active=True,
        )
        repository = RepositoryInfo.objects.create(
            type=ScmType.GERRIT, repo_owner="platform", repo_name="server", base_url=f"https://{host}",
        )
        return repository, ScmGerritBinding.objects.create(scm=repository, org_integration=integration)

    @staticmethod
    def blob_url(pv: AISTProjectVersion, raw_subpath: str) -> str:
        """Blob URL of ``pv`` whose subpath is appended exactly as given (already percent-encoded where intended)."""
        prefix = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": pv.id, "subpath": "_"},
        ).removesuffix("_")
        return prefix + raw_subpath
