"""
Unit tests for the central authorization policy tables (aist/authz/policy.py).

These assert the single source of truth is wired correctly — the action→permission
ladder, the resource→getter map, and ResourcePolicy read/write resolution — so a
later accidental edit that, say, drops PROJECT_OPERATE to a read permission fails here.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Engagement, Finding, Product, Test

from aist import queries
from aist.authz.policy import (
    ACTION_PERMISSIONS,
    INTERNAL_SERVICE,
    PUBLIC,
    RESOURCE_GETTERS,
    Action,
    ResourcePolicy,
    is_valid_authz,
)
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    DastProjectBinding,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    PipelineLaunchRequest,
    WorkItemProvider,
)


class ActionPermissionTableTests(SimpleTestCase):

    """Task 1 — the action→permission ladder is exactly the agreed tiers."""

    def test_write_ladder_maps_to_expected_permissions(self):
        self.assertEqual(ACTION_PERMISSIONS[Action.FINDING_EDIT], Permissions.Finding_Edit)
        self.assertEqual(ACTION_PERMISSIONS[Action.RISK_ACCEPT], Permissions.Risk_Acceptance)
        self.assertEqual(ACTION_PERMISSIONS[Action.PROJECT_OPERATE], Permissions.Product_Edit)
        self.assertEqual(ACTION_PERMISSIONS[Action.PROJECT_CREATE], Permissions.Product_Type_Add_Product)
        self.assertEqual(ACTION_PERMISSIONS[Action.ORG_MANAGE], Permissions.Product_Type_Manage_Members)
        self.assertEqual(ACTION_PERMISSIONS[Action.OWNER_GRANT], Permissions.Product_Type_Member_Add_Owner)

    def test_read_tiers_map_to_view_permissions(self):
        self.assertEqual(ACTION_PERMISSIONS[Action.PRODUCT_READ], Permissions.Product_View)
        self.assertEqual(ACTION_PERMISSIONS[Action.FINDING_READ], Permissions.Finding_View)
        self.assertEqual(ACTION_PERMISSIONS[Action.TEST_READ], Permissions.Test_View)
        self.assertEqual(ACTION_PERMISSIONS[Action.ENGAGEMENT_READ], Permissions.Engagement_View)
        self.assertEqual(ACTION_PERMISSIONS[Action.ORG_READ], Permissions.Product_Type_View)

    def test_every_action_has_a_permission(self):
        for action in Action:
            self.assertIn(action, ACTION_PERMISSIONS, f"{action} missing from ACTION_PERMISSIONS")


class ResourceGetterTableTests(SimpleTestCase):

    """Task 1 — every resource resolves through an org-scoped queries.py getter."""

    def test_getters_are_callables_from_queries_module(self):
        for resource, getter in RESOURCE_GETTERS.items():
            self.assertTrue(callable(getter), f"{resource.__name__} getter not callable")
            self.assertIs(
                getattr(queries, getter.__name__, None),
                getter,
                f"{resource.__name__} getter must come from aist.queries",
            )

    def test_expected_resources_are_registered(self):
        for resource in (
            Product, AISTProject, AISTProjectVersion, AISTPipeline, Finding, Test,
            Engagement, LaunchSchedule, Organization, OrgIntegration, WorkItemProvider,
            DastProjectBinding, PipelineLaunchRequest,
        ):
            self.assertIn(resource, RESOURCE_GETTERS)

    def test_pipeline_getter_is_pipeline_getter(self):
        self.assertIs(RESOURCE_GETTERS[AISTPipeline], queries.get_authorized_aist_pipelines)
        self.assertIs(RESOURCE_GETTERS[Finding], queries.get_authorized_findings)
        self.assertIs(
            RESOURCE_GETTERS[DastProjectBinding],
            queries.get_authorized_dast_project_bindings,
        )
        self.assertIs(
            RESOURCE_GETTERS[PipelineLaunchRequest],
            queries.get_authorized_aist_launch_requests,
        )


class ResourcePolicyTests(SimpleTestCase):

    """Task 2 — ResourcePolicy resolves read vs write by HTTP method."""

    def test_permission_for_reads_and_writes(self):
        pol = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)
        self.assertEqual(pol.permission_for("GET"), Permissions.Product_View)
        self.assertEqual(pol.permission_for("HEAD"), Permissions.Product_View)
        self.assertEqual(pol.permission_for("OPTIONS"), Permissions.Product_View)
        self.assertEqual(pol.permission_for("POST"), Permissions.Product_Edit)
        self.assertEqual(pol.permission_for("PATCH"), Permissions.Product_Edit)
        self.assertEqual(pol.permission_for("DELETE"), Permissions.Product_Edit)

    def test_finding_edit_policy(self):
        pol = ResourcePolicy(resource=Finding, read=Action.FINDING_READ, write=Action.FINDING_EDIT)
        self.assertEqual(pol.permission_for("GET"), Permissions.Finding_View)
        self.assertEqual(pol.permission_for("PATCH"), Permissions.Finding_Edit)

    def test_getter_resolves_from_resource(self):
        pol = ResourcePolicy(resource=Finding, read=Action.FINDING_READ, write=Action.FINDING_EDIT)
        self.assertIs(pol.getter, queries.get_authorized_findings)

    def test_unknown_resource_rejected(self):
        with self.assertRaises(KeyError):
            ResourcePolicy(resource=SimpleTestCase, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)


class AuthzMarkerTests(SimpleTestCase):

    """Task 2 — the two escape hatches are distinct, recognised markers."""

    def test_markers_are_distinct(self):
        self.assertIsNot(PUBLIC, INTERNAL_SERVICE)

    def test_is_valid_authz(self):
        self.assertTrue(is_valid_authz(PUBLIC))
        self.assertTrue(is_valid_authz(INTERNAL_SERVICE))
        self.assertTrue(is_valid_authz(
            ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE),
        ))
        self.assertFalse(is_valid_authz(None))
        self.assertFalse(is_valid_authz("write"))
        self.assertFalse(is_valid_authz(object()))
