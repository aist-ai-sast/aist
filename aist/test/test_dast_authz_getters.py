from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from dojo.authorization.roles_permissions import Permissions, Roles
from dojo.models import (
    Product,
    Product_Member,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
)

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTProject,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    OrgMemberAccessScope,
)
from aist.queries import get_authorized_dast_project_bindings
from aist.services.dast_targets import refresh_dast_targets
from aist.test.test_dast_target_models import _integration_config, _target_wire


class DastBindingAuthorizationGetterTests(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="DAST authz SLA")
        self.reader_role, _ = Role.objects.get_or_create(
            id=Roles.Reader,
            defaults={"name": "Reader"},
        )
        self.organization_a, projects_a = self._organization_with_projects("A", ("app", "worker"))
        self.organization_b, projects_b = self._organization_with_projects("B", ("foreign",))
        self.project_app, self.project_worker = projects_a
        self.project_foreign = projects_b[0]
        self.binding_app, self.binding_worker = self._bindings(
            self.organization_a,
            ((self.project_app, "app"), (self.project_worker, "worker")),
        )
        self.binding_foreign = self._bindings(
            self.organization_b,
            ((self.project_foreign, "foreign"),),
        )[0]

    def _organization_with_projects(self, prefix, project_names):
        product_type = Product_Type.objects.create(name=f"DAST authz {prefix} PT")
        organization = Organization.objects.create(name=f"DAST authz {prefix} org", product_type=product_type)
        projects = []
        for project_name in project_names:
            product = Product.objects.create(
                name=f"DAST authz {prefix} {project_name}",
                description="",
                prod_type=product_type,
                sla_configuration=self.sla,
            )
            projects.append(AISTProject.objects.create(product=product))
        return organization, projects

    @staticmethod
    def _bindings(organization, project_keys):
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name=f"DAST {organization.id}",
            config=_integration_config(f"pub_authz_{organization.id}"),
            is_active=True,
        )
        snapshots = [DastTargetSnapshot.from_snapshot(_target_wire(key)) for _, key in project_keys]
        targets = refresh_dast_targets(integration, snapshots, seen_at=timezone.now())
        target_by_key = {target.provider_id: target for target in targets}
        return [
            DastProjectBinding.objects.create(
                project=project,
                target=target_by_key[key],
                source_repo_key=key,
                parameter_snapshot={"depth": "light"},
            )
            for project, key in project_keys
        ]

    def _user(self, username, *organizations):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="pass",  # noqa: S106
        )
        for organization in organizations:
            Product_Type_Member.objects.create(
                product_type=organization.product_type,
                user=user,
                role=self.reader_role,
            )
        return user

    def test_full_member_cannot_see_other_organization_binding(self):
        user = self._user("binding_org_a", self.organization_a)

        binding_ids = set(
            get_authorized_dast_project_bindings(Permissions.Product_View, user=user)
            .values_list("id", flat=True),
        )
        self.assertEqual(binding_ids, {self.binding_app.id, self.binding_worker.id})
        self.assertNotIn(self.binding_foreign.id, binding_ids)

    def test_restricted_member_sees_only_explicitly_granted_project(self):
        user = self._user("binding_restricted", self.organization_a)
        OrgMemberAccessScope.objects.create(
            organization=self.organization_a,
            user=user,
            restricted=True,
        )
        Product_Member.objects.create(
            product=self.project_app.product,
            user=user,
            role=self.reader_role,
        )

        binding_ids = set(
            get_authorized_dast_project_bindings(Permissions.Product_View, user=user)
            .values_list("id", flat=True),
        )
        self.assertEqual(binding_ids, {self.binding_app.id})

    def test_pat_organization_marker_narrows_multi_org_user(self):
        user = self._user("binding_multi_org", self.organization_a, self.organization_b)
        user._aist_token_organization_id = self.organization_a.id

        binding_ids = set(
            get_authorized_dast_project_bindings(Permissions.Product_View, user=user)
            .values_list("id", flat=True),
        )
        self.assertEqual(binding_ids, {self.binding_app.id, self.binding_worker.id})

    def test_reader_has_no_write_scoped_bindings(self):
        user = self._user("binding_reader", self.organization_a)
        self.assertFalse(
            get_authorized_dast_project_bindings(Permissions.Product_Edit, user=user).exists(),
        )
