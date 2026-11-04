"""Projects, their scripts, and the versions they are scanned at."""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tarfile
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.storage import default_storage
from django.db import IntegrityError, models, transaction
from django.db.models import Prefetch, prefetch_related_objects
from django.utils import timezone
from dojo.models import Finding, Product

from aist.models.organizations import Organization, OrgIntegration, OrgIntegrationType
from aist.models.scm import RepositoryInfo
from aist.utils.archive import (
    _flatten_single_root_directory,
    _safe_extract_tar_member,
    _safe_extract_zip_member,
    safe_join,
)

if TYPE_CHECKING:
    from aist.models.launching import LaunchSchedule


# --------- Error/validation messages ----------
ERR_FILEHASH_REQUIRES_SOURCE = "For FILE_HASH version type, source_archive is required."


ERR_VERSION_ALREADY_EXISTS = "This version already exists for the selected project."


ERR_UNSUPPORTED_ARCHIVE = "Unsupported archive format: not a ZIP or TAR.*"


ERR_GITHASH_PARENT_MUST_BE_BRANCH = "resolved_from_branch must point to a GIT_BRANCH version."


ERR_GITHASH_PARENT_PROJECT_MISMATCH = "resolved_from_branch must belong to the same project."


ERR_RESOLVED_FROM_BRANCH_ONLY_FOR_GITHASH = "resolved_from_branch is allowed only for GIT_HASH versions."


ERR_SOURCELESS_REQUIRES_VERSION = "A version with no source revision must still name what it identifies."


ERR_SOURCELESS_REJECTS_SOURCE = "A version with no source revision cannot carry a source archive."


ERR_INVALID_GIT_REF = "A Git branch or commit must be a valid Git ref name."


ERR_SCRIPT_BINDING_REQUIRES_BRANCH = "A generated script can be bound only to a GIT_BRANCH version."


class AISTProject(models.Model):
    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    product = models.OneToOneField(Product, on_delete=models.CASCADE)
    supported_languages = models.JSONField(default=list, blank=True)
    compilable = models.BooleanField(default=False)
    profile = models.JSONField(default=dict, blank=True)
    repository = models.OneToOneField(
        RepositoryInfo,
        on_delete=models.CASCADE,
        related_name="project",
        null=True,
        blank=True,
    )
    ai_default_filter = models.JSONField(null=True, blank=True, default=None)

    def __str__(self) -> str:
        return self.product.name

    @property
    def organization(self) -> Organization | None:
        """Return the sole organization that owns this project's Product_Type."""
        if self.product_id is None:
            return None
        try:
            return self.product.prod_type.aist_organization
        except Organization.DoesNotExist:
            return None

    @property
    def organization_id(self) -> int | None:
        organization = self.organization
        return organization.pk if organization is not None else None

    @staticmethod
    def _pick_active_version(versions) -> AISTProjectVersion | None:
        """
        Choose the version whose script the project's next run uses.

        The newest GIT_BRANCH wins: branch runs copy its script into each commit
        they resolve. A project without branches (e.g. only uploaded archives) uses
        its newest version that has a source; sourceless DAST targets run no script.
        """
        candidates = [v for v in versions if v.version_type not in SOURCELESS_VERSION_TYPES]
        branches = [v for v in candidates if v.version_type == VersionType.GIT_BRANCH]
        return max(branches or candidates, key=lambda v: (v.created, v.pk), default=None)

    @classmethod
    def prefetch_active_scripts(cls, projects) -> None:
        """
        Resolve ``active_version`` / ``active_script`` for many projects in two queries.

        Prefetches every project's versions (with scripts, newest first, so
        ``project.versions.all()`` is served from the same prefetch) and fetches the
        shared default once; the properties then read the cached values.
        """
        prefetch_related_objects(
            projects,
            Prefetch("versions", queryset=AISTProjectVersion.objects.select_related("script").order_by("-created")),
        )
        shared_default = AISTProjectScript.get_shared_default()
        for project in projects:
            version = cls._pick_active_version(project.versions.all())
            project._active_version = version
            project._active_script = version.script if version is not None and version.script_id else shared_default

    @property
    def active_version(self) -> AISTProjectVersion | None:
        """
        The version the next run uses (rule: ``_pick_active_version``).

        Served from the ``prefetch_active_scripts`` cache when present; otherwise two
        indexed queries, so a project's many commit versions are never loaded.
        """
        if "_active_version" in self.__dict__:
            return self._active_version
        with_source = (
            self.versions
            .exclude(version_type__in=SOURCELESS_VERSION_TYPES)
            .select_related("script")
            .order_by("-created", "-pk")
        )
        return with_source.filter(version_type=VersionType.GIT_BRANCH).first() or with_source.first()

    @property
    def active_script(self) -> AISTProjectScript:
        """
        Return the script the project's next run uses: ``active_version``'s effective script.

        A project without such a version shows the shared default. A revision not
        bound to any version is never reported as active. List views must call
        ``prefetch_active_scripts`` first; see views/projects.py.
        """
        if "_active_script" in self.__dict__:
            return self._active_script
        version = self.active_version
        return version.effective_script if version is not None else AISTProjectScript.get_shared_default()

    def get_launch_schedule(self) -> LaunchSchedule | None:
        try:
            return self.launch_schedule
        except ObjectDoesNotExist:
            return None


