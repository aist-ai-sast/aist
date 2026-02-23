from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from aist.models import AISTProjectVersion, RepositoryInfo, ScmType, VersionType
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

    def test_file_blob_returns_404_when_source_archive_is_missing_in_storage(self):
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        archive_bytes = self._zip_with_file("src/only.py", "print('ok')\n")
        upload = SimpleUploadedFile("src.zip", archive_bytes, content_type="application/zip")

        resp = self.client.post(
            url,
            data={"version_type": VersionType.FILE_HASH, "source_archive": upload},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201)
        version_id = self._json(resp)["id"]

        version = AISTProjectVersion.objects.get(id=version_id)
        archive_name = version.source_archive.name
        self.assertTrue(default_storage.exists(archive_name))
        default_storage.delete(archive_name)
        self.assertFalse(default_storage.exists(archive_name))

        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": version_id, "subpath": "src/only.py"},
        )
        blob_resp = self.client.get(blob_url)

        self.assertEqual(blob_resp.status_code, 404)
        self.assertEqual(blob_resp.json(), {"detail": "File not found in version archive"})

    @patch("aist.api.files.requests.get")
    def test_git_branch_blob_uses_last_resolved_commit(self, mock_get):
        self.project.repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="org",
            repo_name="repo",
            base_url="https://github.com",
        )
        self.project.save(update_fields=["repository"])
        pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
            last_resolved_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

        mock_get.return_value.status_code = 200
        mock_get.return_value.content = b"print('ok')\n"
        mock_get.return_value.raise_for_status.return_value = None

        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": pv.id, "subpath": "src/app.py"},
        )
        resp = self.client.get(blob_url)
        self.assertEqual(resp.status_code, 200)

        called_url = mock_get.call_args.args[0]
        self.assertIn("/raw/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src/app.py", called_url)

    @patch("aist.api.files.requests.get")
    def test_git_hash_blob_uses_version(self, mock_get):
        self.project.repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="org",
            repo_name="repo",
            base_url="https://github.com",
        )
        self.project.save(update_fields=["repository"])
        pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )

        mock_get.return_value.status_code = 200
        mock_get.return_value.content = b"print('ok')\n"
        mock_get.return_value.raise_for_status.return_value = None

        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": pv.id, "subpath": "src/app.py"},
        )
        resp = self.client.get(blob_url)
        self.assertEqual(resp.status_code, 200)

        called_url = mock_get.call_args.args[0]
        self.assertIn("/raw/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/src/app.py", called_url)

    @patch("aist.api.files.requests.get")
    def test_git_branch_blob_without_last_resolved_uses_branch_version(self, mock_get):
        self.project.repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="org",
            repo_name="repo",
            base_url="https://github.com",
        )
        self.project.save(update_fields=["repository"])
        pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
            last_resolved_commit="",
        )

        mock_get.return_value.status_code = 200
        mock_get.return_value.content = b"print('ok')\n"
        mock_get.return_value.raise_for_status.return_value = None

        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": pv.id, "subpath": "src/app.py"},
        )
        resp = self.client.get(blob_url)
        self.assertEqual(resp.status_code, 200)

        called_url = mock_get.call_args.args[0]
        self.assertIn("/raw/main/src/app.py", called_url)

    def test_create_version_denies_other_product_project(self):
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.other_project.id})
        resp = self.client.post(
            url,
            data={"version_type": VersionType.GIT_HASH, "version": "cafebabecafebabecafebabecafebabecafebabe"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_file_blob_denies_other_product_version(self):
        archive_bytes = self._zip_with_file("main.py", "print('other')\n")
        upload = SimpleUploadedFile("other.zip", archive_bytes, content_type="application/zip")
        other_version = AISTProjectVersion.objects.create(
            project=self.other_project,
            version_type=VersionType.FILE_HASH,
            source_archive=upload,
        )
        blob_url = reverse(
            "aist_api:project_version_file_blob",
            kwargs={"project_version_id": other_version.id, "subpath": "main.py"},
        )
        resp = self.client.get(blob_url)
        self.assertEqual(resp.status_code, 404)
