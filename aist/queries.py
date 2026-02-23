from __future__ import annotations

from crum import get_current_user
from django.db.models import Q, Subquery
from dojo.authorization.authorization import get_roles_for_permission, user_has_global_permission
from dojo.models import Finding, Product, Product_Type_Group, Product_Type_Member

from aist.models import (
    AISTLaunchConfigAction,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    PipelineLaunchQueue,
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
    authorized_product_type_roles = Product_Type_Member.objects.filter(
        user=user,
        role__in=roles,
    ).values("product_type_id")
    authorized_product_type_groups = Product_Type_Group.objects.filter(
        group__users=user,
        role__in=roles,
    ).values("product_type_id")

    return Product.objects.filter(
        Q(prod_type_id__in=Subquery(authorized_product_type_roles))
        | Q(prod_type_id__in=Subquery(authorized_product_type_groups)),
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

    orgs_by_product_type = Organization.objects.filter(
        Q(product_type_id__in=Subquery(authorized_product_type_roles))
        | Q(product_type_id__in=Subquery(authorized_product_type_groups)),
    ).distinct()
    products = get_authorized_aist_products(permission, user=user)
    orgs_by_projects = Organization.objects.filter(projects__product__in=products).distinct()
    return (orgs_by_product_type | orgs_by_projects).distinct()
