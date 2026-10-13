"""
The SCM clone credential must never be stored in launch params.

`REPO_URL` is built from the org's SCM integration secret (a GitLab PAT here), so it is
resolved per run by `SastPipelineArguments.runtime_env` and kept out of
`AISTProjectLaunchConfig.params` and `PipelineLaunchRequest.params_snapshot`.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration
from rest_framework.test import APIClient

from aist.execution.enqueue import LaunchPrincipal, enqueue_pipeline_launch
from aist.forms import AISTLaunchConfigForm
from aist.models import (
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    OrgIntegration,
    PipelineLaunchRequest,
    RepositoryInfo,
    ScmGitlabBinding,
    ScmType,
    VersionType,
)
from aist.pipeline_args import PipelineArguments
from aist.tasks.launch_schedule import process_launch_schedules

GITLAB_PAT = "xglpat-launchparams1234567890".removeprefix("x")
ROTATED_GITLAB_PAT = "xglpat-rotated0987654321".removeprefix("x")
CLONE_URL = f"https://oauth2:{GITLAB_PAT}@gitlab.example.com/group/repo.git"
ROTATED_CLONE_URL = f"https://oauth2:{ROTATED_GITLAB_PAT}@gitlab.example.com/group/repo.git"


class LaunchParamsScmCredentialBase(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA launch params")
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})

        self.prod_type = Product_Type.objects.create(name="Launch params PT")
        self.organization = Organization.objects.create(
            name="Launch params org",
            product_type=self.prod_type,
        )
        self.product = Product.objects.create(
            name="Launch params product",
            description="d",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )

        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type="GITLAB",
            name="Launch params GitLab",
            secret=GITLAB_PAT,
        )
        self.repository = RepositoryInfo.objects.create(
            type=ScmType.GITLAB,
            repo_owner="group",
            repo_name="repo",
            base_url="https://gitlab.example.com",
        )
        ScmGitlabBinding.objects.create(scm=self.repository, org_integration=self.integration)

        self.project = AISTProject.objects.create(
            product=self.product,
            repository=self.repository,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )
        self.pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )

        self.maintainer = get_user_model().objects.create_user(
            "launch_params_maintainer",
            "launch_params_maintainer@example.com",
            "pass",
        )
        Product_Type_Member.objects.create(
            product_type=self.prod_type,
            user=self.maintainer,
            role=self.role_maintainer,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.maintainer)

    def _list_create_url(self):
        return reverse("aist_api:project_launch_config_list_create", kwargs={"project_id": self.project.id})

    def _detail_url(self, config_id: int):
        return reverse(
            "aist_api:project_launch_config_detail",
            kwargs={"project_id": self.project.id, "config_id": config_id},
        )

    def _rotate_integration_secret(self):
        self.integration.secret = ROTATED_GITLAB_PAT
        self.integration.save(update_fields=["secret"])

    def assert_no_credential(self, payload):
        self.assertNotIn(GITLAB_PAT, str(payload))
        self.assertNotIn(ROTATED_GITLAB_PAT, str(payload))


class LaunchConfigParamsPersistenceTests(LaunchParamsScmCredentialBase):

    """An operator saves a preset; only their own env survives in the database."""

    def test_create_through_api_persists_only_caller_env(self):
        resp = self.client.post(
            self._list_create_url(),
            data={
                "name": "Nightly",
                "params": {"project_version": self.pv.id, "env": {"FOO": "1"}},
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        config = AISTProjectLaunchConfig.objects.get(pk=resp.data["id"])
        self.assertEqual(config.params["env"], {"FOO": "1"})
        self.assert_no_credential(config.params)
        self.assertNotIn("REPO_URL", resp.data["params"]["env"])

    def test_create_through_api_drops_caller_supplied_system_env(self):
        # REPO_URL/PROJECT_NAME are system-computed. A caller-supplied value is dropped
        # rather than rejected, so re-normalizing a snapshot written by an older release
        # (which did persist these keys) stays valid instead of failing the launch.
        resp = self.client.post(
            self._list_create_url(),
            data={
                "name": "Attempted override",
                "params": {
                    "project_version": self.pv.id,
                    "env": {
                        "REPO_URL": "https://attacker.example.com/repo.git",
                        "PROJECT_NAME": "spoofed",
                        "FOO": "1",
                    },
                },
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        config = AISTProjectLaunchConfig.objects.get(pk=resp.data["id"])
        self.assertEqual(config.params["env"], {"FOO": "1"})

    def test_update_through_api_persists_only_caller_env(self):
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            params=PipelineArguments.normalize_params(
                project=self.project,
                raw_params={"project_version": self.pv.id},
            ),
        )

        resp = self.client.patch(
            self._detail_url(config.id),
            data={"params": {"project_version": self.pv.id, "env": {"FOO": "2", "REPO_URL": "https://x/"}}},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        config.refresh_from_db()
        self.assertEqual(config.params["env"], {"FOO": "2"})
        self.assert_no_credential(config.params)

    def test_create_through_ui_form_persists_no_credential(self):
        # The form reads its bound data with getlist(), so it needs a real QueryDict.
        posted = QueryDict(mutable=True)
        posted.update({
            "name": "From UI",
            "description": "",
            "log_level": "INFO",
            "ai_mode": "MANUAL",
            "project_version": str(self.pv.id),
        })
        form = AISTLaunchConfigForm(posted, project=self.project)

        self.assertTrue(form.is_valid(), form.errors)
        payload = form.to_api_create_payload(project=self.project)

        self.assertEqual(payload["params"]["env"], {})
        self.assert_no_credential(payload["params"])


class LaunchConfigReaderExposureTests(LaunchParamsScmCredentialBase):

    """A Reader in the org can list presets and launch requests; neither leaks the PAT."""

    def setUp(self):
        super().setUp()
        self.reader = get_user_model().objects.create_user(
            "launch_params_reader",
            "launch_params_reader@example.com",
            "pass",
        )
        Product_Type_Member.objects.create(
            product_type=self.prod_type,
            user=self.reader,
            role=self.role_reader,
        )
        self.config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Legacy preset",
            params={
                "project_version": {"id": self.pv.id},
                "env": {"REPO_URL": CLONE_URL, "PROJECT_NAME": "legacy", "FOO": "1"},
            },
        )
        enqueue_pipeline_launch(
            arguments=PipelineArguments.for_sast(
                project=self.project,
                raw_params=dict(self.config.params),
            ),
            principal=LaunchPrincipal.for_user(organization=self.organization, requester=self.maintainer),
            launch_config=self.config,
        )
        self.client.force_authenticate(user=self.reader)

    def test_reader_launch_config_list_hides_the_credential(self):
        resp = self.client.get(self._list_create_url())

        self.assertEqual(resp.status_code, 200)
        self.assert_no_credential(resp.data)
        # The seeded row predates the persist/runtime split, so it still carries the key.
        # Nothing strips it on read — the response masker replaces the URL password, which
        # is what keeps a row the data migration has not reached yet from leaking its PAT.
        self.assertIn("@gitlab.example.com", resp.data[0]["params"]["env"]["REPO_URL"])
        self.assertEqual(resp.data[0]["params"]["env"]["FOO"], "1")

    def test_reader_launch_request_list_hides_the_credential(self):
        resp = self.client.get(reverse("aist_api:pipeline_launch_request_list"))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["results"])
        self.assert_no_credential(resp.data)


class LaunchRuntimeEnvTests(LaunchParamsScmCredentialBase):

    """A launch freezes a credential-free snapshot, and the runner still gets a clone URL."""

    def _manual_request(self, *, raw_params: dict | None = None) -> PipelineLaunchRequest:
        return enqueue_pipeline_launch(
            arguments=PipelineArguments.for_sast(
                project=self.project,
                raw_params=raw_params if raw_params is not None else {"project_version": self.pv.id},
            ),
            principal=LaunchPrincipal.for_user(organization=self.organization, requester=self.maintainer),
        ).request

    def test_manual_launch_snapshot_is_credential_free_and_runner_gets_current_token(self):
        request = self._manual_request(raw_params={"project_version": self.pv.id, "env": {"FOO": "1"}})

        self.assertEqual(request.params_snapshot["env"], {"FOO": "1"})
        self.assert_no_credential(request.params_snapshot)

        runtime_env = PipelineArguments.from_dict(dict(request.params_snapshot)).sast.runtime_env

        self.assertEqual(runtime_env["REPO_URL"], CLONE_URL)
        self.assertEqual(runtime_env["PROJECT_NAME"], "launch_params_product")
        self.assertEqual(runtime_env["FOO"], "1")

    def test_rotated_token_reaches_the_next_run_without_touching_the_frozen_snapshot(self):
        request = self._manual_request()
        self._rotate_integration_secret()

        request.refresh_from_db()
        runtime_env = PipelineArguments.from_dict(dict(request.params_snapshot)).sast.runtime_env

        self.assertNotIn("REPO_URL", request.params_snapshot["env"])
        self.assertEqual(runtime_env["REPO_URL"], ROTATED_CLONE_URL)

    def test_coalesce_key_survives_a_token_rotation(self):
        # The clone URL used to be part of params_snapshot, so rotating the PAT changed
        # the frozen execution identity and two otherwise identical launches stopped
        # coalescing against each other.
        first = self._manual_request()
        self._rotate_integration_secret()
        second = self._manual_request()

        self.assertEqual(first.coalesce_key, second.coalesce_key)

    def test_scheduled_launch_snapshot_is_credential_free(self):
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Scheduled preset",
            params=PipelineArguments.normalize_params(
                project=self.project,
                raw_params={"project_version": self.pv.id, "env": {"FOO": "1"}},
            ),
        )
        LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            launch_config=config,
            next_run_at=timezone.now() - timedelta(minutes=10),
        )

        process_launch_schedules()

        request = PipelineLaunchRequest.objects.get(launch_config=config)
        self.assertEqual(request.params_snapshot["env"], {"FOO": "1"})
        self.assert_no_credential(request.params_snapshot)
        self.assertEqual(
            PipelineArguments.from_dict(dict(request.params_snapshot)).sast.runtime_env["REPO_URL"],
            CLONE_URL,
        )
