"""The environment a SAST builder receives from AIST, and the rules its variable names follow."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import SecretStr

from aist.integrations.resolver import resolve_integration
from aist.models import OrgIntegrationType
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.build_configuration import BUILDER_RESERVED_ENV  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aist.models import AISTProject, OrgIntegration

# Builder variables the platform computes for every run. REPO_URL embeds the SCM integration
# credential, so neither key may come from a caller or be persisted with a launch.
SYSTEM_ENV_KEYS = frozenset({"REPO_URL", "PROJECT_NAME"})

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class BuildEnvNameViolation(Enum):
    INVALID = "is not a valid environment variable name"
    SYSTEM = "is set by the platform"
    BUILDER = "is reserved by the builder"


def build_env_name_violation(name: str) -> BuildEnvNameViolation | None:
    """Why ``name`` cannot name a build-environment integration variable, or None when it can."""
    if not _ENV_NAME.fullmatch(name):
        return BuildEnvNameViolation.INVALID
    if name in SYSTEM_ENV_KEYS:
        return BuildEnvNameViolation.SYSTEM
    if name in BUILDER_RESERVED_ENV:
        return BuildEnvNameViolation.BUILDER
    return None


def probe_build_env(integration: OrgIntegration) -> tuple[bool, str]:
    """A build environment has no remote counterpart to check its variables against."""
    return True, "Build environment variables are used as stored and have no remote check."


class BuildEnvConflictError(ValueError):

    """A launch environment key is also a variable of the project's build-environment integration."""


@dataclass(frozen=True, slots=True)
class BuildEnvironment:

    """The variables of the build-environment integration a project resolves to; empty when it has none."""

    integration_name: str = ""
    variables: Mapping[str, SecretStr] = field(default_factory=dict)

    def check_launch_env(self, launch_env: Mapping[str, object]) -> None:
        """Raise :class:`BuildEnvConflictError` naming the first key of ``launch_env`` this environment defines."""
        conflicts = sorted(self.variables.keys() & launch_env.keys())
        if conflicts:
            msg = f'env: {conflicts[0]} is provided by build environment "{self.integration_name}"'
            raise BuildEnvConflictError(msg)

    def secret_values(self) -> dict[str, str]:
        """The variables unwrapped for the builder container."""
        return {name: value.get_secret_value() for name, value in self.variables.items()}


def build_environment(project: AISTProject) -> BuildEnvironment:
    """The build environment ``project`` resolves to through its integration override or organization default."""
    resolved = resolve_integration(project, OrgIntegrationType.BUILD_ENV)
    if resolved is None:
        return BuildEnvironment()
    integration = resolved.integration
    return BuildEnvironment(
        integration_name=integration.name,
        variables={row.name: SecretStr(row.value) for row in integration.build_env_vars.all()},
    )
