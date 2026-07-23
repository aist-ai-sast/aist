"""Django security checks for authorization invariants."""
from __future__ import annotations

from django.core.checks import Error, Tags, register
from django.db import OperationalError, ProgrammingError
from dojo.models import Global_Role


@register(Tags.security)
def forbid_non_superuser_global_roles(app_configs, **kwargs):
    """AIST tenant isolation recognizes only ``is_superuser`` as a global bypass."""
    try:
        forbidden = Global_Role.objects.filter(group__isnull=False).exists() or Global_Role.objects.filter(
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
