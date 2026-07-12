"""Shared ranking of DefectDojo roles used across AIST membership code."""
from __future__ import annotations

from dojo.authorization.roles_permissions import Roles

# Higher rank = more privileged. Used to pick the "best" role when a user reaches
# an organization through several grant sources.
ROLE_RANK: dict[int, int] = {
    Roles.Reader.value: 0,
    Roles.API_Importer.value: 1,
    Roles.Writer.value: 2,
    Roles.Maintainer.value: 3,
    Roles.Owner.value: 4,
}


def role_rank(role_id: int | None) -> int:
    return ROLE_RANK.get(role_id, -1)
