"""
An organization's build-environment integration supplies credentials to the SAST builder.

Org admins keep named variables whose values are encrypted and never returned; a project's run
receives the variables of the integration it resolves to, and nothing a launch persists holds a value.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.db import connection
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles

from aist.integrations.build_env import BuildEnvConflictError, BuildEnvironment
from aist.models import (
    AISTPipeline,
    AISTProjectLaunchConfig,
    AISTStatus,
    Organization,
    OrgIntegration,
    OrgIntegrationBuildEnvVar,
    OrgIntegrationType,
    PipelineLaunchRequest,
    ProjectIntegrationOverride,
)
from aist.pipeline_args import PipelineArguments
from aist.tasks.sast_execution import execute_pipeline
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin
from aist.test.test_api import AISTApiBase

CONAN_PASSWORD = "xconan-remote-password-31f9".removeprefix("x")
ROTATED_PASSWORD = "xconan-rotated-password-8a2c".removeprefix("x")
STAGING_PASSWORD = "xconan-staging-password-5d7e".removeprefix("x")
RETIRED_TOKEN = "xretired-token-0b41".removeprefix("x")
FOREIGN_TOKEN = "xforeign-org-token-c2e8".removeprefix("x")


def make_build_env(organization, name: str = "conan", **variables: str) -> OrgIntegration:
    integration = OrgIntegration.objects.create(
        organization=organization, integration_type=OrgIntegrationType.BUILD_ENV, name=name,
    )
    for var_name, value in variables.items():
        OrgIntegrationBuildEnvVar.objects.create(integration=integration, name=var_name, value=value)
    return integration


class BuildEnvIntegrationAPITests(AISTApiBase):

    """An org admin manages the variables; values go in and never come back out."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Acme org", product_type=self.prod_type)
        self.list_url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})

    def _detail_url(self, integration_id: int) -> str:
        return reverse("aist_api:org_integration_detail", kwargs={"integration_id": integration_id})

    def _create(self, build_env, **fields):
        return self.client.post(
            self.list_url,
            {"integration_type": "BUILD_ENV", "name": "conan", "config": {}, "build_env": build_env, **fields},
            format="json",
        )

    def _stored(self, integration_id: int) -> dict[str, str]:
        return dict(
            OrgIntegrationBuildEnvVar.objects.filter(integration_id=integration_id).values_list("name", "value"),
        )

    def test_admin_creates_variables_whose_values_are_never_returned(self):
        resp = self._create([
            {"name": "CONAN_LOGIN_USERNAME_ACME", "value": "ci-bot"},
            {"name": "CONAN_PASSWORD_ACME", "value": CONAN_PASSWORD},
        ])

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["build_env"], [
            {"name": "CONAN_LOGIN_USERNAME_ACME", "has_value": True},
            {"name": "CONAN_PASSWORD_ACME", "has_value": True},
        ])
        self.assertEqual(
            self._stored(resp.data["id"]),
            {"CONAN_LOGIN_USERNAME_ACME": "ci-bot", "CONAN_PASSWORD_ACME": CONAN_PASSWORD},
        )
        for response in (
            resp,
            self.client.get(self.list_url),
            self.client.get(self._detail_url(resp.data["id"])),
        ):
            self.assertNotIn(CONAN_PASSWORD, json.dumps(response.data, default=str))
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM aist_orgintegrationbuildenvvar")
            self.assertNotIn(CONAN_PASSWORD, str(cursor.fetchall()))

    def test_admin_rotates_one_value_keeps_another_and_removes_a_third_in_one_edit(self):
        integration = make_build_env(
            self.org, CONAN_LOGIN_USERNAME_ACME="ci-bot", CONAN_PASSWORD_ACME=CONAN_PASSWORD, OLD_TOKEN=RETIRED_TOKEN,
        )

        resp = self.client.patch(
            self._detail_url(integration.pk),
            {"build_env": [
                {"name": "CONAN_LOGIN_USERNAME_ACME"},
                {"name": "CONAN_PASSWORD_ACME", "value": ROTATED_PASSWORD},
            ]},
            format="json",
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(
            self._stored(integration.pk),
            {"CONAN_LOGIN_USERNAME_ACME": "ci-bot", "CONAN_PASSWORD_ACME": ROTATED_PASSWORD},
        )

    def test_an_edit_without_variables_leaves_them_untouched(self):
        integration = make_build_env(self.org, CONAN_PASSWORD_ACME=CONAN_PASSWORD)

        resp = self.client.patch(self._detail_url(integration.pk), {"name": "conan-prod"}, format="json")

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._stored(integration.pk), {"CONAN_PASSWORD_ACME": CONAN_PASSWORD})

    def test_names_the_builder_cannot_take_are_refused(self):
        cases = {
            "REPO_URL": "REPO_URL is set by the platform.",
            "PIPELINE_ID": "PIPELINE_ID is reserved by the builder.",
            "JOBS": "JOBS is reserved by the builder.",
            "1BAD": "1BAD is not a valid environment variable name.",
            "CONAN-PASSWORD": "CONAN-PASSWORD is not a valid environment variable name.",
        }
        for name, message in cases.items():
            with self.subTest(name=name):
                resp = self._create([{"name": name, "value": "x"}])

                self.assertEqual(resp.status_code, 400)
                self.assertIn(message, json.dumps(resp.data))
        self.assertFalse(OrgIntegration.objects.filter(integration_type=OrgIntegrationType.BUILD_ENV).exists())

    def test_a_value_with_a_line_break_is_refused(self):
        resp = self._create([{"name": "CONAN_KEY", "value": "-----BEGIN KEY-----\nabc"}])

        self.assertEqual(resp.status_code, 400)
        self.assertIn("A value cannot contain a line break.", json.dumps(resp.data))
        self.assertFalse(OrgIntegrationBuildEnvVar.objects.exists())

    def test_a_duplicate_name_or_a_new_name_without_value_is_refused(self):
        duplicate = self._create([{"name": "TOKEN", "value": "a"}, {"name": "TOKEN", "value": "b"}])
        missing = self._create([{"name": "TOKEN"}])

        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("TOKEN is listed more than once.", json.dumps(duplicate.data))
        self.assertEqual(missing.status_code, 400)
        self.assertIn("TOKEN needs a value.", json.dumps(missing.data))

    def test_a_build_environment_takes_neither_a_secret_nor_a_vpn_route(self):
        vpn = OrgIntegration.objects.create(organization=self.org, integration_type=OrgIntegrationType.VPN, name="corp")

        with_secret = self._create([{"name": "TOKEN", "value": "a"}], secret=RETIRED_TOKEN)
        with_route = self._create([{"name": "TOKEN", "value": "a"}], vpn_integration=vpn.pk)

        self.assertEqual(with_secret.status_code, 400)
        self.assertIn("A build environment integration keeps its values in variables.", json.dumps(with_secret.data))
        self.assertEqual(with_route.status_code, 400)
        self.assertIn("A build environment integration has no network route.", json.dumps(with_route.data))
        self.assertFalse(OrgIntegration.objects.filter(integration_type=OrgIntegrationType.BUILD_ENV).exists())

    def test_variables_belong_only_to_a_build_environment_integration(self):
        on_slack = self.client.post(
            self.list_url,
            {"integration_type": "SLACK", "name": "alerts", "config": {}, "build_env": [{"name": "T", "value": "a"}]},
            format="json",
        )

        self.assertEqual(on_slack.status_code, 400)
        self.assertIn("Only a build environment integration has variables.", json.dumps(on_slack.data))

    def test_the_type_of_an_existing_integration_cannot_change(self):
        integration = make_build_env(self.org, CONAN_PASSWORD_ACME=CONAN_PASSWORD)
        gitlab = OrgIntegration.objects.create(
            organization=self.org, integration_type=OrgIntegrationType.GITLAB, name="gl", secret=RETIRED_TOKEN,
        )

        to_gitlab = self.client.patch(self._detail_url(integration.pk), {"integration_type": "GITLAB"}, format="json")
        to_build_env = self.client.patch(self._detail_url(gitlab.pk), {"integration_type": "BUILD_ENV"}, format="json")

        self.assertEqual(to_gitlab.status_code, 400)
        self.assertEqual(to_build_env.status_code, 400)
        self.assertIn("The type of an existing integration cannot change.", json.dumps(to_gitlab.data))
        integration.refresh_from_db()
        gitlab.refresh_from_db()
        self.assertEqual(integration.integration_type, OrgIntegrationType.BUILD_ENV)
        self.assertEqual(gitlab.integration_type, OrgIntegrationType.GITLAB)

    def test_a_reader_cannot_see_or_change_the_variables(self):
        integration = make_build_env(self.org, CONAN_PASSWORD_ACME=CONAN_PASSWORD)
        reader = self.client_for_member("acme_reader", Roles.Reader)

        listing = reader.get(self.list_url)
        edit = reader.patch(
            self._detail_url(integration.pk),
            {"build_env": [{"name": "CONAN_PASSWORD_ACME", "value": "hijacked"}]},
            format="json",
        )

        self.assertEqual(listing.status_code, 404)
        self.assertEqual(edit.status_code, 404)
        self.assertEqual(self._stored(integration.pk), {"CONAN_PASSWORD_ACME": CONAN_PASSWORD})

    def test_an_admin_of_another_organization_cannot_reach_the_variables(self):
        other_org = Organization.objects.create(name="Other org", product_type=self.other_prod_type)
        integration = make_build_env(other_org, CONAN_PASSWORD_ACME=CONAN_PASSWORD)
        other_list = reverse("aist_api:org_integration_list_create", kwargs={"org_id": other_org.pk})

        listing = self.client.get(other_list)
        detail = self.client.get(self._detail_url(integration.pk))
        edit = self.client.patch(
            self._detail_url(integration.pk),
            {"build_env": [{"name": "CONAN_PASSWORD_ACME", "value": "hijacked"}]},
            format="json",
        )

        self.assertEqual([listing.status_code, detail.status_code, edit.status_code], [404, 404, 404])
        self.assertNotIn(CONAN_PASSWORD, json.dumps([listing.data, detail.data], default=str))
        self.assertEqual(self._stored(integration.pk), {"CONAN_PASSWORD_ACME": CONAN_PASSWORD})

    def test_the_admin_site_shows_variable_names_and_never_their_values(self):
        integration = make_build_env(self.org, CONAN_PASSWORD_ACME=CONAN_PASSWORD)
        variable = integration.build_env_vars.get()
        self.login_as_superuser()

        changelist = self.client.get(reverse("admin:aist_orgintegrationbuildenvvar_changelist"))
        change = self.client.get(reverse("admin:aist_orgintegrationbuildenvvar_change", args=[variable.pk]))

        self.assertEqual([changelist.status_code, change.status_code], [200, 200])
        self.assertContains(changelist, "CONAN_PASSWORD_ACME")
        self.assertNotContains(changelist, CONAN_PASSWORD)
        self.assertNotContains(change, CONAN_PASSWORD)


