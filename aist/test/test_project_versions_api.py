from __future__ import annotations

import io
import json
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from aist.models import AISTProjectVersion, VersionType
from aist.test.test_api import AISTApiBase


class ProjectVersionsAPITests(AISTApiBase):
    def _json(self, resp):
        return json.loads(resp.content.decode("utf-8") or "{}")

    def _zip_with_file(self, filename: str, content: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(filename, content)
        return buf.getvalue()

    def _zip_with_files(self, files: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def test_create_version_file_hash_and_blob(self):
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        archive_bytes = self._zip_with_files(
            {
                "main.py": "print('ok')\n",
                "README.txt": "readme\n",
            },
        )
        upload = SimpleUploadedFile("src.zip", archive_bytes, content_type="application/zip")

        resp = self.client.post(
            url,
            data={"version_type": VersionType.FILE_HASH, "source_archive": upload},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201)
        data = self._json(resp)
        version_id = data["id"]

        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": version_id, "subpath": "main.py"},
        )
        blob_resp = self.client.get(blob_url)
        self.assertEqual(blob_resp.status_code, 200)
        content = b"".join(blob_resp.streaming_content)
        self.assertIn(b"print('ok')", content)

    def test_create_version_git_hash_requires_version(self):
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        resp = self.client.post(url, data={"version_type": VersionType.GIT_HASH}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_create_version_file_hash_requires_archive(self):
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        resp = self.client.post(url, data={"version_type": VersionType.FILE_HASH}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_create_version_duplicate_git_hash(self):
        AISTProjectVersion.objects.get_or_create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        resp = self.client.post(url, data={"version_type": VersionType.GIT_HASH, "version": "main"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_file_blob_missing_file(self):
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        archive_bytes = self._zip_with_file("src/only.py", "print('ok')\n")
        upload = SimpleUploadedFile("src.zip", archive_bytes, content_type="application/zip")

        resp = self.client.post(
            url,
            data={"version_type": VersionType.FILE_HASH, "source_archive": upload},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201)
        data = self._json(resp)
        version_id = data["id"]

        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": version_id, "subpath": "missing.py"},
        )
        blob_resp = self.client.get(blob_url)
        self.assertEqual(blob_resp.status_code, 404)
