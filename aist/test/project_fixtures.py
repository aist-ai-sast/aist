"""Project and source-version fixtures for build-configuration scenarios."""

from dojo.models import Product

from aist.models import AISTProject, AISTProjectVersion, VersionType


def create_project(*, name, product_type, sla) -> AISTProject:
    product = Product.objects.create(name=name, description="d", prod_type=product_type, sla_configuration=sla)
    return AISTProject.objects.create(product=product, supported_languages=[], compilable=False, profile={})


def create_project_version(*, project, name, version_type=VersionType.GIT_BRANCH, **extra) -> AISTProjectVersion:
    return AISTProjectVersion.objects.create(project=project, version=name, version_type=version_type, **extra)
