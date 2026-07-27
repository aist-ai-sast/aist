from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aist.models import VersionType

if TYPE_CHECKING:
    from aist.models import AISTProjectVersion


_FULL_GIT_HASH = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ERR_REPOSITORY = "DAST trigger requires the binding repository key."
_ERR_BRANCH = "DAST GIT_BRANCH trigger requires a non-blank branch ref."
_ERR_HASH = "DAST GIT_HASH trigger requires a full 40-hex commit."
_ERR_TYPE = "DAST autonomous execution does not support this project version type."


class DastTriggerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DastTrigger:
    project_version_id: int
    repository_key: str
    type: str
    ref: str

    @classmethod
    def from_project_version(
        cls,
        project_version: AISTProjectVersion,
        *,
        repository_key: str,
    ) -> DastTrigger:
        normalized_repository = repository_key.strip() if isinstance(repository_key, str) else ""
        if _REPOSITORY_KEY.fullmatch(normalized_repository) is None:
            raise DastTriggerError(_ERR_REPOSITORY)
        ref = project_version.requested_ref()
        if project_version.version_type == VersionType.GIT_BRANCH:
            if not _valid_git_branch(ref):
                raise DastTriggerError(_ERR_BRANCH)
        elif project_version.version_type == VersionType.GIT_HASH:
            if _FULL_GIT_HASH.fullmatch(ref) is None:
                raise DastTriggerError(_ERR_HASH)
        else:
            raise DastTriggerError(_ERR_TYPE)
        return cls(
            project_version_id=project_version.pk,
            repository_key=normalized_repository,
            type=str(project_version.version_type),
            ref=ref,
        )

    def to_wire(self) -> dict[str, str]:
        return {
            "repository_key": self.repository_key,
            "type": self.type,
            "ref": self.ref,
        }


def _valid_git_branch(value: str) -> bool:
    if not value or len(value) > 255:
        return False
    if value.startswith(("-", ".", "/")) or value.endswith((".", "/", ".lock")):
        return False
    if ".." in value or "@{" in value or "//" in value:
        return False
    return not any(
        character.isspace() or ord(character) < 32 or character in "~^:?*[\\"
        for character in value
    )
