from unittest.mock import patch

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type, Product_Type_Member, Role
from rest_framework.test import APIClient

from aist.models import (
    AISTApiToken,
    ApiTokenScope,
    DastIntegrationValidationState,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)
from aist.test.test_api import AISTApiBase

TOKEN = "pub_aist.secretvaluevaluevalue"  # noqa: S105
ROTATED_TOKEN = "pub_aist.rotatedsecretvaluevalue"  # noqa: S105


def _bundle(**overrides):
    payload = {
        "bundle_version": 1,
        "gateway_url": "https://gateway.example",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": "pub_aist",
        "server_fingerprint": "sha256:server-fingerprint",
        "token": TOKEN,
    }
    payload.update(overrides)
    return payload


class DastOnboardingAPITests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(name="DAST API org", product_type=self.prod_type)
        self.import_url = reverse(
            "aist_api:organization_dast_integration_import",
            kwargs={"org_id": self.organization.pk},
        )

    def _import(self, **overrides):
        payload = {"name": "Primary DAST", "bundle": _bundle()}
        payload.update(overrides)
        return self.client.post(self.import_url, payload, format="json")

    def test_maintainer_imports_reads_updates_rotates_and_disables_without_secret_disclosure(self):
        imported = self._import()

        self.assertEqual(imported.status_code, 201)
        integration = OrgIntegration.objects.get(pk=imported.data["id"])
        self.assertEqual(integration.secret, TOKEN)
        self.assertNotIn("token", imported.data)
        self.assertNotIn(TOKEN, str(imported.data))
        self.assertEqual(
            imported.data["dast_state"]["validation_state"],
            DastIntegrationValidationState.VALIDATING,
        )

        detail_url = reverse(
            "aist_api:dast_integration_onboarding_detail",
            kwargs={"integration_id": integration.pk},
        )
        # A rename carries no bundle: the stored token cannot be read back, so demanding one would
        # mean re-exporting a bundle from the gateway just to change a label. The connection behind
        # the integration has to survive that untouched.
        renamed = self.client.patch(detail_url, {"name": "Renamed DAST"}, format="json")
        self.assertEqual(renamed.status_code, 200, renamed.data)
        integration.refresh_from_db()
        self.assertEqual(integration.name, "Renamed DAST")
        self.assertEqual(integration.secret, TOKEN)
        self.assertEqual(integration.config["gateway_url"], "https://gateway.example")

        unknown_field = self.client.patch(detail_url, {"is_active": False}, format="json")
        self.assertEqual(unknown_field.status_code, 400)
        integration.refresh_from_db()
        self.assertTrue(integration.is_active)

        updated = self.client.patch(
            detail_url,
            {"bundle": _bundle(gateway_url="https://new-gateway.example", token=ROTATED_TOKEN)},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        integration.refresh_from_db()
        self.assertEqual(integration.config["gateway_url"], "https://new-gateway.example")
        self.assertEqual(integration.secret, ROTATED_TOKEN)
        self.assertNotIn(ROTATED_TOKEN, str(updated.data))

        rotate_url = reverse(
            "aist_api:dast_integration_rotate_token",
            kwargs={"integration_id": integration.pk},
        )
        rotated_again = f"{ROTATED_TOKEN}-again"
        unknown_rotation = self.client.post(
            rotate_url,
            {"token": rotated_again, "integration_id": -1},
            format="json",
        )
        self.assertEqual(unknown_rotation.status_code, 400)
        integration.refresh_from_db()
        self.assertEqual(integration.secret, ROTATED_TOKEN)

        rotated = self.client.post(rotate_url, {"token": rotated_again}, format="json")
        self.assertEqual(rotated.status_code, 200)
        self.assertNotIn(rotated_again, str(rotated.data))

        disable_url = reverse(
            "aist_api:dast_integration_disable",
            kwargs={"integration_id": integration.pk},
        )
        disabled = self.client.post(disable_url, format="json")
        self.assertEqual(disabled.status_code, 200)
        integration.refresh_from_db()
        self.assertFalse(integration.is_active)

    @patch("aist.integrations.dast_validation.current_app.send_task")
    def test_importing_a_bundle_starts_its_validation(self, send_task):
        """
        Import used to only mark the integration as awaiting validation, leaving the operator to
        find a separate button. Skipping it left the integration stuck with no targets and no
        indication why, so storing a usable connection now starts the check itself.
        """
        with self.captureOnCommitCallbacks(execute=True):
            imported = self._import()

        self.assertEqual(imported.status_code, 201)
        integration = OrgIntegration.objects.get(pk=imported.data["id"])
        send_task.assert_called_once()
        self.assertEqual(send_task.call_args.args[0], "aist.tasks.validate.validate_dast_integration")
        self.assertEqual(send_task.call_args.kwargs["args"][0], integration.pk)
        self.assertEqual(
            integration.dast_state.validation_state,
            DastIntegrationValidationState.VALIDATING,
        )

    @patch("aist.integrations.dast_validation.current_app.send_task")
    def test_replacing_a_bundle_revalidates_the_new_connection(self, send_task):
        with self.captureOnCommitCallbacks(execute=True):
            integration_id = self._import().data["id"]
        send_task.reset_mock()
        detail_url = reverse(
            "aist_api:dast_integration_onboarding_detail",
            kwargs={"integration_id": integration_id},
        )

        with self.captureOnCommitCallbacks(execute=True):
            replaced = self.client.patch(
                detail_url,
                {"bundle": _bundle(gateway_url="https://replacement.example")},
                format="json",
            )

        self.assertEqual(replaced.status_code, 200, replaced.data)
        send_task.assert_called_once()

    @patch("aist.integrations.dast_validation.current_app.send_task")
    def test_renaming_keeps_the_validated_connection_and_does_not_reprobe(self, send_task):
        """
        A rename does not change what a probe would reach. Revalidating anyway would drop a READY
        integration back to VALIDATING -- and spend a probe against the tenant's gateway -- to
        rediscover the state it already had.
        """
        with self.captureOnCommitCallbacks(execute=True):
            integration_id = self._import().data["id"]
        integration = OrgIntegration.objects.get(pk=integration_id)
        integration.dast_state.validation_state = DastIntegrationValidationState.READY
        integration.dast_state.save(update_fields=["validation_state"])
        send_task.reset_mock()
        detail_url = reverse(
            "aist_api:dast_integration_onboarding_detail",
            kwargs={"integration_id": integration_id},
        )

        with self.captureOnCommitCallbacks(execute=True):
            renamed = self.client.patch(detail_url, {"name": "Renamed only"}, format="json")

        self.assertEqual(renamed.status_code, 200, renamed.data)
        send_task.assert_not_called()
        integration.refresh_from_db()
        integration.dast_state.refresh_from_db()
        self.assertEqual(integration.name, "Renamed only")
        self.assertEqual(
            integration.dast_state.validation_state,
            DastIntegrationValidationState.READY,
        )

    @patch("aist.integrations.dast_validation.current_app.send_task")
    def test_moving_to_another_vpn_route_revalidates_without_a_new_bundle(self, send_task):
        """A different egress route can reach a different host, so the connection must be re-probed."""
        with self.captureOnCommitCallbacks(execute=True):
            integration_id = self._import().data["id"]
        vpn = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.VPN,
            name="Route B",
            is_active=True,
        )
        send_task.reset_mock()
        detail_url = reverse(
            "aist_api:dast_integration_onboarding_detail",
            kwargs={"integration_id": integration_id},
        )

        with self.captureOnCommitCallbacks(execute=True):
            rerouted = self.client.patch(detail_url, {"vpn_integration_id": vpn.pk}, format="json")

        self.assertEqual(rerouted.status_code, 200, rerouted.data)
        send_task.assert_called_once()
        integration = OrgIntegration.objects.get(pk=integration_id)
        self.assertEqual(integration.vpn_integration_id, vpn.pk)
        self.assertEqual(integration.secret, TOKEN)

    @patch("aist.integrations.dast_validation.current_app.send_task")
    def test_disabling_an_integration_does_not_start_a_validation(self, send_task):
        """A disabled connection has nothing worth probing, and probing it would be misleading."""
        with self.captureOnCommitCallbacks(execute=True):
            integration_id = self._import().data["id"]
        send_task.reset_mock()
        disable_url = reverse(
            "aist_api:dast_integration_disable",
            kwargs={"integration_id": integration_id},
        )

        with self.captureOnCommitCallbacks(execute=True):
            disabled = self.client.post(disable_url, format="json")

        self.assertEqual(disabled.status_code, 200)
        send_task.assert_not_called()
        self.assertEqual(
            OrgIntegration.objects.get(pk=integration_id).dast_state.validation_state,
            DastIntegrationValidationState.PENDING_VALIDATION,
        )
        integration = OrgIntegration.objects.get(pk=integration_id)
        generations = (
            integration.dast_state.validation_generation,
            integration.dast_state.sync_generation,
        )
        disabled_again = self.client.post(disable_url, format="json")
        self.assertEqual(disabled_again.status_code, 200)
        integration.dast_state.refresh_from_db()
        self.assertEqual(
            (integration.dast_state.validation_generation, integration.dast_state.sync_generation),
            generations,
        )

    def test_delete_requires_disable_and_preserves_onboarding_anti_replay(self):
        imported = self._import()
        integration_id = imported.data["id"]
        delete_url = reverse(
            "aist_api:org_integration_detail",
            kwargs={"integration_id": integration_id},
        )

        active = self.client.delete(delete_url)
        self.assertEqual(active.status_code, 409)
        self.assertEqual(active.data["code"], "INTEGRATION_MUST_BE_DISABLED")

        disable_url = reverse(
            "aist_api:dast_integration_disable",
            kwargs={"integration_id": integration_id},
        )
        self.assertEqual(self.client.post(disable_url, format="json").status_code, 200)
        self.assertEqual(self.client.delete(delete_url).status_code, 204)
        self.assertFalse(OrgIntegration.objects.filter(pk=integration_id).exists())
        self.assertEqual(self._import(name="Replay after teardown").status_code, 409)

    def test_second_active_import_returns_controlled_conflict(self):
        self.assertEqual(self._import().status_code, 201)

        response = self._import(name="Conflicting DAST", bundle=_bundle(integrator_public_id="pub_other"))

        self.assertEqual(response.status_code, 409)

    @patch("aist.integrations.dast_validation.current_app.send_task")
    def test_reimporting_the_same_bundle_in_another_organization_is_a_controlled_conflict(self, send_task):
        """
        Regression for H6/A17: an onboarding bundle identifies one export from the gateway
        role, not a permanent slot on an organization. Once consumed it must not be
        replayable — not even by a fully authorized admin of a different organization.
        """
        with self.captureOnCommitCallbacks(execute=True):
            imported = self._import()
        self.assertEqual(imported.status_code, 201)
        send_task.reset_mock()

        other_product_type = Product_Type.objects.create(name="Other onboarding PT")
        other_organization = Organization.objects.create(name="Other onboarding org", product_type=other_product_type)
        Product_Type_Member.objects.create(
            product_type=other_product_type,
            user=self.user,
            role=self.role_maintainer,
        )
        other_import_url = reverse(
            "aist_api:organization_dast_integration_import",
            kwargs={"org_id": other_organization.pk},
        )

        response = self.client.post(
            other_import_url,
            {"name": "Replayed bundle", "bundle": _bundle()},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(OrgIntegration.objects.filter(organization=other_organization).exists())
        send_task.assert_not_called()

    def test_reimporting_the_same_bundle_after_the_integration_is_disabled_still_conflicts(self):
        imported = self._import()
        self.assertEqual(imported.status_code, 201)
        OrgIntegration.objects.filter(pk=imported.data["id"]).update(is_active=False)

        response = self._import(name="Retry after disable")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            OrgIntegration.objects.filter(organization=self.organization, integration_type=OrgIntegrationType.DAST).count(),
            1,
        )

    def test_import_rejects_server_owned_and_unknown_fields(self):
        response = self._import(
            organization=self.organization.pk,
            created_by=self.user.pk,
            is_active=False,
            legacy_url="https://legacy.invalid",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(OrgIntegration.objects.filter(organization=self.organization).exists())

    def test_cross_org_and_reader_cannot_access_onboarding(self):
        integration_id = self._import().data["id"]
        other_product_type = Product_Type.objects.create(name="Other DAST API PT")
        other_organization = Organization.objects.create(name="Other DAST API org", product_type=other_product_type)
        cross_org_url = reverse(
            "aist_api:organization_dast_integration_import",
            kwargs={"org_id": other_organization.pk},
        )
        self.assertEqual(self.client.get(cross_org_url).status_code, 404)

        foreign_integration = OrgIntegration.objects.create(
            organization=other_organization,
            integration_type=OrgIntegrationType.DAST,
            name="Foreign DAST",
            config={
                "gateway_url": "https://foreign-gateway.example",
                "ca_bundle": "",
                "contract_major": 2,
                "integrator_public_id": "pub_foreign",
                "server_fingerprint": "sha256:foreign-fingerprint",
            },
            secret="pub_foreign.secretvaluevaluevalue",  # noqa: S106
        )
        denied_requests = (
            ("get", "dast_integration_onboarding_detail", None),
            ("patch", "dast_integration_onboarding_detail", {"bundle": _bundle()}),
            ("post", "dast_integration_disable", None),
            ("post", "dast_integration_rotate_token", {"token": ROTATED_TOKEN}),
            ("post", "dast_integration_sync_capabilities", None),
        )
        for method, route_name, payload in denied_requests:
            with self.subTest(route=route_name, method=method):
                url = reverse(
                    f"aist_api:{route_name}",
                    kwargs={"integration_id": foreign_integration.pk},
                )
                response = getattr(self.client, method)(url, payload, format="json")
                self.assertEqual(response.status_code, 404)

        foreign_catalog_url = reverse(
            "aist_api:organization_dast_target_catalog",
            kwargs={"org_id": other_organization.pk},
        )
        self.assertEqual(self.client.get(foreign_catalog_url).status_code, 404)

        rotate_url = reverse(
            "aist_api:dast_integration_rotate_token",
            kwargs={"integration_id": integration_id},
        )
        for role_id, role_name in ((Roles.Reader, "Reader"), (Roles.Writer, "Writer")):
            role, _created = Role.objects.get_or_create(id=role_id, defaults={"name": role_name})
            Product_Type_Member.objects.filter(product_type=self.prod_type, user=self.user).update(role=role)
            with self.subTest(role=role_name):
                self.assertEqual(
                    self.client.post(rotate_url, {"token": ROTATED_TOKEN}, format="json").status_code,
                    404,
                )

    def test_read_only_pat_cannot_import_or_rotate(self):
        integration_id = self._import().data["id"]
        _token, raw = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="dast-read-only",
            scope=ApiTokenScope.READ_ONLY,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

        self.assertEqual(client.post(self.import_url, {"bundle": _bundle()}, format="json").status_code, 403)
        rotate_url = reverse(
            "aist_api:dast_integration_rotate_token",
            kwargs={"integration_id": integration_id},
        )
        self.assertEqual(client.post(rotate_url, {"token": ROTATED_TOKEN}, format="json").status_code, 403)
