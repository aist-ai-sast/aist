"""
Single source of truth for AIST endpoint authorization.

Everything an endpoint is allowed to do is expressed here as data, not scattered
`Permissions.*` literals across `aist/api/*.py`:

- ``Action`` - the named capability ladder (read tiers + the write ladder T1-T3).
- ``ACTION_PERMISSIONS`` — the ONE place mapping each action to a DefectDojo
  ``Permissions`` value. Changing which role a capability needs = editing one row.
- ``RESOURCE_GETTERS`` — resource model → its org-scoped ``aist.queries`` getter,
  so object resolution always knows its tenant-scoping query.
- ``ResourcePolicy`` — an endpoint's declared contract: which resource, and which
  action applies to reads vs writes. ``AISTAPIView`` resolves objects through it.
- ``PUBLIC`` / ``INTERNAL_SERVICE`` — the only sanctioned escape hatches for
  endpoints that are not org-owned-resource CRUD (login, callbacks, static refs).

This module holds NO request/response logic — it is pure policy data + resolution
helpers, imported by ``aist.authz.base.AISTAPIView``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from dojo.authorization.roles_permissions import Permissions
from dojo.models import Engagement, Finding, Product, Test

from aist import queries
from aist.models import (
    AISTLaunchConfigAction,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    DastProjectBinding,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    PipelineLaunchRequest,
    WorkItemProvider,
)

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Action(Enum):

    """The named capability ladder. Every read/write maps to exactly one here."""

    # Read tiers (all *_View permissions are Reader-level; resource-specific so the
    # permission passed to the getter matches the object being read).
    PRODUCT_READ = "product_read"
    FINDING_READ = "finding_read"
    TEST_READ = "test_read"
    ENGAGEMENT_READ = "engagement_read"
    ORG_READ = "org_read"
    ORG_MANAGE_READ = "org_manage_read"

    # Write ladder.
    FINDING_EDIT = "finding_edit"        # T1  Writer+
    RISK_ACCEPT = "risk_accept"          # T1  Writer+ (own permission)
    PROJECT_OPERATE = "project_operate"  # T2  Maintainer+ (Product_Edit)
    PROJECT_CREATE = "project_create"    # T2  Maintainer+ (Product_Type_Add_Product)
    ORG_MANAGE = "org_manage"            # T3  Maintainer+ (Manage_Members)
    OWNER_GRANT = "owner_grant"          # T3  Owner (Add_Owner)


# THE single action→permission table. One edit here changes a capability's tier
# everywhere it is used.
ACTION_PERMISSIONS: dict[Action, int] = {
    Action.PRODUCT_READ: Permissions.Product_View,
    Action.FINDING_READ: Permissions.Finding_View,
    Action.TEST_READ: Permissions.Test_View,
    Action.ENGAGEMENT_READ: Permissions.Engagement_View,
    Action.ORG_READ: Permissions.Product_Type_View,
    Action.ORG_MANAGE_READ: Permissions.Product_Type_Manage_Members,
    Action.FINDING_EDIT: Permissions.Finding_Edit,
    Action.RISK_ACCEPT: Permissions.Risk_Acceptance,
    Action.PROJECT_OPERATE: Permissions.Product_Edit,
    Action.PROJECT_CREATE: Permissions.Product_Type_Add_Product,
    Action.ORG_MANAGE: Permissions.Product_Type_Manage_Members,
    Action.OWNER_GRANT: Permissions.Product_Type_Member_Add_Owner,
}


def queryset_for_action(*, resource: type[Model], action: Action, user) -> QuerySet:
    """Return one tenant-scoped resource queryset through the central action map."""
    try:
        getter = RESOURCE_GETTERS[resource]
    except KeyError as exc:
        msg = f"No org-scoped getter registered for {resource.__name__}"
        raise KeyError(msg) from exc
    return getter(ACTION_PERMISSIONS[action], user=user)


# Resource model → the org-scoped queryset getter that enforces tenant isolation.
# Object resolution NEVER bypasses these (see AISTAPIView.resolve).
RESOURCE_GETTERS: dict[type[Model], object] = {
    Product: queries.get_authorized_aist_products,
    AISTProject: queries.get_authorized_aist_projects,
    AISTProjectVersion: queries.get_authorized_aist_project_versions,
    DastProjectBinding: queries.get_authorized_dast_project_bindings,
    AISTPipeline: queries.get_authorized_aist_pipelines,
    AISTProjectLaunchConfig: queries.get_authorized_aist_launch_configs,
    AISTLaunchConfigAction: queries.get_authorized_aist_launch_config_actions,
    LaunchSchedule: queries.get_authorized_aist_launch_schedules,
    PipelineLaunchRequest: queries.get_authorized_aist_launch_requests,
    Finding: queries.get_authorized_findings,
    Test: queries.get_authorized_tests,
    Engagement: queries.get_authorized_engagements,
    Organization: queries.get_authorized_aist_organizations,
    OrgIntegration: queries.get_authorized_org_integrations,
    WorkItemProvider: queries.get_authorized_work_item_providers,
}


@dataclass(frozen=True, slots=True)
class ResourcePolicy:

    """
    An endpoint's authorization contract.

    ``read``/``write`` are ``Action`` values; ``permission_for`` picks read for safe
    HTTP methods and write for mutating ones, then maps through ``ACTION_PERMISSIONS``.
    ``getter`` is resolved from ``RESOURCE_GETTERS[resource]``.
    """

    resource: type[Model]
    read: Action
    write: Action

    def __post_init__(self) -> None:
        if self.resource not in RESOURCE_GETTERS:
            msg = f"No org-scoped getter registered for {self.resource.__name__}"
            raise KeyError(msg)
        for action in (self.read, self.write):
            if action not in ACTION_PERMISSIONS:
                msg = f"Action {action} is not in ACTION_PERMISSIONS"
                raise KeyError(msg)

    @property
    def getter(self):
        return RESOURCE_GETTERS[self.resource]

    def permission_for(self, method: str) -> int:
        action = self.read if (method or "").upper() in _SAFE_METHODS else self.write
        return ACTION_PERMISSIONS[action]

    def queryset_for(self, method: str, user) -> QuerySet:
        action = self.read if (method or "").upper() in _SAFE_METHODS else self.write
        return queryset_for_action(resource=self.resource, action=action, user=user)


class _AuthzMarker:

    """A sanctioned non-resource authorization mode (see ``PUBLIC``/``INTERNAL_SERVICE``)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<authz {self.name}>"


# The ONLY escape hatches. Anything else must be a ResourcePolicy.
PUBLIC = _AuthzMarker("PUBLIC")                       # unauthenticated or self-scoped, no org resource
INTERNAL_SERVICE = _AuthzMarker("INTERNAL_SERVICE")   # superuser service principal only


def is_valid_authz(authz: object) -> bool:
    """True iff ``authz`` is a recognised declaration (policy or sanctioned marker)."""
    return isinstance(authz, ResourcePolicy) or authz in {PUBLIC, INTERNAL_SERVICE}
