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
    "WorkItemProvider", "ProjectIntegrationOverride", "AISTProjectScript", "WorkItemLink",
)

# Tenant credential/gateway-config surface: must never regain a tenant-authz violation,
# known or otherwise. Enforced separately from KNOWN_TENANT_AUTHZ_CONSTRUCTS below.
_MUST_STAY_CLEAN = frozenset({"dast_targets.py", "org_integrations.py"})

_RAW_REQUEST_DATA = re.compile(
    r"\bdict\(\s*request\.data\s*\)|request\.data\.(?:get|copy)\s*\(|"
    r"request\.data\s*\[|\{\s*\*\*request\.data",
)
_RAW_LOOKUP = re.compile(
    rf"get_object_or_404\(\s*(?:{'|'.join(_ORG_OWNED_MODELS)})\b",
)
# Matches a chained lookup spanning several lines, which is the same raw lookup.
_RAW_TENANT_GET = re.compile(
    rf"\b(?:{'|'.join(_ORG_OWNED_MODELS)})\s*\.\s*objects"
    rf"(?:\s*\.\s*\w+\s*\([^()]*\))*\s*\.\s*get\s*\(",
    re.DOTALL,
)
_RAW_PERMISSION = re.compile(r"\bPermissions\.[A-Za-z_][A-Za-z0-9_]*")
# A literal ``Permissions.`` is easy to rename around, so the import itself is forbidden:
# ``from dojo.authorization.roles_permissions import Permissions as P`` evaded the above.
_PERMISSION_IMPORT = re.compile(
    r"from\s+dojo\.authorization\.roles_permissions\s+import[^\n]*\bPermissions\b",
)
_PUBLIC_AUTHZ = re.compile(r"\bauthz\s*=\s*PUBLIC\b")

# Both constructs are narrowed by a parent the view already resolved, so they are scoped.
KNOWN_FORBIDDEN_CONSTRUCTS = {
    ("projects.py", "get_object_or_404(AISTProjectScript"),
    ("work_items.py", "get_object_or_404(WorkItemLink"),
}

# Deliberate PUBLIC endpoints or remaining direct tenant lookups. Raw Permissions are
# checked separately and can never be allowlisted.
# Per-occurrence baseline, keyed by (file, normalized construct). Each entry is one
# construct established as scoped: a re-lock of an object the view already resolved, a
# lookup narrowed by a resolved parent, the shared non-tenant script singleton, or a
# deliberately public endpoint. Anything not listed fails, in every file.
KNOWN_TENANT_AUTHZ_CONSTRUCTS = {
    # Deliberately public / self-service endpoints.
    ("account.py", "authz=PUBLIC"),
    ("cwe.py", "authz=PUBLIC"),
    ("github_integration.py", "authz=PUBLIC"),
    ("launch_schedules.py", "authz=PUBLIC"),
    ("organizations.py", "authz=PUBLIC"),
    ("tokens.py", "authz=PUBLIC"),
    # Re-lock of an object the view already resolved through the scoped getter.
    ("ai.py", "AISTPipeline.objects.select_for_update().get("),
    ("launch_configs.py", "AISTProject.objects.select_for_update().only().get("),
    ("projects.py", "AISTProject.objects.select_for_update().only().get("),
    ("project_versions.py", "AISTProjectVersion.objects.select_related().get("),
    ("project_versions.py", "AISTProjectVersion.objects.get("),
    # The shared script singleton is not organization-owned; its own guard precedes it.
    ("projects.py", "AISTProjectScript.objects.select_for_update().get("),
}


def _api_python_files():
    return _API_DIR.rglob("*.py")


def _relative_path(path: Path) -> str:
    return path.relative_to(_API_DIR).as_posix()


def _normalize_construct(text: str) -> str:
    """
    Collapse a match to a key that survives reformatting but not a real change.

    Whitespace and the arguments of intermediate chained calls carry no authorization
    meaning, so a line break or a changed ``only("pk")`` must not silently create a new
    unreviewed entry -- nor let one hide.
    """
    collapsed = re.sub(r"\s+", "", text)
    return re.sub(r"\([^()]*\)", "()", collapsed)


def _constructs(patterns) -> set[tuple[str, str]]:
    found = set()
    for path in _api_python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            found.update((_relative_path(path), _normalize_construct(match.group(0))) for match in pattern.finditer(text))
    return found


def _forbidden_constructs() -> set[tuple[str, str]]:
    return _constructs((_RAW_REQUEST_DATA, _RAW_LOOKUP))


def _tenant_authz_constructs() -> set[tuple[str, str]]:
    return _constructs((_RAW_TENANT_GET, _PUBLIC_AUTHZ))


class AuthzLintTests(SimpleTestCase):

    def test_api_uses_named_actions_instead_of_defectdojo_permissions(self):
        """No baseline here: a direct permission reference is never acceptable."""
        offenders = {
            _relative_path(path)
            for path in _api_python_files()
            if _RAW_PERMISSION.search(path.read_text(encoding="utf-8"))
            or _PERMISSION_IMPORT.search(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            offenders,
            set(),
            "aist/api must express authorization through Action values; a direct DefectDojo "
            f"Permissions reference or import remains in: {sorted(offenders)}",
        )

    def test_no_new_forbidden_patterns(self):
        unexpected = _forbidden_constructs() - KNOWN_FORBIDDEN_CONSTRUCTS
        self.assertEqual(
            unexpected, set(),
            "New forbidden authz pattern (raw request.data transformation or get_object_or_404 "
            f"on an org-owned model): {sorted(unexpected)}. Resolve via AISTAPIView + "
            "a serializer.",
        )

    def test_allowlist_has_no_stale_entries(self):
        stale = KNOWN_FORBIDDEN_CONSTRUCTS - _forbidden_constructs()
        self.assertEqual(
            stale, set(),
            f"Gone from the code — remove from KNOWN_FORBIDDEN_CONSTRUCTS: {sorted(stale)}",
        )

    def test_no_new_tenant_authz_bypass_patterns(self):
        unexpected = _tenant_authz_constructs() - KNOWN_TENANT_AUTHZ_CONSTRUCTS
        self.assertEqual(
            unexpected, set(),
            "New use of PUBLIC or a direct .objects.get() on a tenant-owned model: "
            f"{sorted(unexpected)}. Resolve via ResourcePolicy + a named Action. Only add an "
            "entry after reading the construct and establishing it is scoped.",
        )

    def test_tenant_authz_allowlist_has_no_stale_entries(self):
        """A baseline entry must name a construct the code still contains."""
        stale = KNOWN_TENANT_AUTHZ_CONSTRUCTS - _tenant_authz_constructs()
        self.assertEqual(
            stale, set(),
            f"Gone from the code — remove from KNOWN_TENANT_AUTHZ_CONSTRUCTS: {sorted(stale)}",
        )

    def test_dast_tenant_apis_use_only_central_authz_primitives(self):
        offenders = {f for f, _ in _tenant_authz_constructs()} & _MUST_STAY_CLEAN
        self.assertEqual(
            offenders,
            set(),
            "DAST tenant APIs cannot use PUBLIC, raw Permissions.*, or direct .objects.get() "
            f"on tenant-owned models: {sorted(offenders)}",
        )
        self.assertFalse(
            {f for f, _ in KNOWN_TENANT_AUTHZ_CONSTRUCTS} & _MUST_STAY_CLEAN,
            "dast_targets.py/org_integrations.py must never be allowlisted for a tenant-authz "
            "bypass — they hold tenant credentials and gateway configuration.",
        )
