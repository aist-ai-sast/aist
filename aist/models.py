from __future__ import annotations

import base64
import hashlib
import io
import logging
import shutil
import tarfile
import uuid
import zipfile
from contextlib import contextmanager, suppress
from datetime import datetime as dt
from pathlib import Path
from urllib.parse import quote

import gitlab
from asgiref.sync import async_to_sync
from croniter import croniter
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import RegexValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.crypto import get_random_string
from django_github_app.models import Installation
from dojo.models import Finding, Product, Product_Type, Test
from encrypted_model_fields.fields import EncryptedCharField

from aist.execution.launch_request import (
    LaunchRequestSnapshotError,
    LaunchRequestSnapshots,
    validated_secret_free_json,
)
from aist.integrations.dast_config import (
    DastBindingParameters,
    DastConfigError,
    DastIntegrationConfig,
    DastTargetSnapshot,
)
from aist.profile import ProjectProfile

_repo_part_validator = RegexValidator(
    regex=r"^[A-Za-z0-9._/\-]+$",
    message="Only letters, digits, dot, underscore, hyphen and slash are allowed.",
)

# --------- Error/validation messages ----------
ERR_FILEHASH_REQUIRES_SOURCE = "For FILE_HASH version type, source_archive is required."
ERR_VERSION_ALREADY_EXISTS = "This version already exists for the selected project."
ERR_UNSUPPORTED_ARCHIVE = "Unsupported archive format: not a ZIP or TAR.*"
ERR_GITHASH_PARENT_MUST_BE_BRANCH = "resolved_from_branch must point to a GIT_BRANCH version."
ERR_GITHASH_PARENT_PROJECT_MISMATCH = "resolved_from_branch must belong to the same project."
ERR_RESOLVED_FROM_BRANCH_ONLY_FOR_GITHASH = "resolved_from_branch is allowed only for GIT_HASH versions."
ERR_SOURCELESS_REQUIRES_VERSION = "A version with no source revision must still name what it identifies."
ERR_SOURCELESS_REJECTS_SOURCE = "A version with no source revision cannot carry a source archive."


class ScmType(models.TextChoices):
    GITHUB = "GITHUB", "Github"
    GITLAB = "GITLAB", "Gitlab"
    GERRIT = "GERRIT", "Gerrit"
    GITEA = "GITEA", "Gitea"


class RepositoryInfo(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    type = models.CharField(max_length=64, choices=ScmType.choices, default=ScmType.GITHUB)
    repo_owner = models.CharField(max_length=100, validators=[_repo_part_validator])
    repo_name = models.CharField(max_length=100, validators=[_repo_part_validator])
    base_url = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["repo_owner", "repo_name", "type"])]

    def get_binding(self):
        mapping = {
            ScmType.GITHUB: "github_binding",
            ScmType.GITLAB: "gitlab_binding",
            ScmType.GERRIT: "gerrit_binding",
            ScmType.GITEA: "gitea_binding",
        }
        attr = mapping.get(self.type)
        return getattr(self, attr, None) if attr else None

    @property
    def clone_url(self) -> str:
        binding = self.get_binding()
        if binding:
            url = binding.build_clone_url(self)
            if url:
                return url
        return f"{self.host()}/{self.repo_full}.git"

    @property
    def repo_full(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    def host(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return "https://github.com" if self.type == ScmType.GITHUB else "https://gitlab.com"


def _inject_creds(host: str, creds: str) -> str:
    """
    Insert ``creds@`` right after the scheme of an http(s) URL.

    Self-hosted SCM instances (Gerrit, Gitea, GitHub/GitLab Enterprise) are
    frequently reachable only over plain ``http://`` on an internal network.
    A hardcoded ``.replace('https://', ...)`` silently no-ops on those hosts,
    returning a credential-less URL that fails non-interactive ``git clone``
    with "could not read Username" instead of authenticating.
    """
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            return f"{scheme}{creds}@{host[len(scheme):]}"
    return host


class ScmGithubBinding(models.Model):

    """GitHub-specific binding for ScmInfo."""

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="github_binding")
    installation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    org_integration = models.ForeignKey(
        "OrgIntegration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        limit_choices_to={"integration_type": "GITHUB"},
        related_name="+",
    )

    def _base_api_url(self) -> str:
        """GitHub Enterprise API base URL from org integration config."""
        if self.org_integration:
            return (self.org_integration.config or {}).get("base_api_url", "")
        return ""

    def host(self, scm: RepositoryInfo) -> str:
        return scm.host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        # sync option
        logger = logging.getLogger("aist")
        if not self.installation_id:
            logger.warning("No installation ID for GitHub binding")
            return None
        inst = Installation.objects.filter(installation_id=self.installation_id).first()
        if not inst:
            logger.warning("No installation object for GitHub binding")
            return None
        token = async_to_sync(_aget_installation_access_token)(inst)
        if not token:
            logger.warning("No access token for GitHub binding installation_id=%s", self.installation_id)
            return None
        return f"{_inject_creds(self.host(scm), 'x-access-token:' + token)}/{scm.repo_full}.git"

    def build_blob_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # https://github.com/owner/repo/blob/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        return f"{self.host(scm).rstrip('/')}/{scm.repo_full}/blob/{ref}/{fp}"

    def build_raw_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # https://raw.githubusercontent.com/owner/repo/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        host = self.host(scm)
        if "github.com" in host:
            raw_host = "https://raw.githubusercontent.com"
        else:
            return f"{host.rstrip('/')}/{scm.repo_full}/raw/{ref}/{fp}"
        return f"{raw_host.rstrip('/')}/{scm.repo_full}/{ref}/{fp}"

    def get_auth_headers(self) -> dict[str, str]:
        if not self.installation_id:
            return {}
        inst = Installation.objects.filter(installation_id=self.installation_id).first()
        if not inst:
            return {}
        token = async_to_sync(_aget_installation_access_token)(inst)
        if not token:
            return {}
        return {"Authorization": f"token {token}"}

    def get_project_info(self, scm: RepositoryInfo):
        logger = logging.getLogger("aist")

        owner = scm.repo_owner
        name = scm.repo_name

        if not self.installation_id:
            logger.warning("No installation ID for GitHub binding")
            return None
        installation = Installation.objects.filter(installation_id=self.installation_id).first()
        if not installation:
            logger.warning("No installation object for GitHub binding")
            return None

        try:
            data = async_to_sync(_aget_github_repo_info)(installation, owner, name)
        except Exception:
            logger.exception("Failed to fetch repo data from GitHub API for %s/%s", owner, name)
            return None

        return data


async def _aget_github_repo_info(installation: Installation, owner: str, name: str) -> dict:
    async with installation.get_gh_client() as gh:
        return await gh.getitem(f"/repos/{owner}/{name}")


async def _aget_installation_access_token(installation: Installation) -> str | None:
    async with installation.get_gh_client() as gh:
        return await installation.aget_access_token(gh)


class ScmGitlabBinding(models.Model):

    """GitLab-specific binding for ScmInfo."""

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="gitlab_binding")
    org_integration = models.ForeignKey(
        "OrgIntegration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        limit_choices_to={"integration_type": "GITLAB"},
        related_name="+",
    )

    def _token(self) -> str:
        """Personal access token from org integration."""
        if self.org_integration:
            return (self.org_integration.secret or "").strip()
        return ""

    def host(self, scm: RepositoryInfo) -> str:
        return scm.host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        token = self._token()
        if not token:
            return None
        # GitLab HTTPS clone with PAT: https://oauth2:<PAT>@gitlab.com/owner/repo.git
        return f"{_inject_creds(self.host(scm), 'oauth2:' + token)}/{scm.repo_full}.git"

    def build_blob_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # https://gitlab.com/group/repo/-/blob/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        return f"{self.host(scm).rstrip('/')}/{scm.repo_full}/-/blob/{ref}/{fp}"

    def build_raw_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        """Return GitLab API v4 raw-file URL (no redirects, works with PRIVATE-TOKEN)."""
        fp = quote(path.lstrip("/").replace("\\", "/"), safe="")          # encode path
        proj = quote(scm.repo_full, safe="")                               # encode owner/repo
        ref_q = quote(ref or "master", safe="")                            # encode ref
        base = scm.host()                                                 # e.g. https://gitlab.com
        api_base = f"{base}/api/v4"
        return f"{api_base}/projects/{proj}/repository/files/{fp}/raw?ref={ref_q}"

    def get_auth_headers(self) -> dict[str, str]:
        """Return API auth header for GitLab."""
        tok = self._token()
        return {"PRIVATE-TOKEN": tok} if tok else {}

    @staticmethod
    def _fetch_gitlab_project(base: str, token: str | None, repo_full: str, proxy_url: str | None):
        kwargs: dict = {"private_token": token or None}
        if proxy_url:
            import requests as _requests  # noqa: PLC0415
            session = _requests.Session()
            session.proxies = {"http": proxy_url, "https": proxy_url}
            kwargs["session"] = session
        gl = gitlab.Gitlab(base, **kwargs)
        return gl.projects.get(repo_full)

    def get_project_info(self, scm: RepositoryInfo, *, proxy_url: str | None = None):
        logger = logging.getLogger("aist")
        base = self.host(scm).rstrip("/")
        token = self._token()

        try:
            project = self._fetch_gitlab_project(base, token, scm.repo_full, proxy_url)
        except gitlab.exceptions.GitlabGetError as exc:
            logger.warning(
                "GitLab API %s returned %s when requesting default branch for %s",
                base,
                exc.response_code,
                scm.repo_full,
            )
            return None
        except Exception:
            logger.exception("Failed to query GitLab API for default branch of %s", scm.repo_full)
            return None

        return project.attributes


