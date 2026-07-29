"""
Lint gate — forbid the patterns that let an AIST endpoint dodge the central layer.

Forbidden forms in ``aist/api/*.py``:

1. Transforming or indexing ``request.data`` directly — raw request body not run through a serializer
   (AGENTS.md "no request.data accessed directly in views"). This is exactly the
   shape behind the unvalidated AI-callback payload (G-5).
2. ``get_object_or_404(<OrgOwnedModel>, ...)`` — object resolution that bypasses the
   tenant-scoped ``aist.queries`` getters (the root pattern behind G-3). Object
   lookups must go through ``AISTAPIView.resolve``.
3. Direct ``<OrgOwnedModel>.objects.get(...)`` and raw ``Permissions.*`` references,
   anywhere in ``aist/api/*.py`` (originally checked only in the DAST tenant modules;
   widened per H15 — the same bypass is just as real in every other endpoint). Object
   resolution must go through ``ResourcePolicy`` and named ``Action`` values from the
   central authz layer.
4. ``authz = PUBLIC``, anywhere in ``aist/api/*.py``.

Checks 1-2 keep an empty allowlist — a new hit there is always a fresh regression. Checks
3-4 retain a narrow allowlist for deliberate public/self-service endpoints and remaining
direct tenant lookups. Direct ``Permissions.*`` use has no allowlist and is forbidden for
the entire package. ``dast_targets.py`` and ``org_integrations.py`` must stay fully clean:
they hold tenant credentials and gateway configuration.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

_API_DIR = Path(__file__).resolve().parent.parent / "api"

# Org-owned models that must be resolved through the scoped getters, never raw.
_ORG_OWNED_MODELS = (
    "AISTPipeline", "AISTProject", "AISTProjectVersion", "AISTProjectLaunchConfig",
    "AISTLaunchConfigAction", "DastIntegrationState", "DastProjectBinding", "DastTarget",
    "LaunchSchedule", "PipelineLaunchRequest", "Finding", "OrgIntegration",
    "WorkItemProvider", "ProjectIntegrationOverride",
)

# Tenant credential/gateway-config surface: must never regain a tenant-authz violation,
# known or otherwise. Enforced separately from KNOWN_TENANT_AUTHZ_VIOLATIONS below.
_MUST_STAY_CLEAN = frozenset({"dast_targets.py", "org_integrations.py"})

_RAW_REQUEST_DATA = re.compile(
    r"\bdict\(\s*request\.data\s*\)|request\.data\.(?:get|copy)\s*\(|"
    r"request\.data\s*\[|\{\s*\*\*request\.data",
)
_RAW_LOOKUP = re.compile(
    rf"get_object_or_404\(\s*(?:{'|'.join(_ORG_OWNED_MODELS)})\b",
)
_RAW_TENANT_GET = re.compile(
    rf"\b(?:{'|'.join(_ORG_OWNED_MODELS)})\.objects\.get\s*\(",
)
_RAW_PERMISSION = re.compile(r"\bPermissions\.[A-Za-z_][A-Za-z0-9_]*")
_PUBLIC_AUTHZ = re.compile(r"\bauthz\s*=\s*PUBLIC\b")

KNOWN_VIOLATIONS = set()

# Deliberate PUBLIC endpoints or remaining direct tenant lookups. Raw Permissions are
# checked separately and can never be allowlisted.
KNOWN_TENANT_AUTHZ_VIOLATIONS = {
    "account.py", "cwe.py", "github_integration.py", "launch_schedules.py", "organizations.py",
    "project_versions.py", "tokens.py",
}


def _api_python_files():
    return _API_DIR.rglob("*.py")


def _relative_path(path: Path) -> str:
    return path.relative_to(_API_DIR).as_posix()


def _files_with_violations() -> set[str]:
    offenders = set()
    for path in _api_python_files():
        text = path.read_text(encoding="utf-8")
        if _RAW_REQUEST_DATA.search(text) or _RAW_LOOKUP.search(text):
            offenders.add(_relative_path(path))
    return offenders


def _files_with_tenant_authz_violations() -> set[str]:
    offenders = set()
    for path in _api_python_files():
        text = path.read_text(encoding="utf-8")
        if _RAW_TENANT_GET.search(text) or _RAW_PERMISSION.search(text) or _PUBLIC_AUTHZ.search(text):
            offenders.add(_relative_path(path))
    return offenders


class AuthzLintTests(SimpleTestCase):

    def test_api_uses_named_actions_instead_of_defectdojo_permissions(self):
        offenders = {
            _relative_path(path)
            for path in _api_python_files()
            if _RAW_PERMISSION.search(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            offenders,
            set(),
            "aist/api must express authorization through Action values; direct "
            f"DefectDojo Permissions references remain in: {sorted(offenders)}",
        )

    def test_no_new_forbidden_patterns(self):
        offenders = _files_with_violations()
        unexpected = offenders - KNOWN_VIOLATIONS
        self.assertEqual(
            unexpected, set(),
            "New forbidden authz pattern (raw request.data transformation or get_object_or_404 "
            f"on an org-owned model) in: {sorted(unexpected)}. Resolve via AISTAPIView + "
            "a serializer.",
        )

    def test_allowlist_has_no_stale_entries(self):
        offenders = _files_with_violations()
        stale = KNOWN_VIOLATIONS - offenders
        self.assertEqual(
            stale, set(),
            f"These files are clean now — remove them from KNOWN_VIOLATIONS: {sorted(stale)}",
        )

    def test_no_new_tenant_authz_bypass_patterns(self):
        offenders = _files_with_tenant_authz_violations()
        unexpected = offenders - KNOWN_TENANT_AUTHZ_VIOLATIONS
        self.assertEqual(
            unexpected, set(),
            "New use of PUBLIC, raw Permissions.*, or direct .objects.get() on a tenant-owned "
            f"model in: {sorted(unexpected)}. Resolve via ResourcePolicy + a named Action, or "
            "add to KNOWN_TENANT_AUTHZ_VIOLATIONS if this is deliberately public/self-service.",
        )

    def test_tenant_authz_allowlist_has_no_stale_entries(self):
        offenders = _files_with_tenant_authz_violations()
        stale = KNOWN_TENANT_AUTHZ_VIOLATIONS - offenders
        self.assertEqual(
            stale, set(),
            "These files are clean now — remove them from KNOWN_TENANT_AUTHZ_VIOLATIONS: "
            f"{sorted(stale)}",
        )

    def test_dast_tenant_apis_use_only_central_authz_primitives(self):
        offenders = _files_with_tenant_authz_violations() & _MUST_STAY_CLEAN
        self.assertEqual(
            offenders,
            set(),
            "DAST tenant APIs cannot use PUBLIC, raw Permissions.*, or direct .objects.get() "
            f"on tenant-owned models: {sorted(offenders)}",
        )
        self.assertFalse(
            KNOWN_TENANT_AUTHZ_VIOLATIONS & _MUST_STAY_CLEAN,
            "dast_targets.py/org_integrations.py must never be allowlisted for a tenant-authz "
            "bypass — they hold tenant credentials and gateway configuration.",
        )
