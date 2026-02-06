from __future__ import annotations

from crum import get_current_user
from dojo.product.queries import get_authorized_products

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


def get_authorized_aist_projects(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTProject.objects.none()
    products = get_authorized_products(permission, user=user)
    return AISTProject.objects.filter(product__in=products)


def get_authorized_aist_project_versions(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTProjectVersion.objects.none()
    products = get_authorized_products(permission, user=user)
    return AISTProjectVersion.objects.filter(project__product__in=products)


def get_authorized_aist_pipelines(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTPipeline.objects.none()
    products = get_authorized_products(permission, user=user)
    return AISTPipeline.objects.filter(project__product__in=products)


def get_authorized_aist_launch_configs(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTProjectLaunchConfig.objects.none()
    products = get_authorized_products(permission, user=user)
    return AISTProjectLaunchConfig.objects.filter(project__product__in=products)


def get_authorized_aist_launch_config_actions(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return AISTLaunchConfigAction.objects.none()
    products = get_authorized_products(permission, user=user)
    return AISTLaunchConfigAction.objects.filter(launch_config__project__product__in=products)


def get_authorized_aist_launch_schedules(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return LaunchSchedule.objects.none()
    products = get_authorized_products(permission, user=user)
    return LaunchSchedule.objects.filter(launch_config__project__product__in=products)


def get_authorized_aist_queue_items(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return PipelineLaunchQueue.objects.none()
    products = get_authorized_products(permission, user=user)
    return PipelineLaunchQueue.objects.filter(project__product__in=products)


def get_authorized_aist_organizations(permission, user=None):
    user = _resolve_user(user)
    if user is None:
        return Organization.objects.none()
    products = get_authorized_products(permission, user=user)
    return Organization.objects.filter(projects__product__in=products).distinct()