class ScmGerritBinding(models.Model):

    """
    Gerrit-specific binding for RepositoryInfo.

    Gerrit has no owner/repo split: a project is a slash-path
    (e.g. ``platform/build/soong``). The import splits it by the last ``/`` so
    that ``scm.repo_full`` (``repo_owner`` + ``/`` + ``repo_name``)
    reconstructs the full project path loss-lessly. Authenticated REST and
    clone use the ``/a/`` path prefix with an HTTP password (Gerrit "HTTP
    credentials"); the HTTP user lives in ``org_integration.config["username"]``
    and the password in ``org_integration.secret``.
    """

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="gerrit_binding")
    org_integration = models.ForeignKey(
        "OrgIntegration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        limit_choices_to={"integration_type": "GERRIT"},
        related_name="+",
    )

    def _username(self) -> str:
        if self.org_integration:
            return ((self.org_integration.config or {}).get("username") or "").strip()
        return ""

    def _password(self) -> str:
        if self.org_integration:
            return (self.org_integration.secret or "").strip()
        return ""

    @staticmethod
    def _project_path(scm: RepositoryInfo) -> str:
        """
        Full Gerrit project path.

        Import splits the path by the last ``/`` into ``repo_owner``/``repo_name``;
        single-segment projects (e.g. ``All-Projects``) have an empty owner, so
        fall back to ``repo_name`` to avoid a leading slash in ``repo_full``.
        """
        return scm.repo_full if scm.repo_owner else scm.repo_name

    def host(self, scm: RepositoryInfo) -> str:
        return scm.host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        user = self._username()
        pw = self._password()
        if not user or not pw:
            return None
        creds = f"{quote(user, safe='')}:{quote(pw, safe='')}"
        # Authenticated Gerrit HTTP clone: https://user:pass@host/a/<full/project/path>
        return f"{_inject_creds(self.host(scm), creds)}/a/{self._project_path(scm)}"

    def build_blob_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # Gitiles browse URL: https://host/plugins/gitiles/<project>/+/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        return f"{self.host(scm).rstrip('/')}/plugins/gitiles/{self._project_path(scm)}/+/{ref}/{fp}"

    def build_raw_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        """
        Return the Gerrit REST content endpoint URL.

        The body is base64-encoded — use :meth:`fetch_raw_bytes` to obtain the
        decoded file. A plain GET on this URL yields base64 text, not raw bytes.
        """
        proj = quote(self._project_path(scm), safe="")
        ref_q = quote(ref or "master", safe="")
        fp = quote(path.lstrip("/").replace("\\", "/"), safe="")
        base = self.host(scm).rstrip("/")
        return f"{base}/a/projects/{proj}/branches/{ref_q}/files/{fp}/content"

    def get_auth_headers(self) -> dict[str, str]:
        user = self._username()
        pw = self._password()
        if not user or not pw:
            return {}
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def fetch_raw_bytes(
        self, scm: RepositoryInfo, ref: str, path: str, *, proxy_url: str | None = None,
    ) -> bytes | None:
        """
        Fetch a file's decoded bytes from Gerrit (content endpoint is base64).

        When ``proxy_url`` is set the request routes through the warm per-VPN
        egress proxy; connection/timeout errors are then re-raised so the caller
        can detect a cold tunnel and answer ``202 warming`` instead of ``404``.
        """
        import requests as _requests  # noqa: PLC0415

        logger = logging.getLogger("aist")
        url = self.build_raw_url(scm, ref, path)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = _requests.get(url, headers=self.get_auth_headers(), timeout=10, proxies=proxies)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return base64.b64decode(resp.text)
        except (_requests.ConnectionError, _requests.Timeout):
            if proxy_url:
                raise  # cold egress → let the endpoint return 202 warming
            logger.exception("Failed to fetch raw file from Gerrit for %s", scm.repo_full)
            return None
        except Exception:
            logger.exception("Failed to fetch raw file from Gerrit for %s", scm.repo_full)
            return None

    def get_project_info(self, scm: RepositoryInfo, *, proxy_url: str | None = None):
        import pygerrit2  # noqa: PLC0415

        logger = logging.getLogger("aist")
        base = self.host(scm).rstrip("/")
        user = self._username()
        pw = self._password()
        proj = quote(self._project_path(scm), safe="")
        try:
            auth = pygerrit2.HTTPBasicAuth(user, pw) if user and pw else None
            rest = pygerrit2.GerritRestAPI(url=base, auth=auth)
            if proxy_url:
                rest.session.proxies = {"http": proxy_url, "https": proxy_url}
            head = rest.get(f"/projects/{proj}/HEAD")
        except Exception:
            logger.exception("Failed to query Gerrit API for default branch of %s", scm.repo_full)
            return None

        default_branch = str(head or "").removeprefix("refs/heads/").strip() or "master"
        return {"default_branch": default_branch}


class ScmGiteaBinding(models.Model):

    """
    Gitea-specific binding for RepositoryInfo.

    Gitea projects are ``owner/repo`` like GitHub/GitLab (no arbitrary nested
    subgroups), so the import splits on the last ``/`` the same way GitLab
    paths do. Auth is a personal access token stored in
    ``org_integration.secret``, sent as ``Authorization: token <PAT>`` (Gitea's
    documented header format — not Bearer, not Basic). No dedicated Gitea
    Python client is vendored in this project, so REST calls use ``requests``
    directly against Gitea's stable ``/api/v1`` surface, same as the ad-hoc
    Gerrit raw-content fetch already does.
    """

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="gitea_binding")
    org_integration = models.ForeignKey(
        "OrgIntegration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        limit_choices_to={"integration_type": "GITEA"},
        related_name="+",
    )

    def _token(self) -> str:
        if self.org_integration:
            return (self.org_integration.secret or "").strip()
        return ""

    def host(self, scm: RepositoryInfo) -> str:
        return scm.host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        token = self._token()
        if not token:
            return None
        # Gitea documented HTTPS clone with a personal access token as the
        # username: https://<token>@gitea.example.com/owner/repo.git
        return f"{_inject_creds(self.host(scm), quote(token, safe=''))}/{scm.repo_full}.git"

    def build_blob_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # Gitea web UI browse URL: https://host/owner/repo/src/branch/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        return f"{self.host(scm).rstrip('/')}/{scm.repo_full}/src/branch/{ref}/{fp}"

    def build_raw_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # Gitea REST raw-content endpoint — returns raw bytes directly (no
        # base64 wrapping, unlike Gerrit), so no fetch_raw_bytes hook is needed.
        fp = quote(path.lstrip("/").replace("\\", "/"), safe="")
        ref_q = quote(ref or "master", safe="")
        base = self.host(scm).rstrip("/")
        return f"{base}/api/v1/repos/{scm.repo_full}/raw/{fp}?ref={ref_q}"

    def get_auth_headers(self) -> dict[str, str]:
        token = self._token()
        return {"Authorization": f"token {token}"} if token else {}

    def get_project_info(self, scm: RepositoryInfo, *, proxy_url: str | None = None):
        import requests as _requests  # noqa: PLC0415

        logger = logging.getLogger("aist")
        base = self.host(scm).rstrip("/")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = _requests.get(
                f"{base}/api/v1/repos/{scm.repo_full}",
                headers=self.get_auth_headers(),
                timeout=15,
                proxies=proxies,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Failed to query Gitea API for default branch of %s", scm.repo_full)
            return None

        return {"default_branch": data.get("default_branch") or "master"}


class PullRequest(models.Model):
    project_version = models.ForeignKey(
        "AISTProjectVersion",
        on_delete=models.CASCADE,
        related_name="pull_requests",
    )

    repository = models.ForeignKey(
        RepositoryInfo,
        on_delete=models.CASCADE,
        related_name="pull_requests",
    )

    pr_number = models.PositiveIntegerField()

    base_ref = models.CharField(max_length=255, blank=True)
    head_ref = models.CharField(max_length=255, blank=True)
    is_from_fork = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project_version", "repository", "pr_number"],
                name="uniq_pr_per_project_version",
            ),
        ]
        indexes = [
            models.Index(fields=["repository", "pr_number"]),
        ]

    def __str__(self):
        return f"{self.repository.repo_full}#{self.pr_number}->PV:{self.project_version_id}"


class AISTStatus(models.TextChoices):
    ADMITTED = "ADMITTED", "Admitted"
    EXECUTING = "EXECUTING", "Executing"
    UPLOADING_RESULTS = "UPLOADING_RESULTS", "Uploading Results"
    FINDING_POSTPROCESSING = "FINDING_POSTPROCESSING", "Finding post-processing"
    WAITING_DEDUPLICATION_TO_FINISH = "WAITING_DEDUPLICATION_TO_FINISH", "Waiting Deduplication To Finish"
    WAITING_CONFIRMATION_TO_PUSH_TO_AI = "WAITING_CONFIRMATION_TO_PUSH_TO_AI", "Waiting Confirmation To Push to AI"
    PUSH_TO_AI = "PUSH_TO_AI", "Push to AI"
    WAITING_RESULT_FROM_AI = "WAITING_RESULT_FROM_AI", "Waiting Result From AI"
    FINISHED = "FINISHED", "Finished"
    FINISHED_WITH_WARNINGS = "FINISHED_WITH_WARNINGS", "Finished With Warnings"


