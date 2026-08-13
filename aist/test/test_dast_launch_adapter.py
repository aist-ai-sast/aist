from datetime import timedelta

from django.utils import timezone

import aist.execution.dast as dast_module
from aist.execution.contracts import (
    EffectiveVersionPolicy,
    ExecutionPlanError,
    PipelineExecutionKind,
    PipelineTaskName,
)
from aist.execution.dast import (
    DAST_CAPABILITY_REVISION_MISMATCH,
    DastPipelineLaunchAdapter,
)
from aist.execution.dispatching import plan_claimed_launch
from aist.execution.enqueue import LaunchPrincipal, enqueue_pipeline_launch
from aist.execution.sast import planning_context_from_launch_request
from aist.integrations.dast_config import DastConfigError, DastTargetSnapshot
from aist.models import (
    AISTProjectLaunchConfig,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
)
from aist.pipeline_args import PipelineArguments
from aist.services.dast_targets import refresh_dast_targets
from aist.tasks import pipeline_dispatcher
from aist.test import dast_fixtures
from aist.test.test_api import AISTApiBase
from aist.test.test_dast_target_models import _integration_config, _target_wire


class DastPipelineLaunchAdapterTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.organization = Organization.objects.create(
            name="DAST adapter organization",
            product_type=self.prod_type,
        )
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST adapter integration",
            config=_integration_config("dast-adapter-public-id"),
            secret="runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        self.state = DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=self.now,
            contract_version="2.0",
            capabilities_etag="adapter-catalog",
            capabilities_synced_at=self.now,
        )
        self.target = refresh_dast_targets(
            self.integration,
            (DastTargetSnapshot.from_snapshot(_target_wire("adapter-api")),),
            seen_at=self.now,
        )[0]
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=self.target,
            source_repo_key="adapter-api",
            enabled=True,
            parameter_snapshot={"depth": "deep"},
        )
        self.config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=self.pv,
            name="DAST adapter config",
            params={"depth": "deep"},
        )
        self.adapter = DastPipelineLaunchAdapter()

    def _request(self, *, config=None, raw_params=None):
        launch_config = config or self.config
        arguments = (
            PipelineArguments.for_dast(
                project=self.project,
                binding=launch_config.dast_binding,
                trigger_project_version=launch_config.trigger_project_version,
                raw_params=raw_params,
            )
            if raw_params is not None
            else PipelineArguments.from_launch_config(launch_config)
        )
        return enqueue_pipeline_launch(
            arguments=arguments,
            principal=LaunchPrincipal.for_schedule(organization=self.organization),
            launch_config=launch_config,
        ).request

    def _plan(self, request):
        return self.adapter.build_plan(planning_context_from_launch_request(request))

    def test_ready_binding_builds_standalone_identity_only_plan(self):
        request = self._request()

        plan = self._plan(request)

        self.assertEqual(plan.execution_type, PipelineExecutionKind.DAST)
        self.assertEqual(plan.task_name, PipelineTaskName.RUN_PIPELINE_EXECUTION)
        self.assertEqual(plan.task_args, ())
        self.assertEqual(plan.project_id, self.project.pk)
        self.assertEqual(plan.trigger_project_version_id, self.pv.pk)
        self.assertEqual(plan.effective_version_policy, EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT)
        self.assertIsNone(plan.effective_project_version_id)
        self.assertEqual(plan.resource_key, f"dast-integration:{self.integration.pk}")
        self.assertEqual(plan.resource_limit, 1)
        self.assertEqual(plan.coalesce_key, request.coalesce_key)
        snapshot = plan.initial_launch_data["dast_execution"]
        self.assertEqual(snapshot["binding_id"], self.binding.pk)
        self.assertEqual(snapshot["source_repo_key"], "adapter-api")
        self.assertEqual(snapshot["parameters"], {"depth": "deep"})
        self.assertEqual(snapshot["capability"]["capability_revision"], self.target.capability_revision)
        self.assertNotIn("gateway_url", str(plan))
        self.assertNotIn(self.integration.secret, str(plan))

    def test_two_bindings_share_capacity_but_keep_distinct_coalesce_identity(self):
        second_target = refresh_dast_targets(
            self.integration,
            (
                DastTargetSnapshot.from_snapshot(_target_wire("adapter-api")),
                DastTargetSnapshot.from_snapshot(_target_wire("adapter-admin")),
            ),
            seen_at=self.now,
        )[1]
        second_binding = DastProjectBinding.objects.create(
            project=self.project,
            target=second_target,
            source_repo_key="adapter-admin",
            enabled=True,
            parameter_snapshot={"depth": "deep"},
        )
        second_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=second_binding,
            trigger_project_version=self.pv,
            name="Second DAST adapter config",
            params={"depth": "deep"},
        )

        first_plan = self._plan(self._request())
        second_plan = self._plan(self._request(config=second_config))

        self.assertEqual(first_plan.resource_key, second_plan.resource_key)
        self.assertNotEqual(first_plan.coalesce_key, second_plan.coalesce_key)

    def test_different_full_parameter_snapshots_do_not_coalesce(self):
        light_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=self.pv,
            name="Light DAST adapter config",
            params={"depth": "light"},
        )

        deep_plan = self._plan(self._request())
        light_plan = self._plan(self._request(config=light_config))

        self.assertNotEqual(deep_plan.coalesce_key, light_plan.coalesce_key)
        self.assertEqual(light_plan.initial_launch_data["dast_execution"]["parameters"], {"depth": "light"})

    def test_stale_catalog_is_rejected_before_pipeline_planning(self):
        request = self._request()
        type(self.state).objects.filter(pk=self.state.pk).update(
            capabilities_synced_at=self.now - timedelta(hours=25),
        )

        with self.assertRaisesRegex(ExecutionPlanError, "CATALOG_STALE"):
            self._plan(request)

    def test_capability_revision_mismatch_marks_integration_for_resync(self):
        request = self._request()
        type(self.target).objects.filter(pk=self.target.pk).update(
            capability_revision="sha256:changed-provider-revision",
        )

        with self.assertRaisesRegex(ExecutionPlanError, "synchronize the catalog"):
            self._plan(request)

        self.state.refresh_from_db()
        self.assertEqual(self.state.sync_error_code, DAST_CAPABILITY_REVISION_MISMATCH)

    def test_untrusted_parameter_fields_fail_closed(self):
        type(self.config).objects.filter(pk=self.config.pk).update(
            params={"depth": "deep", "resource_key": "attacker", "gateway_url": "https://evil.example"},
        )
        self.config.refresh_from_db()
        with self.assertRaisesRegex(DastConfigError, "parameter_schema"):
            self._request()

    def test_adapter_has_no_runtime_gateway_client_surface(self):
        self.assertFalse(hasattr(dast_module, "DastGatewayClient"))
        self.assertFalse(hasattr(self.adapter, "run"))
        self.assertFalse(hasattr(self.adapter, "stop"))
        self.assertFalse(hasattr(self.adapter, "logs"))

    def test_dispatcher_registers_only_database_identity_for_dast_task(self):
        request = self._request()
        self.assertEqual(
            pipeline_dispatcher.run_pipeline_execution.name,
            PipelineTaskName.RUN_PIPELINE_EXECUTION.value,
        )
        claim_owner = "dast-adapter-test"
        type(request).objects.filter(pk=request.pk).update(
            state="CLAIMED",
            claim_owner=claim_owner,
            claimed_at=timezone.now(),
        )
        result = plan_claimed_launch(
            request_id=request.pk,
            claim_owner=claim_owner,
            adapter_registry=pipeline_dispatcher.launch_adapter_registry,
        )
        request.refresh_from_db()

        self.assertEqual(result.status, "READY")
        self.assertEqual(request.task_args_snapshot, [])


