"""
AIST's own role ladder: how roles rank, and which of them satisfy a permission.

``MINIMUM_ROLE_FOR_PERMISSION`` is the table published in
``docs/security/access-control-and-roles.md``; one edited row changes a capability's
tier everywhere. ``aist.authz.policy.ACTION_PERMISSIONS`` maps each named ``Action``
onto one of these permissions.
"""
from __future__ import annotations

from dojo.authorization.roles_permissions import Permissions, Roles

# Higher rank = more privileged.
ROLE_RANK: dict[int, int] = {
    Roles.Reader.value: 0,
    Roles.API_Importer.value: 1,
    Roles.Writer.value: 2,
    Roles.Maintainer.value: 3,
    Roles.Owner.value: 4,
}

# The least privileged role that satisfies each permission AIST uses.
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
    """Role ids that satisfy ``permission``. Raises for an undeclared one, never returns empty."""
    try:
        minimum = MINIMUM_ROLE_FOR_PERMISSION[permission]
        floor = ROLE_RANK[minimum.value]
    except KeyError as exc:
        msg = f"No AIST role requirement is declared for permission {permission!r}"
        raise UndeclaredPermissionError(msg) from exc
    return frozenset(role_id for role_id, rank in ROLE_RANK.items() if rank >= floor)