class Organization(models.Model):

    """
    Simple organization/group entity for AIST projects.

    Project ownership is derived through the organization's Product_Type;
    AISTProject intentionally has no duplicated organization foreign key.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    updated = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    product_type = models.OneToOneField(
        Product_Type,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="aist_organization",
    )

    ai_default_filter = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def ensure_product_type(self) -> Product_Type:
        if self.product_type_id:
            return self.product_type
        product_type, _ = Product_Type.objects.get_or_create(name=self.name)
        self.product_type = product_type
        self.save(update_fields=["product_type"])
        return product_type


class OrgIntegrationType(models.TextChoices):
    GITLAB = "GITLAB", "GitLab"
    GITHUB = "GITHUB", "GitHub"
    SLACK = "SLACK", "Slack"
    EMAIL = "EMAIL", "Email"
    VPN = "VPN", "VPN"
    CLAUDE_CODE = "CLAUDE_CODE", "Claude Code"
    GERRIT = "GERRIT", "Gerrit"
    GITEA = "GITEA", "Gitea"
    DAST = "DAST", "DAST"


class OrgIntegration(models.Model):

    """
    Org-level credential/config store for external integrations.

    Replaces per-binding GitLab PATs and per-action Slack tokens.
    ``secret`` is encrypted at rest and never returned by the API.
    ``config`` holds non-secret settings (base_url, channel, from_email …).
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="integrations")
    integration_type = models.CharField(max_length=32, choices=OrgIntegrationType.choices)
    name = models.CharField(max_length=255)
    config = models.JSONField(default=dict, blank=True)
    secret = EncryptedCharField(max_length=4096, blank=True, default="")
    is_active = models.BooleanField(default=True)
    vpn_integration = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        limit_choices_to={"integration_type": "VPN"},
        related_name="dependent_integrations",
        help_text=(
            "VPN integration to route this integration's requests through. "
            "Must be a VPN-type integration in the same organization."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "integration_type", "name")]
        ordering = ["organization", "integration_type", "name"]
        constraints = [
            # Claude integrations are single-credential per org by design
            # (OAuth-token model with one active subscription). Other
            # integration types intentionally allow multiple active rows
            # per org (e.g. several GitHub PATs bound to different repos),
            # so the constraint is partial and scoped to CLAUDE_CODE only.
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(integration_type="CLAUDE_CODE", is_active=True),
                name="one_active_claude_integration_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(integration_type="DAST", is_active=True),
                name="one_active_dast_integration_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization.name} / {self.integration_type} / {self.name}"

    def clean(self):
        super().clean()
        errors = {}
        if self.integration_type == OrgIntegrationType.DAST:
            try:
                DastIntegrationConfig.from_snapshot(self.config or {})
            except DastConfigError as exc:
                errors["config"] = str(exc)
        if self.vpn_integration_id is None:
            if errors:
                raise ValidationError(errors)
            return
        vpn = self.vpn_integration
        if vpn.integration_type != OrgIntegrationType.VPN:
            errors["vpn_integration"] = "The selected integration is not a VPN integration."
        elif vpn.organization_id != self.organization_id:
            errors["vpn_integration"] = "VPN integration must belong to the same organization."
        if errors:
            raise ValidationError(errors)

    def get_dast_config(self) -> DastIntegrationConfig:
        if self.integration_type != OrgIntegrationType.DAST:
            msg = "DAST config requested for a non-DAST integration."
            raise DastConfigError(msg)
        return DastIntegrationConfig.from_snapshot(self.config or {})

    @contextmanager
    def scoped_session(self, execution_id: str):
        """
        Context manager — yield a requests.Session with VPN proxy configured
        if vpn_integration is set and active.

        Must run in a Celery worker (Docker socket access required for VPN sidecar).
        When no VPN is configured, yields a plain session (no proxy).
        """
        import requests as _requests  # noqa: PLC0415

        from aist.integrations.resolver import ResolvedIntegration  # noqa: PLC0415
        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        vpn = self.vpn_integration
        vpn_resolved = (
            ResolvedIntegration(integration=vpn, config=dict(vpn.config or {}))
            if (vpn and getattr(vpn, "is_active", False)) else None
        )
        with vpn_sidecar_context(vpn_resolved, execution_id=execution_id) as (_, proxy_url), _requests.Session() as session:
            if proxy_url:
                session.proxies.update({"http": proxy_url, "https": proxy_url})
            yield session


class DastOnboardingBundleUse(models.Model):

    """
    Marks a versioned DAST onboarding bundle as consumed.

    ``integrator_public_id`` identifies one bundle export from the gateway role, not the
    gateway installation itself — re-onboarding after this record exists requires a fresh
    bundle (``dast_gateway_rotate_token: true``), not replaying the old one. Kept even if
    the ``org_integration`` it produced is later deleted, so the same bundle file cannot
    be replayed against a different organization either.
    """

    integrator_public_id = models.CharField(max_length=255, unique=True)
    org_integration = models.ForeignKey(
        OrgIntegration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onboarding_bundle_uses",
    )
    used_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"DastOnboardingBundleUse(integrator_public_id={self.integrator_public_id})"


class DastIntegrationValidationState(models.TextChoices):
    UNVALIDATED = "UNVALIDATED", "Unvalidated"
    PENDING_VALIDATION = "PENDING_VALIDATION", "Pending validation"
    VALIDATING = "VALIDATING", "Validating"
    READY = "READY", "Ready"
    INVALID = "INVALID", "Invalid"


class DastIntegrationState(models.Model):
    integration = models.OneToOneField(
        OrgIntegration,
        on_delete=models.CASCADE,
        related_name="dast_state",
    )
    validation_state = models.CharField(
        max_length=24,
        choices=DastIntegrationValidationState.choices,
        default=DastIntegrationValidationState.UNVALIDATED,
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    validation_error_code = models.CharField(max_length=64, blank=True, default="")
    validation_generation = models.PositiveBigIntegerField(default=0)
    validation_task_id = models.CharField(max_length=255, blank=True, default="")
    validation_claimed_at = models.DateTimeField(null=True, blank=True)
    contract_version = models.CharField(max_length=32, blank=True, default="")
    capabilities_etag = models.CharField(max_length=255, blank=True, default="")
    capabilities_synced_at = models.DateTimeField(null=True, blank=True)
    sync_error_code = models.CharField(max_length=64, blank=True, default="")
    sync_generation = models.PositiveBigIntegerField(default=0)
    sync_task_id = models.CharField(max_length=255, blank=True, default="")
    sync_claimed_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"DAST state[{self.integration_id}] {self.validation_state}"

    def clean(self):
        super().clean()
        if self.integration_id and self.integration.integration_type != OrgIntegrationType.DAST:
            raise ValidationError({"integration": "DAST integration state requires a DAST integration."})


class DastTarget(models.Model):
    integration = models.ForeignKey(
        OrgIntegration,
        on_delete=models.PROTECT,
        related_name="dast_targets",
    )
    provider_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    contract_revision = models.CharField(max_length=64)
    capability_revision = models.CharField(max_length=96)
    schema_digest = models.CharField(max_length=96)
    parameter_schema = models.JSONField(default=dict)
    provider_defaults = models.JSONField(default=dict)
    repository_keys = models.JSONField(default=list, blank=True)
    launch_requirements = models.JSONField(default=list, blank=True)
    autonomous_ready = models.BooleanField(default=False)
    is_available = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField()
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["integration", "provider_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "provider_id"],
                name="uniq_dast_target_provider_per_integration",
            ),
        ]

    def __str__(self) -> str:
        return f"DAST target[{self.integration_id}:{self.provider_id}]"

    def get_snapshot(self) -> DastTargetSnapshot:
        return DastTargetSnapshot.from_snapshot({
            "id": self.provider_id,
            "display_name": self.display_name,
            "contract_revision": self.contract_revision,
            "capability_revision": self.capability_revision,
            "schema_digest": self.schema_digest,
            "parameter_schema": self.parameter_schema,
            "defaults": self.provider_defaults,
            "repository_keys": self.repository_keys,
            "launch_requirements": self.launch_requirements,
            "autonomous_ready": self.autonomous_ready,
        })

    def clean(self):
        super().clean()
        errors = {}
        if self.integration_id and self.integration.integration_type != OrgIntegrationType.DAST:
            errors["integration"] = "DAST targets require a DAST integration."
        if self.pk:
            original_integration_id = type(self).objects.filter(pk=self.pk).values_list(
                "integration_id",
                flat=True,
            ).first()
            if original_integration_id is not None and original_integration_id != self.integration_id:
                errors["integration"] = "A discovered DAST target cannot be moved to another integration."
        try:
            self.get_snapshot()
        except DastConfigError as exc:
            errors["parameter_schema"] = str(exc)
        if errors:
            raise ValidationError(errors)


class DastProjectBinding(models.Model):
    project = models.ForeignKey(
        "AISTProject",
        on_delete=models.CASCADE,
        related_name="dast_bindings",
    )
    target = models.ForeignKey(
        DastTarget,
        on_delete=models.PROTECT,
        related_name="project_bindings",
    )
    source_repo_key = models.CharField(max_length=128, blank=True, default="")
    enabled = models.BooleanField(default=True)
    parameter_snapshot = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "target"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "target"],
                name="uniq_dast_binding_project_target",
            ),
        ]

    def __str__(self) -> str:
        return f"DAST binding[{self.project_id}:{self.target_id}]"

    def get_parameters(self) -> DastBindingParameters:
        return DastBindingParameters.from_snapshot(
            self.parameter_snapshot,
            target=self.target.get_snapshot(),
        )

    @property
    def requires_source_repository(self) -> bool:
        """
        Whether this binding's target scenario declares a REPOSITORY_TRIGGER requirement.

        The single accessor every other DAST module asks instead of re-deriving the answer from
        `repository_keys`/`source_repo_key` truthiness; see `aist/integrations/dast_config.py`'s
        `DastLaunchRequirements`.
        """
        return self.target.get_snapshot().launch_requirements.requires_repository()

    def clean(self):
        super().clean()
        errors = {}
        if self.target_id:
            integration = self.target.integration
            if integration.integration_type != OrgIntegrationType.DAST:
                errors["target"] = "Binding target must belong to a DAST integration."
            elif not integration.is_active:
                errors["target"] = "Binding target must belong to the active DAST integration."
            elif self.project_id and self.project.organization_id != integration.organization_id:
                errors["target"] = "Binding project and DAST target must belong to the same organization."
            if self.requires_source_repository:
                if self.source_repo_key not in self.target.get_snapshot().repository_keys:
                    errors["source_repo_key"] = "Source repository key is not advertised by the DAST target."
            elif self.source_repo_key:
                errors["source_repo_key"] = "Source repository key is not accepted by a target with no repository requirement."
            try:
                self.get_parameters()
            except DastConfigError as exc:
                errors["parameter_snapshot"] = str(exc)
        if errors:
            raise ValidationError(errors)


