"""API tokens, organization membership history, and project access scopes."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone
from django.utils.crypto import get_random_string

from aist.models.organizations import Organization
from aist.models.projects import AISTProject


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

    It is an explicit, org-wide mode switch that only
    ``OrganizationMembershipService`` sets and clears (inviting or resetting a
    member, and removing a member). Touching a single project's access
    (``grant_project``/``revoke_project``) never flips it: a "full" member
    who gets one project explicitly granted or denied stays "full" for every
    other project — see ``ProjectAccessDenial`` for how a full member's
    single-project "No access" is represented instead.

    Written and read exclusively through
    ``aist.members.service.OrganizationMembershipService`` and
    ``aist.queries.get_restricted_organization_ids`` (used by
    ``get_authorized_aist_products``) — never re-derive "is this member
    restricted" from ``Product_Member`` row counts; a full member CAN
    legitimately have ``Product_Member`` rows (per-project downgrades),
    so their existence does not imply restricted.
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
