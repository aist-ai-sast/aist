"""
Central AIST authorization layer.

Import the public surface from here:

    from aist.authz import AISTAPIView, ResourcePolicy, Action, PUBLIC, INTERNAL_SERVICE
"""
from __future__ import annotations

from aist.authz.base import AISTAPIView, AISTAuthzMixin
from aist.authz.permissions import IsInternalService
from aist.authz.policy import (
    ACTION_PERMISSIONS,
    INTERNAL_SERVICE,
    PUBLIC,
    RESOURCE_GETTERS,
    Action,
    ResourcePolicy,
    is_valid_authz,
)

__all__ = [
    "ACTION_PERMISSIONS",
    "INTERNAL_SERVICE",
    "PUBLIC",
    "RESOURCE_GETTERS",
    "AISTAPIView",
    "AISTAuthzMixin",
    "Action",
    "IsInternalService",
    "ResourcePolicy",
    "is_valid_authz",
]
