"""
Organization membership & per-project access management.

This module owns ALL business logic for managing who belongs to an organization
and which projects they can reach. API views are thin: they resolve/authorize the
organization, then delegate every mutation and every read to
``OrganizationMembershipService``. Keeping the rules here (instead of spreading
them across views and serializers) is deliberate — membership is security
sensitive and must have a single, auditable owner.

Membership model (org membership REQUIRED, per-project overrides are LOCAL):
- Every member is a ``Product_Type_Member`` of the org — membership is the
  precondition for any access.
- Full member (``OrgMemberAccessScope.restricted`` is False) → sees every
  project at their org-wide role, EXCEPT projects with an explicit
  per-project override: a ``ProjectAccessDenial`` (no access) or a
  ``Product_Member`` grant capped at (never above) their org-wide role.
  Touching one project's override never affects any other project.
- Restricted member (``OrgMemberAccessScope.restricted`` is True, set only by
  a restricted invite or cleared by ``reset_to_full_access``) → org member
  (baseline Reader) PLUS ``Product_Member`` grants that are the ONLY source of
  their access — including zero of them, meaning zero access everywhere.
- A ``Product_Member`` without org membership grants NOTHING.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import Count
from django.utils.crypto import get_random_string
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions, Roles
from dojo.models import Product_Member, Product_Type_Member, Role, UserContactInfo
from rest_framework.exceptions import ValidationError

from aist.members.email import send_set_password_email
from aist.models import (
    AISTApiToken,
    AISTProject,
    Organization,
    OrgMemberAccessScope,
    OrgMembershipAction,
    OrgMembershipHistory,
    ProjectAccessDenial,
)
from aist.roles import ROLE_RANK, role_rank

User = get_user_model()

# invite_member outcomes, distinct from the internal "created" boolean:
# INVITED covers both a brand-new user AND a former member re-invited after
# being removed from every org they belonged to (see remove_member's
# is_active handling below) — both get a set-password email, per product
# decision treated identically to a fresh invite. EXISTING_USER_NO_EMAIL is an
# already-active user of another org being added here for the first time —
# they already have working credentials, so no email is sent.
INVITE_OUTCOME_INVITED = "invited"
INVITE_OUTCOME_EXISTING_USER_NO_EMAIL = "existing_user_added_no_email"


@dataclass(slots=True)
class ProjectGrantView:
    project_id: int
    product_id: int
    project_name: str
    role_id: int
    role_name: str


@dataclass(slots=True)
class MemberView:
    user_id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool
    role_id: int | None
    role_name: str
    membership_type: str  # "full" | "restricted"
    token_count: int = 0
    project_grants: list[ProjectGrantView] = field(default_factory=list)
    denied_project_ids: list[int] = field(default_factory=list)

    @property
    def has_token(self) -> bool:
        return self.token_count > 0


class OrganizationMembershipService:

    """
    Membership operations scoped to a single organization.

    The caller (an API view) is responsible for having already authorized
    ``actor`` for ``Permissions.Product_Type_Manage_Members`` on ``organization``
    (typically by resolving the org through
    ``get_authorized_aist_organizations(Product_Type_Manage_Members, ...)``).
    This service enforces the finer-grained rules on top of that gate:
    Owner-role escalation, the "keep at least one owner" guard, and the strict
    "grants stay inside this organization" invariant.
    """

    def __init__(self, organization: Organization, actor: User) -> None:
        self.organization = organization
        self.product_type = organization.ensure_product_type()
        self.actor = actor

    # -- reads -----------------------------------------------------------

    def list_members(self) -> list[MemberView]:
        # Every member is a Product_Type_Member (org membership is required). A
        # member is "restricted" when OrgMemberAccessScope says so — never
        # re-derived from whether they happen to have any grants (see
        # OrgMemberAccessScope's docstring for why).
        grants_by_user = self._project_grants_by_user()
        denied_by_user = self._denied_project_ids_by_user()
        restricted_user_ids = self._restricted_user_ids()
        members = [
            self._member_view(
                tm, grants_by_user.get(tm.user_id, []), denied_by_user.get(tm.user_id, []), restricted_user_ids,
            )
            for tm in (
                Product_Type_Member.objects
                .filter(product_type=self.product_type)
                .select_related("user", "role")
            )
        ]

        # Token indicator (count only — never the token itself) so org admins and
        # superusers see who holds API tokens from the same user-management panel.
        token_counts = self._token_counts([member.user_id for member in members])
        for member in members:
            member.token_count = token_counts.get(member.user_id, 0)

        return sorted(members, key=lambda m: (m.username or "").lower())

    @staticmethod
    def _token_counts(user_ids) -> dict[int, int]:
        rows = (
            AISTApiToken.objects
            .filter(user_id__in=user_ids)
            .values("user_id")
            .annotate(count=Count("id"))
        )
        return {row["user_id"]: row["count"] for row in rows}

    def list_project_grants(self, user_id: int) -> list[ProjectGrantView]:
        self._member_or_404(user_id)
        return self._project_grants_by_user().get(user_id, [])

    # -- membership mutations -------------------------------------------

    def _record_history(
        self, *, target_user: User, action: str, previous_role: int | None, new_role: int | None,
    ) -> None:
        OrgMembershipHistory.objects.create(
            organization=self.organization,
            actor=self.actor,
            target_user=target_user,
            action=action,
            previous_role=previous_role,
            new_role=new_role,
        )

    @transaction.atomic
    def invite_member(
        self,
        *,
        email: str,
        first_name: str = "",
        last_name: str = "",
        role_id: int | None = None,
        project_grants: list[dict] | None = None,
    ) -> tuple[User, str]:
        self._require_full_or_restricted(role_id, project_grants)
        user, created = self._get_or_create_user(email, first_name, last_name)
        # A deactivated user only got that way by remove_member finding them
        # orphaned (no membership left in ANY org) — an active user of another
        # org already has working credentials and must not be re-emailed.
        is_reactivation = not created and not user.is_active
        if is_reactivation:
            # Treated exactly like a brand-new invite: force them through the
            # set-password link rather than assuming their old password (set
            # before whatever removed them) is still appropriate to reuse.
            user.is_active = True
            user.set_password(get_random_string(50))
            user.save(update_fields=["is_active", "password"])

        if role_id is not None:
            # Full member: org-wide role, sees every project in the org. Set
            # explicitly (not just relying on "no scope row yet") in case this
            # email previously belonged to a restricted member of this same
            # org who was removed — remove_member clears the scope row, but
            # being explicit here doesn't depend on that invariant holding.
            self._set_org_role(user, self._role_or_400(role_id))
            self._set_restricted(user, restricted=False)
        else:
            # Restricted member: MUST still be an org member (baseline Reader), then
            # per-project grants narrow access to the chosen projects. Marked
            # restricted explicitly (not inferred from grant count) so this
            # stays true even if every grant is later revoked.
            self._ensure_org_membership(user)
            self._set_restricted(user, restricted=True)
            for grant in project_grants or []:
                self._grant_project(user, grant["project_id"], grant["role_id"])

        if created or is_reactivation:
            send_set_password_email(user)
            outcome = INVITE_OUTCOME_INVITED
        else:
            outcome = INVITE_OUTCOME_EXISTING_USER_NO_EMAIL
        self._record_history(
            target_user=user,
            action=OrgMembershipAction.INVITED,
            previous_role=None,
            new_role=role_id if role_id is not None else Roles.Reader.value,
        )
        return user, outcome

    @transaction.atomic
    def change_role(self, *, user_id: int, role_id: int) -> None:
        member = self._org_member_or_404(user_id)
        new_role = self._role_or_400(role_id)
        # Touching an Owner in either direction (promoting to, or editing/demoting
        # an existing one) requires the Add_Owner permission. Mirrors DefectDojo's
        # own edit_product_type_member, which gates on the CURRENT role too — so a
        # Maintainer cannot strip an Owner's privileges.
        if Roles.Owner.value in {member.role_id, new_role.id}:
            self._require_owner_grant_permission()
        if member.role_id == Roles.Owner.value and new_role.id != Roles.Owner.value:
            self._guard_last_owner(excluding_user_id=user_id)
        previous_role_id = member.role_id
        member.role = new_role
        member.save(update_fields=["role"])
        self._record_history(
            target_user=member.user,
            action=OrgMembershipAction.ROLE_CHANGED,
            previous_role=previous_role_id,
            new_role=new_role.id,
        )

    @transaction.atomic
    def remove_member(self, *, user_id: int) -> None:
        target_user = self._member_or_404(user_id)
        org_member = (
            Product_Type_Member.objects
            .filter(product_type=self.product_type, user_id=user_id)
            .first()
        )
        if org_member is not None and org_member.role_id == Roles.Owner.value:
            # Evicting an Owner is an owner-level action: it needs Add_Owner (a
            # Maintainer must not remove an Owner) as well as the last-owner guard.
            self._require_owner_grant_permission()
            self._guard_last_owner(excluding_user_id=user_id)
        # Remove every trace of the user's access inside this organization.
        Product_Member.objects.filter(
            user_id=user_id, product__prod_type=self.product_type,
        ).delete()
        Product_Type_Member.objects.filter(
            product_type=self.product_type, user_id=user_id,
        ).delete()
        OrgMemberAccessScope.objects.filter(
            organization=self.organization, user_id=user_id,
        ).delete()
        self._deactivate_if_orphaned(user_id)
        self._record_history(
            target_user=target_user,
            action=OrgMembershipAction.REMOVED,
            previous_role=org_member.role_id if org_member is not None else None,
            new_role=None,
        )

    @staticmethod
    def _deactivate_if_orphaned(user_id: int) -> None:
        """
        If this removal left the user with no membership in any organization,
        deactivate their account. Without this, re-inviting the same email
        later is indistinguishable from adding an already-active user of a
        different org — invite_member would silently skip the email.
        Superusers are exempt: their access doesn't derive from org
        membership, so losing their last org membership must not lock them
        out of everything else.
        """
        has_any_membership = Product_Type_Member.objects.filter(user_id=user_id).exists()
        if not has_any_membership:
            User.objects.filter(pk=user_id, is_active=True, is_superuser=False).update(is_active=False)

    def reset_password(self, *, user_id: int) -> None:
        user = self._member_or_404(user_id)
        send_set_password_email(user, purpose="reset")

    # -- per-project grant mutations ------------------------------------

    @transaction.atomic
    def grant_project(self, *, user_id: int, project_id: int, role_id: int) -> None:
        # Only existing members can receive additional grants; brand-new people
        # are onboarded through invite_member. This avoids granting to (and thus
        # enumerating) arbitrary users outside the organization.
        user = self._member_or_404(user_id)
        # This never touches the org-wide restricted flag: for a full member
        # it's a single-project downgrade override (capped at their org role,
        # enforced in _grant_project), for a restricted member it's an
        # allow-list grant — neither should affect any other project.
        self._grant_project(user, project_id, role_id, lock=True)

    @transaction.atomic
    def revoke_project(self, *, user_id: int, project_id: int) -> None:
        user = self._member_or_404(user_id)
        project = self._project_in_org_or_400(project_id, lock=True)
        existing = Product_Member.objects.filter(user_id=user_id, product=project.product).first()
        if existing is not None:
            # Removing an existing Owner grant requires the Add_Owner permission.
            if existing.role_id == Roles.Owner.value:
                user_has_permission_or_403(self.actor, project.product, Permissions.Product_Member_Add_Owner)
            existing.delete()
        # Always record an explicit denial for THIS project — the only way to
        # subtract access from a full member's org-wide role without
        # affecting any other project (this is the fix: revoking one project
        # used to be a no-op for a full member, since there was no grant to
        # delete, silently leaving them with full access everywhere).
        # Redundant but harmless for an already-restricted member, who has no
        # access here anyway without a grant.
        ProjectAccessDenial.objects.get_or_create(user=user, project=project)

    @transaction.atomic
    def reset_to_full_access(self, *, user_id: int) -> None:
        """
        Explicit, deliberate way back to full org access — the only path that
        clears ``restricted``. Also drops any stray Product_Member grants and
        ProjectAccessDenial overrides so a later re-narrowing doesn't silently
        resurrect old, forgotten state.
        """
        user = self._member_or_404(user_id)
        Product_Member.objects.filter(user=user, product__prod_type=self.product_type).delete()
        ProjectAccessDenial.objects.filter(user=user, project__product__prod_type=self.product_type).delete()
        self._set_restricted(user, restricted=False)

    # -- internal: grants ------------------------------------------------

    def _grant_project(self, user: User, project_id: int, role_id: int, *, lock: bool = False) -> None:
        project = self._project_in_org_or_400(project_id, lock=lock)
        role = self._role_or_400(role_id)
        if not self._is_restricted(user):
            # A full member's per-project role is a DOWNGRADE override only —
            # it must never exceed their org-wide role (an elevation on one
            # project would be a privilege escalation). Restricted members are
            # exempt: their org-wide role is just a baseline-membership Reader
            # with no real access of its own — grants are the only source of
            # their actual access, so any role is legitimate there.
            if role_rank(role.id) > role_rank(self._org_role_id(user)):
                raise ValidationError(
                    {"role_id": "Cannot grant a project role higher than the member's organization role."},
                )
        existing = Product_Member.objects.filter(user=user, product=project.product).first()
        # Creating an Owner grant, or overwriting an existing Owner grant, both
        # require Add_Owner — a Maintainer must not silently demote a project Owner.
        touches_owner = role.id == Roles.Owner.value or (existing is not None and existing.role_id == Roles.Owner.value)
        if touches_owner:
            user_has_permission_or_403(self.actor, project.product, Permissions.Product_Member_Add_Owner)
        Product_Member.objects.update_or_create(
            user=user, product=project.product, defaults={"role": role},
        )
        ProjectAccessDenial.objects.filter(user=user, project=project).delete()

    def _set_org_role(self, user: User, role: Role) -> None:
        if role.id == Roles.Owner.value:
            self._require_owner_grant_permission()
        Product_Type_Member.objects.update_or_create(
            product_type=self.product_type, user=user, defaults={"role": role},
        )

    # -- internal: user lifecycle ---------------------------------------

    def _get_or_create_user(self, email: str, first_name: str, last_name: str) -> tuple[User, bool]:
        normalized = User.objects.normalize_email(email).strip()
        if not normalized:
            raise ValidationError({"email": "A valid email is required."})
        # The DB now enforces case-insensitive email uniqueness (see migration
        # 0037), but that alone would surface a concurrent double-invite as an
        # unhandled IntegrityError/500 rather than the graceful "reuse the
        # existing user" outcome below. This advisory lock, keyed by the
        # normalized email, serializes concurrent invites for the SAME email so
        # the check-then-create stays race-free and the second invite just sees
        # the first invite's committed row; it's released automatically at
        # transaction end (this method always runs inside invite_member's
        # @transaction.atomic).
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [normalized.lower()])
        existing = User.objects.filter(email__iexact=normalized).order_by("id").first()
        if existing is not None:
            return existing, False
        user = self._create_invited_user(normalized, first_name, last_name)
        UserContactInfo.objects.get_or_create(user=user)
        return user, True

    def _create_invited_user(self, email: str, first_name: str, last_name: str) -> User:
        # Retry on the username unique constraint so two concurrent invites whose
        # emails share a local-part don't 500 on a race; each retry re-derives a
        # fresh unique candidate.
        for _attempt in range(5):
            user = User(
                username=self._unique_username(email),
                email=email,
                first_name=first_name or "",
                last_name=last_name or "",
                is_active=True,
            )
            # A random, immediately-discarded password: the invitee never receives
            # a shared secret — they set their own password via the emailed link.
            # A *usable* password is required so Django's password-reset email is sent.
            user.set_password(get_random_string(50))
            try:
                with transaction.atomic():
                    user.save()
            except IntegrityError:
                continue
            return user
        msg = "Could not allocate a unique username; please retry."
        raise ValidationError({"email": msg})

    @staticmethod
    def _unique_username(email: str) -> str:
        base = email.split("@", 1)[0].strip() or "user"
        candidate = base
        suffix = 1
        while User.objects.filter(username=candidate).exists():
            suffix += 1
            candidate = f"{base}{suffix}"
        return candidate

    # -- internal: lookups & guards -------------------------------------

    def _project_grants_by_user(self) -> dict[int, list[ProjectGrantView]]:
        projects_by_product = {
            project.product_id: project
            for project in AISTProject.objects
            .filter(product__prod_type=self.product_type)
            .select_related("product")
        }
        grants: dict[int, list[ProjectGrantView]] = {}
        for pm in (
            Product_Member.objects
            .filter(product__prod_type=self.product_type)
            .select_related("product", "role")
        ):
            project = projects_by_product.get(pm.product_id)
            if project is None:
                continue
            grants.setdefault(pm.user_id, []).append(
                ProjectGrantView(
                    project_id=project.id,
                    product_id=pm.product_id,
                    project_name=pm.product.name,
                    role_id=pm.role_id,
                    role_name=pm.role.name,
                ),
            )
        return grants

    def _denied_project_ids_by_user(self) -> dict[int, list[int]]:
        denials: dict[int, list[int]] = {}
        for denial in ProjectAccessDenial.objects.filter(project__product__prod_type=self.product_type):
            denials.setdefault(denial.user_id, []).append(denial.project_id)
        return denials

    def _restricted_user_ids(self) -> set[int]:
        # Purely the persisted flag now — see OrgMemberAccessScope's docstring
        # for why a Product_Member grant no longer implies restricted.
        return set(
            OrgMemberAccessScope.objects
            .filter(organization=self.organization, restricted=True)
            .values_list("user_id", flat=True),
        )

    def _is_restricted(self, user: User) -> bool:
        return OrgMemberAccessScope.objects.filter(
            organization=self.organization, user=user, restricted=True,
        ).exists()

    def _org_role_id(self, user: User) -> int | None:
        member = Product_Type_Member.objects.filter(product_type=self.product_type, user=user).first()
        return member.role_id if member is not None else None

    def _set_restricted(self, user: User, *, restricted: bool) -> None:
        OrgMemberAccessScope.objects.update_or_create(
            organization=self.organization, user=user, defaults={"restricted": restricted},
        )

    def _member_view(
        self,
        tm: Product_Type_Member,
        grants: list[ProjectGrantView],
        denied_project_ids: list[int],
        restricted_user_ids: set[int],
    ) -> MemberView:
        # Membership always comes from Product_Type_Member; "restricted" is the
        # persisted OrgMemberAccessScope flag, NOT whether grants happen to be
        # non-empty (a restricted member with zero grants has zero access, not
        # full access — see OrgMemberAccessScope's docstring).
        return MemberView(
            user_id=tm.user_id,
            username=tm.user.username,
            email=tm.user.email,
            first_name=tm.user.first_name,
            last_name=tm.user.last_name,
            is_active=tm.user.is_active,
            role_id=tm.role_id,
            denied_project_ids=denied_project_ids,
            role_name=tm.role.name if tm.role else "Reader",
            membership_type="restricted" if tm.user_id in restricted_user_ids else "full",
            project_grants=grants,
        )

    def _project_in_org_or_400(self, project_id: int, *, lock: bool = False) -> AISTProject:
        qs = AISTProject.objects.filter(pk=project_id, product__prod_type=self.product_type).select_related("product")
        if lock:
            # Serializes concurrent grant_project/revoke_project calls on the
            # SAME project (whole-project granularity, not per-user) so one
            # can't half-commit a Product_Member delete + ProjectAccessDenial
            # create while the other concurrently creates a grant — closing a
            # TOCTOU window that would otherwise leave both a grant and a
            # denial existing for the same (user, project) at once. Only used
            # by grant_project/revoke_project — both already @transaction.atomic.
            qs = qs.select_for_update()
        project = qs.first()
        if project is None:
            raise ValidationError({"project_id": "Project does not belong to this organization."})
        return project

    def _role_or_400(self, role_id: int) -> Role:
        role = Role.objects.filter(id=role_id).first()
        if role is None or role_id not in ROLE_RANK:
            raise ValidationError({"role_id": "Unknown role."})
        return role

    def _member_or_404(self, user_id: int) -> User:
        """Return the user iff they are an organization member (Product_Type_Member)."""
        if not Product_Type_Member.objects.filter(
            product_type=self.product_type, user_id=user_id,
        ).exists():
            msg = "User is not a member of this organization."
            raise ValidationError({"user_id": msg})
        return self._user_or_404(user_id)

    def _ensure_org_membership(self, user: User) -> None:
        """Guarantee a baseline org membership (Reader) without downgrading an existing role."""
        Product_Type_Member.objects.get_or_create(
            product_type=self.product_type,
            user=user,
            defaults={"role": self._role_or_400(Roles.Reader.value)},
        )

    def _org_member_or_404(self, user_id: int) -> Product_Type_Member:
        member = (
            Product_Type_Member.objects
            .filter(product_type=self.product_type, user_id=user_id)
            .first()
        )
        if member is None:
            raise ValidationError({"user_id": "User is not an organization-level member."})
        return member

    @staticmethod
    def _user_or_404(user_id: int) -> User:
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            raise ValidationError({"user_id": "Unknown user."})
        return user

    def _guard_last_owner(self, *, excluding_user_id: int) -> None:
        # select_for_update locks the current Owner rows for the lifetime of the
        # caller's transaction (change_role/remove_member are both
        # @transaction.atomic), so a second concurrent demote/remove targeting a
        # different Owner blocks here until the first commits — then re-reads
        # the post-commit role/existence, closing the race that could otherwise
        # leave the organization with zero Owners.
        owner_ids = list(
            Product_Type_Member.objects
            .select_for_update()
            .filter(product_type=self.product_type, role_id=Roles.Owner.value)
            .values_list("user_id", flat=True),
        )
        remaining_owners = any(uid != excluding_user_id for uid in owner_ids)
        if not remaining_owners:
            msg = "The organization must keep at least one owner."
            raise ValidationError(msg)

    def _require_owner_grant_permission(self) -> None:
        user_has_permission_or_403(self.actor, self.product_type, Permissions.Product_Type_Member_Add_Owner)

    @staticmethod
    def _require_full_or_restricted(role_id: int | None, project_grants: list[dict] | None) -> None:
        has_role = role_id is not None
        has_grants = bool(project_grants)
        if has_role == has_grants:
            msg = "Provide exactly one of 'role_id' (full member) or 'project_grants' (restricted member)."
            raise ValidationError(msg)
