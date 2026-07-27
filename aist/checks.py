"""Django checks for AIST security and execution-runtime invariants."""
from __future__ import annotations

from pathlib import Path

import yaml
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.db import OperationalError, ProgrammingError
from dojo.models import Global_Role

from aist.models import PipelineExecutionLease

AIST_EXECUTION_CHECK_TAG = "aist_execution"


@register(Tags.security)
def forbid_non_superuser_global_roles(app_configs, **kwargs):
    """AIST tenant isolation recognizes only ``is_superuser`` as a global bypass."""
    try:
        forbidden = Global_Role.objects.filter(
            group__isnull=False,
            role__isnull=False,
        ).exists() or Global_Role.objects.filter(
            role__isnull=False,
            user__is_superuser=False,
        ).exists()
    except (OperationalError, ProgrammingError):
        # The vendor table may not exist yet during first-install migration checks.
        return []
    if not forbidden:
        return []
    return [
        Error(
            "AIST forbids DefectDojo global roles for non-superusers and groups.",
            hint="Remove the Global_Role row; grant organization membership instead.",
            id="aist.E001",
        ),
    ]


@register(AIST_EXECUTION_CHECK_TAG, deploy=True)
def validate_execution_runtime(app_configs, **kwargs):
    """Fail deployment when the generic DAST runtime is incomplete or misdeclared."""
    pipeline_root = Path(getattr(settings, "AIST_PIPELINE_CODE_PATH", ""))
    required_paths = (
        pipeline_root / "pipeline" / "execution.py",
        pipeline_root / "pipeline" / "dast" / "executor.py",
        pipeline_root / "Dockerfiles" / "dast_connector" / "Dockerfile",
    )
    missing_paths = [str(path) for path in required_paths if not path.is_file()]
    if missing_paths:
        return [
            Error(
                "AIST generic execution runtime is incomplete.",
                hint=f"Missing required files: {', '.join(missing_paths)}",
                id="aist.E002",
            ),
        ]

    catalog_path = pipeline_root / "pipeline" / "config" / "analyzers.yaml"
    try:
        catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            Error(
                "AIST analyzer catalog cannot be loaded.",
                hint=str(exc),
                id="aist.E003",
            ),
        ]

    analyzers = catalog.get("analyzers", []) if isinstance(catalog, dict) else []
    dast_entries = [entry for entry in analyzers if isinstance(entry, dict) and entry.get("name") == "dast"]
    if len(dast_entries) != 1:
        return [
            Error(
                "The shared analyzer catalog must contain exactly one DAST provider.",
                id="aist.E004",
            ),
        ]
    dast = dast_entries[0]
    if dast.get("execution_type") != "dast" or dast.get("type") != "standalone" or not dast.get("image"):
        return [
            Error(
                "The shared DAST provider must be a standalone generic execution with a connector image.",
                id="aist.E005",
            ),
        ]

    lease_constraints = {constraint.name for constraint in PipelineExecutionLease._meta.constraints}
    if "uniq_active_execution_lease_slot" not in lease_constraints:
        return [
            Error(
                "The generic execution lease uniqueness constraint is missing.",
                id="aist.E006",
            ),
        ]
    return []
