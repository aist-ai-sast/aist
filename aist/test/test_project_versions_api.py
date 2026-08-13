from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.urls import reverse

from aist.api.files import ProjectVersionFileBlobAPI
from aist.api.project_versions import AISTProjectVersionCreateSerializer
from aist.models import AISTProjectScript, AISTProjectVersion, RepositoryInfo, ScmType, VersionType
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

    def test_local_file_rejects_parent_directory_escape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            extraction_root = Path(tmp_dir) / "extracted"
            extraction_root.mkdir()
            outside_file = Path(tmp_dir) / "outside.py"
            outside_file.write_text("secret", encoding="utf-8")
            project_version = SimpleNamespace(ensure_extracted=lambda: extraction_root)

            with self.assertRaises(Http404):
                ProjectVersionFileBlobAPI._return_local_file(project_version, "../outside.py")

    def test_local_file_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            extraction_root = Path(tmp_dir) / "extracted"
            extraction_root.mkdir()
            outside_file = Path(tmp_dir) / "outside.py"
            outside_file.write_text("secret", encoding="utf-8")
            (extraction_root / "linked.py").symlink_to(outside_file)
            project_version = SimpleNamespace(ensure_extracted=lambda: extraction_root)

            with self.assertRaises(Http404):
                ProjectVersionFileBlobAPI._return_local_file(project_version, "linked.py")

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

    def test_create_version_rejects_a_sourceless_type(self):
        """
        A DAST target version identifies a scan target and is created by the import that produces
        its findings; an operator has nothing to fill in, so the API must not offer the choice.
        """
        url = reverse("aist_api:project_version_create", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={"version_type": VersionType.DAST_TARGET, "version": "perimeter"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            AISTProjectVersion.objects.filter(version_type=VersionType.DAST_TARGET).exists(),
        )
        # The response body cannot be asserted on here: vendor's APITrailingSlashMiddleware
        # replaces the body of every 400 on a POST to an api/v2 path without a trailing slash,
        # and this route is declared without one. The rejected field is checked directly.
        serializer = AISTProjectVersionCreateSerializer(
            data={"version_type": VersionType.DAST_TARGET, "version": "perimeter"},
            context={"project": self.project},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("version_type", serializer.errors)

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


class ProjectVersionScriptGetAPITests(AISTApiBase):

    """GET on the per-version script endpoint with inheritance resolution."""

    def _json(self, resp):
        return json.loads(resp.content.decode("utf-8") or "{}")

    def _url(self, project_id: int, version_id: int) -> str:
        return reverse(
            "aist_api:project_version_script_update",
            kwargs={"project_id": project_id, "version_id": version_id},
        )

    def test_returns_versions_own_script(self):
        own_script = AISTProjectScript.objects.create(
            project=self.project,
            is_shared=False,
            content="#!/bin/bash\necho version-own\n",
        )
        self.pv.script = own_script
        self.pv.save(update_fields=["script"])

        resp = self.client.get(self._url(self.project.id, self.pv.id))
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        own_script.refresh_from_db()
        self.assertEqual(data["id"], own_script.id)
        self.assertEqual(data["content"].rstrip("\n"), own_script.content.rstrip("\n"))
        self.assertFalse(data["inherited"])
        self.assertEqual(data["source"], "version")
        self.assertFalse(data["is_shared"])

    def test_falls_back_to_latest_project_revision_when_version_has_no_script(self):
        # version intentionally created without script in AISTApiBase.setUp
        self.assertIsNone(self.pv.script_id)

        older_rev = AISTProjectScript.objects.create(
            project=self.project,
            is_shared=False,
            content="#!/bin/bash\necho old\n",
        )
        newer_rev = AISTProjectScript.objects.create(
            project=self.project,
            is_shared=False,
            content="#!/bin/bash\necho new\n",
        )

        resp = self.client.get(self._url(self.project.id, self.pv.id))
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        self.assertEqual(data["id"], newer_rev.id)
        newer_rev.refresh_from_db()
        self.assertEqual(data["content"].rstrip("\n"), newer_rev.content.rstrip("\n"))
        self.assertNotEqual(data["id"], older_rev.id)
        self.assertTrue(data["inherited"])
        self.assertEqual(data["source"], "project_revision")
        self.assertFalse(data["is_shared"])

    def test_falls_back_to_shared_default_when_no_revisions(self):
        self.assertIsNone(self.pv.script_id)
        self.assertFalse(self.project.script_revisions.exists())

        resp = self.client.get(self._url(self.project.id, self.pv.id))
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        self.assertEqual(data["content"].rstrip("\n"), AISTProjectScript.get_shared_default().content.rstrip("\n"))
        self.assertTrue(data["inherited"])
        self.assertEqual(data["source"], "shared_default")
        self.assertTrue(data["is_shared"])

    def test_returns_404_for_other_org_project(self):
        resp = self.client.get(self._url(self.other_project.id, self.other_pv.id))
        self.assertEqual(resp.status_code, 404)

    def test_returns_404_for_version_not_in_project(self):
        # other_pv belongs to other_project, not self.project; must not leak.
        resp = self.client.get(self._url(self.project.id, self.other_pv.id))
        self.assertEqual(resp.status_code, 404)

    def test_returns_404_for_unknown_version(self):
        resp = self.client.get(self._url(self.project.id, 99999999))
        self.assertEqual(resp.status_code, 404)