class BuildEnvLaunchTests(SastWorkerTestMixin, AISTApiBase):

    """A run hands its project's variables to the builder; a launch never persists them."""

    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Acme org", product_type=self.prod_type)
        self.project.refresh_from_db()
        self.conan = make_build_env(
            self.org, CONAN_LOGIN_USERNAME_ACME="ci-bot", CONAN_PASSWORD_ACME=CONAN_PASSWORD,
        )

    def _run(self, pipeline_id: str, **kwargs):
        pipeline = AISTPipeline.objects.create(id=pipeline_id, project=self.project, project_version=self.pv)
        run = self.run_sast(pipeline, project_version=self.pv, upload_results=[], **kwargs)
        return run.execute.call_args.args[1].runtime_arguments, PipelineLaunchRequest.objects.get(pipeline=pipeline)

    def test_the_builder_receives_the_organization_variables_and_the_launch_stores_none(self):
        arguments, request = self._run("build-env-default", launch_params={"env": {"FOO": "1"}})

        self.assertEqual(
            dict(arguments.secret_env),
            {"CONAN_LOGIN_USERNAME_ACME": "ci-bot", "CONAN_PASSWORD_ACME": CONAN_PASSWORD},
        )
        self.assertEqual(arguments.additional_env["FOO"], "1")
        self.assertNotIn("CONAN_PASSWORD_ACME", arguments.additional_env)
        self.assertNotIn(CONAN_PASSWORD, repr(arguments))
        self.assertNotIn(CONAN_PASSWORD, json.dumps(request.params_snapshot, default=str))

    def test_a_project_override_selects_another_set_or_none(self):
        staging = make_build_env(self.org, name="conan-staging", CONAN_PASSWORD_ACME=STAGING_PASSWORD)
        override = ProjectIntegrationOverride.objects.create(
            project=self.project, integration_type=OrgIntegrationType.BUILD_ENV, org_integration=staging,
        )
        staged, _ = self._run("build-env-staging")

        override.org_integration = None
        override.is_disabled = True
        override.save()
        disabled, _ = self._run("build-env-disabled")

        self.assertEqual(dict(staged.secret_env), {"CONAN_PASSWORD_ACME": STAGING_PASSWORD})
        self.assertEqual(dict(disabled.secret_env), {})

    def test_another_organization_s_variables_never_reach_this_project(self):
        self.conan.delete()
        other_org = Organization.objects.create(name="Other org", product_type=self.other_prod_type)
        foreign = make_build_env(other_org, name="foreign", FOREIGN_TOKEN=FOREIGN_TOKEN)

        refused = self.client.put(
            reverse(
                "aist_api:project_integration_override_detail",
                kwargs={"project_id": self.project.id, "integration_type": "BUILD_ENV"},
            ),
            {"org_integration": foreign.pk},
            format="json",
        )
        without_own, _ = self._run("build-env-no-own")
        own = make_build_env(self.org, name="own", CONAN_PASSWORD_ACME=CONAN_PASSWORD)
        with_own, _ = self._run("build-env-own")

        self.assertGreater(own.created, foreign.created)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(dict(without_own.secret_env), {})
        self.assertEqual(dict(with_own.secret_env), {"CONAN_PASSWORD_ACME": CONAN_PASSWORD})

    def test_a_launch_env_key_the_integration_defines_is_refused_at_admission(self):
        with self.assertRaisesMessage(
            BuildEnvConflictError, 'env: CONAN_PASSWORD_ACME is provided by build environment "conan"',
        ):
            PipelineArguments.for_sast(
                project=self.project, raw_params={"project_version": self.pv.id, "env": {"CONAN_PASSWORD_ACME": "x"}},
            )

    def test_a_saved_launch_config_with_a_colliding_key_is_refused(self):
        resp = self.client.post(
            reverse("aist_api:project_launch_config_list_create", kwargs={"project_id": self.project.id}),
            {"name": "Nightly", "params": {"project_version": self.pv.id, "env": {"CONAN_PASSWORD_ACME": "x"}}},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn('env: CONAN_PASSWORD_ACME is provided by build environment \\"conan\\"', json.dumps(resp.data))
        self.assertFalse(AISTProjectLaunchConfig.objects.filter(project=self.project).exists())

    def test_editing_a_launch_config_to_a_colliding_key_is_refused(self):
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Nightly",
            params=PipelineArguments.normalize_params(
                project=self.project, raw_params={"project_version": self.pv.id, "env": {"FOO": "1"}},
            ),
        )

        resp = self.client.patch(
            reverse(
                "aist_api:project_launch_config_detail",
                kwargs={"project_id": self.project.id, "config_id": config.id},
            ),
            {"params": {"project_version": self.pv.id, "env": {"CONAN_PASSWORD_ACME": "x"}}},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("env: CONAN_PASSWORD_ACME is provided by build environment", json.dumps(resp.data))
        config.refresh_from_db()
        self.assertEqual(config.params["env"], {"FOO": "1"})

    def test_a_collision_that_appears_after_admission_stops_the_run_before_the_builder(self):
        pipeline = AISTPipeline.objects.create(id="build-env-late", project=self.project, project_version=self.pv)
        builder = MagicMock()

        with self.assertRaisesMessage(ValueError, "Secret builder environment redefines CONAN_PASSWORD_ACME"):
            self.run_sast(
                pipeline,
                project_version=self.pv,
                upload_results=[],
                launch_params={"env": {"CONAN_PASSWORD_ACME": "launch-value"}},
                extra_patches=(
                    # Admitted while the integration did not yet define the name.
                    patch("aist.pipeline_args.build_environment", return_value=BuildEnvironment()),
                    patch("aist.tasks.sast_execution.execute_pipeline", wraps=execute_pipeline),
                    patch("pipeline.sast_execution.cleanup_pipeline_containers"),
                    patch("pipeline.docker_utils.build_image", builder),
                    patch("pipeline.docker_utils.run_container", builder),
                ),
            )

        builder.assert_not_called()
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
