"""
Organization membership & per-project access management.

This module owns ALL business logic for managing who belongs to an organization
and which projects they can reach. API views are thin: they resolve/authorize the
organization, then delegate every mutation and every read to
``OrganizationMembershipService``. Keeping the rules here (instead of spreading
them across views and serializers) is deliberate — membership is security
sensitive and must have a single, auditable owner.

Membership model (org membership REQUIRED, per-project grants NARROW):
- Every member is a ``Product_Type_Member`` of the org — membership is the
  precondition for any access.
- Full member → no per-project grant; sees every project at their org-wide role.
- Restricted member → org member (baseline Reader) PLUS ``Product_Member`` grants
  that narrow access to only the granted projects (at the per-project role).
- A ``Product_Member`` without org membership grants NOTHING.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils.crypto import get_random_string
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions, Roles
from dojo.models import Product_Member, Product_Type_Member, Role, UserContactInfo
from rest_framework.exceptions import ValidationError

from aist.members.email import send_set_password_email
from aist.models import AISTApiToken, AISTProject, Organization
from aist.roles import ROLE_RANK

User = get_user_model()


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
        # member with per-project grants is "restricted"; without, "full".
        grants_by_user = self._project_grants_by_user()
        members = [
            self._member_view(tm, grants_by_user.get(tm.user_id, []))
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

    @transaction.atomic
    def invite_member(
        self,
        *,
        email: str,
        first_name: str = "",
        last_name: str = "",
        role_id: int | None = None,
        project_grants: list[dict] | None = None,
    ) -> User:
        self._require_full_or_restricted(role_id, project_grants)
        user, created = self._get_or_create_user(email, first_name, last_name)

        if role_id is not None:
            # Full member: org-wide role, sees every project in the org.
            self._set_org_role(user, self._role_or_400(role_id))
        else:
            # Restricted member: MUST still be an org member (baseline Reader), then
            # per-project grants narrow access to the chosen projects.
            self._ensure_org_membership(user)
            for grant in project_grants or []:
                self._grant_project(user, grant["project_id"], grant["role_id"])

        if created:
            send_set_password_email(user)
        return user

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
        member.role = new_role
        member.save(update_fields=["role"])

    @transaction.atomic
    def remove_member(self, *, user_id: int) -> None:
        self._member_or_404(user_id)
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
        self._grant_project(user, project_id, role_id)

    @transaction.atomic
    def revoke_project(self, *, user_id: int, project_id: int) -> None:
        project = self._project_in_org_or_400(project_id)
        existing = Product_Member.objects.filter(user_id=user_id, product=project.product).first()
        if existing is None:
            return
        # Removing an existing Owner grant requires the Add_Owner permission.
        if existing.role_id == Roles.Owner.value:
            user_has_permission_or_403(self.actor, project.product, Permissions.Product_Member_Add_Owner)
        existing.delete()

    # -- internal: grants ------------------------------------------------

    def _grant_project(self, user: User, project_id: int, role_id: int) -> None:
        project = self._project_in_org_or_400(project_id)
        role = self._role_or_400(role_id)
        existing = Product_Member.objects.filter(user=user, product=project.product).first()
        # Creating an Owner grant, or overwriting an existing Owner grant, both
        # require Add_Owner — a Maintainer must not silently demote a project Owner.
        touches_owner = role.id == Roles.Owner.value or (existing is not None and existing.role_id == Roles.Owner.value)
        if touches_owner:
            user_has_permission_or_403(self.actor, project.product, Permissions.Product_Member_Add_Owner)
        Product_Member.objects.update_or_create(
            user=user, product=project.product, defaults={"role": role},
        )

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
        existing = User.objects.filter(email__iexact=normalized).first()
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

    def _member_view(self, tm: Product_Type_Member, grants: list[ProjectGrantView]) -> MemberView:
        # Membership always comes from Product_Type_Member; per-project grants make
        # the member "restricted" (narrowed to those projects).
        return MemberView(
            user_id=tm.user_id,
            username=tm.user.username,
            email=tm.user.email,
            first_name=tm.user.first_name,
            last_name=tm.user.last_name,
            is_active=tm.user.is_active,
            role_id=tm.role_id,
            role_name=tm.role.name if tm.role else "Reader",
            membership_type="restricted" if grants else "full",
            project_grants=grants,
        )

    def _project_in_org_or_400(self, project_id: int) -> AISTProject:
        project = (
            AISTProject.objects
            .filter(pk=project_id, product__prod_type=self.product_type)
            .select_related("product")
            .first()
        )
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
        remaining_owners = (
            Product_Type_Member.objects
            .filter(product_type=self.product_type, role_id=Roles.Owner.value)
            .exclude(user_id=excluding_user_id)
            .exists()
        )
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