class PerimeterDastLaunchTests(AISTApiBase):

    """A target that declares no repository trigger must reach a plan with no source version."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.organization = Organization.objects.create(
            name="Perimeter organization",
            product_type=self.prod_type,
        )
        self.integration, self.state = dast_fixtures.create_dast_integration(
            organization=self.organization,
            public_id="perimeter-public-id",
            now=self.now,
        )
        # Both shapes come from one catalog: a refresh that omits a target marks it unavailable.
        self.target, self.source_target = dast_fixtures.create_dast_targets(
            integration=self.integration,
            wires=(
                dast_fixtures.perimeter_target_wire(),
                dast_fixtures.target_wire("source-based-api"),
            ),
            seen_at=self.now,
        )
        self.binding = dast_fixtures.create_dast_binding(project=self.project, target=self.target)
        self.config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=None,
            name="Perimeter config",
            params={"depth": "deep"},
        )

    def _enqueue(self):
        return enqueue_pipeline_launch(
            arguments=PipelineArguments.from_launch_config(self.config),
            principal=LaunchPrincipal.for_schedule(organization=self.organization),
            launch_config=self.config,
        ).request

    def test_perimeter_launch_is_enqueued_and_planned_without_a_source_version(self):
        request = self._enqueue()

        self.assertIsNone(request.trigger_project_version_id)
        self.assertTrue(request.coalesce_key)

        plan = DastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(request))

        self.assertIsNone(plan.trigger_project_version_id)
        self.assertEqual(plan.initial_launch_data["dast_execution"]["source_repo_key"], "")

    def test_a_launch_without_explicit_parameters_runs_the_binding_configuration(self):
        """
        The binding is where the operator configured this target, so its parameters are what runs.

        Validating the raw input alone froze an empty set, and the scan then used the provider's
        own defaults -- the operator's configuration was silently ignored.
        """
        arguments = PipelineArguments.for_dast(
            project=self.project,
            binding=self.binding,
            trigger_project_version=None,
            raw_params={},
        )

        self.assertEqual(self.binding.parameter_snapshot, {"depth": "deep"})
        self.assertEqual(arguments.params_snapshot, {"depth": "deep"})

    def test_an_explicit_parameter_still_overrides_the_binding(self):
        arguments = PipelineArguments.for_dast(
            project=self.project,
            binding=self.binding,
            trigger_project_version=None,
            raw_params={"depth": "light"},
        )

        self.assertEqual(arguments.params_snapshot, {"depth": "light"})

    def test_perimeter_launches_of_one_binding_share_a_coalesce_identity(self):
        self.assertEqual(self._enqueue().coalesce_key, self._enqueue().coalesce_key)

    def test_perimeter_and_source_based_launches_never_share_a_coalesce_identity(self):
        source_binding = dast_fixtures.create_dast_binding(project=self.project, target=self.source_target)
        source_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=source_binding,
            trigger_project_version=self.pv,
            name="Source-based config",
            params={"depth": "deep"},
        )
        source_request = enqueue_pipeline_launch(
            arguments=PipelineArguments.from_launch_config(source_config),
            principal=LaunchPrincipal.for_schedule(organization=self.organization),
            launch_config=source_config,
        ).request

        self.assertNotEqual(self._enqueue().coalesce_key, source_request.coalesce_key)