class ProjectIntegrationOverride(models.Model):

    """
    Per-project override for an org-level integration.

    Only non-secret config can be overridden here (e.g. Slack channel,
    email recipients). The secret is always taken from ``org_integration``.
    If ``org_integration`` is None, the first active org integration of
    ``integration_type`` for the project's org is used.
    """

    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE, related_name="integration_overrides")
    integration_type = models.CharField(max_length=32, choices=OrgIntegrationType.choices)
    org_integration = models.ForeignKey(
        OrgIntegration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="project_overrides",
    )
    config_override = models.JSONField(default=dict, blank=True)
    is_disabled = models.BooleanField(
        default=False,
        help_text="When True, the org-level integration of this type is disabled for this project.",
    )

    class Meta:
        unique_together = [("project", "integration_type")]

    def __str__(self) -> str:
        return f"{self.project} / {self.integration_type}"

    def clean(self):
        super().clean()
        if self.org_integration_id is None:
            return
        integration = self.org_integration
        errors = {}
        if integration.organization_id != self.project.organization_id:
            errors["org_integration"] = "Integration belongs to a different organization."
        if integration.integration_type != self.integration_type:
            errors["org_integration"] = "Integration type must match the override type."
        if errors:
            raise ValidationError(errors)


class AISTProjectScript(models.Model):

    """
    Versioned snapshot of a project's entrypoint script.

    Project-specific revisions are append-only: each new save creates a new row.
    The shared default (``is_shared=True``, ``project=None``) is the singleton
    template used when creating new versions without an explicit script.
    It may be updated in-place via ``scope=global`` (superuser only).
    Use ``get_shared_default()`` to obtain it.

    ``AISTProjectVersion.script`` always references a project-scoped script
    (``project=version.project``), never the shared singleton directly —
    this guarantees org isolation and immutable version history.
    Use ``get_or_create_for_project()`` to snapshot the current global template
    into a project-scoped copy without content duplication.
    """

    project = models.ForeignKey(
        AISTProject,
        on_delete=models.CASCADE,
        related_name="script_revisions",
        null=True,   # NULL for the shared default script
        blank=True,
    )
    is_shared = models.BooleanField(
        default=False,
        help_text="True for the singleton shared default script (project=None).",
    )
    content = models.TextField()
    sha256 = models.CharField(max_length=64, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Database-level guarantee: only one shared default can ever exist.
            # Combined with Django's get_or_create() IntegrityError retry this
            # makes get_shared_default() race-condition-safe.
            models.UniqueConstraint(
                fields=["is_shared"],
                condition=models.Q(is_shared=True),
                name="uniq_aistprojectscript_shared_singleton",
            ),
        ]

    def __str__(self) -> str:
        if self.is_shared:
            return f"Shared default script (sha256={self.sha256[:8]})"
        return f"Script rev for {self.project} @ {self.created_at} (sha256={self.sha256[:8]})"

    def save(self, *args, **kwargs):
        self.sha256 = hashlib.sha256(self.content.encode()).hexdigest()
        super().save(*args, **kwargs)

    @classmethod
    def get_shared_default(cls) -> AISTProjectScript:
        """
        Return the singleton shared default script, creating it if absent.

        Safe under concurrent access: the partial unique index on is_shared=True
        ensures only one row can exist.  If two workers race to create the
        singleton simultaneously, the loser catches IntegrityError and falls
        back to a plain GET, so callers always receive a valid instance.
        """
        from aist.default_script import DEFAULT_ENTRYPOINT_SCRIPT  # noqa: PLC0415

        try:
            script, _ = cls.objects.get_or_create(
                is_shared=True,
                defaults={"project": None, "content": DEFAULT_ENTRYPOINT_SCRIPT},
            )
        except IntegrityError:
            # Another worker won the race and already created the singleton.
            script = cls.objects.get(is_shared=True)
        return script

    @classmethod
    def get_or_create_for_project(
        cls,
        content: str,
        project: AISTProject,
        user=None,
    ) -> tuple[AISTProjectScript, bool]:
        """
        Return a project-scoped script with the given content, creating it if absent.

        Deduplicates by sha256 within the project so identical content reuses the
        same row instead of proliferating copies.  The returned script always has
        ``project=project`` and ``is_shared=False``.
        """
        sha = hashlib.sha256(content.encode()).hexdigest()
        existing = cls.objects.filter(sha256=sha, project=project, is_shared=False).first()
        if existing:
            return existing, False
        return cls.objects.create(
            content=content,
            project=project,
            is_shared=False,
            created_by=user,
        ), True

    @classmethod
    def for_new_version(cls, project: AISTProject) -> AISTProjectScript:
        """
        Return the script a newly created version of ``project`` starts with.

        The latest project revision (set at project creation or via API), else a
        project-scoped copy of the shared default. Every code path that creates a
        version without an explicit script uses this, so a new version never runs
        something other than what the project was configured with.
        """
        latest_revision = project.script_revisions.order_by("-created_at").first()
        if latest_revision:
            return latest_revision
        script, _ = cls.get_or_create_for_project(
            content=cls.get_shared_default().content,
            project=project,
        )
        return script

    @classmethod
    def create_for_version(
        cls,
        content: str,
        version: AISTProjectVersion,
        user=None,
    ) -> tuple[AISTProjectScript, bool]:
        """
        Store ``content`` as a project revision and make it the script of ``version``.

        Only a GIT_BRANCH version is accepted: branch pipelines copy the branch
        script into each resolved GIT_HASH version, so binding the branch is what
        makes the script run. No other version is touched. The project row lock
        serializes concurrent calls, so identical content never yields two revisions.
        """
        with transaction.atomic():
            # Lock order project → version, parent before child.
            project = AISTProject.objects.select_for_update().get(pk=version.project_id)
            locked_version = (
                AISTProjectVersion.objects
                .select_for_update()
                .get(pk=version.pk, project=project)
            )
            if locked_version.version_type != VersionType.GIT_BRANCH:
                raise ValidationError({"version": ERR_SCRIPT_BINDING_REQUIRES_BRANCH})
            script, created = cls.get_or_create_for_project(content=content, project=project, user=user)
            locked_version.script = script
            locked_version.clean()
            locked_version.save(update_fields=["script", "updated"])
        return script, created