class OrgIntegrationVPNSecret(models.Model):

    """
    Encrypted VPN credentials for an OrgIntegration of type VPN.

    Kept in a separate model so that each credential component is independently
    updatable and VPN-type-specific fields don't pollute OrgIntegration (which is
    shared by all integration types).

    ``ovpn_content`` is the primary field and accepts a full .ovpn file including
    inline <ca>/<cert>/<key> blocks.  The separate cert fields are optional extras:
    the sidecar entrypoint appends them only if the corresponding block is NOT already
    present in ``ovpn_content``, supporting both upload styles without user error.
    """

    integration = models.OneToOneField(
        OrgIntegration,
        on_delete=models.CASCADE,
        related_name="vpn_secret",
    )
    # Full .ovpn file — may already contain inline <ca>/<cert>/<key> blocks.
    # max_length=16384 accommodates enterprise configs with embedded certs.
    ovpn_content = EncryptedCharField(max_length=16384, blank=True, default="")
    # Optional separate PEM blocks (appended only if not already inline in ovpn_content)
    ca_cert = EncryptedCharField(max_length=8192, blank=True, default="")
    client_cert = EncryptedCharField(max_length=8192, blank=True, default="")
    client_key = EncryptedCharField(max_length=8192, blank=True, default="")
    tls_auth_key = EncryptedCharField(max_length=4096, blank=True, default="")
    # Which OpenVPN inline block was used: "tls-auth" (legacy) or "tls-crypt" (modern).
    # Not encrypted — not sensitive. Determines correct block tag on sidecar reassembly.
    tls_key_type = models.CharField(max_length=16, blank=True, default="tls-auth")
    # Optional auth-user-pass credentials
    vpn_username = EncryptedCharField(max_length=512, blank=True, default="")
    vpn_password = EncryptedCharField(max_length=512, blank=True, default="")

    def __str__(self) -> str:
        return f"VPNSecret for {self.integration}"


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

    def get_excluded_paths(self) -> list[str]:
        return ProjectProfile.from_dict(self.profile).get_excluded_paths()

    def get_excluded_severities(self) -> list[str]:
        return ProjectProfile.from_dict(self.profile).get_excluded_severities()

    @property
    def active_script(self) -> AISTProjectScript:
        """
        Return the current effective script for this project.

        Resolution order:
        1. Latest version's script (authoritative for pipeline history)
        2. Latest project-scoped revision (set at creation before any version exists)
        3. Shared default singleton

        This property replaces the former stored FK.
        Do NOT call in list-view loops without prefetching; see views/projects.py.
        """
        latest_version = (
            self.versions
            .order_by("-created")
            .select_related("script")
            .first()
        )
        if latest_version and latest_version.script_id:
            return latest_version.script
        latest_revision = self.script_revisions.order_by("-created_at").first()
        if latest_revision:
            return latest_revision
        return AISTProjectScript.get_shared_default()

    def get_launch_schedule(self) -> LaunchSchedule | None:
        try:
            return self.launch_schedule
        except LaunchSchedule.DoesNotExist:
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

    def ensure_extracted(self) -> Path | None:
        """
        Ensure the uploaded archive is extracted under `get_extracted_root()`.

        - Idempotent: if marker exists and matches current SHA, skip work.
        - Secure extraction: delegates to _safe_extract_* helpers to prevent path traversal.
        - Post-process: if extraction yields exactly one top-level directory, flatten it.
        - Writes `.extracted.ok` containing the archive SHA so we can detect changes.
        """
        from aist.utils.archive import (  # noqa: PLC0415
            _flatten_single_root_directory,
            _safe_extract_tar_member,
            _safe_extract_zip_member,
        )

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


class PipelineExecutionType(models.TextChoices):
    SAST = "SAST", "SAST"
    DAST = "DAST", "DAST"
    MANUAL_IMPORT = "MANUAL_IMPORT", "Manual report import"


class DastExecutionOutcome(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    STOP_PENDING = "STOP_PENDING", "Remote stop pending"
    TERMINAL = "TERMINAL", "Provider terminal"
    CANCELLED_BEFORE_START = "CANCELLED_BEFORE_START", "Cancelled before provider start"
    UNREACHABLE = "UNREACHABLE", "Provider unreachable"


class AISTPipeline(models.Model):
    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)
    started = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    id = models.CharField(primary_key=True, max_length=64)

    project = models.ForeignKey(AISTProject, on_delete=models.PROTECT, related_name="aist_pipelines")
    project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        related_name="pipelines",
        db_index=True,
        null=True, blank=True,
    )
    trigger_project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        related_name="triggered_pipelines",
        db_index=True,
        null=True,
        blank=True,
        help_text="Source version that triggered a DAST run; its effective version may be resolved later.",
    )
    execution_type = models.CharField(
        max_length=24,
        choices=PipelineExecutionType.choices,
        default=PipelineExecutionType.SAST,
        db_index=True,
    )
    status = models.CharField(max_length=64, choices=AISTStatus.choices, default=AISTStatus.ADMITTED)

    tests = models.ManyToManyField(Test, related_name="aist_pipelines", blank=True)
    launch_data = models.JSONField(default=dict, blank=True)

    run_task_id = models.CharField(max_length=64, null=True, blank=True)
    watch_dedup_task_id = models.CharField(max_length=64, null=True, blank=True)

    response_from_ai = models.JSONField(default=dict, blank=True)

    pull_request = models.ForeignKey(
        PullRequest,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="pipelines",
    )

    class Meta:
        ordering = ("-created",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        execution_type=PipelineExecutionType.SAST,
                        project_version__isnull=False,
                        trigger_project_version__isnull=True,
                    )
                    # No nullness condition on trigger_project_version: whether a DAST run has one
                    # depends on its binding's target requirement (dast_config.DastLaunchRequirements),
                    # which this table cannot join against. That is checked earlier, with the binding
                    # in scope, at PipelineLaunchRequest/AISTProjectLaunchConfig creation time.
                    | models.Q(execution_type=PipelineExecutionType.DAST)
                    | models.Q(
                        execution_type=PipelineExecutionType.MANUAL_IMPORT,
                        trigger_project_version__isnull=True,
                    )
                ),
                name="aist_pipeline_execution_source_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.execution_type}Pipeline[{self.id}] {self.status}"

    def clean(self):
        errors = {}
        if self.project_version_id and self.project_version.project_id != self.project_id:
            errors["project_version"] = "Effective version must belong to the pipeline project."
        if self.trigger_project_version_id and self.trigger_project_version.project_id != self.project_id:
            errors["trigger_project_version"] = "Trigger version must belong to the pipeline project."

        if self.execution_type == PipelineExecutionType.SAST:
            if not self.project_version_id:
                errors["project_version"] = "SAST pipelines require an effective project version."
            if self.trigger_project_version_id:
                errors["trigger_project_version"] = "SAST pipelines cannot have a DAST trigger version."
        elif self.execution_type == PipelineExecutionType.MANUAL_IMPORT and self.trigger_project_version_id:
            errors["trigger_project_version"] = "Manual imports cannot have a DAST trigger version."

        if errors:
            raise ValidationError(errors)


class DastExecutionState(models.Model):

    """Typed provider runtime state kept outside the common pipeline row."""

    pipeline = models.OneToOneField(
        AISTPipeline,
        on_delete=models.CASCADE,
        related_name="dast_execution_state",
    )
    run_id = models.CharField(max_length=255, null=True, blank=True)
    log_cursor = models.PositiveBigIntegerField(default=0)
    outcome = models.CharField(
        max_length=32,
        choices=DastExecutionOutcome.choices,
        blank=True,
        default="",
    )
    # When the provider last delivered a run id or new log events -- the run's sign of life.
    # NULL means "no baseline yet"; a reader must never read it as "stalled".
    last_progress_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    recovery_checkpoint = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.pipeline.execution_type != PipelineExecutionType.DAST:
            raise ValidationError({"pipeline": "DAST execution state requires a DAST pipeline."})


# Column name -> attribute on the validated coverage / token-usage value objects. The two
# names differ only where a column reads better than the wire counter it came from.
_DAST_COVERAGE_COUNT_COLUMNS = ("discovered", "reachable", "analysed", "planned")
_DAST_COVERAGE_NAME_COLUMNS = ("analysed_names", "beyond_plan_names")
_DAST_TOKEN_COUNT_COLUMNS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thinking_tokens",
    "cache_creation_tokens": "cache_creation_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "model_calls": "calls",
}


class DastRunMetadataManager(models.Manager):

    """Persists one validated ``dast_run_metadata`` block; the only value-object-to-column map."""

    @staticmethod
    def _reported(block, attribute: str):
        """Read one optional attribute off a block the report may not have carried at all."""
        return None if block is None else getattr(block, attribute)

    @classmethod
    def _reported_names(cls, coverage, attribute: str) -> list[str] | None:
        names = cls._reported(coverage, attribute)
        return None if names is None else list(names)

    @classmethod
    def _reported_buckets(cls, usage, attribute: str) -> list[dict] | None:
        buckets = cls._reported(usage, attribute)
        return None if buckets is None else [bucket.as_wire() for bucket in buckets]

    @classmethod
    def columns_from_report(cls, metadata, *, source_verified: bool | None = None) -> dict:
        """Flatten one validated metadata block onto this table's columns."""
        coverage = metadata.coverage
        usage = metadata.token_usage
        total = cls._reported(usage, "total")
        return {
            "run_id": metadata.run_id,
            "target_id": metadata.target_id,
            "stand_id": metadata.stand_id,
            "product_family": metadata.product_family,
            "tier": metadata.tier,
            "run_type": metadata.run_type,
            "target_host": metadata.target_host,
            "scan_started": metadata.scan_started,
            "scan_finished": metadata.scan_finished,
            "delivery_quality": metadata.delivery_quality,
            "audit_state": metadata.audit_state,
            "findings_complete": metadata.findings_complete,
            "source_verified": source_verified,
            "operator_actions_persisted": metadata.operator_actions_persisted,
            "operator_actions": (
                None if metadata.operator_actions is None
                else [row.as_wire() for row in metadata.operator_actions]
            ),
            "operator_actions_total": metadata.operator_actions_total,
            "operator_actions_truncated": metadata.operator_actions_truncated,
            "excluded_findings": (
                None if metadata.excluded_findings is None
                else [row.as_wire() for row in metadata.excluded_findings]
            ),
            "excluded_findings_total": metadata.excluded_findings_total,
            "excluded_findings_truncated": metadata.excluded_findings_truncated,
            "coverage_unit": cls._reported(coverage, "unit"),
            **{column: cls._reported(coverage, column) for column in _DAST_COVERAGE_COUNT_COLUMNS},
            **{column: cls._reported_names(coverage, column) for column in _DAST_COVERAGE_NAME_COLUMNS},
            **{
                column: cls._reported(total, attribute)
                for column, attribute in _DAST_TOKEN_COUNT_COLUMNS.items()
            },
            "token_by_phase": cls._reported_buckets(usage, "by_phase"),
            "token_by_agent_type": cls._reported_buckets(usage, "by_agent_type"),
            "token_accounting_consistent": cls._reported(usage, "accounting_consistent"),
        }

    def build_from_report(self, metadata) -> DastRunMetadata:
        """
        An unsaved row for a report that has not been imported yet.

        Lets the import preview run the same read derivations the pipeline list runs after the
        import, so the operator sees exactly the numbers that will appear.
        """
        return self.model(**self.columns_from_report(metadata))

    def upsert_from_report(self, *, pipeline_id: str, report) -> DastRunMetadata:
        """
        Write the accepted report's run metadata, replacing any earlier write for this pipeline.

        Idempotent by construction: finalization calls this before its own already-finalized
        short circuit, so redelivering the same report rewrites identical data and a pipeline
        finalized before this table existed gains its row.
        """
        row, _created = self.update_or_create(
            pipeline_id=pipeline_id,
            defaults=self.columns_from_report(
                report.run_metadata,
                source_verified=report.source_verified,
            ),
        )
        return row


