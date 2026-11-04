"""Source-control repositories, their per-host bindings, and pull requests."""

from __future__ import annotations

import base64
import binascii
import logging
from urllib.parse import quote, urlparse

import gitlab
import requests
from asgiref.sync import async_to_sync
from django.core.validators import RegexValidator
from django.db import models
from django_github_app.models import Installation

from aist.integrations.scm_errors import ScmFetchError

SCM_FILE_TIMEOUT_SECONDS = 10

_repo_part_validator = RegexValidator(
    regex=r"^[A-Za-z0-9._/\-]+$",
    message="Only letters, digits, dot, underscore, hyphen and slash are allowed.",
)


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

    def raw_file_url(self, ref: str, path: str) -> str | None:
        """
        Return the URL that downloads `path` at `ref` over plain HTTP, or None when this host has none.

        `path` is relative to the repository root; `path` and `ref` are percent-encoded into the URL. GitLab is
        read through its API client instead, and Gerrit only through its binding's authenticated endpoint.
        """
        host = self.host()
        if self.type == ScmType.GITHUB:
            file_path = f"{quote(ref, safe='/')}/{quote(path, safe='/')}"
            if urlparse(host).hostname == "github.com":
                return f"https://raw.githubusercontent.com/{self.repo_full}/{file_path}"
            return f"{host}/{self.repo_full}/raw/{file_path}"
        if self.type == ScmType.GITEA:
            return f"{host}/api/v1/repos/{self.repo_full}/raw/{quote(path, safe='')}?ref={quote(ref, safe='')}"
        if self.type == ScmType.GERRIT:
            binding = self.get_binding()
            return binding.build_raw_url(self, ref, path) if binding else None
        return None

    def read_file(self, ref: str, path: str, *, proxy_url: str | None = None) -> bytes | None:
        """
        Return the bytes of `path` at `ref`, or None when the repository has no such file.

        `path` is relative to the repository root. The request carries the binding's credentials when
        a binding exists and goes through `proxy_url` when it is set. Any other failure status raises
        ScmFetchError; connection errors and timeouts propagate.
        """
        binding = self.get_binding()
        if self.type == ScmType.GITLAB:
            return ScmGitlabBinding.read_raw_file(
                self.host(), binding._token() if binding else "", self.repo_full, ref, path, proxy_url=proxy_url,
            )
        url = self.raw_file_url(ref, path)
        if url is None:
            return None
        response = requests.get(
            url,
            headers=binding.get_auth_headers() if binding else {},
            timeout=SCM_FILE_TIMEOUT_SECONDS,
            allow_redirects=True,
            proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ScmFetchError(response.status_code)
        if self.type == ScmType.GERRIT:
            try:
                return base64.b64decode(response.content)
            except binascii.Error as exc:
                raise ScmFetchError(response.status_code) from exc
        return response.content


def _inject_creds(host: str, creds: str) -> str:
    """
    Insert ``creds@`` right after the scheme of an ``https://`` or ``http://`` URL; any other value is returned as is.

    Self-hosted SCM instances are often reachable only over plain ``http://`` on an internal network.
    """
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            return f"{scheme}{creds}@{host[len(scheme):]}"
    return host


class ScmGithubBinding(models.Model):

    """GitHub-specific binding for RepositoryInfo."""

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

    """GitLab-specific binding for RepositoryInfo."""

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

    def get_auth_headers(self) -> dict[str, str]:
        """Return API auth header for GitLab."""
        tok = self._token()
        return {"PRIVATE-TOKEN": tok} if tok else {}

    @staticmethod
    def _client(base: str, token: str | None, proxy_url: str | None, **options) -> gitlab.Gitlab:
        """Return a GitLab API client for `base` authenticated by `token`, routed through `proxy_url` when set."""
        if proxy_url:
            session = requests.Session()
            session.proxies = {"http": proxy_url, "https": proxy_url}
            options["session"] = session
        return gitlab.Gitlab(base, private_token=token or None, **options)

    @staticmethod
    def _fetch_gitlab_project(base: str, token: str | None, repo_full: str, proxy_url: str | None):
        return ScmGitlabBinding._client(base, token, proxy_url).projects.get(repo_full)

    @staticmethod
    def read_raw_file(
        base: str, token: str | None, repo_full: str, ref: str, path: str, *, proxy_url: str | None = None,
    ) -> bytes | None:
        """
        Return the bytes of `path` at `ref` in project `repo_full`, or None when GitLab has no such file.

        Any other failure status raises ScmFetchError; connection errors and timeouts propagate.
        """
        client = ScmGitlabBinding._client(base.rstrip("/"), token, proxy_url, timeout=SCM_FILE_TIMEOUT_SECONDS)
        try:
            return client.projects.get(repo_full, lazy=True).files.raw(file_path=path, ref=ref, obey_rate_limit=False)
        except gitlab.exceptions.GitlabError as exc:
            if exc.response_code == 404:
                return None
            raise ScmFetchError(exc.response_code or 502) from exc

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
        """Return the Gerrit REST content endpoint URL of `path` at `ref`; its body is the file in base64."""
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

    def get_auth_headers(self) -> dict[str, str]:
        token = self._token()
        return {"Authorization": f"token {token}"} if token else {}

    def get_project_info(self, scm: RepositoryInfo, *, proxy_url: str | None = None):
        logger = logging.getLogger("aist")
        base = self.host(scm).rstrip("/")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        try:
            resp = requests.get(
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
