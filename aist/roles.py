"""
AIST's own role ladder: how roles rank, and which of them satisfy a permission.

Both facts used to be inherited from DefectDojo at runtime
(``get_roles_for_permission``). DefectDojo 3.x releases role-based authorization
to its paid plugin and turns that function into a stub that answers "no roles",
which would collapse every tenant-scoped queryset to empty. Tenant isolation is
this platform's first rule, so the ladder is stated here instead of borrowed.

``MINIMUM_ROLE_FOR_PERMISSION`` is the table documented in
``docs/security/access-control-and-roles.md``: changing which role a capability
needs is one edited row. ``aist.authz.policy.ACTION_PERMISSIONS`` maps each named
``Action`` onto one of these permissions.
"""
from __future__ import annotations

from dojo.authorization.roles_permissions import Permissions, Roles

# Higher rank = more privileged. Used to pick the "best" role when a user reaches
# an organization through several grant sources, and to expand a minimum role
# into the set that satisfies it.
ROLE_RANK: dict[int, int] = {
    Roles.Reader.value: 0,
    Roles.API_Importer.value: 1,
    Roles.Writer.value: 2,
    Roles.Maintainer.value: 3,
    Roles.Owner.value: 4,
}

# The least privileged role that satisfies each permission AIST uses. Reads are
# Reader-level, finding mutation and risk acceptance are Writer-level, project
# and organization management are Maintainer-level, and granting Owner is
# Owner-only.
MINIMUM_ROLE_FOR_PERMISSION: dict[int, int] = {
    Permissions.Product_View: Roles.Reader,
    Permissions.Finding_View: Roles.Reader,
    Permissions.Test_View: Roles.Reader,
    Permissions.Engagement_View: Roles.Reader,
    Permissions.Product_Type_View: Roles.Reader,
    Permissions.Finding_Edit: Roles.Writer,
    Permissions.Risk_Acceptance: Roles.Writer,
    Permissions.Product_Edit: Roles.Maintainer,
    Permissions.Product_Type_Add_Product: Roles.Maintainer,
    Permissions.Product_Type_Manage_Members: Roles.Maintainer,
    Permissions.Product_Member_Add_Owner: Roles.Owner,
    Permissions.Product_Type_Member_Add_Owner: Roles.Owner,
}


class UndeclaredPermissionError(KeyError):

    """A permission reached tenant scoping without a declared role requirement."""


def role_rank(role_id: int | None) -> int:
    return ROLE_RANK.get(role_id, -1)


def roles_for_permission(permission: int) -> frozenset[int]:
    """
    Role ids that satisfy ``permission``, as role ladder membership.

    Raises for an undeclared permission rather than returning an empty set: every
    caller reads an empty set as the valid answer "no role qualifies", so a typo
    would silently deny all access instead of failing where it was introduced.
    """
    try:
        minimum = MINIMUM_ROLE_FOR_PERMISSION[permission]
        floor = ROLE_RANK[minimum.value]
    except KeyError as exc:
        msg = f"No AIST role requirement is declared for permission {permission!r}"
        raise UndeclaredPermissionError(msg) from exc
    return frozenset(role_id for role_id, rank in ROLE_RANK.items() if rank >= floor)