class DastRunMetadata(models.Model):

    """
    Provider-reported run metadata carried by one accepted DAST report.

    Every reported column is nullable and stays NULL when the report did not carry it —
    absent must never read as zero, and an empty inventory the provider *did* report is a
    different fact from one it never mentioned.

    Readers may treat NULL as the only empty state, but that comes from the writer rather than
    from the column: :meth:`DastRunMetadataManager.columns_from_report` is the only thing that
    fills this table, and it stores either None or a value the report validator has already
    refused to accept blank. ``blank=True`` is kept because the fields genuinely are optional,
    so a form would be wrong to demand them; this model is not registered in the admin, and a
    future form-based writer would have to preserve that invariant itself.

    Distinct from :class:`DastExecutionState`, which is provider *runtime* state written
    before any report exists so a run can be polled and resumed. This row is *reported*
    state, written once when a report crosses the trust boundary, and it is the same row for
    an autonomous run and an operator upload — which is why the report's own identity
    (``run_id``, ``target_id``, ``stand_id``) lives here rather than being read from two
    different places depending on the pipeline's execution type.
    """

    pipeline = models.OneToOneField(
        AISTPipeline,
        on_delete=models.CASCADE,
        related_name="dast_run_metadata",
    )

    run_id = models.CharField(max_length=255)
    target_id = models.CharField(max_length=255)
    stand_id = models.CharField(max_length=255, null=True, blank=True)

    product_family = models.CharField(max_length=64, null=True, blank=True)
    tier = models.CharField(max_length=64, null=True, blank=True)
    run_type = models.CharField(max_length=64, null=True, blank=True)
    target_host = models.CharField(max_length=255, null=True, blank=True)
    scan_started = models.DateTimeField(null=True, blank=True)
    scan_finished = models.DateTimeField(null=True, blank=True)

    delivery_quality = models.CharField(max_length=16, null=True, blank=True)
    audit_state = models.CharField(max_length=16, null=True, blank=True)
    findings_complete = models.BooleanField(null=True, blank=True)
    # Transport provenance: NULL for manual uploads, never inferred from report metadata.
    source_verified = models.BooleanField(null=True, blank=True)
    operator_actions_persisted = models.BooleanField(null=True, blank=True)
    operator_actions = models.JSONField(null=True, blank=True, default=None)
    operator_actions_total = models.PositiveIntegerField(null=True, blank=True)
    operator_actions_truncated = models.BooleanField(null=True, blank=True)
    excluded_findings = models.JSONField(null=True, blank=True, default=None)
    excluded_findings_total = models.PositiveIntegerField(null=True, blank=True)
    excluded_findings_truncated = models.BooleanField(null=True, blank=True)

    coverage_unit = models.CharField(max_length=64, null=True, blank=True)
    discovered = models.PositiveIntegerField(null=True, blank=True)
    reachable = models.PositiveIntegerField(null=True, blank=True)
    analysed = models.PositiveIntegerField(null=True, blank=True)
    planned = models.PositiveIntegerField(null=True, blank=True)
    analysed_names = models.JSONField(null=True, blank=True, default=None)
    beyond_plan_names = models.JSONField(null=True, blank=True, default=None)

    input_tokens = models.BigIntegerField(null=True, blank=True)
    output_tokens = models.BigIntegerField(null=True, blank=True)
    thinking_tokens = models.BigIntegerField(null=True, blank=True)
    cache_creation_tokens = models.BigIntegerField(null=True, blank=True)
    cache_read_tokens = models.BigIntegerField(null=True, blank=True)
    model_calls = models.PositiveIntegerField(null=True, blank=True)
    token_by_phase = models.JSONField(null=True, blank=True, default=None)
    token_by_agent_type = models.JSONField(null=True, blank=True, default=None)
    # False when a breakdown could be compared against the reported total and disagreed with
    # it. Recorded and surfaced rather than rejected: both sides are individually well-formed,
    # and a report full of real findings must not be lost to an accounting mismatch.
    token_accounting_consistent = models.BooleanField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = DastRunMetadataManager()

    def __str__(self) -> str:
        return f"DastRunMetadata(pipeline={self.pipeline_id}, run={self.run_id})"

    def clean(self):
        if self.pipeline.execution_type not in {PipelineExecutionType.DAST, PipelineExecutionType.MANUAL_IMPORT}:
            raise ValidationError({"pipeline": "DAST run metadata requires a DAST or manual-import pipeline."})


class AISTTestMeta(models.Model):
    test = models.OneToOneField(
        Test,
        on_delete=models.CASCADE,
        related_name="aist_meta",
    )
    deduplication_complete = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["deduplication_complete"],
                name="aist_testmeta_dedup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AISTTestMeta(test={self.test_id}, dedup_complete={self.deduplication_complete})"


class TestDeduplicationProgress(models.Model):

    """Deduplication progress on one Test."""

    test = models.OneToOneField(
        Test, on_delete=models.CASCADE, related_name="dedupe_progress",
    )
    pending_tasks = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    deduplication_complete = models.BooleanField(default=False)
    last_progress_at = models.DateTimeField(null=True, blank=True)
    last_reconcile_at = models.DateTimeField(null=True, blank=True)
    reconcile_attempts = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["test", "pending_tasks"])]

    def __str__(self) -> str:
        return f"DeduplicationTaskGroup(test={self.test_id}, remaining={self.pending_tasks})"

    def mark_complete_if_finished(self) -> None:
        if self.pending_tasks == 0 and not self.deduplication_complete:
            self.deduplication_complete = True
            self.completed_at = timezone.now()
            self.save(update_fields=["deduplication_complete", "completed_at"])
            AISTTestMeta.objects.update_or_create(
                test_id=self.test_id,
                defaults={"deduplication_complete": True},
            )

    def refresh_pending_tasks(self) -> None:
        with transaction.atomic():
            group = (
                TestDeduplicationProgress.objects
                .select_for_update()
                .get(pk=self.pk)
            )
            now = timezone.now()
            fields_to_update = []

            if group.started_at is None:
                group.started_at = now
                fields_to_update.append("started_at")
            if group.last_progress_at is None:
                group.last_progress_at = now
                fields_to_update.append("last_progress_at")
            # test current findings
            qs_findings = Finding.objects.filter(test_id=group.test_id)

            # pending = findings, for which ProcessedFinding doesn't exist with same test_id and finding_id
            pending_qs = qs_findings.filter(
                ~models.Exists(
                    ProcessedFinding.objects.filter(
                        test_id=group.test_id,
                        finding_id=models.OuterRef("id"),
                    ),
                ),
            )

            pending = pending_qs.count()
            # completed if pending == 0 (even if 0/0)
            is_complete = (pending == 0)

            fields_to_update = []
            if group.pending_tasks != pending:
                group.pending_tasks = pending
                fields_to_update.append("pending_tasks")
                group.last_progress_at = now
                if "last_progress_at" not in fields_to_update:
                    fields_to_update.append("last_progress_at")
            if group.deduplication_complete != is_complete:
                group.deduplication_complete = is_complete
                fields_to_update.append("deduplication_complete")
                group.last_progress_at = now
                if "last_progress_at" not in fields_to_update:
                    fields_to_update.append("last_progress_at")

            if fields_to_update:
                group.save(update_fields=fields_to_update)
            AISTTestMeta.objects.update_or_create(
                test_id=group.test_id,
                defaults={"deduplication_complete": is_complete},
            )


class ProcessedFinding(models.Model):

    """Set which findings are considered to avoid double decrement"""

    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    finding = models.ForeignKey(Finding, null=True, blank=True,
                                on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test", "finding"],
                name="uniq_processed_test_finding_not_null",
                condition=models.Q(finding__isnull=False),
            ),
        ]
        indexes = [
            # to anti JOIN work fast
            models.Index(fields=["test", "finding"]),
        ]


class AISTAIResponse(models.Model):
    class Source(models.TextChoices):
        # Default value: existing post-import Claude/n8n triage flow.
        AI_TRIAGE = "ai_triage", "AI Triage"
        # Analyzer-produced AI verdict artifacts imported as part of the SAST
        # pipeline before the regular post-import triage queue.
        AGENT_ANALYZER = "agent_analyzer", "Agent Analyzer"

    pipeline = models.ForeignKey(
        "AISTPipeline",
        on_delete=models.CASCADE,
        related_name="ai_responses",
        db_index=True,
    )
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        default=Source.AI_TRIAGE,
        db_index=True,
    )

    class Meta:
        ordering = ["-created"]  # last one is on top

    def __str__(self):
        return f"AIResponse[{self.pipeline_id}] @ {self.created:%Y-%m-%d %H:%M:%S}"


class AISTAIFindingResponse(models.Model):
    class Verdict(models.TextChoices):
        TRUE_POSITIVE = "true_positive", "True Positive"
        FALSE_POSITIVE = "false_positive", "False Positive"
        UNCERTAIN = "uncertain", "Uncertain"

    class FixType(models.TextChoices):
        CODE_CHANGE = "code_change", "Code Change"
        CONFIG_CHANGE = "config_change", "Config Change"
        ARCHITECTURAL = "architectural", "Architectural"

    pipeline = models.ForeignKey(
        "AISTPipeline",
        on_delete=models.CASCADE,
        related_name="ai_finding_responses",
        db_index=True,
    )
    source_response = models.ForeignKey(
        "AISTAIResponse",
        on_delete=models.SET_NULL,
        related_name="finding_responses",
        null=True,
        blank=True,
    )
    finding = models.ForeignKey(
        Finding,
        on_delete=models.CASCADE,
        related_name="aist_ai_responses",
        db_index=True,
    )
    verdict = models.CharField(
        max_length=32,
        choices=Verdict.choices,
        db_index=True,
    )
    title = models.CharField(max_length=512, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    references = models.JSONField(default=list, blank=True)
    epss_score = models.FloatField(null=True, blank=True)
    impact_score = models.FloatField(null=True, blank=True)
    exploitability_score = models.FloatField(null=True, blank=True)
    uncertainty_level = models.FloatField(null=True, blank=True)
    uncertainty_spread = models.FloatField(null=True, blank=True)
    exploit_code_maturity = models.CharField(max_length=64, blank=True, default="")
    fix = models.JSONField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pipeline", "finding"],
                name="uniq_aist_ai_finding_per_pipeline",
            ),
        ]
        indexes = [
            models.Index(fields=["pipeline", "verdict"], name="aist_aistai_pipelin_3868d4_idx"),
            models.Index(fields=["pipeline", "finding"], name="aist_aistai_pipelin_6a546f_idx"),
        ]
        ordering = ["-updated"]

    def __str__(self):
        return f"AIFindingResponse[pipeline={self.pipeline_id}, finding={self.finding_id}]"


