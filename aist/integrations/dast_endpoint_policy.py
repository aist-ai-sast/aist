"""Shared DAST destination policy used by onboarding and connector execution."""

from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.dast.endpoint_policy import (  # noqa: E402
    AddressResolver,
    DastEndpointPolicy,
    DastEndpointPolicyError,
    ValidatedDastEndpoint,
)

__all__ = ["AddressResolver", "DastEndpointPolicy", "DastEndpointPolicyError", "ValidatedDastEndpoint"]
