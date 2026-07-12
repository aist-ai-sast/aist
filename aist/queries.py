from __future__ import annotations

from crum import get_current_user
from django.db.models import Q, Subquery
from dojo.authorization.authorization import get_roles_for_permission, user_has_global_permission
from dojo.authorization.roles_permissions import Permissions
from dojo.models import (
    Finding,
    Product,
    Product_Member,
    Product_Type_Group,
    Product_Type_Member,
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
    PipelineLaunchQueue,
    WorkItemProvider,
)


def _resolve_user(user):
    return user or get_current_user()


def get_authorized_aist_products(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Product.objects.none()
    if user.is_superuser or user_has_global_permission(user, permission):
        return Product.objects.all()

    roles = get_roles_for_permission(permission)

    # Access model: ORG MEMBERSHIP IS REQUIRED, per-project grants only NARROW.
    #
    # 1. Membership in a product type (org) is the precondition for ANY access.
    #    A Product_Member without a Product_Type_Member grants NOTHING — a user is
    #    never "outside every org but inside a project".
    # 2. Within an org the user belongs to, a per-project Product_Member NARROWS
    #    access: if the user has any grant in that org they see ONLY the granted
    #    products (at the granted role); otherwise they see all the org's products
    #    at their org-wide role.
    member_type_ids = set(
        Product_Type_Member.objects.filter(user=user).values_list("product_type_id", flat=True),
    ) | set(
        Product_Type_Group.objects.filter(group__users=user).values_list("product_type_id", flat=True),
    )
    if not member_type_ids:
        return Product.objects.none()

    # Orgs where the user has per-project grants → access is narrowed to those.
    restricted_type_ids = set(
        Product_Member.objects.filter(
            user=user, product__prod_type_id__in=member_type_ids,
        ).values_list("product__prod_type_id", flat=True),
    )

    # Narrowed orgs: only the specifically granted products, at a qualifying role.
    granted_product_ids = Product_Member.objects.filter(
        user=user, role__in=roles, product__prod_type_id__in=restricted_type_ids,
    ).values("product_id")

    # Non-narrowed orgs: every product, but only if the org-wide role qualifies
    # for the requested permission.
    unrestricted_type_ids = member_type_ids - restricted_type_ids
    qualifying_type_ids = set(
        Product_Type_Member.objects.filter(
            user=user, role__in=roles, product_type_id__in=unrestricted_type_ids,
        ).values_list("product_type_id", flat=True),
    ) | set(
        Product_Type_Group.objects.filter(
            group__users=user, role__in=roles, product_type_id__in=unrestricted_type_ids,
        ).values_list("product_type_id", flat=True),
    )

    return Product.objects.filter(
        Q(prod_type_id__in=qualifying_type_ids) | Q(pk__in=Subquery(granted_product_ids)),
    ).distinct()


def get_authorized_findings(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Finding.objects.none()
    products = get_authorized_aist_products(permission, user=user)
    return Finding.objects.filter(test__engagement__product__in=products).order_by("id")


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