def _ensure_aware(value: dt) -> dt:
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_default_timezone())
    return value


class LaunchSchedule(models.Model):
    cron_expression = models.CharField(
        max_length=100,
        help_text="Cron expression in standard 5-field format (e.g. '0 15 * * 1' for Mondays at 15:00).",
    )
    enabled = models.BooleanField(default=True)

    max_concurrent_runs = models.PositiveIntegerField(
        default=1,
        help_text="Maximum number of concurrent pipeline runs this schedule's own resource slot allows.",
    )

    launch_config = models.OneToOneField(
        "AISTProjectLaunchConfig",
        on_delete=models.CASCADE,
        related_name="launch_schedule",
        help_text="Anchor launch config. Project is derived from launch_config.project.",
    )

    last_run_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last time this schedule launched pipelines.",
    )
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    last_error_detail = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        verbose_name = "Launch Schedule"
        verbose_name_plural = "Launch Schedules"
        indexes = [
            models.Index(fields=["enabled", "next_run_at"], name="aist_schedule_due_idx"),
        ]

    def __str__(self) -> str:
        return (f"LaunchSchedule(project={self.launch_config.project_id}, "
                f"launch_config={self.launch_config_id}, cron={self.cron_expression})")

    def get_next_run_time(self, *, now=None):
        """
        Return the most recent scheduled tick time that is <= now (i.e. "due time").

        This avoids missing a tick when the scheduler task runs slightly позднее,
        e.g. tick at 12:55 but Celery beat triggers at 12:56.
        """
        now = now or timezone.now()
        if timezone.is_naive(now):
            now = timezone.make_aware(now, timezone.get_default_timezone())

        # The last scheduled time at or before "now"
        itr = croniter(self.cron_expression, now)
        due_time = itr.get_prev(dt)

        if timezone.is_naive(due_time):
            due_time = timezone.make_aware(due_time, timezone.get_default_timezone())

        return due_time

    def get_next_scheduled_time(self, *, now=None):
        """
        Return the next scheduled tick strictly after now.

        This is a UI/helper method and must not be used to decide "due".
        Scheduler semantics should keep using get_next_run_time() (prev <= now).
        """
        now = now or timezone.now()
        now = _ensure_aware(now)

        itr = croniter(self.cron_expression, now)
        nxt = itr.get_next(dt)
        return _ensure_aware(nxt)

    def preview_next_runs(self, *, count: int = 5, now=None) -> list[dt]:
        """
        Return next N scheduled ticks after now (strictly in the future).
        Used by UI preview; backend-only logic.
        """
        now = now or timezone.now()
        now = _ensure_aware(now)

        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 5

        if count < 1:
            return []
        count = min(count, 20)

        itr = croniter(self.cron_expression, now)
        out: list[dt] = []
        for _ in range(count):
            nxt = itr.get_next(dt)
            out.append(_ensure_aware(nxt))
        return out


class PipelineLaunchOrigin(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    SCHEDULE = "SCHEDULE", "Schedule"
    SCM_WEBHOOK = "SCM_WEBHOOK", "SCM webhook"
    RECONCILER = "RECONCILER", "Reconciler"


class PipelineLaunchAuthorityKind(models.TextChoices):
    USER = "USER", "User session"
    PAT = "PAT", "AIST personal access token"
    SCHEDULE = "SCHEDULE", "Stored schedule authority"
    SCM_WEBHOOK = "SCM_WEBHOOK", "Verified SCM webhook"
    RECONCILER = "RECONCILER", "Existing request reconciliation"


class PipelineLaunchRequestState(models.TextChoices):
    PENDING = "PENDING", "Pending"
    CLAIMED = "CLAIMED", "Claimed"
    PLANNED = "PLANNED", "Planned"
    PUBLISHED = "PUBLISHED", "Published"
    DISPATCHED = "DISPATCHED", "Dispatched"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    FAILED = "FAILED", "Failed"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"


class PipelineLaunchRequest(models.Model):

    """
    Durable launch request and broker outbox shared by every execution type.

    Dynamic parameter and capability values are frozen at enqueue time. Credentials are
    referenced only by their public database record and never copied into JSON snapshots.
    """

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True)
    origin = models.CharField(
        max_length=24,
        choices=PipelineLaunchOrigin.choices,
        default=PipelineLaunchOrigin.SCHEDULE,
    )
    execution_type = models.CharField(
        max_length=24,
        choices=PipelineExecutionType.choices,
        default=PipelineExecutionType.SAST,
        db_index=True,
    )
    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE)
    dast_binding = models.ForeignKey(
        "DastProjectBinding",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="launch_requests",
    )
    trigger_project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="launch_requests",
    )
    schedule = models.ForeignKey(
        LaunchSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    launch_config = models.ForeignKey(
        "AISTProjectLaunchConfig",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="launch_requests",
        help_text="Launch config used to build pipeline_args snapshot.",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_launch_requests",
    )
    api_token = models.ForeignKey(
        "AISTApiToken",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pipeline_launch_requests",
        help_text="Public PAT record used for authority revalidation; never stores the token secret.",
    )
    authority_kind = models.CharField(
        max_length=24,
        choices=PipelineLaunchAuthorityKind.choices,
        default=PipelineLaunchAuthorityKind.SCHEDULE,
    )
    params_snapshot = models.JSONField(default=dict, blank=True)
    capability_snapshot = models.JSONField(default=dict, blank=True)
    initial_launch_data_snapshot = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        help_text="Secret-free pipeline launch metadata frozen by the authorized producer.",
    )
    state = models.CharField(
        max_length=24,
        choices=PipelineLaunchRequestState.choices,
        default=PipelineLaunchRequestState.PENDING,
        db_index=True,
    )
    coalesce_key = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superseded_requests",
    )
    priority = models.SmallIntegerField(default=0)
    not_before = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    capacity_retry_count = models.PositiveIntegerField(default=0)
    claim_owner = models.CharField(max_length=255, null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    task_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    task_name = models.CharField(
        max_length=128,
        blank=True,
        default="",
        editable=False,
        help_text="Trusted Celery task selected by the launch adapter during planning.",
    )
    task_args_snapshot = models.JSONField(
        default=list,
        blank=True,
        editable=False,
        help_text="Secret-free JSON task arguments frozen before broker publication.",
    )
    client_request_key_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        editable=False,
        help_text="Server-namespaced digest of an optional producer idempotency key.",
    )
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_detail = models.TextField(blank=True, default="")
    pipeline = models.OneToOneField(
        "AISTPipeline",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="launch_request",
    )

    class Meta:
        ordering = ["created"]
        indexes = [
            models.Index(
                fields=["state", "not_before", "priority"],
                name="aist_launch_req_dispatch_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=PipelineLaunchRequestState.SUPERSEDED,
                        superseded_by__isnull=False,
                    )
                    | (
                        ~models.Q(state=PipelineLaunchRequestState.SUPERSEDED)
                        & models.Q(superseded_by__isnull=True)
                    )
                ),
                name="aist_launch_request_supersede_link_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        execution_type=PipelineExecutionType.SAST,
                        dast_binding__isnull=True,
                        trigger_project_version__isnull=True,
                    )
                    # Whether trigger_project_version must be set depends on the binding's target
                    # requirement, which this constraint cannot join against; checked in clean()
                    # instead, with the binding in scope.
                    | models.Q(
                        execution_type=PipelineExecutionType.DAST,
                        dast_binding__isnull=False,
                    )
                ),
                name="aist_launch_request_execution_target_valid",
            ),
        ]

    def __str__(self):
        return f"LaunchRequest(project={self.project_id}, type={self.execution_type}, state={self.state})"

    @property
    def dispatched(self) -> bool:
        """Compatibility value returned by the existing SAST queue API."""
        return self.state == PipelineLaunchRequestState.DISPATCHED

    def get_snapshots(self) -> LaunchRequestSnapshots:
        return LaunchRequestSnapshots.from_values(
            params=self.params_snapshot,
            capability=self.capability_snapshot,
        )

    def get_initial_launch_data_snapshot(self) -> dict:
        error_message = "initial_launch_data_snapshot must be a JSON object."
        value = validated_secret_free_json(
            self.initial_launch_data_snapshot,
            label="initial_launch_data_snapshot",
        )
        if not isinstance(value, dict):
            raise LaunchRequestSnapshotError(error_message)
        return value

    def clean(self):
        errors: dict[str, str] = {}
        try:
            self.get_snapshots()
        except LaunchRequestSnapshotError as exc:
            errors["params_snapshot"] = str(exc)
        try:
            self.get_initial_launch_data_snapshot()
        except LaunchRequestSnapshotError as exc:
            errors["initial_launch_data_snapshot"] = str(exc)

        if self.launch_config_id and self.launch_config.project_id != self.project_id:
            errors["launch_config"] = "Launch config must belong to the request project."
        elif self.launch_config_id and self.launch_config.execution_type != self.execution_type:
            errors["execution_type"] = "Request execution type must match its launch config."
        elif self.launch_config_id and self.launch_config.dast_binding_id != self.dast_binding_id:
            errors["dast_binding"] = "Request DAST binding must match its launch config."
        if self.schedule_id and self.schedule.launch_config_id != self.launch_config_id:
            errors["schedule"] = "Schedule must belong to the request launch config."
        if self.trigger_project_version_id and self.trigger_project_version.project_id != self.project_id:
            errors["trigger_project_version"] = "Trigger version must belong to the request project."
        elif self.trigger_project_version_id and self.trigger_project_version.version_type not in {
            VersionType.GIT_BRANCH,
            VersionType.GIT_HASH,
        }:
            errors["trigger_project_version"] = "DAST trigger version must be a Git branch or Git hash."
        if self.dast_binding_id and self.dast_binding.project_id != self.project_id:
            errors["dast_binding"] = "DAST binding must belong to the request project."
        if self.superseded_by_id and self.superseded_by.project_id != self.project_id:
            errors["superseded_by"] = "Superseding request must belong to the same project."
        if self.superseded_by_id and self.superseded_by_id == self.pk:
            errors["superseded_by"] = "A launch request cannot supersede itself."
        if self.api_token_id and self.api_token.organization_id != self.project.organization_id:
            errors["api_token"] = "PAT record must belong to the request organization."  # noqa: S105

        if self.authority_kind == PipelineLaunchAuthorityKind.PAT and not self.api_token_id:
            errors["api_token"] = "PAT authority requires a public PAT record."  # noqa: S105
        if self.api_token_id and self.authority_kind != PipelineLaunchAuthorityKind.PAT:
            errors["authority_kind"] = "A PAT record is valid only for PAT authority."
        if self.created and self.expires_at and self.expires_at <= self.created:
            errors["expires_at"] = "Expiry must be later than request creation."

        if self.execution_type == PipelineExecutionType.SAST:
            if self.dast_binding_id or self.trigger_project_version_id:
                errors["execution_type"] = "SAST launch requests cannot contain DAST execution input."
        elif self.execution_type == PipelineExecutionType.DAST:
            if not self.dast_binding_id:
                errors["dast_binding"] = "DAST launch requests require a project binding."
            elif self.dast_binding.requires_source_repository:
                if not self.trigger_project_version_id:
                    errors["trigger_project_version"] = "DAST launch requests require a Git trigger version."
            elif self.trigger_project_version_id:
                errors["trigger_project_version"] = "DAST launch request for a sourceless binding cannot select a trigger version."

        if errors:
            raise ValidationError(errors)


