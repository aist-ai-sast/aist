from __future__ import annotations

import base64
import hashlib
import io
import logging
import shutil
import tarfile
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
    SAST_LAUNCHED = "SAST_LAUNCHED", "Launched"
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
    One organization can have many AISTProject objects.
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
        ]

    def __str__(self) -> str:
        return f"{self.organization.name} / {self.integration_type} / {self.name}"

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
        with vpn_sidecar_context(vpn_resolved, execution_id=execution_id) as (_, proxy_url):
            session = _requests.Session()
            if proxy_url:
                session.proxies.update({"http": proxy_url, "https": proxy_url})
            yield session


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
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="projects",
        null=True,
        blank=True,
    )
    ai_default_filter = models.JSONField(null=True, blank=True, default=None)

    def __str__(self) -> str:
        return self.product.name

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


class AISTProjectVersion(models.Model):
    project = models.ForeignKey(
        AISTProject, on_delete=models.CASCADE, related_name="versions",
    )
    version = models.CharField(max_length=64, db_index=True)
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


class AISTPipeline(models.Model):
    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)
    started = models.DateTimeField(auto_now=True)

    id = models.CharField(primary_key=True, max_length=64)

    project = models.ForeignKey(AISTProject, on_delete=models.PROTECT, related_name="aist_pipelines")
    project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        related_name="pipelines",
        db_index=True,
        null=True, blank=True,
    )
    status = models.CharField(max_length=64, choices=AISTStatus.choices, default=AISTStatus.FINISHED)

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

    def __str__(self) -> str:
        return f"SASTPipeline[{self.id}] {self.status}"


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

    max_concurrent_per_worker = models.PositiveIntegerField(
        default=1,
        help_text="Maximum number of concurrent pipeline runs per worker for this schedule.",
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

    class Meta:
        verbose_name = "Launch Schedule"
        verbose_name_plural = "Launch Schedules"

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


class PipelineLaunchQueue(models.Model):

    """
    Queued pipeline launch request. Items are created by LaunchSchedule when a cron triggers,
    and later dispatched by the pipeline dispatcher respecting concurrency limits.
    """

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE)
    schedule = models.ForeignKey(
        LaunchSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    launch_config = models.ForeignKey(
        "AISTProjectLaunchConfig",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="launch_queue_items",
        help_text="Launch config used to build pipeline_args snapshot.",
    )
    dispatched = models.BooleanField(default=False, db_index=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    pipeline = models.ForeignKey(
        "AISTPipeline",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="launch_queue_item",
    )

    class Meta:
        ordering = ["created"]

    def __str__(self):
        return f"LaunchQueue(project={self.project_id}, dispatched={self.dispatched})"


class AISTProjectLaunchConfig(models.Model):

    """
    Saved launch configuration ("preset") for a specific AISTProject.

    Stores PipelineArguments-like options excluding:
      - project_id (comes from FK)
      - project_version (chosen at run time)
    """

    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE, related_name="launch_configs")

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
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.name}"


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
    A user-scoped personal access token for the AIST client API.

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
        unique_together = (("user", "name"),)
        indexes = [
            models.Index(fields=["user"], name="aist_api_token_user_idx"),
        ]

    def __str__(self) -> str:
        return f"AISTApiToken(user={self.user_id}, name={self.name!r}, scope={self.scope})"

    @classmethod
    def issue(cls, *, user, name: str, scope: str, expires_at=None) -> tuple[AISTApiToken, str]:
        """Create a token and return (instance, raw_secret_token). Raw is available only here."""
        public_id = get_random_string(16)
        secret = get_random_string(40)
        token = cls.objects.create(
            user=user,
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
