"""
AIST's tenancy, registered as DefectDojo's own finding authorization.

DefectDojo code that re-authorizes a queryset — the risk-acceptance serializer, report
views, metrics — resolves access through ``dojo.authorization.query_filters``. Its
default reads ``Product.authorized_users``, which AIST never writes: membership lives in
``Product_Type_Member`` and is narrowed by ``OrgMemberAccessScope``,
``ProjectAccessDenial`` and the token's organization. Without this registration those
paths deny every AIST user who is not a superuser.
"""
from __future__ import annotations

from dojo.authorization.query_filters import register_auth_filter
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding

from aist.queries import get_authorized_aist_products

# What a vendor caller asks for, in AIST terms. A vendor action is a plain string on 3.x
# and a Permissions member on older call sites; both arrive here. Mutating intents share
# the Writer rank: the vendor pages that use them are behind the superuser-only
# ``/aist-admin/`` perimeter, so the rank bounds AIST endpoints, not those pages.
_READ_ACTIONS = frozenset({"view", Permissions.Finding_View})
_WRITE_ACTIONS = frozenset({"edit", "add", "delete", "import", Permissions.Finding_Edit})


def _aist_permission(permission) -> int | None:
    """The AIST permission a vendor request maps onto, or None when nobody qualifies."""
    if permission in _READ_ACTIONS:
        return Permissions.Finding_View
    if permission in _WRITE_ACTIONS:
        return Permissions.Finding_Edit
    name = getattr(permission, "name", "")
    if name.endswith(("_View", "_View_History")):
        return Permissions.Finding_View
    if name.endswith(("_Edit", "_Add", "_Delete")) or name == "Risk_Acceptance":
        return Permissions.Finding_Edit
    # Staff-only and superuser-only intents have no AIST role equivalent.
    return None


def authorized_findings(permission, queryset=None, user=None):
    """Findings the user may reach, scoped exactly as ``aist.queries`` scopes products."""
    base = Finding.objects.all() if queryset is None else queryset
    aist_permission = _aist_permission(permission)
    if aist_permission is None:
        return base.none()
    products = get_authorized_aist_products(aist_permission, user=user)
    return base.filter(test__engagement__product__in=products)


def install_vendor_finding_filters() -> None:
    """Replace the vendor defaults; ``override`` makes the wiring order-independent."""
    for key in ("finding.get_authorized_findings", "finding.get_authorized_findings_for_queryset"):
        register_auth_filter(key, authorized_findings, override=True)