class PipelineExecutionLease(models.Model):

    """Database-backed ownership of one bounded external execution resource slot."""

    resource_key = models.CharField(max_length=255)
    slot = models.PositiveSmallIntegerField()
    request = models.ForeignKey(
        PipelineLaunchRequest,
        on_delete=models.CASCADE,
        related_name="execution_leases",
    )
    pipeline = models.ForeignKey(
        AISTPipeline,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_leases",
    )
    acquired_at = models.DateTimeField(default=timezone.now)
    heartbeat_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    released_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["resource_key", "slot", "acquired_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["resource_key", "slot"],
                condition=models.Q(released_at__isnull=True),
                name="uniq_active_execution_lease_slot",
            ),
        ]

    def __str__(self) -> str:
        return f"ExecutionLease(resource={self.resource_key}, slot={self.slot}, request={self.request_id})"

    def clean(self):
        errors: dict[str, str] = {}
        if not self.resource_key.strip():
            errors["resource_key"] = "Resource key must not be empty."
        if self.expires_at <= self.acquired_at:
            errors["expires_at"] = "Lease expiry must be later than acquisition."
        if self.heartbeat_at < self.acquired_at:
            errors["heartbeat_at"] = "Heartbeat cannot predate acquisition."
        if self.pipeline_id and self.pipeline.project_id != self.request.project_id:
            errors["pipeline"] = "Lease pipeline and launch request must belong to the same project."
        if errors:
            raise ValidationError(errors)


class AISTProjectLaunchConfig(models.Model):

    """
    Saved launch configuration ("preset") for a specific AISTProject.

    Stores PipelineArguments-like options excluding:
      - project_id (comes from FK)
      - project_version (chosen at run time)
    """

    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE, related_name="launch_configs")
    execution_type = models.CharField(
        max_length=24,
        choices=(
            (PipelineExecutionType.SAST, "SAST"),
            (PipelineExecutionType.DAST, "DAST"),
        ),
        default=PipelineExecutionType.SAST,
        db_index=True,
    )
    dast_binding = models.ForeignKey(
        DastProjectBinding,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="launch_configs",
    )
    trigger_project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dast_launch_configs",
    )

    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")

    # PipelineArguments-equivalent options (except project_id/project_version)
    params = models.JSONField(default=dict, blank=True)

    is_default = models.BooleanField(default=False)

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="uniq_aist_launch_cfg_name_per_project"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        execution_type=PipelineExecutionType.SAST,
                        dast_binding__isnull=True,
                        trigger_project_version__isnull=True,
                    )
                    # Whether trigger_project_version must be set depends on the binding's target
                    # requirement, which this constraint cannot join against; checked in clean()
                    # instead, with the binding in scope.
                    | models.Q(
                        execution_type=PipelineExecutionType.DAST,
                        dast_binding__isnull=False,
                    )
                ),
                name="aist_launch_config_execution_target_valid",
            ),
            models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(is_default=True),
                name="uniq_default_launch_config_per_project",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(execution_type=PipelineExecutionType.SAST)
                    | ~models.Q(params__has_key="analyzers")
                ),
                name="aist_dast_launch_config_no_analyzers",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.name}"

    def get_launch_schedule(self) -> LaunchSchedule | None:
        try:
            return self.launch_schedule
        except LaunchSchedule.DoesNotExist:
            return None

    def clean(self):
        errors: dict[str, str] = {}
        if self.execution_type == PipelineExecutionType.SAST:
            if self.dast_binding_id:
                errors["dast_binding"] = "SAST launch config cannot select a DAST binding."
            if self.trigger_project_version_id:
                errors["trigger_project_version"] = "SAST launch config cannot select a DAST trigger version."
        elif self.execution_type == PipelineExecutionType.DAST:
            if not self.dast_binding_id:
                errors["dast_binding"] = "DAST launch config requires an explicit binding."
            elif self.dast_binding.project_id != self.project_id:
                errors["dast_binding"] = "DAST binding must belong to the launch config project."
            elif not self.dast_binding.enabled:
                errors["dast_binding"] = "DAST binding must be enabled."
            if self.dast_binding_id and self.dast_binding.requires_source_repository:
                if not self.trigger_project_version_id:
                    errors["trigger_project_version"] = "DAST launch config requires a Git trigger version."
                elif self.trigger_project_version.project_id != self.project_id:
                    errors["trigger_project_version"] = "DAST trigger version must belong to the launch config project."
                elif self.trigger_project_version.version_type not in {VersionType.GIT_BRANCH, VersionType.GIT_HASH}:
                    errors["trigger_project_version"] = "DAST trigger version must be a Git branch or Git hash."
            elif self.dast_binding_id and self.trigger_project_version_id:
                errors["trigger_project_version"] = "DAST launch config for a sourceless binding cannot select a trigger version."
            if "analyzers" in self.params:
                errors["params"] = "DAST launch config cannot contain SAST analyzers."
        else:
            errors["execution_type"] = "Launch configs support only SAST or DAST execution."
        if errors:
            raise ValidationError(errors)


class AISTLaunchConfigAction(models.Model):
    class ActionType(models.TextChoices):
        PUSH_TO_SLACK = "PUSH_TO_SLACK", "push_to_slack"
        SEND_EMAIL = "SEND_EMAIL", "send_email"
        WRITE_LOG = "WRITE_LOG", "write_log"

    launch_config = models.ForeignKey(
        AISTProjectLaunchConfig,
        on_delete=models.CASCADE,
        related_name="actions",
    )
    trigger_status = models.CharField(max_length=64, choices=AISTStatus.choices)
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    config = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = []

    def __str__(self) -> str:
        return f"Action({self.launch_config_id}:{self.action_type}@{self.trigger_status})"


class WorkItemProviderType(models.TextChoices):
    JIRA = "JIRA", "Jira"
    YOUTRACK = "YOUTRACK", "YouTrack"
    GITHUB = "GITHUB", "GitHub Issues"
    GITLAB = "GITLAB", "GitLab Issues"
    LINEAR = "LINEAR", "Linear"
    AZURE_DEVOPS = "AZURE_DEVOPS", "Azure DevOps"
    GENERIC = "GENERIC", "Generic (URL only)"