class VersionType(models.TextChoices):
    GIT_BRANCH = "GIT_BRANCH", "Git branch"
    GIT_HASH = "GIT_HASH", "Git commit/hash"
    FILE_HASH = "FILE_HASH", "File hash (uploaded archive)"
    DAST_TARGET = "DAST_TARGET", "DAST target (no source revision)"


# A scan of a running system that carries no source revision still produces results that
# belong somewhere: findings reach a project only through a version, so the target itself is
# the version they are attached to. One row per target per project, reused by every run.
SOURCELESS_VERSION_TYPES = frozenset({VersionType.DAST_TARGET})


# Sourceless versions are created by the import that produces their findings, never by hand:
# they identify a scan target, so an operator has nothing to fill in and nothing to choose.
OPERATOR_CREATABLE_VERSION_TYPES = tuple(
    (value, label)
    for value, label in VersionType.choices
    if value not in SOURCELESS_VERSION_TYPES
)


ERR_FILE_NOT_FOUND_IN_ARCHIVE = "File not found in version archive"
ERR_FILE_NOT_FOUND_IN_REPOSITORY = "File not found in remote repository"


logger = logging.getLogger(__name__)


def is_valid_git_ref(value: str) -> bool:
    """Whether `value` is a Git branch, tag or commit name by the `git check-ref-format` rules."""
    if not value or len(value) > 255:
        return False
    if value.startswith(("-", ".", "/")) or value.endswith((".", "/", ".lock")):
        return False
    if ".." in value or "@{" in value or "//" in value or "/." in value:
        return False
    return not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 or character in "~^:?*[\\"
        for character in value
    )


class SourceFileNotFoundError(Exception):

    """The requested file is absent from the version's source or its path is refused; the message is client-safe."""


def _relative_source_path(subpath: str) -> str | None:
    """Return `subpath` with `/` separators, or None when it is empty, absolute, has a `..` segment or a NUL."""
    path = PurePosixPath(subpath.replace("\\", "/"))
    if not subpath or "\x00" in subpath or path.is_absolute() or ".." in path.parts:
        return None
    return str(path)


