import uuid
from unittest.mock import patch

from dojo.models import Product

from aist.execution.contracts import (
    EffectiveVersionPolicy,
    LaunchAuthorityKind,
    LaunchSource,
    PipelineExecutionKind,
    PipelineTaskName,
)
from aist.execution.sast import (
    SastPipelineLaunchAdapter,
    planning_context_from_launch_request,
)
from aist.models import (
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    PipelineLaunchAuthorityKind,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    VersionType,
)
from aist.test.test_api import AISTApiBase


class SastPipelineLaunchAdapterTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name=f"SAST adapter org {uuid.uuid4().hex}",
            product_type=self.prod_type,
        )

    def _request(self, *, origin, authority_kind, requester=None, schedule=None):
        params = {
            "project_version": {"id": self.pv.id},
            "log_level": "DEBUG",
            "selected_languages": ["python"],
        }
        if schedule is None:
            config = AISTProjectLaunchConfig.objects.create(
                project=self.project,
                name=f"SAST adapter {uuid.uuid4().hex}",
                params=params,
            )
            schedule = LaunchSchedule.objects.create(
                launch_config=config,
                cron_expression="*/5 * * * *",
                max_concurrent_runs=3,
            )
        return PipelineLaunchRequest.objects.create(
            project=self.project,
            schedule=schedule,
            launch_config=schedule.launch_config,
            origin=origin,
            authority_kind=authority_kind,
            requester=requester,
            params_snapshot=params,
        )

    @patch("aist.execution.sast.PipelineArguments.normalize_params")
    def test_manual_and_scheduled_requests_keep_current_sast_plan(self, normalize_params):
        normalized = {
            "project_version": {"id": self.pv.id},
            "log_level": "DEBUG",
            "selected_languages": ["python"],
        }
        normalize_params.side_effect = lambda **_kwargs: dict(normalized)
        # Both requests target the same schedule on purpose: capacity and coalescing are
        # now scoped per schedule (H13), so only requests sharing a schedule are expected
        # to share a resource key / coalesce key — not every SAST launch in the install.
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name=f"SAST adapter {uuid.uuid4().hex}",
            params={
                "project_version": {"id": self.pv.id},
                "log_level": "DEBUG",
                "selected_languages": ["python"],
            },
        )
        shared_schedule = LaunchSchedule.objects.create(
            launch_config=config,
            cron_expression="*/5 * * * *",
            max_concurrent_runs=3,
        )
        cases = (
            (PipelineLaunchOrigin.MANUAL, PipelineLaunchAuthorityKind.USER, self.user),
            (PipelineLaunchOrigin.SCHEDULE, PipelineLaunchAuthorityKind.SCHEDULE, None),
        )
        coalesce_keys = []

        for origin, authority_kind, requester in cases:
            with self.subTest(origin=origin):
                request = self._request(
                    origin=origin,
                    authority_kind=authority_kind,
                    requester=requester,
                    schedule=shared_schedule,
                )
                plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(request))

                self.assertEqual(plan.execution_type, PipelineExecutionKind.SAST)
                self.assertEqual(plan.task_name, PipelineTaskName.RUN_SAST_PIPELINE)
                self.assertEqual(plan.project_id, self.project.id)
                self.assertIsNone(plan.trigger_project_version_id)
                self.assertEqual(plan.effective_project_version_id, self.pv.id)
                self.assertEqual(plan.effective_version_policy, EffectiveVersionPolicy.PRESELECT_EFFECTIVE_VERSION)
                self.assertEqual(plan.resource_key, f"sast-schedule:{shared_schedule.pk}")
                self.assertEqual(plan.resource_limit, 3)
                self.assertTrue(plan.coalesce_key.startswith("sast:v1:"))
                coalesce_keys.append(plan.coalesce_key)
                self.assertEqual(plan.initial_launch_data, {})
                self.assertEqual(plan.task_args[0], {**normalized, "launch_config_id": request.launch_config_id})
                self.assertEqual(plan.authority.source, LaunchSource(origin))
                self.assertEqual(plan.authority.kind, LaunchAuthorityKind(authority_kind))
                self.assertEqual(plan.authority.requester_id, requester.id if requester else None)

        self.assertEqual(normalize_params.call_count, 2)
        self.assertEqual(len(set(coalesce_keys)), 1)
        for call in normalize_params.call_args_list:
            self.assertEqual(call.kwargs["raw_params"]["project_version"], {"id": self.pv.id})

    def test_manual_request_without_config_uses_default_capacity_and_preserves_launch_data(self):
        params = {
            "project_version": {"id": self.pv.id},
            "log_level": "INFO",
        }
        initial_launch_data = {
            "one_off_actions": [{"id": "manual-action", "action_type": "WRITE_LOG"}],
            "one_off_actions_done": [],
        }
        request = PipelineLaunchRequest.objects.create(
            project=self.project,
            origin=PipelineLaunchOrigin.MANUAL,
            authority_kind=PipelineLaunchAuthorityKind.USER,
            requester=self.user,
            params_snapshot=params,
            initial_launch_data_snapshot=initial_launch_data,
        )

        plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(request))

        self.assertEqual(plan.resource_limit, 1)
        self.assertEqual(plan.resource_key, f"sast-project:{self.project.id}")
        self.assertEqual(plan.initial_launch_data, initial_launch_data)
        self.assertNotIn("launch_config_id", plan.task_args[0])
        self.assertEqual(plan.effective_project_version_id, self.pv.id)

    def test_two_schedules_do_not_share_a_resource_key_or_capacity(self):
        """
        Regression for H13: distinct schedules used to collide on the single global
        "sast-worker" resource key, so one schedule's concurrency limit throttled every
        other schedule's runs too. Each schedule must own its own resource slot.
        """
        first = self._request(origin=PipelineLaunchOrigin.SCHEDULE, authority_kind=PipelineLaunchAuthorityKind.SCHEDULE)
        second = self._request(origin=PipelineLaunchOrigin.SCHEDULE, authority_kind=PipelineLaunchAuthorityKind.SCHEDULE)

        first_plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(first))
        second_plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(second))

        self.assertNotEqual(first_plan.resource_key, second_plan.resource_key)
        self.assertEqual(first_plan.resource_key, f"sast-schedule:{first.schedule_id}")
        self.assertEqual(second_plan.resource_key, f"sast-schedule:{second.schedule_id}")

    def test_manual_requests_for_different_projects_do_not_share_a_resource_key(self):
        other_product = Product.objects.create(
            name=f"SAST adapter other product {uuid.uuid4().hex}",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        other_project = AISTProject.objects.create(
            product=other_product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )
        other_pv = AISTProjectVersion.objects.create(
            project=other_project,
            version_type=VersionType.GIT_HASH,
            version="other-branch",
        )
        other_project_request = PipelineLaunchRequest.objects.create(
            project=other_project,
            origin=PipelineLaunchOrigin.MANUAL,
            authority_kind=PipelineLaunchAuthorityKind.USER,
            requester=self.user,
            params_snapshot={
                "project_version": {"id": other_pv.id},
                "log_level": "INFO",
            },
        )
        own_project_request = PipelineLaunchRequest.objects.create(
            project=self.project,
            origin=PipelineLaunchOrigin.MANUAL,
            authority_kind=PipelineLaunchAuthorityKind.USER,
            requester=self.user,
            params_snapshot={
                "project_version": {"id": self.pv.id},
                "log_level": "INFO",
            },
        )

        other_plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(other_project_request))
        own_plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(own_project_request))

        self.assertNotEqual(other_plan.resource_key, own_plan.resource_key)
