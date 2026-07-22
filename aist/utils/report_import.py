"""Storage and project-version helpers for manual report imports."""
from __future__ import annotations

import hashlib
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from django.core.files.storage import default_storage
from django.db import transaction

from aist.models import AISTProjectVersion, VersionType


class ReportImportError(Exception):

    """Raised when a report import cannot be mapped onto the selected project."""


def resolve_import_version(project, commit_hash: str) -> AISTProjectVersion:
    """Resolve the canonical ``GIT_HASH`` project version for an import."""
    if project.repository is None:
        msg = f"Project '{project}' has no linked repository; cannot resolve a report import version."
        raise ReportImportError(msg)

    if not commit_hash:
        msg = "commit_hash is required to resolve which project version to attach findings to."
        raise ReportImportError(msg)

    with transaction.atomic():
        version, _created = (
            AISTProjectVersion.objects
            .select_for_update()
            .get_or_create(project=project, version=commit_hash, version_type=VersionType.GIT_HASH)
        )
    return version


def store_uploaded_report(uploaded_file) -> tuple[str, str]:
    """Persist an upload for Celery and return ``(storage_name, sha256_hex)``."""
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    suffix = Path(uploaded_file.name).suffix
    storage_name = default_storage.save(f"report_imports/{uuid4().hex}{suffix}", uploaded_file)
    return storage_name, digest.hexdigest()


def discard_uploaded_report(storage_name: str) -> None:
    """Delete a stored report upload."""
    with suppress(OSError):
        default_storage.delete(storage_name)