class AISTProjectVersion(models.Model):
    project = models.ForeignKey(
        AISTProject, on_delete=models.CASCADE, related_name="versions",
    )
    version = models.CharField(max_length=255, db_index=True)
    last_resolved_commit = models.CharField(max_length=40, blank=True, default="")
    last_resolved_at = models.DateTimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    findings = models.ManyToManyField(Finding, related_name="aist_project_versions", blank=True)
    resolved_from_branch = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="resolved_hash_versions",
    )

    created = models.DateTimeField(auto_now_add=True, editable=False)
    updated = models.DateTimeField(auto_now=True)
    version_type = models.CharField(
        max_length=16,
        choices=VersionType.choices,
        default=VersionType.GIT_BRANCH,
        db_index=True,
    )

    def _upload_to(self, filename: str) -> str:
        return f"aist_versions/{self.project_id}/{timezone.now():%Y/%m/%d}/{filename}"

    source_archive = models.FileField(upload_to=_upload_to, null=True, blank=True)  # noqa: DJ012
    source_archive_sha256 = models.CharField(max_length=64, blank=True, null=True, default="")
    script = models.ForeignKey(
        "AISTProjectScript",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="version_overrides",
        help_text="Script used for this version. Always project-scoped (script.project == self.project).",
    )

    class Meta:  # noqa: DJ012
        constraints = [
            models.UniqueConstraint(
                fields=["project", "version", "version_type"],
                name="uniq_project_version_type_per_project",
            ),
        ]
        ordering = ["-created"]

    def __str__(self):  # noqa: DJ012
        return f"{self.project_id}:{self.version}"

    def save(self, *args, **kwargs):  # noqa: DJ012
        if self.version_type == VersionType.FILE_HASH and not self.version:
            if self.source_archive:
                sha = self._compute_file_sha256()
                self.source_archive_sha256 = sha
                self.version = sha

        super().save(*args, **kwargs)

    def clean(self):
        if self.script_id and self.script.project_id != self.project_id:
            raise ValidationError(
                {"script": "Script must belong to the same project as this version."},
            )

        if self.version_type == VersionType.FILE_HASH:
            if not self.source_archive:
                raise ValidationError(ERR_FILEHASH_REQUIRES_SOURCE)
            v = (self.version or "").strip()
            if v:
                exists = AISTProjectVersion.objects.filter(
                    project=self.project, version=v,
                ).exclude(pk=self.pk).exists()
                if exists:
                    raise ValidationError({"version": ERR_VERSION_ALREADY_EXISTS})

        if self.is_git() and not is_valid_git_ref(self.version or ""):
            raise ValidationError({"version": ERR_INVALID_GIT_REF})

        if self.version_type in SOURCELESS_VERSION_TYPES:
            if not (self.version or "").strip():
                raise ValidationError({"version": ERR_SOURCELESS_REQUIRES_VERSION})
            if self.source_archive:
                raise ValidationError({"source_archive": ERR_SOURCELESS_REJECTS_SOURCE})

        if self.resolved_from_branch_id and self.version_type != VersionType.GIT_HASH:
            raise ValidationError({"resolved_from_branch": ERR_RESOLVED_FROM_BRANCH_ONLY_FOR_GITHASH})

        if self.version_type == VersionType.GIT_HASH and self.resolved_from_branch_id:
            parent = self.resolved_from_branch
            if parent.version_type != VersionType.GIT_BRANCH:
                raise ValidationError({"resolved_from_branch": ERR_GITHASH_PARENT_MUST_BE_BRANCH})
            if parent.project_id != self.project_id:
                raise ValidationError({"resolved_from_branch": ERR_GITHASH_PARENT_PROJECT_MISMATCH})

    def as_dict(self):
        return {
            "id": self.pk,
            "version": self.version,
            "type": str(self.version_type),
            "extracted_root": str(self.get_extracted_root()),
        }

    def _compute_file_sha256(self) -> str:
        h = hashlib.sha256()
        for chunk in self.source_archive.chunks():
            h.update(chunk)
        return h.hexdigest()

    def get_extracted_root(self) -> Path:
        """
        Folder, where the extracted archive is located.
        Example: MEDIA_ROOT/aist_versions_extracted/<project_version_id>/
        """
        media_root = Path(getattr(settings, "MEDIA_ROOT", "media"))
        return media_root / "aist_versions_extracted" / str(self.id)

    def _extraction_marker_path(self) -> Path:
        return self.get_extracted_root() / ".extracted.ok"

    def _needs_extraction(self) -> bool:
        marker = self._extraction_marker_path()
        if not marker.exists():
            return True
        try:
            txt = marker.read_text(encoding="utf-8").strip()
        except Exception:
            return True
        return txt != (self.source_archive_sha256 or "")

    def requested_ref(self) -> str:
        # single source of truth: this is what user asked (branch/tag/sha or file hash)
        return (self.version or "").strip()

    def is_git(self) -> bool:
        return self.version_type in {VersionType.GIT_BRANCH, VersionType.GIT_HASH}

    @property
    def effective_script(self) -> AISTProjectScript:
        """Return the script a pipeline for this version runs: its own, else the shared default."""
        return self.script or AISTProjectScript.get_shared_default()

    def open_source_file(self, subpath: str, *, proxy_url: str | None = None) -> BinaryIO:
        """
        Open `subpath` of this version's source for binary reading; the caller closes it.

        An uploaded-archive version is read from its extracted archive; a Git version is read from the project's
        repository at :meth:`source_ref`, through `proxy_url` when it is set. A `subpath` that is absolute or has
        a `..` segment, a ref that is not a valid Git ref name, and an archive file that resolves outside the
        extraction are refused before anything is read.

        Raises SourceFileNotFoundError when the file is absent or refused, and ScmFetchError when the repository
        host fails the request; connection errors and timeouts propagate.
        """
        path = _relative_source_path(subpath)
        if self.version_type == VersionType.FILE_HASH:
            stream = self._open_archive_file(path) if path else None
            if stream is None:
                raise SourceFileNotFoundError(ERR_FILE_NOT_FOUND_IN_ARCHIVE)
            return stream
        ref = self.source_ref()
        repository = self.project.repository
        data = None
        if path and repository and self.is_git() and is_valid_git_ref(ref):
            data = repository.read_file(ref, path, proxy_url=proxy_url)
        if data is None:
            raise SourceFileNotFoundError(ERR_FILE_NOT_FOUND_IN_REPOSITORY)
        return io.BytesIO(data)

    def source_ref(self) -> str:
        """Return the commit a branch version last resolved to, else the version's own ref."""
        if self.version_type == VersionType.GIT_BRANCH and self.last_resolved_commit.strip():
            return self.last_resolved_commit.strip()
        return (self.version or "master").strip()

    def _open_archive_file(self, path: str) -> BinaryIO | None:
        """Open `path` in the extracted archive, or return None when it is absent, escapes it, or cannot be extracted."""
        try:
            root = self.ensure_extracted()
        except (FileNotFoundError, ValueError):
            logger.warning("Source archive of project version %s cannot be extracted", self.pk, exc_info=True)
            return None
        if root is None:
            return None
        try:
            file_path = safe_join(root, path)
        except ValueError:
            return None
        return file_path.open("rb") if file_path.is_file() else None

    def ensure_extracted(self) -> Path | None:
        """
        Ensure the uploaded archive is extracted under `get_extracted_root()`.

        - Idempotent: if marker exists and matches current SHA, skip work.
        - Secure extraction: delegates to _safe_extract_* helpers to prevent path traversal.
        - Post-process: if extraction yields exactly one top-level directory, flatten it.
        - Writes `.extracted.ok` containing the archive SHA so we can detect changes.
        """
        root = self.get_extracted_root()
        root.mkdir(parents=True, exist_ok=True)

        if not self.source_archive:
            return None  # nothing to extract

        # If already extracted and SHA matches, return early
        if not self._needs_extraction():
            return root

        # Clean the extraction directory (except the directory itself)
        for p in root.glob("*"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with suppress(OSError):
                    p.unlink()

        # Read the file via storage (works with non-local backends too)
        with default_storage.open(self.source_archive.name, "rb") as f:
            data = f.read()
        bio = io.BytesIO(data)

        # Detect format and extract securely
        if zipfile.is_zipfile(bio):
            bio.seek(0)
            with zipfile.ZipFile(bio) as zf:
                for member in zf.infolist():
                    _safe_extract_zip_member(zf, member, root)
        else:
            bio.seek(0)
            try:
                with tarfile.open(fileobj=bio, mode="r:*") as tf:
                    for member in tf.getmembers():
                        _safe_extract_tar_member(tf, member, root)
            except tarfile.ReadError:
                raise ValueError(ERR_UNSUPPORTED_ARCHIVE)

        # Flatten "<archive_name>/" level if it is the only top-level entry
        _flatten_single_root_directory(root)

        # Write marker with current SHA to avoid repeated extractions
        (root / ".extracted.ok").write_text(self.source_archive_sha256 or "", encoding="utf-8")

        return root
