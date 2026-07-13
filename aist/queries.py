from __future__ import annotations

from crum import get_current_user
from django.db.models import Q, Subquery
from dojo.authorization.authorization import get_roles_for_permission, user_has_global_permission
from dojo.authorization.roles_permissions import Permissions
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Member,
    Product_Type_Group,
    Product_Type_Member,
    Test,
)

from aist.models import (
    AISTLaunchConfigAction,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    OrgMemberAccessScope,
    PipelineLaunchQueue,
    ProjectAccessDenial,
    WorkItemProvider,
)
from aist.roles import role_rank


def _resolve_user(user):
    return user or get_current_user()


def get_restricted_organization_ids(user, product_type_ids) -> set[int]:
    """
    THE single source of truth for "is this member restricted (narrowed to
    only their Product_Member grants) in this org, or full (sees every
    project at their org-wide role, except explicitly denied/downgraded
    projects — see ``get_authorized_aist_products``)".

    Purely the persisted ``OrgMemberAccessScope.restricted`` flag — set
    explicitly by ``OrganizationMembershipService`` on restricted invite or
    cleared by ``reset_to_full_access``. Grant/revoke on individual projects
    never touch it. A ``Product_Member`` row's mere existence does NOT imply
    restricted any more: a "full" member can legitimately have one (a
    per-project downgrade override, capped at their org role — see
    ``OrganizationMembershipService._grant_project``). Never re-derive
    "restricted" any other way.
    """
    if not product_type_ids:
        return set()
    return set(
        OrgMemberAccessScope.objects.filter(
            user=user,
            restricted=True,
            organization__product_type_id__in=product_type_ids,
        ).values_list("organization__product_type_id", flat=True),
    )