class WorkItemProvider(models.Model):

    """
    Connection to an external issue tracker, scoped to an Organization.

    Credentials are stored encrypted. ``provider_config`` holds non-secret
    settings (default project key, field mappings, labels, …).

    When ``sync_enabled=False`` the provider is "manual-only": links can be
    created and displayed but status is never fetched automatically.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="work_item_providers",
    )
    provider_type = models.CharField(max_length=32, choices=WorkItemProviderType.choices)
    name = models.CharField(max_length=255)
    base_url = models.URLField(
        max_length=2048,
        blank=True,
        help_text="Leave blank for cloud-hosted instances (e.g. jira.atlassian.net).",
    )
    # Encrypted PAT / API token. GENERIC providers leave this blank.
    api_token = EncryptedCharField(max_length=2048, blank=True, default="")
    # Non-secret configuration: {"default_project_key": "SEC", "labels": ["aist"]}
    provider_config = models.JSONField(default=dict, blank=True)
    sync_enabled = models.BooleanField(
        default=False,
        help_text="Automatically sync work-item status from the tracker.",
    )
    is_active = models.BooleanField(default=True)
    # Optional VPN integration required to reach this provider's endpoint.
    # Must belong to the same organization (validated in the API serializer).
    vpn_integration = models.ForeignKey(
        "OrgIntegration",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dependent_work_item_providers",
        limit_choices_to={"integration_type": "VPN"},
        help_text="VPN integration to use when connecting to this provider. Must belong to the same organization.",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("organization", "name")]
        ordering = ["organization", "name"]

    def __str__(self) -> str:
        return f"{self.get_provider_type_display()} - {self.name}"

    def clean(self):
        super().clean()
        if self.vpn_integration_id is None:
            return
        vpn = self.vpn_integration
        errors = {}
        if vpn.integration_type != OrgIntegrationType.VPN:
            errors["vpn_integration"] = "The selected integration is not a VPN integration."
        elif vpn.organization_id != self.organization_id:
            errors["vpn_integration"] = "VPN integration must belong to the same organization."
        if errors:
            raise ValidationError(errors)


class WorkItemStatusCategory(models.TextChoices):
    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    DONE = "DONE", "Done"
    CANCELLED = "CANCELLED", "Cancelled / Won't Fix"
    UNKNOWN = "UNKNOWN", "Unknown"


class WorkItemLink(models.Model):

    """
    Associates a Finding with an external work item (Jira ticket, GitHub Issue, …).

    ``provider=None`` means a manual link: the user supplied the URL directly,
    no automatic sync is performed.  When a provider is set and
    ``provider.sync_enabled`` is True, a background task will periodically
    refresh ``raw_status`` / ``status_category`` via the tracker API.
    """

    finding = models.ForeignKey(
        Finding,
        on_delete=models.CASCADE,
        related_name="work_item_links",
    )
    provider = models.ForeignKey(
        WorkItemProvider,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # deleting a provider keeps the URL references
        related_name="work_item_links",
    )
    # Tracker-internal ID (e.g. "10042") and human-readable key (e.g. "SEC-42")
    external_id = models.CharField(max_length=255, blank=True)
    external_key = models.CharField(max_length=255, blank=True)
    external_url = models.URLField(max_length=2048)
    # Cached from last sync - shown in the UI without extra API calls
    title = models.CharField(max_length=500, blank=True)
    raw_status = models.CharField(max_length=255, blank=True)
    status_category = models.CharField(
        max_length=16,
        choices=WorkItemStatusCategory.choices,
        default=WorkItemStatusCategory.UNKNOWN,
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        # One finding can have at most one link per (provider, external_key) pair,
        # which allows multi-tracker scenarios (Jira + GitHub Issues on same finding).
        # provider=None links are NOT deduplicated by external_key on the DB level
        # (NULL != NULL in SQL), so application code must guard against duplicates
        # when provider is None.
        unique_together = [("finding", "provider", "external_key")]
        indexes = [
            models.Index(fields=["finding"], name="work_item_link_finding_idx"),
            models.Index(
                fields=["provider", "status_category"],
                name="work_item_link_prov_status_idx",
            ),
        ]

    def __str__(self) -> str:
        label = self.external_key or self.external_url
        return f"WorkItemLink({label})"

    def clean(self):
        super().clean()
        if self.provider_id is None or self.finding_id is None:
            return
        product_type_id = (
            Finding.objects
            .filter(pk=self.finding_id)
            .values_list("test__engagement__product__prod_type_id", flat=True)
            .first()
        )
        finding_organization_id = (
            Organization.objects
            .filter(product_type_id=product_type_id)
            .values_list("id", flat=True)
            .first()
        )
        if finding_organization_id != self.provider.organization_id:
            raise ValidationError({
                "provider": "Provider must belong to the finding's organization.",
            })


class AISTFindingAnnotation(models.Model):

    """Per-finding AIST-level flags that cannot be stored on the vendor Finding model."""

    finding = models.OneToOneField(
        Finding,
        on_delete=models.CASCADE,
        related_name="aist_annotation",
        db_index=True,
    )
    is_regression = models.BooleanField(
        default=False,
        help_text="True when the finding re-appeared after being previously mitigated.",
    )
    regression_detected_at = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Finding annotation"
        indexes = [
            models.Index(fields=["is_regression"], name="aist_annotation_regression_idx"),
        ]

    def __str__(self) -> str:
        return f"Annotation(finding={self.finding_id}, regression={self.is_regression})"


class ApiTokenScope(models.TextChoices):
    READ_ONLY = "read_only", "Read only"
    READ_WRITE = "read_write", "Read and write"


# Public, non-secret prefix identifying an AIST personal access token.
AIST_TOKEN_PREFIX = "aistpat_"  # noqa: S105  (not a secret — a scheme marker)


class AISTApiToken(models.Model):

    """
    A user-owned, single-organization personal access token for the AIST client API.

    The model OWNS its secret lifecycle (RAII): ``issue()`` generates the token
    and stores only a Django-hashed digest of the secret (via
    ``django.contrib.auth.hashers``); ``verify_secret()`` checks a presented
    secret. The plaintext secret exists only in the return value of ``issue()``
    and is never stored or recoverable.

    ``scope`` narrows the token to a subset of the owner's permissions
    (read-only vs read-write) — a token can never grant more than its owner has.
    Scoped tokens are valid ONLY on the AIST API; the admin guard rejects them on
    the vendor admin API, so a rank-and-file token can never reach ``/aist-admin/``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="aist_api_tokens",
    )
    organization = models.ForeignKey(
        "Organization",
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    name = models.CharField(max_length=100)
    scope = models.CharField(
        max_length=32,
        choices=ApiTokenScope.choices,
        default=ApiTokenScope.READ_ONLY,
    )
    # Non-secret, indexed lookup key parsed from the token; safe to store plainly.
    public_id = models.CharField(max_length=32, unique=True)
    # Django password-hasher encoded digest of the secret. Never the secret itself.
    secret_hash = models.CharField(max_length=128)
    # Last 4 chars of the secret, for disambiguating tokens in the UI.
    last4 = models.CharField(max_length=4)
    created = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AIST API token"
        unique_together = (("user", "organization", "name"),)
        indexes = [
            models.Index(fields=["user"], name="aist_api_token_user_idx"),
            models.Index(fields=["organization"], name="aist_api_token_org_idx"),
        ]

    def __str__(self) -> str:
        return (
            f"AISTApiToken(user={self.user_id}, organization={self.organization_id}, "
            f"name={self.name!r}, scope={self.scope})"
        )

    @classmethod
    def issue(
        cls,
        *,
        user,
        organization: Organization,
        name: str,
        scope: str,
        expires_at=None,
    ) -> tuple[AISTApiToken, str]:
        """Create a token and return (instance, raw_secret_token). Raw is available only here."""
        public_id = get_random_string(16)
        secret = get_random_string(40)
        token = cls.objects.create(
            user=user,
            organization=organization,
            name=name,
            scope=scope,
            public_id=public_id,
            secret_hash=make_password(secret),
            last4=secret[-4:],
            expires_at=expires_at,
        )
        return token, f"{AIST_TOKEN_PREFIX}{public_id}{cls._SECRET_SEP}{secret}"

    # Separator between public id and secret in the wire token. Distinct from the
    # get_random_string alphabet ([a-zA-Z0-9]) so the split is unambiguous.
    _SECRET_SEP = "_"  # noqa: S105  (delimiter, not a secret)

    @classmethod
    def parse_raw(cls, raw: str) -> tuple[str, str] | None:
        """Split a wire token ``aistpat_<public_id>_<secret>`` into (public_id, secret)."""
        if not raw.startswith(AIST_TOKEN_PREFIX):
            return None
        public_id, separator, secret = raw[len(AIST_TOKEN_PREFIX):].partition(cls._SECRET_SEP)
        if not (separator and public_id and secret):
            return None
        return public_id, secret

    def verify_secret(self, secret: str) -> bool:
        return check_password(secret, self.secret_hash)

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_usable(self) -> bool:
        return not self.is_revoked and not self.is_expired


class OrgMembershipAction(models.TextChoices):
    INVITED = "invited", "Invited"
    ROLE_CHANGED = "role_changed", "Role changed"
    REMOVED = "removed", "Removed"


class OrgMembershipHistory(models.Model):

    """
    Append-only audit log of org-membership mutations (invite / role change / removal).

    Written inside the same transaction as the mutation it records (see
    ``aist.members.service.OrganizationMembershipService``), so a membership
    change and its audit row are always created together or not at all. There is
    deliberately no update/delete path — history rows are never revised.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="membership_history",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="org_membership_actions_performed",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="org_membership_history_entries",
    )
    action = models.CharField(max_length=32, choices=OrgMembershipAction.choices)
    # Role ids, matching Product_Type_Member.role_id — null when not applicable
    # (no previous_role for "invited", no new_role for "removed").
    previous_role = models.IntegerField(null=True, blank=True)
    new_role = models.IntegerField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Org membership history"
        verbose_name_plural = "Org membership history"
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["organization", "-created"], name="org_membership_hist_org_idx"),
            models.Index(fields=["target_user", "-created"], name="org_membership_hist_user_idx"),
        ]

    def __str__(self) -> str:
        return f"OrgMembershipHistory(org={self.organization_id}, target={self.target_user_id}, action={self.action})"


class OrgMemberAccessScope(models.Model):

    """
    Explicit, persisted access-scope flag for one (organization, user) pair.

    ``Product_Type_Member``/``Product_Member`` (vendor, read-only) only record
    WHICH projects a user was granted — they cannot represent "this member was
    deliberately narrowed to zero projects" as distinct from "never narrowed."
    That distinction lives here instead: ``restricted=True`` means the
    member's effective access is exactly their ``Product_Member`` grants
    (including none of them); ``restricted=False`` (or no row) means they see
    every project in the organization at their org-wide role.

    This is now a PURELY explicit, org-wide mode switch — it is set ONLY by
    ``invite_member``'s restricted branch and cleared ONLY by
    ``reset_to_full_access``. Touching a single project's access
    (``grant_project``/``revoke_project``) never flips it: a "full" member
    who gets one project explicitly granted or denied stays "full" for every
    other project — see ``ProjectAccessDenial`` for how a full member's
    single-project "No access" is represented instead.

    Written and read exclusively through
    ``aist.members.service.OrganizationMembershipService`` and
    ``aist.queries.get_restricted_organization_ids`` (used by
    ``get_authorized_aist_products``) — never re-derive "is this member
    restricted" from ``Product_Member`` row counts; a full member CAN
    legitimately have ``Product_Member`` rows now (per-project downgrades),
    so their existence no longer implies restricted.
    """

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="member_access_scopes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="org_access_scopes",
    )
    restricted = models.BooleanField(default=False)

    class Meta:
        unique_together = [("organization", "user")]
        verbose_name = "Org member access scope"
        verbose_name_plural = "Org member access scopes"

    def __str__(self) -> str:
        return f"OrgMemberAccessScope(org={self.organization_id}, user={self.user_id}, restricted={self.restricted})"


class ProjectAccessDenial(models.Model):

    """
    Explicit "No access" override for one (project, user) pair — independent
    of the user's org-wide role and of every other project.

    Only meaningful for a "full" member (``OrgMemberAccessScope.restricted``
    is False): it subtracts exactly ONE project from their otherwise-full
    org access, without touching any other project or their org-wide role.
    For a restricted (allow-list) member it's redundant — absence of a
    ``Product_Member`` grant already means no access — but harmless, since
    ``get_authorized_aist_products`` excludes denied projects unconditionally.

    Deliberately a separate model from ``Product_Member`` rather than a
    sentinel "no access" ``Role`` row: vendor's own
    ``dojo.authorization.authorization.user_has_permission`` calls
    ``role_has_permission()`` on any ``Product_Member.role`` it finds once
    the org-wide role doesn't already satisfy the permission being checked —
    and that raises ``RoleDoesNotExistError`` for any role id outside
    vendor's own ``Roles`` enum. A sentinel role would crash every existing
    ``user_has_permission_or_403(user, product, ...)`` call site the moment
    the org role doesn't cover the permission — exactly the common case for
    a denied project. This model is never read by vendor code, so it can't.
    """

    project = models.ForeignKey(
        AISTProject,
        on_delete=models.CASCADE,
        related_name="access_denials",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="project_access_denials",
    )

    class Meta:
        unique_together = [("project", "user")]
        verbose_name = "Project access denial"
        verbose_name_plural = "Project access denials"

    def __str__(self) -> str:
        return f"ProjectAccessDenial(project={self.project_id}, user={self.user_id})"