def get_authorized_aist_products(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Product.objects.none()
    if user.is_superuser or user_has_global_permission(user, permission):
        return Product.objects.all()

    roles = get_roles_for_permission(permission)

    # Access model: ORG MEMBERSHIP IS REQUIRED; per-project overrides adjust
    # ONE product at a time and never affect any other product.
    #
    # 1. Membership in a product type (org) is the precondition for ANY access.
    #    A Product_Member without a Product_Type_Member grants NOTHING — a user is
    #    never "outside every org but inside a project".
    # 2. Restricted orgs (``get_restricted_organization_ids``): access is
    #    exactly the user's qualifying Product_Member grants there — including
    #    zero of them. No cap — grants are their only real access.
    # 3. Non-restricted (full) orgs: every product at the org-wide role,
    #    EXCEPT products with an explicit per-project override — a
    #    ``ProjectAccessDenial`` (always excludes), or a ``Product_Member``
    #    override whose role does not exceed the user's own org-wide role
    #    there. A Product_Member row that DOES exceed it is treated as if it
    #    didn't exist (falls back to plain org-role visibility) rather than
    #    granting the elevated role — this re-checks
    #    ``OrganizationMembershipService._grant_project``'s write-time cap at
    #    READ time too, so a Product_Member row created by any other path
    #    (e.g. vendor's own product-member admin API, which doesn't know
    #    about this cap) can never grant more than the org role would.
    member_type_ids = set(
        Product_Type_Member.objects.filter(user=user).values_list("product_type_id", flat=True),
    ) | set(
        Product_Type_Group.objects.filter(group__users=user).values_list("product_type_id", flat=True),
    )
    if not member_type_ids:
        return Product.objects.none()

    restricted_type_ids = get_restricted_organization_ids(user, member_type_ids)
    unrestricted_type_ids = member_type_ids - restricted_type_ids

    denied_product_ids = set(
        ProjectAccessDenial.objects.filter(
            user=user, project__product__prod_type_id__in=member_type_ids,
        ).values_list("project__product_id", flat=True),
    )

    # Restricted orgs: any qualifying-role grant is the sole source of
    # access, uncapped.
    restricted_qualifying_ids = Product_Member.objects.filter(
        user=user, role__in=roles, product__prod_type_id__in=restricted_type_ids,
    ).values("product_id")

    # Full orgs: a Product_Member override only counts (for inclusion, and
    # for suppressing the org-role fallback below) if its role rank does not
    # exceed the user's org-wide role rank in that same org.
    org_role_rank_by_type = {
        row["product_type_id"]: role_rank(row["role_id"])
        for row in Product_Type_Member.objects.filter(
            user=user, product_type_id__in=unrestricted_type_ids,
        ).values("product_type_id", "role_id")
    }
    full_org_overrides = list(
        Product_Member.objects.filter(
            user=user, product__prod_type_id__in=unrestricted_type_ids,
        ).values_list("product_id", "product__prod_type_id", "role_id"),
    )
    capped_override_product_ids = {
        product_id
        for product_id, prod_type_id, role_id in full_org_overrides
        if role_rank(role_id) <= org_role_rank_by_type.get(prod_type_id, -1)
    }
    capped_qualifying_product_ids = {
        product_id
        for product_id, _prod_type_id, role_id in full_org_overrides
        if product_id in capped_override_product_ids and role_id in roles
    }

    # Full orgs: every product at the qualifying org-wide role, except ones
    # with a conforming per-project override (handled above instead) —
    # denied products are excluded uniformly below, and a non-conforming
    # (rogue) override is deliberately NOT excluded here, so the product
    # falls back to this plain org-role visibility instead of the rogue role.
    qualifying_type_ids = set(
        Product_Type_Member.objects.filter(
            user=user, role__in=roles, product_type_id__in=unrestricted_type_ids,
        ).values_list("product_type_id", flat=True),
    ) | set(
        Product_Type_Group.objects.filter(
            group__users=user, role__in=roles, product_type_id__in=unrestricted_type_ids,
        ).values_list("product_type_id", flat=True),
    )
    org_role_product_ids = Product.objects.filter(
        prod_type_id__in=qualifying_type_ids,
    ).exclude(pk__in=capped_override_product_ids).values("pk")

    return Product.objects.filter(
        Q(pk__in=Subquery(org_role_product_ids))
        | Q(pk__in=Subquery(restricted_qualifying_ids))
        | Q(pk__in=capped_qualifying_product_ids),
    ).exclude(pk__in=denied_product_ids).distinct()


def get_authorized_findings(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Finding.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return Finding.objects.filter(test__engagement__product__in=products).order_by("id")


def get_authorized_tests(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Test.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return Test.objects.filter(engagement__product__in=products)


def get_authorized_engagements(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Engagement.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return Engagement.objects.filter(product__in=products)


def get_authorized_aist_projects(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTProject.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return AISTProject.objects.filter(product__in=products)


def get_authorized_aist_project_versions(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTProjectVersion.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return AISTProjectVersion.objects.filter(project__product__in=products)


def get_authorized_aist_pipelines(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTPipeline.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return AISTPipeline.objects.filter(project__product__in=products)


def get_authorized_aist_launch_configs(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTProjectLaunchConfig.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return AISTProjectLaunchConfig.objects.filter(project__product__in=products)


def get_authorized_aist_launch_config_actions(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTLaunchConfigAction.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return AISTLaunchConfigAction.objects.filter(launch_config__project__product__in=products)


def get_authorized_aist_launch_schedules(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return LaunchSchedule.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return LaunchSchedule.objects.filter(launch_config__project__product__in=products)


def get_authorized_aist_queue_items(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return PipelineLaunchQueue.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return PipelineLaunchQueue.objects.filter(project__product__in=products)


def get_authorized_aist_organizations(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Organization.objects.none()
    if user.is_superuser or user_has_global_permission(user, permission):
        return Organization.objects.all()

    roles = get_roles_for_permission(permission)
    authorized_product_type_roles = Product_Type_Member.objects.filter(
        user=user,
        role__in=roles,
    ).values("product_type_id")
    authorized_product_type_groups = Product_Type_Group.objects.filter(
        group__users=user,
        role__in=roles,
    ).values("product_type_id")

    return Organization.objects.filter(
        Q(product_type_id__in=Subquery(authorized_product_type_roles))
        | Q(product_type_id__in=Subquery(authorized_product_type_groups)),
    ).distinct()


def get_visible_aist_organizations(user=None):
    """
    Organizations a user can see in navigation / membership listings.

    An org is visible when the user has any authorized product in it (which
    already requires org membership — see ``get_authorized_aist_products``). This
    covers both full members and restricted members (narrowed to some projects).

    This getter is for VISIBILITY ONLY (nav, org switcher, self membership
    list). It MUST NOT be used to gate management actions — those keep using
    ``get_authorized_aist_organizations(Permissions.Product_Type_Manage_Members, ...)``.
    """
    user = _resolve_user(user)
    if user is None:
        return Organization.objects.none()
    if user.is_superuser or user_has_global_permission(user, Permissions.Product_View):
        return Organization.objects.all()

    products = get_authorized_aist_products(Permissions.Product_View, user=user)
    return Organization.objects.filter(
        product_type_id__in=Subquery(products.values("prod_type_id")),
    ).distinct()


def get_authorized_work_item_providers(permission, user=None):
    """Return WorkItemProviders whose organization the user can access."""
    user = _resolve_user(user)
    if user is None:
        return WorkItemProvider.objects.none()
    orgs = get_authorized_aist_organizations(permission, user=user)
    return WorkItemProvider.objects.filter(organization__in=orgs)


def get_authorized_org_integrations(permission, user=None):
    """Return OrgIntegrations whose organization the user can access."""
    user = _resolve_user(user)
    if user is None:
        return OrgIntegration.objects.none()
    orgs = get_authorized_aist_organizations(permission, user=user)
    return OrgIntegration.objects.filter(organization__in=orgs)
